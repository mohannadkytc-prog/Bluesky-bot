"""
Bluesky Bot Implementation + Flask wrapper for Render
"""

import os
import time
import random
import json
import logging
import threading
from dataclasses import dataclass
from typing import List, Dict, Optional, Any

from flask import Flask, jsonify, render_template, request

from atproto import Client
from atproto.exceptions import AtProtocolError
from utils import extract_post_info, resolve_post_from_url, save_progress, load_progress

logging.basicConfig(level=logging.INFO)


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
        self.logger = logging.getLogger(__name__)
        self.progress_file = "progress.json"
        self.message_counter = 0
        self.progress_callback = None

    def update_progress(self, current, total, last_processed="", total_reposters=0, already_processed=0):
        progress_info = f"Progress: {current}/{total} ({(current/total*100 if total else 0):.1f}%)"
        self.logger.info(progress_info)
        if self.progress_callback:
            self.progress_callback(
                current=current,
                total=total,
                last_processed=last_processed,
                total_reposters=total_reposters,
                already_processed=already_processed,
            )

    def authenticate(self) -> bool:
        try:
            self.client.login(self.config.bluesky_handle, self.config.bluesky_password)
            return True
        except Exception as e:
            self.logger.error(f"Auth failed: {e}")
            return False

    def process_reposters_with_replies(self, post_url: str, messages: List[str]) -> bool:
        try:
            if not self.authenticate():
                return False
            resolved_info = resolve_post_from_url(self.client, post_url)
            if not resolved_info:
                return False
            post_uri, post_cid = resolved_info["uri"], resolved_info["cid"]
            reposters = self.client.app.bsky.feed.get_reposted_by({"uri": post_uri, "cid": post_cid}).reposted_by
            if not reposters:
                return True
            total = len(reposters)
            for i, r in enumerate(reposters, 1):
                self.update_progress(i, total, r.handle, total, i - 1)
                time.sleep(random.randint(self.config.min_delay, self.config.max_delay))
            return True
        except Exception as e:
            self.logger.error(f"Error: {e}")
            return False


# ---------------------- Flask Wrapper ----------------------

app = Flask(__name__)

_state: Dict[str, Any] = {
    "running": False,
    "last_error": None,
    "total": 0,
    "current": 0,
    "last_processed": "",
    "total_reposters": 0,
    "already_processed": 0,
    "uptime_seconds": 0,
}
_worker: Optional[threading.Thread] = None
_started_at = None

def _get_env(*names: str, default=None):
    for n in names:
        v = os.getenv(n)
        if v:
            return v
    return default

BOT = BlueSkyBot(
    Config(
        _get_env("BSKY_HANDLE", "BLUESKY_HANDLE"),
        _get_env("BSKY_PASSWORD", "BLUESKY_PASSWORD"),
        int(_get_env("MIN_DELAY", default="200")),
        int(_get_env("MAX_DELAY", default="250")),
    )
)

def _progress_callback(**kw):
    _state.update(kw)

BOT.progress_callback = _progress_callback

def _worker_func(post_url: str, messages: List[str]):
    global _started_at
    try:
        BOT.process_reposters_with_replies(post_url, messages)
    except Exception as e:
        _state["last_error"] = str(e)
    finally:
        _state["running"] = False
        _started_at = None

@app.route("/")
def home():
    try:
        return render_template("persistent.html")
    except:
        return "Persistent Bluesky Bot is running."

@app.get("/progress")
def progress():
    if _started_at:
        _state["uptime_seconds"] = int(time.time() - _started_at)
    return jsonify(_state)

@app.post("/queue")
def queue():
    global _worker, _started_at
    if _state["running"]:
        return jsonify({"ok": True, "msg": "Already running"})
    post_url = request.form.get("post_url") or (request.json.get("post_url") if request.is_json else None)
    if not post_url:
        return jsonify({"ok": False, "msg": "post_url is required"}), 400
    messages = request.json.get("messages", []) if request.is_json else []
    _state.update({"running": True, "last_error": None, "current": 0})
    _started_at = time.time()
    _worker = threading.Thread(target=_worker_func, args=(post_url, messages), daemon=True)
    _worker.start()
    return jsonify({"ok": True, "msg": "تم إرسال المهمة بنجاح."})

@app.post("/stop")
def stop():
    _state["running"] = False
    return jsonify({"ok": True, "msg": "تم إيقاف المهمة."})

@app.post("/resume")
def resume():
    return jsonify({"ok": False, "msg": "استخدم /queue مع post_url"}), 400

@app.get("/healthz")
def health():
    return "ok", 200

# ---------------- Aliases for frontend compatibility ----------------
@app.post("/queue_task")
def queue_task_alias():
    return queue()

@app.get("/detailed_progress")
def detailed_progress_alias():
    return progress()

@app.post("/stop_task")
def stop_task_alias():
    return stop()

@app.post("/resume_task")
def resume_task_alias():
    return resume()

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", "5000")))
