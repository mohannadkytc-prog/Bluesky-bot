"""
Bluesky Bot + Flask wrapper (audience processing: likes/reposts)
- يعطيك المعالجة على كل معجبي المنشور أو كل من أعادوا نشره (أو الاثنين)
- يمرّ على المستخدمين واحدًا واحدًا: يفتح آخر منشور لهم ويردّ برسالة من القوالب
- ينتظر بين كل مستخدم والذي بعده ضمن المدى الذي تحدديه
- المسارات: / (واجهة) /queue_task /detailed_progress /stop_task /resume_task
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
from atproto.exceptions import AtProtocolError
from utils import resolve_post_from_url  # موجود عندك من قبل

# إعداد اللوج
logging.basicConfig(level=logging.INFO)
log = logging.getLogger("bluesky-bot")

# ================== إعدادات البوت ==================
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
        self.stop_flag = False  # لإيقاف المهمة بأمان

    # ---------- المصادقة ----------
    def login(self) -> None:
        log.info(f"🔑 تسجيل الدخول: {self.config.bluesky_handle}")
        self.client.login(self.config.bluesky_handle, self.config.bluesky_password)

    # ---------- حلّ رابط المنشور ----------
    def _resolve_post(self, post_url: str) -> Optional[Dict[str, str]]:
        try:
            return resolve_post_from_url(self.client, post_url)
        except Exception as e:
            log.error(f"❌ فشل حلّ رابط المنشور: {e}")
            return None

    # ---------- جلب كل المعجبين (مع ترقيم الصفحات) ----------
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

    # ---------- جلب كل من أعادوا النشر (مع ترقيم الصفحات) ----------
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

    # ---------- آخر منشور/رد للمستخدم ----------
    def fetch_latest_post(self, handle: str) -> Optional[Dict]:
        try:
            res = self.client.app.bsky.feed.get_author_feed(
                {
                    "actor": handle,
                    "filter": "posts_with_replies",
                    "includePins": False,
                    "limit": 25,
                }
            )
            for item in res.feed:
                # نتجنب Reposts: نريد منشورًا أصليًّا أو ردًّا
                if getattr(item, "reason", None):
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

    # ---------- إرسال رد ----------
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

    # ---------- منطقك: معالجة الجمهور واحدًا واحدًا ----------
    def process_audience(
        self,
        post_url: str,
        messages: List[str],
        mode: str = "likes",          # "likes" أو "reposts" أو "both"
        min_delay: int = 180,
        max_delay: int = 300,
    ) -> Dict[str, int]:

        self.login()

        ref = self._resolve_post(post_url)
        if not ref:
            return {"completed": 0, "failed": 1}

        post_uri, post_cid = ref["uri"], ref["cid"]

        # اجمع الجمهور حسب الوضع المختار
        audience: List[Dict] = []
        if mode in ("likes", "both"):
            audience.extend(self.get_likers(post_uri))
        if mode in ("reposts", "both"):
            audience.extend(self.get_reposters(post_uri, post_cid))

        # إزالة المكرر حسب الـ handle
        seen = set()
        uniq_users: List[Dict] = []
        for u in audience:
            if u["handle"] not in seen:
                seen.add(u["handle"])
                uniq_users.append(u)

        total = len(uniq_users)
        log.info(f"👥 الجمهور المستهدف: {total} مستخدم")

        completed = 0
        failed = 0

        for idx, user in enumerate(uniq_users, start=1):
            if self.stop_flag:
                log.warning("⛔ تم إيقاف المهمة بطلب المستخدم.")
                break

            handle = user["handle"]
            latest = self.fetch_latest_post(handle)
            if not latest:
                log.info(f"⚠️ @{handle} لا يملك منشورات مناسبة — تخطٍ")
                failed += 1
                if self.progress_cb:
                    self.progress_cb(completed, failed)
                continue

            msg = random.choice(messages) if messages else "🙏"
            ok = self.reply_to_post(latest, msg)

            if ok:
                completed += 1
                try:
                    bot_progress["total_mentions_sent"] = bot_progress.get("total_mentions_sent", 0) + 1
                except Exception:
                    pass
                log.info(f"✅ [{idx}/{total}] تم الرد على @{handle}")
            else:
                failed += 1
                log.info(f"❌ [{idx}/{total}] فشل الرد على @{handle}")

            # انتظار بين المستخدمين
            delay = random.randint(min_delay, max_delay)
            log.info(f"⏳ انتظار {delay} ثانية قبل المستخدم التالي…")
            time.sleep(delay)

            if self.progress_cb:
                self.progress_cb(completed, failed)

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

    # الحقول (ندعم أسماء مرنة كما اتفقنا)
    post_urls = data.get("post_urls") or [data.get("post_url")]
    post_urls = [u for u in (post_urls or []) if u]
    target_post = post_urls[0] if post_urls else None

    messages = data.get("message_templates") or data.get("messages") or []

    bluesky_handle = data.get("bluesky_handle") or os.getenv("BLUESKY_HANDLE") or os.getenv("BSKY_HANDLE")
    bluesky_password = data.get("bluesky_password") or os.getenv("BLUESKY_PASSWORD") or os.getenv("BSKY_PASSWORD")

    # نوع الجمهور: likes / reposts / both
    processing_type = data.get("processing_type", "likes")

    min_delay = int(data.get("min_delay", 180))
    max_delay = int(data.get("max_delay", 300))

    if not bluesky_handle or not bluesky_password or not target_post:
        return jsonify({"error": "❌ البيانات ناقصة (handle/password/post_url)"}), 400
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

    # عامل تشغيل في الخلفية
    def run_bot():
        start = time.time()
        runtime_stats["status"] = "Running"
        runtime_stats["current_task"] = "Processing audience"
        bot_instance.stop_flag = False

        def cb(done, failed):
            update_progress(done, failed)

        bot_instance.progress_cb = cb

        result = bot_instance.process_audience(
            post_url=target_post,
            messages=messages,
            mode=processing_type,
            min_delay=min_delay,
            max_delay=max_delay,
        )

        runtime_stats["status"] = "Idle"
        runtime_stats["current_task"] = None
        runtime_stats["session_uptime"] = f"{int(time.time() - start)}s"

        update_progress(result["completed"], result["failed"])

    bot_thread = threading.Thread(target=run_bot, daemon=True)
    bot_thread.start()

    return jsonify({"status": "✅ المهمة بدأت"})

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

@app.route("/detailed_progress")
def detailed_progress():
    return jsonify({"runtime_stats": runtime_stats, "bot_progress": bot_progress})

# Aliases
@app.post("/queue")
def queue_alias():
    return queue_task()

@app.get("/progress")
def progress_alias():
    return detailed_progress()

if __name__ == "__main__":
    port = int(os.getenv("PORT", 10000))
    app.run(host="0.0.0.0", port=port)
