"""
Bluesky Bot + Flask wrapper (robust payload parsing)
- يقبل post_url أو post_urls[] أو أسماء بديلة
- يقبل messages أو message_templates
- مسارات جاهزة: /queue_task, /detailed_progress, /stop_task, /resume_task
- التخزين: تلقائي /data إن وُجد، وإلا /tmp (Starter)
"""

import os
import time
import random
import logging
import threading
from dataclasses import dataclass
from typing import List, Dict, Optional
from datetime import datetime, timezone

from flask import Flask, jsonify, render_template, request
from atproto import Client

from config import Config, PROGRESS_PATH  # مسار ملف التقدم جاهز
from utils import resolve_post_from_url, save_progress, load_progress, validate_message_template, format_duration

# ===== إعداد اللوج =====
logging.basicConfig(level=logging.INFO)
log = logging.getLogger("bluesky-bot")


# ================== منطق البوت ==================
@dataclass
class BotMetrics:
    completed: int = 0
    failed: int = 0
    replies_sent: int = 0


class BlueSkyBot:
    def __init__(self, config: Config):
        self.config = config
        self.client = Client()
        self.progress_cb = None  # callback للتحديث

    def login(self) -> None:
        log.info(f"🔑 تسجيل الدخول: {self.config.bluesky_handle}")
        self.client.login(self.config.bluesky_handle, self.config.bluesky_password)

    def _safe_choice(self, messages: List[str]) -> str:
        valid = [m.strip() for m in messages if validate_message_template(m.strip())]
        return random.choice(valid) if valid else "🙏"

    def process_posts(self, post_urls: List[str], messages: List[str], processing_type: str) -> Dict[str, int]:
        """
        يعالج قائمة روابط منشورات:
        - Repost عبر app.bsky.feed.repost
        - Reply عبر app.bsky.feed.post مع reply.root/parent
        """
        self.login()

        metrics = BotMetrics()

        for url in post_urls:
            # حمل تقدّم سابق (لو موجود)
            progress = load_progress(PROGRESS_PATH, url)
            already_done = bool(progress.get("done"))
            if already_done:
                log.info(f"⏭️ تم إنجاز هذا الرابط سابقًا، تخطي: {url}")
                continue

            try:
                post_ref = resolve_post_from_url(self.client, url)
                if not post_ref:
                    log.error(f"❌ فشل حلّ الرابط: {url}")
                    metrics.failed += 1
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
                        metrics.failed += 1
                        # نكمل للرد إذا مطلوب

                # 2) رد إن طُلب
                if processing_type in ("replies", "both", "reposts_and_replies"):
                    msg = self._safe_choice(messages)
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
                        metrics.replies_sent += 1
                        log.info(f"💬 Reply OK: {msg[:40]}…")
                    except Exception as e:
                        log.error(f"⚠️ Reply failed: {e}")
                        metrics.failed += 1

                metrics.completed += 1

                # احفظ التقدم لهذا الرابط
                save_progress(PROGRESS_PATH, url, {
                    "done": True,
                    "last_at": datetime.now(timezone.utc).isoformat(),
                    "processing_type": processing_type,
                    "replies_sent": metrics.replies_sent,
                })

                # تأخير بين العمليات
                delay = random.randint(self.config.min_delay, self.config.max_delay)
                log.info(f"⏳ الانتظار {delay} ثانية قبل المهمة التالية")
                time.sleep(delay)

                if self.progress_cb:
                    self.progress_cb(metrics.completed, metrics.failed)

            except Exception as e:
                log.error(f"⚠️ خطأ عام أثناء المعالجة: {e}")
                metrics.failed += 1

        return {"completed": metrics.completed, "failed": metrics.failed, "replies_sent": metrics.replies_sent}


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
}

bot_thread: Optional[threading.Thread] = None
bot_instance: Optional[BlueSkyBot] = None


def _update_progress(completed: int, failed: int) -> None:
    bot_progress["completed_runs"] = completed
    bot_progress["failed_runs"] = failed
    total = completed + failed
    bot_progress["total_bot_runs"] = total
    bot_progress["success_rate"] = (completed / total) if total else 0.0


@app.route("/")
def index():
    # نفس صفحة الواجهة التي عندك: templates/persistent.html
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

    min_delay = int(data.get("min_delay")) if data.get("min_delay") is not None else None
    max_delay = int(data.get("max_delay")) if data.get("max_delay") is not None else None

    if not bluesky_handle or not bluesky_password or not post_urls:
        return jsonify({"error": "❌ البيانات ناقصة (handle/password/post_url)"}), 400
    if not messages:
        messages = ["🙏 Thank you for supporting."]

    # ضبط الإعدادات
    cfg = Config(
        bluesky_handle=bluesky_handle,
        bluesky_password=bluesky_password,
        min_delay=min_delay,
        max_delay=max_delay,
    )
    bot_instance = BlueSkyBot(cfg)
    bot_instance.progress_cb = _update_progress

    # عامل تشغيل بالخلفية
    def run_bot():
        start = time.time()
        runtime_stats["status"] = "Running"
        runtime_stats["current_task"] = "Processing audience"
        try:
            result = bot_instance.process_posts(post_urls, messages, processing_type)
            bot_progress["total_mentions_sent"] += int(result.get("replies_sent", 0))
            _update_progress(result["completed"], result["failed"])
        finally:
            runtime_stats["status"] = "Idle"
            runtime_stats["current_task"] = None
            runtime_stats["session_uptime"] = format_duration(int(time.time() - start))

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
