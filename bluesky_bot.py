"""
Bluesky Bot + Flask wrapper
- متناسق مع config.py و utils.py
- يستخدم /data للتخزين
- كل القيم (يوزر، باسورد، روابط، رسائل) من الواجهة
"""

import os
import time
import random
import logging
import threading
from typing import List, Dict, Optional
from datetime import datetime, timezone

from flask import Flask, jsonify, render_template, request
from atproto import Client
from atproto.exceptions import AtProtocolError

from config import Config
from utils import resolve_post_from_url, save_progress, load_progress

# إعداد اللوج
logging.basicConfig(level=logging.INFO)
log = logging.getLogger("bluesky-bot")

# ================== منطق البوت ==================
class BlueSkyBot:
    def __init__(self, config: Config):
        self.config = config
        self.client = Client()
        self.progress_cb = None

    def login(self) -> None:
        log.info(f"🔑 تسجيل الدخول: {self.config.bluesky_handle}")
        self.client.login(self.config.bluesky_handle, self.config.bluesky_password)

    def process_posts(self, post_urls: List[str], messages: List[str], processing_type: str,
                      progress_key: str) -> Dict[str, int]:
        """يعالج قائمة روابط منشورات (ردود + إعادة نشر حسب الإعدادات)"""
        self.login()

        completed = 0
        failed = 0

        # تحميل التقدم السابق
        progress = load_progress(None, progress_key, config=self.config)
        processed_urls = set(progress.get("processed_urls", []))

        for url in post_urls:
            if url in processed_urls:
                log.info(f"⏭️ تخطي: {url} (معالج مسبقاً)")
                continue

            try:
                post_ref = resolve_post_from_url(self.client, url)
                if not post_ref:
                    log.error(f"❌ فشل حلّ الرابط: {url}")
                    failed += 1
                    continue

                uri = post_ref["uri"]
                cid = post_ref["cid"]

                # إعادة نشر
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

                # الرد
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

                completed += 1
                processed_urls.add(url)

                # حفظ التقدم
                progress_data = {
                    "processed_urls": list(processed_urls),
                    "completed": completed,
                    "failed": failed,
                    "last_url": url,
                }
                save_progress(None, progress_key, progress_data, config=self.config)

                # تأخير
                delay = random.randint(self.config.min_delay, self.config.max_delay)
                log.info(f"⏳ الانتظار {delay} ثانية قبل المهمة التالية")
                time.sleep(delay)

                if self.progress_cb:
                    self.progress_cb(completed, failed)

            except Exception as e:
                log.error(f"⚠️ خطأ أثناء المعالجة: {e}")
                failed += 1

        return {"completed": completed, "failed": failed}


# ================== خادم الويب ==================
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
    return render_template("persistent.html")


@app.route("/queue_task", methods=["POST"])
def queue_task():
    global bot_thread, bot_instance

    data = request.get_json(force=True)
    log.info(f"📥 Payload: {data}")

    post_urls = data.get("post_urls") or [data.get("post_url")]
    post_urls = [u for u in (post_urls or []) if u]

    messages = data.get("message_templates") or data.get("messages") or []
    bluesky_handle = data.get("bluesky_handle")
    bluesky_password = data.get("bluesky_password")
    processing_type = data.get("processing_type", "replies")

    min_delay = int(data.get("min_delay", 200))
    max_delay = int(data.get("max_delay", 250))

    if not bluesky_handle or not bluesky_password or not post_urls:
        return jsonify({"error": "❌ البيانات ناقصة (handle/password/post_url)"}), 400
    if not messages:
        messages = ["🙏 Thank you for supporting."]

    config = Config(
        bluesky_handle=bluesky_handle,
        bluesky_password=bluesky_password,
    )
    config.min_delay = min_delay
    config.max_delay = max_delay

    bot_instance = BlueSkyBot(config)

    progress_key = f"{bluesky_handle}_{post_urls[0]}"

    def run_bot():
        start = time.time()
        runtime_stats["status"] = "Running"
        runtime_stats["current_task"] = "Processing posts"

        result = bot_instance.process_posts(post_urls, messages, processing_type, progress_key)

        runtime_stats["status"] = "Idle"
        runtime_stats["current_task"] = None
        runtime_stats["session_uptime"] = f"{int(time.time() - start)}s"

        update_progress(result["completed"], result["failed"])

    bot_thread = threading.Thread(target=run_bot, daemon=True)
    bot_thread.start()

    return jsonify({"status": "✅ المهمة بدأت"})


@app.route("/stop_task", methods=["POST", "GET"])
def stop_task():
    runtime_stats["status"] = "Stopped"
    return jsonify({"status": "🛑 تم الإيقاف"})


@app.route("/resume_task", methods=["POST", "GET"])
def resume_task():
    runtime_stats["status"] = "Running"
    return jsonify({"status": "▶️ تم الاستئناف"})


@app.route("/detailed_progress")
def detailed_progress():
    return jsonify({"runtime_stats": runtime_stats, "bot_progress": bot_progress})


if __name__ == "__main__":
    port = int(os.getenv("PORT", 10000))
    app.run(host="0.0.0.0", port=port)
