# -*- coding: utf-8 -*-
"""
Bluesky Audience Replier + Flask UI
- يجلب جمهور بوست واحد (likers أو reposters) مرتباً تصاعدياً
- يرد على آخر منشور لكل مستخدم برسالة من قائمتك
- تأخير بين كل مستخدم والآخر (من الواجهة)
- حفظ واستئناف التقدم لكل (حساب/رابط)
- يتجاهل الحسابات التي لا تملك منشورات (skipped)
"""

import os
import time
import random
import logging
import threading
from dataclasses import dataclass
from typing import List, Dict, Optional, Any, Tuple
from datetime import datetime, timezone

from flask import Flask, jsonify, render_template, request

from atproto import Client
from atproto.exceptions import AtProtocolError

# إعدادات ومسارات
from config import Config, DATA_DIR, PROGRESS_PATH, DEFAULT_MIN_DELAY, DEFAULT_MAX_DELAY

# --- استيراد utils مع بدائل آمنة ---
# نحتاج دائمًا هذه:
from utils import resolve_post_from_url, save_progress, load_progress

# وقد لا تتوفر validate_message_template في utils لديك، فنعرّف بديلًا عند الحاجة:
try:
    from utils import validate_message_template as _validate_message_template
    def _is_valid_message(s: str) -> bool:
        return _validate_message_template(s)
except Exception:
    def _is_valid_message(s: str) -> bool:
        # بديل بسيط وآمن: نص، طول ≤ 280 وعدم وجود أنماط خطيرة
        if not isinstance(s, str): return False
        if not (1 <= len(s) <= 280): return False
        bad = ("<script", "javascript:", "data:", "vbscript:", "onerror", "onclick")
        ss = s.lower()
        return not any(b in ss for b in bad)

# لوج
logging.basicConfig(level=logging.INFO)
log = logging.getLogger("bluesky-bot")

# ================== منطق البوت ==================

@dataclass
class RunState:
    post_url: str
    processing_type: str                 # "likers" أو "reposters"
    messages: List[str]
    min_delay: int
    max_delay: int


class BlueSkyBot:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.client = Client()
        self.progress_cb = None
        self._stop_flag = False

    # --- تحكّم ---
    def stop(self):
        self._stop_flag = True

    def reset_stop(self):
        self._stop_flag = False

    # --- دخول ---
    def login(self) -> None:
        log.info(f"🔑 تسجيل الدخول: {self.cfg.bluesky_handle}")
        self.client.login(self.cfg.bluesky_handle, self.cfg.bluesky_password)

    # --- أدوات API ---

    def _get_post_audience(self, post_uri: str, which: str) -> List[Dict[str, Any]]:
        """
        which: 'likers' | 'reposters'
        تُعيد قائمة dicts مع مفاتيح: handle, did, indexedAt
        مرتبة تصاعدياً (أقدم → أحدث) لضمان الاستئناف السلس.
        """
        audience: List[Dict[str, Any]] = []

        if which == "likers":
            cursor = None
            while True:
                resp = self.client.app.bsky.feed.get_likes({"uri": post_uri, "cursor": cursor, "limit": 100})
                for it in (resp.likes or []):
                    actor = it.actor
                    audience.append({
                        "handle": getattr(actor, "handle", None),
                        "did": getattr(actor, "did", None),
                        "indexedAt": getattr(it, "indexed_at", getattr(it, "created_at", None)) or "",
                    })
                cursor = getattr(resp, "cursor", None)
                if not cursor:
                    break

        elif which == "reposters":
            cursor = None
            while True:
                resp = self.client.app.bsky.feed.get_reposted_by({"uri": post_uri, "cursor": cursor, "limit": 100})
                for actor in (resp.reposted_by or []):
                    audience.append({
                        "handle": getattr(actor, "handle", None),
                        "did": getattr(actor, "did", None),
                        "indexedAt": getattr(actor, "indexed_at", None) or "",
                    })
                cursor = getattr(resp, "cursor", None)
                if not cursor:
                    break
        else:
            raise ValueError("processing_type must be 'likers' or 'reposters'.")

        # ترتيب ثابت
        audience.sort(key=lambda x: (x.get("indexedAt", ""), x.get("handle", "")))
        return audience

    def _get_users_latest_post(self, actor: str) -> Optional[Tuple[str, str]]:
        """
        تُعيد (uri, cid) لآخر منشور (غير رد) للمستخدم المعطى (handle أو DID).
        """
        cursor = None
        while True:
            feed = self.client.app.bsky.feed.get_author_feed({"actor": actor, "cursor": cursor, "limit": 50})
            posts = getattr(feed, "feed", []) or []
            if not posts:
                return None

            # أول بوست ليس ردّاً
            for item in posts:
                try:
                    post = item.post
                    rec = getattr(post, "record", None)
                    if rec and not getattr(rec, "reply", None):
                        return post.uri, post.cid
                except Exception:
                    continue

            cursor = getattr(feed, "cursor", None)
            if not cursor:
                break
        return None

    def _reply_to(self, target_uri: str, target_cid: str, text: str) -> bool:
        try:
            self.client.com.atproto.repo.create_record({
                "repo": self.client.me.did,
                "collection": "app.bsky.feed.post",
                "record": {
                    "$type": "app.bsky.feed.post",
                    "text": text,
                    "createdAt": datetime.now(timezone.utc).isoformat(),
                    "reply": {
                        "root": {"uri": target_uri, "cid": target_cid},
                        "parent": {"uri": target_uri, "cid": target_cid},
                    },
                },
            })
            return True
        except Exception as e:
            log.error(f"⚠️ Reply failed: {e}")
            return False

    # --- التشغيل الرئيسي ---

    def run_replies(self, state: RunState) -> Dict[str, Any]:
        """
        يعالج الجمهور ويجري ردّاً على آخر منشور لكل مستخدم.
        يُحدّث progress.json للاستئناف.
        """
        self.login()

        # حلّ رابط البوست
        post_ref = resolve_post_from_url(self.client, state.post_url)
        if not post_ref:
            raise RuntimeError("حلّ رابط البوست فشل.")

        post_uri = post_ref["uri"]

        # تحميل التقدم السابق
        p = load_progress(PROGRESS_PATH, state.post_url) or {}
        start_index = int(p.get("next_index", 0))

        # جلب الجمهور بالترتيب (أو من الكاش إن وُجد)
        if start_index == 0 or not p.get("audience_cache"):
            audience = self._get_post_audience(post_uri, state.processing_type)
            audience_cache = [{"handle": a["handle"], "did": a["did"]} for a in audience]
        else:
            audience_cache = p["audience_cache"]

        total = len(audience_cache)
        completed = int(p.get("completed", 0))
        failed = int(p.get("failed", 0))
        skipped = int(p.get("skipped", 0))

        log.info(f"👥 Audience = {total} ({state.processing_type}) - resume at index {start_index}")

        # حفظ لقطة
        save_progress(PROGRESS_PATH, state.post_url, {
            "processing_type": state.processing_type,
            "audience_total": total,
            "audience_cache": audience_cache,
            "next_index": start_index,
            "completed": completed,
            "failed": failed,
            "skipped": skipped,
            "last_updated": datetime.utcnow().isoformat() + "Z",
            "handle": self.cfg.bluesky_handle,
        })

        # الحلقة
        for idx in range(start_index, total):
            if self._stop_flag:
                log.warning("🛑 تم إيقاف التشغيل من المستخدم.")
                break

            entry = audience_cache[idx]
            actor = entry.get("handle") or entry.get("did")
            if not actor:
                failed += 1
                continue

            # جهّز الرسالة
            msg_choices = [m for m in state.messages if _is_valid_message(m)]
            message = random.choice(msg_choices) if msg_choices else "🙏"

            try:
                target = self._get_users_latest_post(actor)
                if not target:
                    # الفلتر المطلوب: تجاهل الحساب بدون منشورات
                    log.info(f"⏭️ تجاوز @{actor}: لا يملك منشورات.")
                    skipped += 1
                else:
                    uri, cid = target
                    if self._reply_to(uri, cid, message):
                        log.info(f"💬 Reply OK → @{actor}")
                        completed += 1
                    else:
                        failed += 1
            except AtProtocolError as e:
                log.error(f"⚠️ API error @{actor}: {e}")
                failed += 1
            except Exception as e:
                log.error(f"⚠️ Unexpected error @{actor}: {e}")
                failed += 1

            # تحديث التقدم بعد كل محاولة
            save_progress(PROGRESS_PATH, state.post_url, {
                "processing_type": state.processing_type,
                "audience_total": total,
                "audience_cache": audience_cache,
                "next_index": idx + 1,
                "completed": completed,
                "failed": failed,
                "skipped": skipped,
                "last_updated": datetime.utcnow().isoformat() + "Z",
                "handle": self.cfg.bluesky_handle,
            })

            # تأخير
            delay = random.randint(state.min_delay, state.max_delay)
            log.info(f"⏳ انتظار {delay} ثانية …")
            for _ in range(delay):
                if self._stop_flag:
                    break
                time.sleep(1)
            if self._stop_flag:
                break

            if self.progress_cb:
                self.progress_cb(completed, failed, skipped)

        return {"completed": completed, "failed": failed, "skipped": skipped}


# ================== خادم الويب (Flask) ==================
app = Flask(__name__)

runtime_stats = {
    "status": "Idle",
    "current_task": None,
    "session_uptime": "0s",
}
bot_progress = {
    "completed_runs": 0,
    "failed_runs": 0,
    "skipped_runs": 0,
    "total_bot_runs": 0,
    "success_rate": 0.0,
    "audience_total": 0,
}

bot_thread: Optional[threading.Thread] = None
bot_instance: Optional[BlueSkyBot] = None
session_start: Optional[float] = None


def update_progress(completed: int, failed: int, skipped: int) -> None:
    bot_progress["completed_runs"] = completed
    bot_progress["failed_runs"] = failed
    bot_progress["skipped_runs"] = skipped
    total_attempted = completed + failed  # فقط ما حاول يرد عليه
    bot_progress["total_bot_runs"] = total_attempted
    bot_progress["success_rate"] = (completed / total_attempted) if total_attempted else 0.0


@app.route("/")
def index():
    return render_template("persistent.html",
                           default_min=DEFAULT_MIN_DELAY,
                           default_max=DEFAULT_MAX_DELAY)


@app.post("/queue_task")
def queue_task():
    global bot_thread, bot_instance, session_start

    data = request.get_json(force=True)
    log.info(f"📥 Payload: {data}")

    post_url = (data.get("post_url") or "").strip()
    post_urls = data.get("post_urls") or []
    if not post_url and post_urls:
        post_url = post_urls[0]

    messages: List[str] = data.get("messages") or data.get("message_templates") or []
    messages = [m.strip() for m in messages if m and m.strip()]
    processing_type = (data.get("processing_type") or "likers").strip()  # 'likers' | 'reposters'

    # بيانات الدخول
    handle = data.get("bluesky_handle") or os.getenv("BLUESKY_HANDLE") or os.getenv("BSKY_HANDLE")
    password = data.get("bluesky_password") or os.getenv("BLUESKY_PASSWORD") or os.getenv("BSKY_PASSWORD")

    # التأخير
    try:
        min_delay = int(data.get("min_delay", DEFAULT_MIN_DELAY))
        max_delay = int(data.get("max_delay", DEFAULT_MAX_DELAY))
    except Exception:
        min_delay, max_delay = DEFAULT_MIN_DELAY, DEFAULT_MAX_DELAY

    if not handle or not password or not post_url:
        return jsonify({"error": "❌ البيانات ناقصة (handle/password/post_url)"}), 400
    if not messages:
        messages = ["🙏 Thank you!"]

    cfg = Config(bluesky_handle=handle, bluesky_password=password, min_delay=min_delay, max_delay=max_delay)
    bot_instance = BlueSkyBot(cfg)

    def on_progress(comp: int, fail: int, skip: int):
        update_progress(comp, fail, skip)

    bot_instance.progress_cb = on_progress
    bot_instance.reset_stop()

    run_state = RunState(
        post_url=post_url,
        processing_type=processing_type,
        messages=messages,
        min_delay=cfg.min_delay,
        max_delay=cfg.max_delay,
    )

    def runner():
        global session_start
        session_start = time.time()
        runtime_stats["status"] = "Running"
        runtime_stats["current_task"] = "Processing audience"

        try:
            res = bot_instance.run_replies(run_state)
        except Exception as e:
            log.error(f"❌ فشل المهمة: {e}")
            res = {"completed": 0, "failed": 1, "skipped": 0}

        runtime_stats["status"] = "Idle"
        runtime_stats["current_task"] = None
        uptime = int(time.time() - session_start) if session_start else 0
        runtime_stats["session_uptime"] = f"{uptime}s"

        update_progress(res.get("completed", 0), res.get("failed", 0), res.get("skipped", 0))

        p = load_progress(PROGRESS_PATH, run_state.post_url) or {}
        bot_progress["audience_total"] = int(p.get("audience_total", 0))

    bot_thread = threading.Thread(target=runner, daemon=True)
    bot_thread.start()

    return jsonify({"status": "✅ المهمة بدأت"})


@app.post("/stop_task")
def stop_task():
    global bot_instance
    if bot_instance:
        bot_instance.stop()
    runtime_stats["status"] = "Stopped"
    return jsonify({"status": "🛑 تم الإيقاف"})


@app.get("/detailed_progress")
def detailed_progress():
    try:
        import json, pathlib
        if pathlib.Path(PROGRESS_PATH).exists():
            with open(PROGRESS_PATH, "r") as f:
                allp = json.load(f)
        else:
            allp = {}
    except Exception:
        allp = {}

    return jsonify({
        "runtime_stats": runtime_stats,
        "bot_progress": bot_progress,
        "raw_progress": allp
    })


# Aliases
@app.post("/queue")
def queue_alias():
    return queue_task()

@app.get("/progress")
def progress_alias():
    return detailed_progress()


if __name__ == "__main__":
    port = int(os.getenv("PORT", 10000))
    app.run(host="0.0.0.0", port=port)
