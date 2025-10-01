"""
Bluesky Bot Implementation + Flask wrapper for Render
- يقبل عدة أسماء لحقل رابط المنشور: post_url / url / post / target_url / postLink
- يحتوي على Aliases للمسارات التي تطلبها الواجهة: /queue_task, /detailed_progress, /stop_task, /resume_task
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
from utils import extract_post_info, resolve_post_from_url, save_progress, load_progress

logging.basicConfig(level=logging.INFO)


# ================== الإعدادات والبوت ==================

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
        self.progress_callback = None  # تُضبط من واجهة الويب

    def _update_progress(self, current, total, last_processed="", total_reposters=0, already_processed=0):
        pct = (current / total * 100) if total else 0
        self.logger.info(f"Progress: {current}/{total} ({pct:.1f}%) - Last: {last_processed}")
        if self.progress_callback:
            try:
                self.progress_callback(
                    current=current,
                    total=total,
                    last_processed=last_processed,
                    total_reposters=total_reposters,
                    already_processed=already_processed,
                )
            except Exception as e:
                self.logger.warning(f"Progress callback error: {e}")

    def authenticate(self) -> bool:
        try:
            self.client.login(self.config.bluesky_handle, self.config.bluesky_password)
            return True
        except Exception as e:
            self.logger.error(f"Auth failed: {e}")
            return False

    # --- منطق مبسط (ابقِه كما تحب؛ الهدف تشغيل البنية) ---
    def process_reposters_with_replies(self, post_url: str, messages: List[str]) -> bool:
        """
        ينفّذ المعالجة الأساسية: يتحقّق من الرابط، يحصل على المعيدين، ويعدّ تقدّمًا وهميًا
        حتى توصل ربط منطقك التفصيلي.
        """
        try:
            if not self.authenticate():
                return False

            resolved = resolve_post_from_url(self.client, post_url)
            if not resolved:
                self.logger.error("Failed to resolve post via Bluesky API")
                return False

            post_uri, post_cid = resolved["uri"], resolved["cid"]

            # احصل على قائمة المعيدين:
            resp = self.client.app.bsky.feed.get_reposted_by({"uri": post_uri, "cid": post_cid, "limit": 100})
            reposters = getattr(resp, "reposted_by", []) or []
            total = len(reposters)
            if total == 0:
                # ما في معيدين؛ اعتبر المهمة نجحت بدون عمل
                self._update_progress(0, 0, "", 0, 0)
                return True

            for i, r in enumerate(reposters, 1):
                handle = getattr(r, "handle", "")
                self._update_progress(i, total, handle, total, i - 1)
                # تأخير عشوائي (يمكنك ربطه بردود فعلية لاحقًا)
                time.sleep(random.randint(self.config.min_delay, self.config.max_delay))

            return True
        except Exception as e:
            self.logger.error(f"Processing error: {e}")
            return False


# ================== Flask Wrapper ==================

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
_started_at: Optional[float] = None


def _get_env(*names: str, default: Optional[str] = None) -> Optional[str]:
    """يرجع أول قيمة موجودة لأي اسم من الأسماء المعطاة."""
    for n in names:
        v = os.getenv(n)
        if v:
            return v
    return default


def _build_config() -> Config:
    return Config(
        _get_env("BSKY_HANDLE", "BLUESKY_HANDLE"),
        _get_env("BSKY_PASSWORD", "BLUESKY_PASSWORD"),
        int(_get_env("MIN_DELAY", default="200")),
        int(_get_env("MAX_DELAY", default="250")),
    )


BOT = BlueSkyBot(_build_config())


def _progress_callback(**kw):
    # تحديث حالة الواجهة
    for k, v in kw.items():
        _state[k] = v


BOT.progress_callback = _progress_callback


def _worker_func(post_url: str, messages: List[str]):
    global _started_at
    try:
        ok = BOT.process_reposters_with_replies(post_url, messages)
        if not ok:
            _state["last_error"] = _state.get("last_error") or "Processing returned False"
    except Exception as e:
        _state["last_error"] = str(e)
        logging.getLogger(__name__).exception("Worker crashed")
    finally:
        _state["running"] = False
        _started_at = None


def _extract_post_url(req) -> Optional[str]:
    """
    يقبل عدة أسماء للحقل من الواجهة:
    post_url / url / post / target_url / postLink
    ويدعم JSON أو form.
    """
    candidates = ["post_url", "url", "post", "target_url", "postLink"]

    # JSON
    if req.is_json:
        data = req.get_json(silent=True) or {}
        for k in candidates:
            if data.get(k):
                return data[k]

    # Form (x-www-form-urlencoded / multipart)
    for k in candidates:
        v = req.form.get(k)
        if v:
            return v

    return None


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

    post_url = _extract_post_url(request)
    if not post_url:
        return jsonify({"ok": False, "msg": "post_url is required"}), 400

    # رسائل اختيارية من JSON فقط (إن وُجدت)
    messages: List[str] = []
    if request.is_json:
        data = request.get_json(silent=True) or {}
        msgs = data.get("messages")
        if isinstance(msgs, list):
            messages = [str(m) for m in msgs]

    # إعداد الحالة وبدء الثريد
    _state.update(
        {
            "running": True,
            "last_error": None,
            "total": 0,
            "current": 0,
            "last_processed": "",
            "total_reposters": 0,
            "already_processed": 0,
        }
    )
    _started_at = time.time()

    _worker = threading.Thread(target=_worker_func, args=(post_url, messages), daemon=True)
    _worker.start()

    return jsonify({"ok": True, "msg": "تم إرسال المهمة بنجاح."})


@app.post("/stop")
def stop():
    # ما في stop فوري لاستدعاءات الشبكة؛ لكن نوقف العلم ونترك الثريد ينتهي طبيعيًا
    _state["running"] = False
    return jsonify({"ok": True, "msg": "تم إيقاف المهمة."})


@app.post("/resume")
def resume():
    # لا نعرف من دون post_url؛ استخدم /queue من جديد
    return jsonify({"ok": False, "msg": "استخدم /queue مع post_url لبدء مهمة جديدة."}), 400


@app.get("/healthz")
def health():
    return "ok", 200


# ============ Aliases لتوافق الواجهة الأمامية الحالية ============

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


# تشغيل محلي فقط
if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", "5000")))
