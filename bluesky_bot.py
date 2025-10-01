"""
Flask wrapper + Bluesky worker
- يقرأ المدخلات من الواجهة (JSON)
- يخزّن التقدم في PROGRESS_PATH داخل /tmp/data
- لا يحتاج أي كتابة خارج /tmp
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

from config import Config, PROGRESS_PATH
from utils import (
    resolve_post_from_url,
    save_progress,
    load_progress,
    format_duration,
    validate_message_template,
)

# ====== logging ======
logging.basicConfig(level=logging.INFO)
log = logging.getLogger("bluesky-bot")

# ====== bot core ======
@dataclass
class BotStats:
    completed: int = 0
    failed: int = 0
    total_users: int = 0
    mentions_sent: int = 0

class BlueSkyBot:
    def __init__(self, config: Config):
        self.config = config
        self.client = Client()

    def login(self) -> None:
        log.info(f"🔑 Login as {self.config.bluesky_handle}")
        self.client.login(self.config.bluesky_handle, self.config.bluesky_password)

    def _repost(self, uri: str, cid: str):
        self.client.com.atproto.repo.create_record({
            "repo": self.client.me.did,
            "collection": "app.bsky.feed.repost",
            "record": {
                "$type": "app.bsky.feed.repost",
                "subject": {"uri": uri, "cid": cid},
                "createdAt": datetime.now(timezone.utc).isoformat(),
            },
        })

    def _reply(self, uri: str, cid: str, text: str):
        self.client.com.atproto.repo.create_record({
            "repo": self.client.me.did,
            "collection": "app.bsky.feed.post",
            "record": {
                "$type": "app.bsky.feed.post",
                "text": text,
                "createdAt": datetime.now(timezone.utc).isoformat(),
                "reply": {
                    "root": {"uri": uri, "cid": cid},
                    "parent": {"uri": uri, "cid": cid},
                },
            },
        })

    def process_single_post(
        self,
        target_post_url: str,
        messages: List[str],
        processing_type: str,
        stats: BotStats,
        progress_key: str,
    ):
        post_ref = resolve_post_from_url(self.client, target_post_url)
        if not post_ref:
            raise RuntimeError(f"Cannot resolve post url: {target_post_url}")

        uri, cid = post_ref["uri"], post_ref["cid"]

        # audience: likers/reposters حسب الطلب
        # مبدئياً نبدأ بالـ likers (ونقدر نضيف تبديل لاحقاً)
        likers = self.client.app.bsky.feed.get_likes({"uri": uri}).likes or []
        user_dids = [lk.actor.did for lk in likers]
        stats.total_users = len(user_dids)

        # استئناف: حمّل التقدم السابق
        progress = load_progress(PROGRESS_PATH, progress_key)
        processed_set = set(progress.get("processed_dids", []))

        for did in user_dids:
            if did in processed_set:
                continue

            try:
                # Repost؟
                if processing_type in ("reposts", "both", "reposts_and_replies"):
                    try:
                        self._repost(uri, cid)
                        log.info("🔁 Repost OK")
                    except Exception as e:
                        log.warning(f"Repost failed: {e}")

                # Reply؟
                if processing_type in ("replies", "both", "reposts_and_replies"):
                    msg = random.choice(messages) if messages else "🙏"
                    self._reply(uri, cid, msg)
                    stats.mentions_sent += 1
                    log.info(f"💬 Reply OK: {msg[:60]}")

                stats.completed += 1
                processed_set.add(did)

                # حفظ التقدم بعد كل مستخدم
                save_progress(
                    PROGRESS_PATH,
                    progress_key,
                    {
                        "processed_dids": list(processed_set),
                        "total_users": stats.total_users,
                        "last_at": datetime.now(timezone.utc).isoformat(),
                    },
                )

                # delay بين المستخدمين
                delay = random.randint(self.config.min_delay, self.config.max_delay)
                log.info(f"⏳ sleeping {delay}s …")
                time.sleep(delay)

            except Exception as e:
                stats.failed += 1
                log.error(f"Process user failed: {e}")

# ====== Flask app ======
app = Flask(__name__)

runtime = {
    "status": "Idle",
    "current_task": None,
    "session_started_at": None,
    "session_uptime": "0s",
}
progress_view = {
    "completed_runs": 0,
    "failed_runs": 0,
    "total_users": 0,
    "success_rate": 0.0,
    "total_mentions_sent": 0,
}

_worker_thread: Optional[threading.Thread] = None
_worker_stop = threading.Event()

def _update_progress_from_stats(stats: BotStats):
    total = stats.completed + stats.failed
    progress_view["completed_runs"] = stats.completed
    progress_view["failed_runs"] = stats.failed
    progress_view["total_users"] = stats.total_users
    progress_view["total_mentions_sent"] = stats.mentions_sent
    progress_view["success_rate"] = (stats.completed / total) if total else 0.0

@app.route("/")
def index():
    return render_template("persistent.html")

@app.route("/queue_task", methods=["POST"])
def queue_task():
    global _worker_thread
    if _worker_thread and _worker_thread.is_alive():
        return jsonify({"error": "Task already running"}), 409

    payload = request.get_json(force=True) or {}
    log.info(f"📥 Payload: {payload}")

    # قراءات مرنة للأسماء
    post_urls = payload.get("post_urls") or [payload.get("post_url")]
    post_urls = [u for u in (post_urls or []) if u]

    messages = payload.get("message_templates") or payload.get("messages") or []
    messages = [m for m in messages if isinstance(m, str) and validate_message_template(m)]

    processing_type = payload.get("processing_type", "replies")
    min_delay = int(payload.get("min_delay", 200))
    max_delay = int(payload.get("max_delay", 250))

    cfg = Config(
        bluesky_handle=payload.get("bluesky_handle"),
        bluesky_password=payload.get("bluesky_password"),
        min_delay=min_delay,
        max_delay=max_delay,
    )
    if not cfg.is_valid():
        return jsonify({"error": "handle/password missing"}), 400
    if not post_urls:
        return jsonify({"error": "post_url(s) required"}), 400
    if not messages:
        messages = ["🙏 Thank you for supporting."]

    # worker
    def _run():
        runtime["status"] = "Running"
        runtime["current_task"] = "Processing audience"
        runtime["session_started_at"] = time.time()

        bot = BlueSkyBot(cfg)
        stats = BotStats()

        try:
            bot.login()
            for post_url in post_urls:
                key = f"{cfg.bluesky_handle}|{post_url}"
                bot.process_single_post(
                    target_post_url=post_url,
                    messages=messages,
                    processing_type=processing_type,
                    stats=stats,
                    progress_key=key,
                )
                _update_progress_from_stats(stats)
        except Exception as e:
            log.error(f"Worker error: {e}")
        finally:
            _update_progress_from_stats(stats)
            runtime["status"] = "Idle"
            runtime["current_task"] = None
            runtime["session_uptime"] = format_duration(int(time.time() - runtime["session_started_at"]))
            runtime["session_started_at"] = None

    _worker_stop.clear()
    _worker_thread = threading.Thread(target=_run, daemon=True)
    _worker_thread.start()

    return jsonify({"status": "✅ started"})

@app.route("/stop_task", methods=["GET", "POST"])
def stop_task():
    global _worker_thread
    if _worker_thread and _worker_thread.is_alive():
        # soft flag فقط (نقدر نضيف فحصه داخل الحلقة إذا بدنا إيقاف فوري)
        _worker_stop.set()
    runtime["status"] = "Stopped"
    return jsonify({"status": "🛑 stopped"})

@app.route("/resume_task", methods=["GET", "POST"])
def resume_task():
    # الواجهة فقط تغيّر الحالة المعروضة
    runtime["status"] = "Running"
    return jsonify({"status": "▶️ resumed"})

@app.route("/detailed_progress")
def detailed_progress():
    # حدّث مدة الجلسة إن كانت شغّالة
    if runtime.get("session_started_at"):
        runtime["session_uptime"] = format_duration(int(time.time() - runtime["session_started_at"]))
    return jsonify({"runtime_stats": runtime, "bot_progress": progress_view})

# اختصارات
@app.post("/queue")
def queue_alias():
    return queue_task()

@app.get("/progress")
def progress_alias():
    return detailed_progress()

if __name__ == "__main__":
    port = int(os.getenv("PORT", "10000"))
    app.run(host="0.0.0.0", port=port)
