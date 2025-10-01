# -*- coding: utf-8 -*-
"""
Bluesky Bot + Flask wrapper
- يجمع جمهور منشور (معجبون أو مُعيدو نشر) حسب الاختيار من الواجهة
- يرد على آخر منشور لكل مستخدم برسالة عشوائية من القائمة
- يحفظ التقدم تلقائياً (مع fallback إلى /tmp إن لم تتوفر أذونات DATA_DIR)
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

from config import PROGRESS_PATH, DEFAULT_MIN_DELAY, DEFAULT_MAX_DELAY
from utils import (
    resolve_post_from_url,
    save_progress,
    load_progress,
    validate_message_template,
    format_duration,
)

# ---------- إعداد اللوج ----------
logging.basicConfig(level=logging.INFO)
log = logging.getLogger("bluesky-bot")


# ---------- نماذج الإعدادات الداخلية ----------
@dataclass
class Config:
    bluesky_handle: str
    bluesky_password: str
    min_delay: int = DEFAULT_MIN_DELAY
    max_delay: int = DEFAULT_MAX_DELAY


# ---------- حالة التشغيل/التقدّم (لواجهة الحالة) ----------
runtime_stats = {
    "status": "Idle",                # Idle | Running | Stopped
    "current_task": None,            # نص وصفي
    "session_uptime": "0s",
    "last_update_ts": 0,
}
bot_progress = {
    "completed_runs": 0,            # عدد المستخدمين الذين تم إرسال رد لهم
    "failed_runs": 0,               # عدد المستخدمين الذين فشل إرسال الرد لهم
    "total_bot_runs": 0,            # المجموع
    "success_rate": 0.0,
    "total_mentions_sent": 0,       # نفس completed حالياً؛ احتفظنا به للتوسع
    "total_followers": 0,           # غير مستخدم الآن (للتوسع لاحقاً)
}

def _stats_touch():
    runtime_stats["last_update_ts"] = int(time.time())


def update_progress_counts(done: int, failed: int) -> None:
    bot_progress["completed_runs"] = done
    bot_progress["failed_runs"] = failed
    total = done + failed
    bot_progress["total_bot_runs"] = total
    bot_progress["success_rate"] = (done / total) if total else 0.0
    bot_progress["total_mentions_sent"] = done
    _stats_touch()


# ---------- البوت الرئيسي ----------
class BlueSkyBot:
    def __init__(self, config: Config, progress_key: str):
        self.config = config
        self.client = Client()
        self.progress_key = progress_key  # مفتاح التقدم (حسب الحساب/الرابط)
        self.stop_event = threading.Event()

    # ===== لوجين =====
    def login(self) -> None:
        log.info(f"🔑 تسجيل الدخول: {self.config.bluesky_handle}")
        self.client.login(self.config.bluesky_handle, self.config.bluesky_password)

    # ===== أدوات جلب الجمهور =====
    def _iter_likers(self, post_uri: str) -> List[Dict[str, Any]]:
        """إرجاع قائمة المعجبين (actors) مع ترقيم صفحات"""
        out = []
        cursor = None
        while True:
            res = self.client.app.bsky.feed.get_likes({"uri": post_uri, "limit": 100, "cursor": cursor})
            for item in getattr(res, "likes", []):
                if getattr(item, "actor", None):
                    out.append(item.actor)
            cursor = getattr(res, "cursor", None)
            if not cursor:
                break
        return out

    def _iter_reposters(self, post_uri: str) -> List[Dict[str, Any]]:
        """إرجاع قائمة مُعيدي النشر (actors) مع ترقيم صفحات"""
        out = []
        cursor = None
        while True:
            res = self.client.app.bsky.feed.get_reposted_by({"uri": post_uri, "limit": 100, "cursor": cursor})
            for actor in getattr(res, "reposted_by", []):
                out.append(actor)
            cursor = getattr(res, "cursor", None)
            if not cursor:
                break
        return out

    def _get_last_post_of_actor(self, actor_handle_or_did: str) -> Optional[Tuple[str, str]]:
        """
        يجلب آخر منشور (uri, cid) لحساب (actor) حتى يمكن الرد عليه.
        نستخدم get_author_feed(limit=1).
        """
        try:
            # بعض الاستدعاءات تقبل handle أو did مباشرة
            feed = self.client.app.bsky.feed.get_author_feed(
                {"actor": actor_handle_or_did, "limit": 1, "filter": "posts_with_replies"}  # أضمن وجود post
            )
            items = getattr(feed, "feed", [])
            if not items:
                return None
            post = getattr(items[0], "post", None)
            if not post:
                return None
            return post.uri, post.cid
        except Exception as e:
            log.error(f"⚠️ فشل جلب آخر منشور للمستخدم {actor_handle_or_did}: {e}")
            return None

    def _reply_to_post(self, target_uri: str, target_cid: str, text: str) -> None:
        """إرسال رد على منشور معيّن."""
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

    # ===== تنفيذ المهمة =====
    def process_audience(
        self,
        source_post_url: str,
        audience_type: str,              # "likers" | "reposters"
        messages: List[str],
        min_delay: int,
        max_delay: int,
    ) -> Dict[str, int]:
        """
        يجمع الجمهور المستهدف من منشور محدّد ويرسل ردًّا على آخر منشور لكل مستخدم.
        يحفظ التقدم باستمرار في PROGRESS_PATH تحت progress_key.
        """
        self.login()

        # استعادة التقدّم السابق (إن وجد)
        progress = load_progress(PROGRESS_PATH, self.progress_key) or {
            "done_user_dids": [],   # مستخدمون تم إنجازهم
            "failed_user_dids": [], # مستخدمون فشلوا
            "cursor": None,
            "last_source": source_post_url,
            "audience_type": audience_type,
        }
        done = len(progress.get("done_user_dids", []))
        failed = len(progress.get("failed_user_dids", []))
        update_progress_counts(done, failed)

        # حلّ رابط المنشور
        ref = resolve_post_from_url(self.client, source_post_url)
        if not ref:
            raise RuntimeError("لم أتمكن من حلّ رابط المنشور.")

        post_uri = ref["uri"]

        # جمع الجمهور
        if audience_type == "likers":
            actors = self._iter_likers(post_uri)
        elif audience_type == "reposters":
            actors = self._iter_reposters(post_uri)
        else:
            raise ValueError("audience_type يجب أن يكون 'likers' أو 'reposters'.")

        log.info(f"👥 تم العثور على {len(actors)} مستخدمين في الجمهور ({audience_type}).")

        # فلترة من تمّت معالجتهم سابقًا
        already = set(progress.get("done_user_dids", [])) | set(progress.get("failed_user_dids", []))
        queue = []
        for a in actors:
            did = getattr(a, "did", None)
            handle = getattr(a, "handle", None)
            if not did:
                continue
            if did in already:
                continue
            queue.append({"did": did, "handle": handle})

        log.info(f"🧾 قائمة المعالجة: {len(queue)} مستخدمين جدد بعد استبعاد المنجزين سابقًا.")

        # حلقة المعالجة
        for idx, user in enumerate(queue, 1):
            if self.stop_event.is_set():
                log.warning("⏸️ تم إيقاف المهمة بواسطة المستخدم.")
                break

            runtime_stats["status"] = "Running"
            runtime_stats["current_task"] = f"Processing audience: {idx}/{len(queue)}"
            _stats_touch()

            did = user["did"]
            handle = user["handle"] or did
            try:
                # آخر منشور للمستخدم
                last_ref = self._get_last_post_of_actor(did)
                if not last_ref:
                    log.warning(f"لا يوجد منشور حديث للمستخدم: {handle}")
                    progress.setdefault("failed_user_dids", []).append(did)
                    failed += 1
                    update_progress_counts(done, failed)
                    save_progress(PROGRESS_PATH, self.progress_key, progress)
                    continue

                target_uri, target_cid = last_ref

                # اختيار رسالة صالحة
                valid_msgs = [m.strip() for m in messages if validate_message_template(m.strip())]
                if not valid_msgs:
                    valid_msgs = ["🙏 Thank you!"]
                msg = random.choice(valid_msgs)

                # إرسال الرد
                self._reply_to_post(target_uri, target_cid, msg)
                log.info(f"💬 Reply OK -> @{handle}: {msg[:60]}")

                # تحديث التقدّم
                progress.setdefault("done_user_dids", []).append(did)
                done += 1
                update_progress_counts(done, failed)
                save_progress(PROGRESS_PATH, self.progress_key, progress)

            except AtProtocolError as e:
                log.error(f"⚠️ بروتوكول: فشل الرد على @{handle}: {e}")
                progress.setdefault("failed_user_dids", []).append(did)
                failed += 1
                update_progress_counts(done, failed)
                save_progress(PROGRESS_PATH, self.progress_key, progress)
            except Exception as e:
                log.error(f"⚠️ خطأ عام عند @{handle}: {e}")
                progress.setdefault("failed_user_dids", []).append(did)
                failed += 1
                update_progress_counts(done, failed)
                save_progress(PROGRESS_PATH, self.progress_key, progress)

            # تأخير بين المستخدمين
            delay = random.randint(int(min_delay), int(max_delay))
            log.info(f"⏳ الانتظار {delay} ثانية قبل المستخدم التالي…")
            for _ in range(delay):
                if self.stop_event.is_set():
                    break
                time.sleep(1)

        return {"completed": done, "failed": failed}


# ---------- Flask App ----------
app = Flask(__name__, template_folder="templates")

bot_thread: Optional[threading.Thread] = None
bot_instance: Optional[BlueSkyBot] = None


@app.route("/")
def index():
    # صفحة الواجهة (persistent.html)
    return render_template("persistent.html")


@app.post("/queue_task")
def queue_task():
    """
    يستقبل JSON من الواجهة:
    {
        "post_url": "...",                  (أو post_urls[0]؛ نستخدم واحد فقط هنا)
        "messages": ["...", "..."],
        "bluesky_handle": "...",
        "bluesky_password": "...",
        "audience_type": "likers" | "reposters",
        "min_delay": 200,
        "max_delay": 250
    }
    """
    global bot_thread, bot_instance

    try:
        payload = request.get_json(force=True) or {}
        log.info(f"📥 Payload: {payload}")
    except Exception:
        return jsonify({"error": "Bad JSON"}), 400

    post_url = payload.get("post_url") or (payload.get("post_urls") or [None])[0]
    messages = payload.get("messages") or payload.get("message_templates") or []
    bluesky_handle = payload.get("bluesky_handle") or os.getenv("BLUESKY_HANDLE") or os.getenv("BSKY_HANDLE")
    bluesky_password = payload.get("bluesky_password") or os.getenv("BLUESKY_PASSWORD") or os.getenv("BSKY_PASSWORD")
    audience_type = payload.get("audience_type", "likers")  # likers | reposters
    min_delay = int(payload.get("min_delay", DEFAULT_MIN_DELAY))
    max_delay = int(payload.get("max_delay", DEFAULT_MAX_DELAY))

    if not (post_url and bluesky_handle and bluesky_password):
        return jsonify({"error": "❌ البيانات ناقصة: post_url / handle / password"}), 400

    if min_delay > max_delay:
        min_delay, max_delay = max_delay, min_delay

    # مفتاح التقدم: حساب + رابط
    progress_key = f"{bluesky_handle}::{post_url}::{audience_type}"

    # إنشاء وتهيئة البوت
    cfg = Config(
        bluesky_handle=bluesky_handle,
        bluesky_password=bluesky_password,
        min_delay=min_delay,
        max_delay=max_delay,
    )
    bot_instance = BlueSkyBot(cfg, progress_key)

    # مسح أي إيقاف سابق
    bot_instance.stop_event.clear()

    def run_bot():
        start_ts = time.time()
        runtime_stats["status"] = "Running"
        runtime_stats["current_task"] = "Processing audience"
        _stats_touch()

        try:
            result = bot_instance.process_audience(
                source_post_url=post_url,
                audience_type=audience_type,
                messages=messages,
                min_delay=min_delay,
                max_delay=max_delay,
            )
            update_progress_counts(result["completed"], result["failed"])
        except Exception as e:
            log.error(f"❌ فشل تنفيذ المهمة: {e}")
        finally:
            runtime_stats["status"] = "Idle"
            runtime_stats["current_task"] = None
            runtime_stats["session_uptime"] = format_duration(int(time.time() - start_ts))
            _stats_touch()

    # تشغيل بالخلفية
    bot_thread = threading.Thread(target=run_bot, daemon=True)
    bot_thread.start()

    return jsonify({"status": "✅ المهمة بدأت"})


@app.post("/stop_task")
def stop_task():
    if bot_instance:
        bot_instance.stop_event.set()
    runtime_stats["status"] = "Stopped"
    runtime_stats["current_task"] = None
    _stats_touch()
    return jsonify({"status": "🛑 تم الإيقاف"})


@app.post("/resume_task")
def resume_task():
    # الاستئناف يعني إرسال طلب queue_task جديد بنفس المعطيات من الواجهة
    # هنا فقط نحدث الحالة بصريًا.
    runtime_stats["status"] = "Running"
    _stats_touch()
    return jsonify({"status": "▶️ تم الاستئناف (أرسلي بدء المهمة من الواجهة لإكمال المعالجة)"})


@app.get("/detailed_progress")
def detailed_progress():
    return jsonify({"runtime_stats": runtime_stats, "bot_progress": bot_progress})


# Aliases اختيارية
@app.post("/queue")
def queue_alias():
    return queue_task()

@app.get("/progress")
def progress_alias():
    return detailed_progress()


if __name__ == "__main__":
    port = int(os.getenv("PORT", 10000))
    app.run(host="0.0.0.0", port=port)
