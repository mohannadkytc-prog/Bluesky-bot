"""
Bluesky Bot + Flask wrapper (robust payload parsing)
- يقبل post_url أو post_urls[] أو أسماء بديلة
- يقبل messages أو message_templates
- Aliases للمسارات: /queue_task, /detailed_progress, /stop_task, /resume_task
"""

import os
import time
import random
import logging
import threading
from dataclasses import dataclass
from typing import List, Dict, Optional, Any
from datetime import datetime, timezone  # <-- مهم: استيراد هنا وليس داخل الدالة

from flask import Flask, jsonify, render_template, request

from atproto import Client
from atproto.exceptions import AtProtocolError
from utils import resolve_post_from_url

# إعداد اللوج
logging.basicConfig(level=logging.INFO)
log = logging.getLogger("bluesky-bot")


# ================== منطق البوت ==================
@dataclass
class Config:
    bluesky_handle: str
    bluesky_password: str
    min_delay: int = 200
    max_delay: int = 250


class BlueSkyBot:
    def __init__(self, config: Config):
        self.config = config
        self.client = Client()
        self.progress_cb = None

    def login(self) -> None:
        log.info(f"🔑 تسجيل الدخول: {self.config.bluesky_handle}")
        self.client.login(self.config.bluesky_handle, self.config.bluesky_password)

    def process_posts(self, post_urls: List[str], messages: List[str], processing_type: str) -> Dict[str, int]:
        """
        يعالج قائمة روابط منشورات:
        - Repost عبر app.bsky.feed.repost
        - Reply عبر app.bsky.feed.post مع reply.root/parent
        """
        self.login()

        completed = 0
        failed = 0

        for url in post_urls:
            try:
                post_ref = resolve_post_from_url(self.client, url)
                if not post_ref:
                    log.error(f"❌ فشل حلّ الرابط: {url}")
                    failed += 1
                    continue

                uri = post_ref["uri"]
                cid = post_ref["cid"]

                # 1) إعادة نشر إن طُلب
                if processing_type in ("reposts", "both", "reposts_and_replies"):
                    try:
                        self.client.com.atproto.repo.create_record({
                            "repo": self.client.me.did,
                            "collection": "app.bsky.feed.repost",
                            "record": {
                                "$type": "app.bsky.feed.repost",
                                "subject": {"uri": uri, "cid": cid},
                                "createdAt": datetime.now(timezone.utc).isoformat(),
                            },
                        })
                        log.info(f"🔁 Repost OK: {url}")
                    except Exception as e:
                        log.error(f"⚠️ Repost failed: {e}")
                        failed += 1
                        # نكمل للرد إذا مطلوب

                # 2) رد إن طُلب
                if processing_type in ("replies", "both", "reposts_and_replies"):
                    msg = random.choice(messages) if messages else "🙏"
                    try:
                        self.client.com.atproto.repo.create_record({
                            "repo": self.client.me.did,
                            "collection": "app.bsky.feed.post",
                            "record": {
                                "$type": "app.bsky.feed.post",
                                "text": msg,
                                "createdAt": datetime.now(timezone.utc).isoformat(),
                                "reply": {
                                    "root": {"uri": uri, "cid": cid},
                                    "parent": {"uri": uri, "cid": cid},
                                },
                            },
                        })
                        log.info(f"💬 Reply OK: {msg[:40]}…")
                    except Exception as e:
                        log.error(f"⚠️ Reply failed: {e}")
                        failed += 1
                        # نتابع للمنشور التالي

                completed += 1

                # تأخير بين العمليات
                delay = random.randint(self.config.min_delay, self.config.max_delay)
                log.info(f"⏳ الانتظار {delay} ثانية قبل المهمة التالية")
                time.sleep(delay)

                if self.progress_cb:
                    self.progress_cb(completed, failed)

            except Exception as e:
                log.error(f"⚠️ خطأ عام أثناء المعالجة: {e}")
                failed += 1

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
    "total_mentions_sent": 0,
    "total_followers": 0,
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
    return render_template("persistent.html")


@app.route("/queue_task", methods=["POST"])
def queue_task():
    global bot_thread, bot_instance

    data = request.get_json(force=True)
    log.info(f"📥 Payload: {data}")

    # دعم أسماء متعددة للحقول
    post_urls = data.get("post_urls") or [data.get("post_url")]
    post_urls = [u for u in (post_urls or []) if u]

    messages = data.get("message_templates") or data.get("messages") or []
    bluesky_handle = data.get("bluesky_handle") or os.getenv("BLUESKY_HANDLE") or os.getenv("BSKY_HANDLE")
    bluesky_password = data.get("bluesky_password") or os.getenv("BLUESKY_PASSWORD") or os.getenv("BSKY_PASSWORD")
    processing_type = data.get("processing_type", "replies")

    min_delay = int(data.get("min_delay", 200))
    max_delay = int(data.get("max_delay", 250))

    if not bluesky_handle or not bluesky_password or not post_urls:
        return jsonify({"error": "❌ البيانات ناقصة (handle/password/post_url)"}), 400
    if not messages:
        # بنسمح برسالة افتراضية إذا نسي المستخدم
        messages = ["🙏 Thank you for supporting."]

    # تهيئة البوت
    config = Config(
        bluesky_handle=bluesky_handle,
        bluesky_password=bluesky_password,
        min_delay=min_delay,
        max_delay=max_delay,
    )
    bot_instance = BlueSkyBot(config)

    # عامل تشغيل بالخلفية
    def run_bot():
        start = time.time()
        runtime_stats["status"] = "Running"
        runtime_stats["current_task"] = "Processing posts"

        result = bot_instance.process_posts(post_urls, messages, processing_type)

        runtime_stats["status"] = "Idle"
        runtime_stats["current_task"] = None
        runtime_stats["session_uptime"] = f"{int(time.time() - start)}s"

        update_progress(result["completed"], result["failed"])

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


@app.route("/detailed_progress")
def detailed_progress():
    return jsonify({"runtime_stats": runtime_stats, "bot_progress": bot_progress})


# Aliases اختيارية إن أردتِ
@app.post("/queue")
def queue_alias():
    return queue_task()

@app.get("/progress")
def progress_alias():
    return detailed_progress()


if __name__ == "__main__":
    port = int(os.getenv("PORT", 10000))
    app.run(host="0.0.0.0", port=port)
