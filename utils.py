"""
Utility helpers for Bluesky bot (URLs, progress, and audience fetch)
"""

import os
import re
import json
import logging
from typing import Optional, Dict, Any, List, Tuple
from urllib.parse import urlparse

# ==== تخزين آمن: /data إن وُجد وإلا /tmp ====
DATA_DIR = "/data" if os.path.exists("/data") else "/tmp"
os.makedirs(DATA_DIR, exist_ok=True)

PROGRESS_PATH = os.path.join(DATA_DIR, "progress.json")
os.makedirs(os.path.dirname(PROGRESS_PATH), exist_ok=True)

logger = logging.getLogger(__name__)
logging.basicConfig(level=logging.INFO)


# ---------- URL parsing ----------
def extract_post_info(post_url: str) -> Optional[Dict[str, str]]:
    """
    Extract handle (username) and post_id from a Bluesky post URL like:
    https://bsky.app/profile/username.bsky.social/post/3k4duaz5vfs2b
    """
    try:
        parsed = urlparse(post_url)
        if parsed.netloc not in ["bsky.app", "staging.bsky.app"]:
            logger.error(f"Invalid Bluesky URL domain: {parsed.netloc}")
            return None

        parts = parsed.path.strip("/").split("/")
        # expected: profile/<handle>/post/<postid>
        if len(parts) != 4 or parts[0] != "profile" or parts[2] != "post":
            logger.error(f"Invalid URL format: {post_url}")
            return None

        return {"username": parts[1], "post_id": parts[3], "url": post_url}
    except Exception as e:
        logger.exception(f"extract_post_info error: {e}")
        return None


def resolve_post_from_url(client, post_url: str) -> Optional[Dict[str, str]]:
    """
    Resolve (uri, cid, did, handle, post_id) from bsky URL using API.
    """
    try:
        base = extract_post_info(post_url)
        if not base:
            return None

        handle = base["username"]
        post_id = base["post_id"]

        prof = client.app.bsky.actor.get_profile({"actor": handle})
        did = prof.did

        uri = f"at://{did}/app.bsky.feed.post/{post_id}"
        posts = client.app.bsky.feed.get_posts({"uris": [uri]})
        if not posts.posts:
            logger.error("Post not found for URI: %s", uri)
            return None
        cid = posts.posts[0].cid
        return {"uri": uri, "cid": cid, "did": did, "handle": handle, "post_id": post_id}
    except Exception as e:
        logger.exception(f"resolve_post_from_url error: {e}")
        return None


# ---------- Progress (save/load per task key) ----------
def _load_all_progress() -> Dict[str, Any]:
    try:
        with open(PROGRESS_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def _save_all_progress(allp: Dict[str, Any]) -> None:
    tmp = PROGRESS_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(allp, f, ensure_ascii=False, indent=2)
    os.replace(tmp, PROGRESS_PATH)


def task_key(handle: str, mode: str, post_url: str) -> str:
    """Unique progress key per (account, mode, link)."""
    return f"{handle}|{mode}|{post_url}"


def load_progress(key: str) -> Dict[str, Any]:
    allp = _load_all_progress()
    return allp.get(key, {
        "queue": [],          # remaining list of DIDs to process (ordered)
        "processed": {},      # did -> {"ok": bool, "err": str|None}
        "stats": {"ok": 0, "fail": 0, "total": 0},
        "last_error": "",
        "cursor": 0
    })


def save_progress(key: str, prog: Dict[str, Any]) -> None:
    allp = _load_all_progress()
    allp[key] = prog
    _save_all_progress(allp)


# ---------- Audience fetch (likers or reposters) ----------
def get_audience_list(client, post_uri: str, mode: str) -> List[str]:
    """
    Returns a list of DIDs in order (top->down) for likers or reposters.
    mode in {"likers", "reposters"}
    """
    dids: List[str] = []
    cursor = None
    try:
        while True:
            if mode == "likers":
                resp = client.app.bsky.feed.get_likes({"uri": post_uri, "limit": 100, "cursor": cursor})
                for it in resp.likes or []:
                    a = it.actor
                    if getattr(a, "did", None):
                        dids.append(a.did)
                cursor = getattr(resp, "cursor", None)
            else:
                resp = client.app.bsky.feed.get_reposted_by({"uri": post_uri, "limit": 100, "cursor": cursor})
                for it in resp.reposted_by or []:
                    if getattr(it, "did", None):
                        dids.append(it.did)
                cursor = getattr(resp, "cursor", None)

            if not cursor:
                break
    except Exception as e:
        logger.exception(f"get_audience_list error: {e}")

    # إزالة التكرارات مع الحفاظ على الترتيب
    seen = set()
    ordered = []
    for d in dids:
        if d not in seen:
            seen.add(d)
            ordered.append(d)
    return ordered


# ---------- Validation helpers ----------
def validate_message_template(template: str) -> bool:
    if not template or not isinstance(template, str):
        return False
    if len(template) > 300:
        return False
    bad = [r"<script", r"javascript:", r"vbscript:", r"onerror", r"onclick"]
    for p in bad:
        if re.search(p, template, re.I):
            return False
    return True
