"""
Bluesky Bot + Flask wrapper (robust payload parsing)
- يقبل post_url أو post_urls[] أو أسماء بديلة
- يقبل messages أو message_templates
- يحتوي على Aliases للمسارات: /queue_task, /detailed_progress, /stop_task, /resume_task
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
from utils import resolve_post_from_url  # تأكدي هذا موجود كما في مشروعك

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("bluesky-bot")

# ================== البوت (مختصر للتشغيل السليم) ==================
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

    def _progress(self, current, total, last_processed=""):
        if self.progress_cb:
            self.progress_cb(current=current, total=total, last_processed=last_processed)
        pct = (current / total * 100) if total else 0
        log.info(f"Progress {current}/{total} ({pct:.1f}%) last={last_processed}")

    def authenticate(self) -> bool:
        try:
            self.client.login(self.config.bluesky_handle, self.config.bluesky_password)
            return True
        except Exception as e:
            log.error(f"Auth failed: {e}")
            return False

    def process_reposters_with_replies(self, post_url: str, messages: List[str]) -> bool:
        """
        منطق مبسط: يتحقق من الرابط، يجلب المعيدين، ويحدث التقدم مع تأخير عشوائي.
        اربطي لاحقًا منطق الردود الفعلي كما يناسبك.
        """
        try:
            if not self.authenticate():
                return False

            resolved = resolve_post_from_url(self.client, post_url)
            if not resolved:
                log.error("Failed to resolve post via Bluesky API")
                return False

            post_uri, post_cid = resolved["uri"], resolved["cid"]
            resp = self.client.app.bsky.feed.get_reposted_by({"uri": post_uri, "cid": post_cid, "limit": 100})
            reposters = getattr(resp, "reposted_by", []) or []
            total = len(reposters)
            if total == 0:
                self._progress(0, 0, "")
                return True

            for i, r in enumerate(reposters, 1):
                handle = getattr(r, "handle", "")
                self._progress(i, total, handle)
                time.sleep(random.randint(self.config.min_delay, self.config.max_delay))
            return True
        except Exception as e:
            log.error(f"Processing error: {e}")
            return False

# ================== Flask ==================
app = Flask(__name__)

_state: Dict[str, Any] = {
    "running": False,
    "last_error": None,
    "total": 0,
    "current": 0,
    "last_processed": "",
    "uptime_seconds": 0,
}
_worker: Optional[threading.Thread] = None
_started_at: Optional[float] = None

def _env(*names: str, default: Optional[str] = None) -> Optional[str]:
    for n in names:
        v = os.getenv(n)
        if v:
            return v
    return default

BOT = BlueSkyBot(
    Config(
        _env("BSKY_HANDLE", "BLUESKY_HANDLE"),
        _env("BSKY_PASSWORD", "BLUESKY_PASSWORD"),
        int(_env("MIN_DELAY", default="180")),
        int(_env("MAX_DELAY", default="300")),
    )
)

def _progress_cb(**kw):
    _state["current"] = kw.get("current", _state["current"])
    _state["total"] = kw.get("total", _state["total"])
    _state["last_processed"] = kw.get("last_processed", _state["last_processed"])
BOT.progress_cb = _progress_cb

def _extract_payload(req) -> Dict[str, Any]:
    """
    يُعيد dict فيه:
      post_url: str | None
      messages: List[str]
    - يدعم JSON و Form
    - يقبل post_url أو post_urls[] وأسماء بديلة
    """
    payload: Dict[str, Any] = {"post_url": None, "messages": []}

    json_data = req.get_json(silent=True) if req.is_json else None
    form = req.form

    # Logging مساعد
    try:
        log.info(f"Incoming /queue payload. is_json={req.is_json} "
                 f"json_keys={list(json_data.keys()) if isinstance(json_data, dict) else None} "
                 f"form_keys={list(form.keys()) if form else None}")
    except Exception:
        pass

    # 1) احصل على post_url
    candidates_single = ["post_url", "url", "post", "target_url", "postLink"]
    candidates_multi = ["post_urls"]  # Array

    def _first_non_empty(d: Dict[str, Any], keys: List[str]) -> Optional[str]:
        for k in keys:
            v = d.get(k)
            if isinstance(v, str) and v.strip():
                return v.strip()
        return None

    if isinstance(json_data, dict):
        # post_urls (مصفوفة)
        for k in candidates_multi:
            arr = json_data.get(k)
            if isinstance(arr, list) and arr:
                first = str(arr[0]).strip()
                if first:
                    payload["post_url"] = first
                    break
        if not payload["post_url"]:
            payload["post_url"] = _first_non_empty(json_data, candidates_single)

        # رسائل: messages أو message_templates
        msgs = json_data.get("messages") or json_data.get("message_templates") or []
        if isinstance(msgs, list):
            payload["messages"] = [str(m) for m in msgs if str(m).strip()]
    else:
        # Form
        post_urls = form.getlist("post_urls") if form else []
        if post_urls:
            first = str(post_urls[0]).strip()
            if first:
                payload["post_url"] = first
        if not payload["post_url"] and form:
            for k in candidates_single:
                v = form.get(k)
                if v and v.strip():
                    payload["post_url"] = v.strip()
                    break
        # رسائل من الفورم (messages[])
        if form:
            msgs = form.getlist("messages") or form.getlist("message_templates")
            if msgs:
                payload["messages"] = [str(m) for m in msgs if str(m).strip()]

    # بديل: من Environment
    if not payload["post_url"]:
        payload["post_url"] = os.getenv("DEFAULT_POST_URL", "").strip() or None

    return payload

def _worker_func(post_url: str, messages: List[str]):
    global _started_at
    try:
        ok = BOT.process_reposters_with_replies(post_url, messages)
        if not ok and not _state["last_error"]:
            _state["last_error"] = "Processing returned False"
    except Exception as e:
        _state["last_error"] = str(e)
        log.exception("Worker crashed")
    finally:
        _state["running"] = False
        _started_at = None

@app.route("/")
def home():
    try:
        return render_template("persistent.html")
    except Exception:
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

    payload = _extract_payload(request)
    post_url = payload.get("post_url")
    messages = payload.get("messages", [])
    log.info(f"/queue resolved post_url={post_url!r}, messages_count={len(messages)}")

    if not post_url:
        return jsonify({"ok": False, "msg": "post_url is required (or set DEFAULT_POST_URL)"}), 400

    _state.update({"running": True, "last_error": None, "total": 0, "current": 0, "last_processed": ""})
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

# --- Aliases لتوافق الواجهة ---
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
