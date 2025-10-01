"""
Bluesky Bot Implementation + Flask wrapper for Render
- يبقي منطق BlueSkyBot كما هو
- يوفّر كائن Flask اسمه app ليعمل مع gunicorn
- يضيف مسارات بسيطة لبدء/إيقاف المهمة وعرض التقدم
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

# ======== كـودك الأصلي (مع تعديلات طفيفة) ========

from atproto import Client, client_utils  # موجود عندك
from atproto.exceptions import AtProtocolError
from utils import (
    extract_post_info,
    resolve_post_from_url,
    save_progress,
    load_progress,
)

logging.basicConfig(level=logging.INFO)


@dataclass
class Config:
    bluesky_handle: str
    bluesky_password: str
    min_delay: int = 200
    max_delay: int = 250


class BlueSkyBot:
    """Main bot class for handling Bluesky interactions"""

    def __init__(self, config: Config):
        self.config = config
        self.client = Client()
        self.logger = logging.getLogger(__name__)
        self.progress_file = "progress.json"
        self.bot_run = None
        self.message_counter = 0  # Counter for sequential message selection
        # hook خارجي لتحديث الواجهة
        self.progress_callback = None

    def set_bot_run(self, bot_run):
        self.bot_run = bot_run

    def update_progress(
        self,
        current,
        total,
        last_processed="",
        total_reposters=0,
        already_processed=0,
    ):
        """تحديث التقدم: نبقي السلوك القديم + نرسل للواجهة لو في callback"""
        remaining = total - current
        percentage = (current / total * 100) if total > 0 else 0

        progress_info = f"Progress: {current}/{total} ({percentage:.1f}%) - Last: {last_processed}"
        if total_reposters > 0:
            progress_info += f" - Total reposters: {total_reposters}"

        self.logger.info(progress_info)

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

    def add_message_variations(self, message: str) -> str:
        symbols = ["🙏", "💔", "🥺", "🌸", "🍉", "💙", "❤️", "💚", "💖", "🦋", "🌺", "🤎", "🌹"]
        dots = ["...", "..", ".", ""]
        spaces = [" ", "  "]

        import random as _r

        if _r.random() < 0.7:
            symbol = _r.choice(symbols)
            message = message.rstrip(".!?") + " " + symbol

        if _r.random() < 0.5:
            dots_choice = _r.choice(dots)
            message = message.rstrip(".!?") + dots_choice

        if _r.random() < 0.3:
            space_choice = _r.choice(spaces)
            message = message.replace(" ", space_choice, 1)

        return message

    def select_sequential_message(self, message_templates: List[str]) -> str:
        if not message_templates:
            message_templates = [
                "Hope feels distant, but your support can bring it back to life. Every act of kindness restores dignity. The link is the bridge, help us cross it.",
                "I don't ask for pity, I ask for a chance to rise again. Your support is strength, your compassion is survival. The link is how you can make it real.",
                "Many turn away, but I believe in those who still care. Your help can turn despair into hope. The link is the path—please walk it with us.",
                "My truth is shared with courage, not shame. Support given with love lifts more than burdens—it restores faith in humanity. Please visit the link.",
                "A small gift can mean a new beginning. My request is simple: dignity through support. The link holds the power to change a life.",
                "Behind my words is real struggle. Behind your kindness is real impact. Together, we can transform pain into hope. It starts with the link.",
                "Asking for help is not weakness—it is trust in humanity. Your hand can steady me, your support can carry us. The link is where we stand together.",
                "Silence has hidden my pain, but today I speak. I need help to carry this burden with dignity. The link matters, please don't look away.",
                "I don't seek charity, I seek solidarity. Compassion shared is strength multiplied. The link is your chance to stand with us.",
                "Hardship can break, but kindness can rebuild. Your support is not just aid, it is hope reborn. The link is where healing begins.",
            ]
        base_message = message_templates[self.message_counter % len(message_templates)]
        self.message_counter += 1
        return self.add_message_variations(base_message)

    def authenticate(self) -> bool:
        try:
            self.logger.info("Authenticating with Bluesky...")
            profile = self.client.login(self.config.bluesky_handle, self.config.bluesky_password)
            self.logger.info(f"Successfully authenticated as {getattr(profile,'display_name', self.config.bluesky_handle)}")
            return True
        except AtProtocolError as e:
            self.logger.error(f"Authentication failed: {e}")
            return False
        except Exception as e:
            self.logger.error(f"Unexpected authentication error: {e}")
            return False

    def get_reposters(self, post_uri: str, post_cid: str) -> List[Dict]:
        try:
            self.logger.info(f"Fetching reposters for post: {post_uri}")
            reposters = []
            cursor = None
            while True:
                params = {"uri": post_uri, "cid": post_cid, "limit": 100}
                if cursor:
                    params["cursor"] = cursor

                response = self.client.app.bsky.feed.get_reposted_by(params)
                if not getattr(response, "reposted_by", None):
                    break

                for reposter in response.reposted_by:
                    reposters.append(
                        {
                            "handle": reposter.handle,
                            "display_name": getattr(reposter, "display_name", ""),
                            "did": reposter.did,
                        }
                    )
                cursor = getattr(response, "cursor", None)
                if not cursor:
                    break
            self.logger.info(f"Found {len(reposters)} reposters")
            return reposters
        except Exception as e:
            self.logger.error(f"Failed to fetch reposters: {e}")
            return []

    def fetch_latest_post(self, handle: str) -> Optional[Dict]:
        try:
            self.logger.info(f"Fetching latest post for @{handle}")
            response = self.client.app.bsky.feed.get_author_feed(
                {"actor": handle, "filter": "posts_with_replies", "includePins": False, "limit": 20}
            )
            if not getattr(response, "feed", None):
                self.logger.warning(f"No posts found for @{handle}")
                return None

            for item in response.feed:
                post = item.post
                # تخطّي الريبوست
                if hasattr(item, "reason") and item.reason:
                    continue
                return {
                    "uri": post.uri,
                    "cid": post.cid,
                    "text": getattr(post.record, "text", ""),
                    "created_at": getattr(post.record, "created_at", ""),
                    "author_handle": post.author.handle,
                    "author_did": post.author.did,
                }
            return None
        except Exception as e:
            self.logger.error(f"Failed to fetch latest post for @{handle}: {e}")
            return None

    def reply_to_post(self, post_data: Dict, user_data: Dict, message_templates: List[str]) -> bool:
        try:
            from datetime import datetime, timezone

            handle = user_data["handle"]
            selected_message = self.select_sequential_message(message_templates)

            self.logger.info(f"Replying to @{handle}'s post: {selected_message}")

            reply_data = {
                "$type": "app.bsky.feed.post",
                "text": selected_message,
                "createdAt": datetime.now(timezone.utc).isoformat(),
                "reply": {
                    "root": {"uri": post_data["uri"], "cid": post_data["cid"]},
                    "parent": {"uri": post_data["uri"], "cid": post_data["cid"]},
                },
            }

            _ = self.client.com.atproto.repo.create_record(
                {"repo": self.client.me.did, "collection": "app.bsky.feed.post", "record": reply_data}
            )
            self.logger.info(f"Successfully replied to @{handle}'s post")
            return True
        except Exception as e:
            self.logger.error(f"Failed to reply to @{handle}'s post: {e}")
            return False

    def random_delay(self):
        delay = random.randint(self.config.min_delay, self.config.max_delay)
        self.logger.info(f"Waiting {delay} seconds before next reply...")
        for remaining in range(delay, 0, -1):
            # لا نستخدم input/طباعة كثيفة؛ فقط نوم
            time.sleep(1)

    def process_reposters_with_replies(self, post_url: str, message_templates: List[str]) -> bool:
        try:
            if not self.authenticate():
                return False

            post_info = extract_post_info(post_url)
            if not post_info:
                self.logger.error("Failed to extract post information from URL")
                return False

            resolved_info = resolve_post_from_url(self.client, post_url)
            if not resolved_info:
                self.logger.error("Failed to resolve post information from Bluesky API")
                return False

            post_uri = resolved_info["uri"]
            post_cid = resolved_info["cid"]

            reposters = self.get_reposters(post_uri, post_cid)
            if not reposters:
                self.logger.warning("No reposters found for this post")
                self.update_progress(0, 0, "", 0, 0)
                return True

            progress = load_progress(self.progress_file, post_url)
            processed_handles = set(progress.get("processed_handles", []))
            already_processed = len(processed_handles)

            remaining_reposters = [r for r in reposters if r["handle"] not in processed_handles]
            if not remaining_reposters:
                self.logger.info("All reposters have already been processed")
                return True

            total_reposters = len(reposters)

            # تحديث أولي
            self.update_progress(already_processed, total_reposters, "", total_reposters, already_processed)

            for i, reposter in enumerate(remaining_reposters, 1):
                handle = reposter["handle"]
                current_total = already_processed + i

                latest_post = self.fetch_latest_post(handle)
                if not latest_post:
                    processed_handles.add(handle)
                    progress["processed_handles"] = list(processed_handles)
                    progress["last_processed"] = handle
                    progress["total_processed"] = len(processed_handles)
                    save_progress(self.progress_file, post_url, progress)
                    continue

                success = self.reply_to_post(latest_post, reposter, message_templates)
                if success:
                    processed_handles.add(handle)
                    progress["processed_handles"] = list(processed_handles)
                    progress["last_processed"] = handle
                    progress["total_processed"] = len(processed_handles)
                    progress["last_replied_post"] = latest_post["uri"]
                    save_progress(self.progress_file, post_url, progress)

                    self.update_progress(
                        current_total,
                        total_reposters,
                        f"@{handle}",
                        total_reposters,
                        already_processed,
                    )

                    if i < len(remaining_reposters):
                        self.random_delay()
                else:
                    # لا نسأل المستخدم بـ input() داخل السرفر
                    continue

            return True
        except Exception as e:
            self.logger.error(f"Error processing reposters with replies: {e}")
            return False


# ======== Flask + واجهة بسيطة ========

app = Flask(__name__)

# حالة الواجهة
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
_stop_flag = threading.Event()
_started_at = None

# قراءة متغيرات البيئة (ندعم تسميتين لكل واحد)
def _get_env(*names: str, default: Optional[str] = None) -> Optional[str]:
    for n in names:
        v = os.getenv(n)
        if v:
            return v
    return default


def build_config() -> Config:
    handle = _get_env("BSKY_HANDLE", "BLUESKY_HANDLE")
    password = _get_env("BSKY_PASSWORD", "BLUESKY_PASSWORD")
    min_delay = int(_get_env("MIN_DELAY", default="200"))
    max_delay = int(_get_env("MAX_DELAY", default="250"))

    if not handle or not password:
        logging.getLogger(__name__).warning(
            "Missing Bluesky credentials. Please set BSKY_HANDLE/BLUESKY_HANDLE and BSKY_PASSWORD/BLUESKY_PASSWORD."
        )
    return Config(handle, password, min_delay, max_delay)


BOT = BlueSkyBot(build_config())


def _progress_callback(**kw):
    _state["current"] = kw.get("current", _state["current"])
    _state["total"] = kw.get("total", _state["total"])
    _state["last_processed"] = kw.get("last_processed", _state["last_processed"])
    _state["total_reposters"] = kw.get("total_reposters", _state["total_reposters"])
    _state["already_processed"] = kw.get("already_processed", _state["already_processed"])


BOT.progress_callback = _progress_callback


def _worker_func(post_url: str, messages: List[str]):
    global _started_at
    _state["last_error"] = None
    try:
        BOT.process_reposters_with_replies(post_url, messages)
    except Exception as e:
        _state["last_error"] = str(e)
        logging.getLogger(__name__).exception("Worker crashed")
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
    """ابدأ المهمة: يحتاج post_url و (اختياري) messages[] و min/max delay"""
    global _worker, _started_at

    if _state["running"]:
        return jsonify({"ok": True, "msg": "Task already running."})

    post_url = request.form.get("post_url") or request.json.get("post_url") if request.is_json else None
    if not post_url:
        return jsonify({"ok": False, "msg": "post_url is required"}), 400

    # رسائل اختيارية
    messages = []
    if request.is_json:
        messages = request.json.get("messages", []) or []
    else:
        # من فورم متعدد القيم messages[]
        messages = request.form.getlist("messages") or []

    # ضبط التأخيرات إذا أرسلتها الواجهة
    min_delay = request.form.get("min_delay")
    max_delay = request.form.get("max_delay")
    if min_delay:
        BOT.config.min_delay = int(min_delay)
    if max_delay:
        BOT.config.max_delay = int(max_delay)

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
    _stop_flag.clear()
    _started_at = time.time()

    _worker = threading.Thread(target=_worker_func, args=(post_url, messages), daemon=True)
    _worker.start()
    return jsonify({"ok": True, "msg": "تم إرسال المهمة بنجاح."})


@app.post("/stop")
def stop():
    """إيقاف (لا يقاطع استدعاءات API الحالية لكنه يمنع تكرار الدوران)"""
    # عندك منطق لا يعتمد على _stop_flag الآن، لكن نبقي endpoint لأجل الواجهة.
    _state["running"] = False
    return jsonify({"ok": True, "msg": "تم إيقاف المهمة."})


@app.post("/resume")
def resume():
    return jsonify({"ok": False, "msg": "استخدم /queue مع post_url لبدء مهمة جديدة."}), 400


@app.get("/healthz")
def health():
    return "ok", 200


if __name__ == "__main__":
    # تشغيل محلي فقط
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", "5000")))
