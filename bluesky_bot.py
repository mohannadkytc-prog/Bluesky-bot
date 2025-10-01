"""
Bluesky Bot + Flask wrapper
- يجمع الجمهور من (المعجبين / معيدو النشر / الاثنين) لكل رابط، ثم يرد على آخر منشور لكل مستخدم
- الواجهة ترسل: post_urls[], message_templates[], bluesky_handle, bluesky_password,
  min_delay, max_delay, audience_type ∈ {likers, reposters, both}
"""

import os
import time
import random
import logging
import threading
from dataclasses import dataclass
from typing import List, Dict, Optional, Any
from datetime import datetime, timezone

from flask import Flask, jsonify, render_template, request

from atproto import Client
from atproto.exceptions import AtProtocolError

from utils import resolve_post_from_url  # يعتمد على utils.py الذي أرسلته سابقاً
from config import Config as AppConfig, PROGRESS_PATH  # يستخدم /data أو /tmp تلقائياً

# ===== إعداد اللوج =====
logging.basicConfig(level=logging.INFO)
log = logging.getLogger("bluesky-bot")


# ================== منطق البوت ==================
@dataclass
class RuntimeConfig:
    bluesky_handle: str
    bluesky_password: str
    min_delay: int
    max_delay: int


class BlueSkyBot:
    def __init__(self, runtime_cfg: RuntimeConfig):
        self.runtime_cfg = runtime_cfg
        self.client = Client()
        self.progress_cb = None

    def login(self) -> None:
        log.info(f"🔑 تسجيل الدخول: {self.runtime_cfg.bluesky_handle}")
        self.client.login(self.runtime_cfg.bluesky_handle, self.runtime_cfg.bluesky_password)

    def _sleep_with_log(self) -> None:
        delay = random.randint(self.runtime_cfg.min_delay, self.runtime_cfg.max_delay)
        log.info(f"⏳ الانتظار {delay} ثانية قبل المستخدم التالي")
        time.sleep(delay)

    def _reply_to_post(self, post_uri: str, post_cid: str, msg: str) -> None:
        self.client.com.atproto.repo.create_record({
            "repo": self.client.me.did,
            "collection": "app.bsky.feed.post",
            "record": {
                "$type": "app.bsky.feed.post",
                "text": msg,
                "createdAt": datetime.now(timezone.utc).isoformat(),
                "reply": {
                    "root": {"uri": post_uri, "cid": post_cid},
                    "parent": {"uri": post_uri, "cid": post_cid},
                },
            },
        })

    def process_audience(
        self,
        post_urls: List[str],
        messages: List[str],
        audience_type: str,  # likers | reposters | both
    ) -> Dict[str, int]:
        """
        يجمع جمهور المنشور (likers/reposters) ويرد على آخر منشور لكل مستخدم.
        """
        self.login()

        completed = 0
        failed = 0

        # 1) اجمع كل الـ DIDs المستهدفة من كل رابط
        target_dids = set()

        for url in post_urls:
            try:
                post_ref = resolve_post_from_url(self.client, url)
                if not post_ref:
                    log.error(f"❌ فشل حلّ الرابط: {url}")
                    continue

                uri = post_ref["uri"]
                log.info(f"🎯 معالجة المصدر '{audience_type}' لهذا الرابط: {url}")

                if audience_type in ("likers", "both"):
                    try:
                        likes_resp = self.client.app.bsky.feed.get_likes({"uri": uri})
                        for item in getattr(likes_resp, "likes", []) or []:
                            if getattr(item, "actor", None) and getattr(item.actor, "did", None):
                                target_dids.add(item.actor.did)
                    except Exception as e:
                        log.error(f"⚠️ get_likes فشل: {e}")

                if audience_type in ("reposters", "both"):
                    try:
                        reps_resp = self.client.app.bsky.feed.get_reposted_by({"uri": uri})
                        for actor in getattr(reps_resp, "reposted_by", []) or []:
                            if getattr(actor, "did", None):
                                target_dids.add(actor.did)
                    except Exception as e:
                        log.error(f"⚠️ get_reposted_by فشل: {e}")

            except Exception as e:
                log.error(f"⚠️ خطأ أثناء جمع الجمهور من {url}: {e}")

        log.info(f"👥 عدد المستخدمين المستهدفين الإجمالي: {len(target_dids)}")

        # 2) رد على آخر منشور لكل DID
        for did in list(target_dids):
            try:
                feed = self.client.app.bsky.feed.get_author_feed({"actor": did, "limit": 1})
                items = getattr(feed, "feed", []) or []
                if not items:
                    log.info(f"ℹ️ لا يوجد منشورات للمستخدم {did}")
                    continue

                post = items[0].post
                if not post or not post.uri or not post.cid:
                    log.info(f"ℹ️ منشور غير صالح للمستخدم {did}")
                    continue

                msg = random.choice(messages) if messages else "🙏"
                self._reply_to_post(post.uri, post.cid, msg)
                log.info(f"💬 Reply OK: {did} ← {msg[:40]}…")
                completed += 1

            except AtProtocolError as e:
                log.error(f"❌ بروتوكول فشل {did}: {e}")
                failed += 1
            except Exception as e:
                log.error(f"❌ خطأ عام عند الرد على {did}: {e}")
                failed += 1

            # تحديث التقدم + التأخير
            if self.progress_cb:
                self.progress_cb(completed, failed)
            self._sleep_with_log()

        return {"completed": completed, "failed": failed}


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
    "total_bot_runs": 0,
    "success_rate": 0.0,
}

bot_thread: Optional[threading.Thread] = None
bot_instance: Optional[BlueSkyBot] = None


def update_progress(completed: int, failed: int) -> None:
    bot_progress["completed_runs"] = completed
    bot_progress["failed_runs"] = failed
    total = completed + failed
    bot_progress["total_bot_runs"] = total
    bot_progress["success_rate"] = (completed / total) if total else 0.0


@app.route("/")
def index():
    # نمرر مسار ملف التقدم للواجهة للعرض فقط
    return render_template("persistent.html", progress_path=PROGRESS_PATH)


@app.post("/queue_task")
def queue_task():
    """
    API لبدء المهمة من الواجهة.
    """
    global bot_thread, bot_instance

    data = request.get_json(force=True)
    log.info(f"📥 Payload: {data}")

    # 1) قراءة المدخلات
    post_urls = data.get("post_urls") or [data.get("post_url")]
    post_urls = [u for u in (post_urls or []) if u]

    messages = data.get("message_templates") or data.get("messages") or []
    bluesky_handle = data.get("bluesky_handle")
    bluesky_password = data.get("bluesky_password")

    min_delay = int(data.get("min_delay")) if data.get("min_delay") else None
    max_delay = int(data.get("max_delay")) if data.get("max_delay") else None

    audience_type = data.get("audience_type", "likers")  # likers | reposters | both

    # 2) إذا لم تُرسل حقول، نسمح بالقيم الافتراضية من AppConfig
    app_cfg = AppConfig(
        bluesky_handle=bluesky_handle,
        bluesky_password=bluesky_password,
        min_delay=min_delay,
        max_delay=max_delay,
    )
    if not app_cfg.is_valid() or not post_urls:
        return jsonify({"error": "❌ البيانات ناقصة (handle/password/post_urls)"}), 400
    if not messages:
        messages = ["🙏 Thank you for supporting."]

    runtime_cfg = RuntimeConfig(
        bluesky_handle=app_cfg.bluesky_handle,
        bluesky_password=app_cfg.bluesky_password,
        min_delay=app_cfg.min_delay,
        max_delay=app_cfg.max_delay,
    )
    bot = BlueSkyBot(runtime_cfg)
    bot.progress_cb = update_progress

    # 3) عامل تشغيل بالخلفية
    def run_bot():
        start = time.time()
        runtime_stats["status"] = "Running"
        runtime_stats["current_task"] = f"Processing audience: {audience_type}"

        result = bot.process_audience(post_urls, messages, audience_type)

        runtime_stats["status"] = "Idle"
        runtime_stats["current_task"] = None
        runtime_stats["session_uptime"] = f"{int(time.time() - start)}s"

        update_progress(result["completed"], result["failed"])

    bot_instance = bot
    bot_thread = threading.Thread(target=run_bot, daemon=True)
    bot_thread.start()

    return jsonify({"status": "✅ المهمة بدأت"})


@app.route("/stop_task", methods=["GET", "POST"])
def stop_task():
    runtime_stats["status"] = "Stopped"
    return jsonify({"status": "🛑 تم الإيقاف"})


@app.route("/resume_task", methods=["GET", "POST"])
def resume_task():
    runtime_stats["status"] = "Running"
    return jsonify({"status": "▶️ تم الاستئناف"})


@app.get("/detailed_progress")
def detailed_progress():
    return jsonify({"runtime_stats": runtime_stats, "bot_progress": bot_progress})


# Aliases إضافية
@app.post("/queue")
def queue_alias():
    return queue_task()

@app.get("/progress")
def progress_alias():
    return detailed_progress()


if __name__ == "__main__":
    port = int(os.getenv("PORT", 10000))
    app.run(host="0.0.0.0", port=port)
