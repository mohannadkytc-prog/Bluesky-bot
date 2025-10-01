"""
Bluesky Bot + Flask wrapper (robust payload parsing)
- يقبل post_url أو post_urls[] أو أسماء بديلة
- يقبل messages أو message_templates
- المسارات Aliases: /queue_task, /detailed_progress, /stop_task, /resume_task
"""

import os
import time
import random
import logging
import threading
from dataclasses import dataclass
from typing import List, Dict, Optional, Any

from flask import Flask, jsonify, render_template, request

from atproto import Client
from atproto.exceptions import AtProtocolError
from utils import resolve_post_from_url

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("bluesky-bot")

# ================== البوت ==================
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

    def login(self):
        log.info(f"🔑 تسجيل الدخول باستخدام الحساب: {self.config.bluesky_handle}")
        self.client.login(self.config.bluesky_handle, self.config.bluesky_password)

    def process_posts(self, post_urls: List[str], messages: List[str], processing_type: str):
        self.login()

        completed = 0
        failed = 0
        for url in post_urls:
            try:
                post_ref = resolve_post_from_url(self.client, url)
                if not post_ref:
                    log.error(f"❌ لم أستطع解析 الرابط: {url}")
                    failed += 1
                    continue

                msg = random.choice(messages)

                if processing_type in ("reposts", "both", "reposts_and_replies"):
                    self.client.repost(post_ref)
                    log.info(f"🔁 تم إعادة النشر: {url}")

                if processing_type in ("replies", "both", "reposts_and_replies"):
                    self.client.send_post(text=msg, reply_to=post_ref)
                    log.info(f"💬 تم الرد برسالة: {msg}")

                completed += 1
                delay = random.randint(self.config.min_delay, self.config.max_delay)
                log.info(f"⏳ الانتظار {delay} ثانية قبل المهمة التالية")
                time.sleep(delay)

                if self.progress_cb:
                    self.progress_cb(completed, failed)

            except Exception as e:
                log.error(f"⚠️ خطأ أثناء المعالجة: {e}")
                failed += 1

        return {"completed": completed, "failed": failed}


# ================== الويب سيرفر ==================
app = Flask(__name__)
runtime_stats = {
    "status": "Idle",
    "current_task": None,
    "session_uptime": "0s"
}
bot_progress = {
    "completed_runs": 0,
    "failed_runs": 0,
    "total_bot_runs": 0,
    "success_rate": 0.0,
    "total_mentions_sent": 0,
    "total_followers": 0
}

bot_thread: Optional[threading.Thread] = None
bot_instance: Optional[BlueSkyBot] = None


def update_progress(completed, failed):
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
    log.info(f"📥 استلمت بيانات: {data}")

    # دعم أسماء مختلفة للحقول
    post_urls = data.get("post_urls") or [data.get("post_url")]
    post_urls = [u for u in post_urls if u] if post_urls else []

    messages = data.get("message_templates") or data.get("messages") or []
    bluesky_handle = data.get("bluesky_handle") or os.getenv("BLUESKY_HANDLE")
    bluesky_password = data.get("bluesky_password") or os.getenv("BLUESKY_PASSWORD")
    processing_type = data.get("processing_type", "replies")

    min_delay = int(data.get("min_delay", 200))
    max_delay = int(data.get("max_delay", 250))

    if not bluesky_handle or not bluesky_password or not post_urls or not messages:
        return jsonify({"error": "❌ البيانات غير مكتملة"}), 400

    config = Config(
        bluesky_handle=bluesky_handle,
        bluesky_password=bluesky_password,
        min_delay=min_delay,
        max_delay=max_delay
    )

    bot_instance = BlueSkyBot(config)

    def run_bot():
        runtime_stats["status"] = "Running"
        runtime_stats["current_task"] = "Processing posts"
        start_time = time.time()

        result = bot_instance.process_posts(post_urls, messages, processing_type)

        runtime_stats["status"] = "Idle"
        runtime_stats["current_task"] = None
        runtime_stats["session_uptime"] = f"{int(time.time()-start_time)}s"

        update_progress(result["completed"], result["failed"])

    bot_thread = threading.Thread(target=run_bot)
    bot_thread.start()

    return jsonify({"status": "✅ المهمة بدأت"})


@app.route("/stop_task")
def stop_task():
    runtime_stats["status"] = "Stopped"
    return jsonify({"status": "🛑 تم الإيقاف"})


@app.route("/resume_task")
def resume_task():
    runtime_stats["status"] = "Running"
    return jsonify({"status": "▶️ تم الاستئناف"})


@app.route("/detailed_progress")
def detailed_progress():
    return jsonify({
        "runtime_stats": runtime_stats,
        "bot_progress": bot_progress
    })


if __name__ == "__main__":
    port = int(os.getenv("PORT", 10000))
    app.run(host="0.0.0.0", port=port)
