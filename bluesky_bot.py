"""
Bluesky Bot (20 services ready) + Flask + Checkpoint (persistent)
- لكل خدمة: واجهة مستقلة وشكل مستقل (من ENV)، ورسائل/رابط/تأخيرات افتراضية تخصّها.
- تغيير الحساب (handle/password) بأي وقت يُكمل المهمة طبيعيًا (الـ checkpoint مرتبط بـ post_url).
- يتطلب قرص دائم لحفظ progress.json عبر إعادة التشغيل/النشر.

Endpoints:
  GET  /                    -> واجهة
  GET  /config              -> قيم الواجهة الافتراضية (من ENV)
  POST /queue_task          -> بدء مهمة (حساب واحد)
  GET  /detailed_progress   -> عرض حالة التنفيذ
  GET/POST /stop_task       -> إيقاف لطيف
  GET/POST /resume_task     -> إزالة stop_flag (بدء مهمة جديدة للمتابعة)
  POST /reset_progress      -> مسح تقدّم post_url معيّن
"""

import os
import time
import json
import random
import logging
import threading
from dataclasses import dataclass
from typing import List, Dict, Optional
from datetime import datetime, timezone

from flask import Flask, jsonify, render_template, request

from atproto import Client
from utils import resolve_post_from_url

# ---------- لوج ----------
logging.basicConfig(level=logging.INFO)
log = logging.getLogger("bluesky-bot")

# ---------- قرص دائم + مسار التقدّم ----------
PROGRESS_PATH = os.getenv("PROGRESS_PATH", "/var/data/progress.json")
os.makedirs(os.path.dirname(PROGRESS_PATH), exist_ok=True)
_progress_lock = threading.Lock()

def _load_all_progress() -> Dict[str, dict]:
    if not os.path.exists(PROGRESS_PATH):
        return {}
    try:
        with open(PROGRESS_PATH, "r", encoding="utf-8") as f:
            return json.load(f) or {}
    except Exception:
        return {}

def _save_all_progress(data: Dict[str, dict]) -> None:
    tmp = PROGRESS_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    os.replace(tmp, PROGRESS_PATH)

def load_progress(post_url: str) -> Dict:
    with _progress_lock:
        allp = _load_all_progress()
        return allp.get(post_url, {"processed_handles": [], "last_processed": None, "total": 0})

def save_progress(post_url: str, progress: Dict) -> None:
    with _progress_lock:
        allp = _load_all_progress()
        allp[post_url] = progress
        _save_all_progress(allp)

# ================== منطق البوت ==================
@dataclass
class Config:
    bluesky_handle: str
    bluesky_password: str
    min_delay: int = 180
    max_delay: int = 300

class BlueSkyBot:
    def __init__(self, config: Config):
        self.config = config
        self.client = Client()
        self.progress_cb = None
        self.stop_flag = False

    def login(self) -> None:
        log.info(f"🔑 تسجيل الدخول كـ {self.config.bluesky_handle}")
        self.client.login(self.config.bluesky_handle, self.config.bluesky_password)

    def get_likers(self, post_uri: str) -> List[Dict]:
        likers: List[Dict] = []
        cursor = None
        while True:
            params = {"uri": post_uri, "limit": 100}
            if cursor:
                params["cursor"] = cursor
            res = self.client.app.bsky.feed.get_likes(params)
            if not getattr(res, "likes", None):
                break
            for like in res.likes:
                actor = like.actor
                likers.append({"handle": actor.handle, "display_name": actor.display_name, "did": actor.did})
            cursor = getattr(res, "cursor", None)
            if not cursor:
                break
        log.info(f"👍 عدد المعجبين: {len(likers)}")
        return likers

    def get_reposters(self, post_uri: str, post_cid: str) -> List[Dict]:
        reposters: List[Dict] = []
        cursor = None
        while True:
            params = {"uri": post_uri, "cid": post_cid, "limit": 100}
            if cursor:
                params["cursor"] = cursor
            res = self.client.app.bsky.feed.get_reposted_by(params)
            if not getattr(res, "reposted_by", None):
                break
            for user in res.reposted_by:
                reposters.append({"handle": user.handle, "display_name": user.display_name, "did": user.did})
            cursor = getattr(res, "cursor", None)
            if not cursor:
                break
        log.info(f"🔁 عدد من أعادوا النشر: {len(reposters)}")
        return reposters

    def fetch_latest_post(self, handle: str) -> Optional[Dict]:
        try:
            res = self.client.app.bsky.feed.get_author_feed(
                {"actor": handle, "filter": "posts_with_replies", "includePins": False, "limit": 25}
            )
            for item in res.feed:
                if getattr(item, "reason", None):  # نتجنب Reposts
                    continue
                post = item.post
                return {
                    "uri": post.uri,
                    "cid": post.cid,
                    "author_handle": post.author.handle,
                    "author_did": post.author.did,
                }
            return None
        except Exception as e:
            log.error(f"⚠️ فشل جلب آخر منشور @{handle}: {e}")
            return None

    def reply_to_post(self, target_post: Dict, text: str) -> bool:
        try:
            self.client.com.atproto.repo.create_record(
                {
                    "repo": self.client.me.did,
                    "collection": "app.bsky.feed.post",
                    "record": {
                        "$type": "app.bsky.feed.post",
                        "text": text,
                        "createdAt": datetime.now(timezone.utc).isoformat(),
                        "reply": {
                            "root": {"uri": target_post["uri"], "cid": target_post["cid"]},
                            "parent": {"uri": target_post["uri"], "cid": target_post["cid"]},
                        },
                    },
                }
            )
            return True
        except Exception as e:
            log.error(f"❌ فشل الرد: {e}")
            return False

    def process_audience(
        self,
        post_url: str,
        messages: List[str],
        mode: str = "likes",   # likes | reposts | both
        min_delay: int = 180,
        max_delay: int = 300,
    ) -> Dict[str, int]:

        self.login()

        ref = resolve_post_from_url(self.client, post_url)
        if not ref:
            log.error("❌ فشل حلّ رابط المنشور.")
            return {"completed": 0, "failed": 1}

        post_uri, post_cid = ref["uri"], ref["cid"]

        audience: List[Dict] = []
        if mode in ("likes", "both"):
            audience.extend(self.get_likers(post_uri))
        if mode in ("reposts", "both"):
            audience.extend(self.get_reposters(post_uri, post_cid))

        # إزالة التكرار حسب handle
        seen = set()
        uniq_users: List[Dict] = []
        for u in audience:
            if u["handle"] not in seen:
                seen.add(u["handle"])
                uniq_users.append(u)

        total = len(uniq_users)
        log.info(f"👥 الجمهور المستهدف: {total}")

        # تقدّم سابق
        progress = load_progress(post_url)
        processed_handles = set(progress.get("processed_handles", []))
        already = len(processed_handles)
        remaining = [u for u in uniq_users if u["handle"] not in processed_handles]
        progress["total"] = total
        save_progress(post_url, progress)

        completed = already
        failed = 0

        for user in remaining:
            if self.stop_flag:
                log.warning("⛔ تم إيقاف المهمة بطلب المستخدم.")
                break

            handle = user["handle"]
            latest = self.fetch_latest_post(handle)
            if not latest:
                failed += 1
                processed_handles.add(handle)
                progress["processed_handles"] = list(processed_handles)
                progress["last_processed"] = handle
                save_progress(post_url, progress)
                if self.progress_cb: self.progress_cb(completed, failed)
                continue

            msg = random.choice(messages) if messages else "🙏"
            ok = self.reply_to_post(latest, msg)

            processed_handles.add(handle)
            progress["processed_handles"] = list(processed_handles)
            progress["last_processed"] = handle
            save_progress(post_url, progress)

            if ok:
                completed += 1
                try:
                    bot_progress["total_mentions_sent"] = bot_progress.get("total_mentions_sent", 0) + 1
                except Exception:
                    pass
                log.info(f"✅ [{completed}/{total}] @{handle}")
            else:
                failed += 1
                log.info(f"❌ فشل الرد على @{handle}")

            delay = random.randint(min_delay, max_delay)
            log.info(f"⏳ انتظار {delay} ثانية…")
            time.sleep(delay)

            if self.progress_cb:
                self.progress_cb(completed, failed)

        return {"completed": completed, "failed": failed}

# ================== خادم الويب ==================
app = Flask(__name__)

runtime_stats = {"status": "Idle", "current_task": None, "session_uptime": "0s"}
bot_progress = {
    "completed_runs": 0,
    "failed_runs": 0,
    "total_bot_runs": 0,
    "success_rate": 0.0,
    "total_mentions_sent": 0,
    "total_followers": 0,
    "last_post_url": None,
}

bot_thread: Optional[threading.Thread] = None
bot_instance: Optional[BlueSkyBot] = None

def update_progress(completed: int, failed: int) -> None:
    bot_progress["completed_runs"] = completed
    bot_progress["failed_runs"] = failed
    total = bot_progress.get("total_bot_runs", 0)
    bot_progress["success_rate"] = (completed / total) if total else 0.0

# ===== واجهة و Config للواجهة =====
@app.get("/")
def index():
    return render_template("persistent.html")

@app.get("/config")
def ui_config():
    def _json_env(name, default):
        import json as _json
        try:
            return _json.loads(os.getenv(name, "")) if os.getenv(name) else default
        except Exception:
            return default

    return jsonify({
        "SERVICE_TITLE": os.getenv("SERVICE_TITLE", "🤖 Persistent Bluesky Bot"),
        "UI_PRIMARY_COLOR": os.getenv("UI_PRIMARY_COLOR", "#667eea"),
        "DEFAULT_POST_URL": os.getenv("DEFAULT_POST_URL", ""),
        "DEFAULT_PROCESSING_TYPE": os.getenv("DEFAULT_PROCESSING_TYPE", "likes"),
        "DEFAULT_MIN_DELAY": int(os.getenv("DEFAULT_MIN_DELAY", "180")),
        "DEFAULT_MAX_DELAY": int(os.getenv("DEFAULT_MAX_DELAY", "300")),
        "DEFAULT_MESSAGES": _json_env("DEFAULT_MESSAGES", []),
        # اختياري: تعبئة الحساب افتراضيًا (لا أنصح إلا إن كانت خدمة خاصة)
        "DEFAULT_HANDLE": os.getenv("DEFAULT_HANDLE", ""),
        "DEFAULT_PASSWORD": os.getenv("DEFAULT_PASSWORD", ""),
    })

# ===== تشغيل مهمة واحدة (حساب واحد في هذه الخدمة) =====
@app.post("/queue_task")
def queue_task():
    global bot_thread, bot_instance

    data = request.get_json(force=True)
    log.info(f"📥 Payload: {data}")

    post_urls = data.get("post_urls") or [data.get("post_url")]
    post_urls = [u for u in (post_urls or []) if u]
    target_post = post_urls[0] if post_urls else None

    messages = data.get("message_templates") or data.get("messages") or []

    bluesky_handle = data.get("bluesky_handle") or os.getenv("BLUESKY_HANDLE") or os.getenv("BSKY_HANDLE")
    bluesky_password = data.get("bluesky_password") or os.getenv("BLUESKY_PASSWORD") or os.getenv("BSKY_PASSWORD")

    processing_type = data.get("processing_type", "likes")
    min_delay = int(data.get("min_delay", os.getenv("DEFAULT_MIN_DELAY", 180)))
    max_delay = int(data.get("max_delay", os.getenv("DEFAULT_MAX_DELAY", 300)))

    if not bluesky_handle or not bluesky_password or not target_post:
        return jsonify({"error": "❌ البيانات ناقصة (handle/password/post_url)"}), 400
    if not messages:
        # من ENV لو موجود
        try:
            env_msgs = json.loads(os.getenv("DEFAULT_MESSAGES", "[]"))
            if isinstance(env_msgs, list) and env_msgs:
                messages = env_msgs
        except Exception:
            pass
        if not messages:
            messages = ["🙏 Thank you for supporting."]

    # تهيئة البوت
    config = Config(
        bluesky_handle=bluesky_handle,
        bluesky_password=bluesky_password,
        min_delay=min_delay,
        max_delay=max_delay,
    )
    bot_instance = BlueSkyBot(config)

    # تجهيز مؤشرات الواجهة من ملف التقدّم
    pr = load_progress(target_post)
    bot_progress["completed_runs"] = len(pr.get("processed_handles", []))
    bot_progress["failed_runs"] = 0
    bot_progress["total_bot_runs"] = pr.get("total", 0)
    bot_progress["success_rate"] = (
        bot_progress["completed_runs"]/bot_progress["total_bot_runs"]
        if bot_progress["total_bot_runs"] else 0.0
    )
    bot_progress["last_post_url"] = target_post

    def run_bot():
        start = time.time()
        runtime_stats["status"] = "Running"
        runtime_stats["current_task"] = "Processing audience"
        bot_instance.stop_flag = False

        def cb(done, failed):
            bot_progress["completed_runs"] = done
            bot_progress["failed_runs"] = failed
            total = bot_progress.get("total_bot_runs", 0)
            bot_progress["success_rate"] = (done/total) if total else 0.0

        bot_instance.progress_cb = cb

        result = bot_instance.process_audience(
            post_url=target_post,
            messages=messages,
            mode=processing_type,
            min_delay=min_delay,
            max_delay=max_delay,
        )

        final_pr = load_progress(target_post)
        bot_progress["total_bot_runs"] = final_pr.get("total", bot_progress["total_bot_runs"])
        update_progress(result["completed"], result["failed"])

        runtime_stats["status"] = "Idle"
        runtime_stats["current_task"] = None
        runtime_stats["session_uptime"] = f"{int(time.time() - start)}s"

    th = threading.Thread(target=run_bot, daemon=True)
    th.start()

    return jsonify({"status": "✅ المهمة بدأت (مع checkpoint)"}), 200

@app.route("/detailed_progress")
def detailed_progress():
    last_url = bot_progress.get("last_post_url")
    if last_url:
        pr = load_progress(last_url)
        if pr:
            bot_progress["total_bot_runs"] = pr.get("total", bot_progress["total_bot_runs"])
            bot_progress["completed_runs"] = len(pr.get("processed_handles", []))
            bot_progress["success_rate"] = (
                (bot_progress["completed_runs"] / bot_progress["total_bot_runs"])
                if bot_progress["total_bot_runs"] else 0.0
            )
    return jsonify({"runtime_stats": runtime_stats, "bot_progress": bot_progress})

@app.route("/stop_task", methods=["GET", "POST"])
def stop_task():
    if bot_instance:
        bot_instance.stop_flag = True
    runtime_stats["status"] = "Stopped"
    return jsonify({"status": "🛑 تم الإيقاف"})

@app.route("/resume_task", methods=["GET", "POST"])
def resume_task():
    if bot_instance:
        bot_instance.stop_flag = False
    runtime_stats["status"] = "Running"
    return jsonify({"status": "▶️ تم الاستئناف"})

@app.post("/reset_progress")
def reset_progress():
    data = request.get_json(force=True)
    post_url = data.get("post_url")
    if not post_url:
        return jsonify({"error": "post_url مطلوب"}), 400

    with _progress_lock:
        allp = _load_all_progress()
        if post_url in allp:
            del allp[post_url]
            _save_all_progress(allp)

    if bot_progress.get("last_post_url") == post_url:
        bot_progress["completed_runs"] = 0
        bot_progress["total_bot_runs"] = 0
        bot_progress["success_rate"] = 0.0

    return jsonify({"status": "♻️ تم مسح التقدّم لهذا الرابط"})

# Aliases بسيطة
@app.post("/queue")
def queue_alias(): return queue_task()
@app.get("/progress")
def progress_alias(): return detailed_progress()

if __name__ == "__main__":
    port = int(os.getenv("PORT", 10000))
    app.run(host="0.0.0.0", port=port)
