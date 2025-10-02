"""
Utility functions for the Bluesky bot
- يجلب معلومات البوست من رابط bsky.app
- يحفظ/يقرأ تقدّم المهام بمفتاح مميّز
- يتحقق من سلامة قوالب الرسائل
"""
import re
import json
import logging
from typing import Optional, Dict, Any
from urllib.parse import urlparse

logger = logging.getLogger(__name__)

def extract_post_info(post_url: str) -> Optional[Dict[str, str]]:
    """
    يستخرج username و post_id من رابط Bluesky على bsky.app أو staging.bsky.app
    مثال: https://bsky.app/profile/username.bsky.social/post/3k4duaz5vfs2b
    """
    try:
        parsed = urlparse(post_url)
        if parsed.netloc not in ["bsky.app", "staging.bsky.app"]:
            logger.error(f"Invalid Bluesky URL domain: {parsed.netloc}")
            return None

        parts = parsed.path.strip("/").split("/")
        # profile/<handle>/post/<rkey>
        if len(parts) != 4 or parts[0] != "profile" or parts[2] != "post":
            logger.error(f"Invalid URL format: {post_url}")
            return None

        return {"username": parts[1], "post_id": parts[3], "url": post_url}
    except Exception as e:
        logger.exception(f"extract_post_info error: {e}")
        return None


def resolve_post_from_url(client, post_url: str) -> Optional[Dict[str, str]]:
    """
    يحوّل رابط bsky.app إلى (uri, cid, did, post_id)
    """
    try:
        info = extract_post_info(post_url)
        if not info:
            return None

        username = info["username"]
        post_id  = info["post_id"]

        # احصلي على DID من الهاندل
        prof = client.app.bsky.actor.get_profile({"actor": username})
        did = prof.did

        # صياغة at-uri للمنشور
        post_uri = f"at://{did}/app.bsky.feed.post/{post_id}"

        # احصلي على CID الحقيقي
        posts = client.app.bsky.feed.get_posts({"uris": [post_uri]}).posts
        if posts:
            return {
                "uri": post_uri,
                "cid": posts[0].cid,
                "did": did,
                "post_id": post_id,
                "username": username,
            }
        return None
    except Exception as e:
        logger.exception(f"resolve_post_from_url error: {e}")
        return None


# ===== حفظ/قراءة التقدّم بمفتاح =====

def save_progress(progress_file: str, key: str, progress_data: Dict[str, Any]) -> None:
    """
    يحفظ التقدم داخل JSON واحد، تحت مفتاح مميّز (key)
    """
    try:
        try:
            with open(progress_file, "r", encoding="utf-8") as f:
                all_p = json.load(f)
        except (FileNotFoundError, json.JSONDecodeError):
            all_p = {}

        all_p[key] = progress_data
        with open(progress_file, "w", encoding="utf-8") as f:
            json.dump(all_p, f, ensure_ascii=False, indent=2)
        logger.debug(f"Progress saved for key={key}")
    except Exception as e:
        logger.exception(f"save_progress error: {e}")


def load_progress(progress_file: str, key: str) -> Dict[str, Any]:
    """
    يقرأ التقدم من الملف؛ يرجّع dict أو {} إذا غير موجود
    """
    try:
        with open(progress_file, "r", encoding="utf-8") as f:
            all_p = json.load(f)
        return all_p.get(key, {})
    except (FileNotFoundError, json.JSONDecodeError):
        return {}
    except Exception as e:
        logger.exception(f"load_progress error: {e}")
        return {}


# ===== التحقق من الرسائل =====

def validate_message_template(template: str) -> bool:
    """يتحقق من الطول والسلامة العامة."""
    if not template or not isinstance(template, str):
        return False
    if len(template) > 280:
        return False

    bad = [r"<script", r"javascript:", r"data:", r"vbscript:", r"on\w+="]
    for pat in bad:
        if re.search(pat, template, re.IGNORECASE):
            return False
    return True
