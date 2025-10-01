"""
Utilities for Bluesky bot:
- resolve_post_from_url
- get_likers / get_reposters (مرتّبة من الأعلى للأسفل)
- get_latest_post (آخر منشور للمستخدم)
- reply_to_post
- حفظ/تحميل التقدم
"""
import re
import json
import logging
from typing import Optional, Dict, Any, List, Tuple
from urllib.parse import urlparse

logger = logging.getLogger("utils")

# --------- URL parsing ---------
def extract_post_info(post_url: str) -> Optional[Dict[str, str]]:
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
        return {"handle": parts[1], "rkey": parts[3], "url": post_url}
    except Exception as e:
        logger.error(f"extract_post_info error: {e}")
        return None

def resolve_post_from_url(client, post_url: str) -> Optional[Dict[str, str]]:
    """يحصل على (uri, cid) الفعليين من الرابط"""
    try:
        info = extract_post_info(post_url)
        if not info:
            return None
        prof = client.app.bsky.actor.get_profile({"actor": info["handle"]})
        did = prof.did
        uri = f"at://{did}/app.bsky.feed.post/{info['rkey']}"
        posts = client.app.bsky.feed.get_posts({"uris": [uri]})
        if posts.posts:
            return {"uri": uri, "cid": posts.posts[0].cid, "did": did}
    except Exception as e:
        logger.error(f"resolve_post_from_url error: {e}")
    return None

# --------- Audience fetchers (ordered) ---------
def get_likers(client, post_uri: str, limit: int = 1000) -> List[str]:
    """
    يرجع قائمة handles للمعجبين مرتّبة من الأعلى للأسفل
    """
    users: List[str] = []
    cursor = None
    while True:
        resp = client.app.bsky.feed.get_likes({"uri": post_uri, "cursor": cursor, "limit": 100})
        # API يرجّع الأحدث أولًا عادة — سنعكس في النهاية لو احتجتِ ترتيبًا معينًا
        for like in resp.likes or []:
            if like.actor and like.actor.handle:
                users.append(like.actor.handle)
        cursor = getattr(resp, "cursor", None)
        if not cursor or len(users) >= limit:
            break
    # الأعلى للأسفل = كما أعادها API (من الأقدم للأحدث؟) كثيرًا ما تكون من الأحدث للأقدم.
    # لضمان "من الأعلى للأسفل" (أقدم -> أحدث) نعكس:
    users.reverse()
    # إزالة التكرار مع الحفاظ على الترتيب
    seen = set()
    ordered = []
    for u in users:
        if u not in seen:
            seen.add(u)
            ordered.append(u)
    return ordered

def get_reposters(client, post_uri: str, limit: int = 1000) -> List[str]:
    """
    يرجع قائمة handles لمعيدي النشر مرتّبة من الأعلى للأسفل
    """
    users: List[str] = []
    cursor = None
    while True:
        resp = client.app.bsky.feed.get_reposted_by({"uri": post_uri, "cursor": cursor, "limit": 100})
        for item in resp.reposted_by or []:
            if item.handle:
                users.append(item.handle)
        cursor = getattr(resp, "cursor", None)
        if not cursor or len(users) >= limit:
            break
    users.reverse()
    seen = set()
    ordered = []
    for u in users:
        if u not in seen:
            seen.add(u)
            ordered.append(u)
    return ordered

# --------- Latest post for an actor ---------
def get_latest_post(client, actor_handle: str) -> Optional[Tuple[str, str]]:
    """
    يرجع (uri, cid) لآخر منشور لهذا المستخدم.
    """
    try:
        feed = client.app.bsky.feed.get_author_feed({"actor": actor_handle, "limit": 1, "filter": "posts_with_replies"})
        if feed.feed:
            post = feed.feed[0].post
            return post.uri, post.cid
    except Exception as e:
        logger.error(f"get_latest_post error for @{actor_handle}: {e}")
    return None

# --------- Reply ---------
def reply_to_post(client, parent_uri: str, parent_cid: str, text: str) -> None:
    client.com.atproto.repo.create_record({
        "repo": client.me.did,
        "collection": "app.bsky.feed.post",
        "record": {
            "$type": "app.bsky.feed.post",
            "text": text,
            "reply": {
                "root": {"uri": parent_uri, "cid": parent_cid},
                "parent": {"uri": parent_uri, "cid": parent_cid},
            },
        },
    })

# --------- Progress JSON helpers ---------
def save_progress(path: str, key: str, data: Dict[str, Any]) -> None:
    try:
        try:
            with open(path, "r") as f:
                allp = json.load(f)
        except Exception:
            allp = {}
        allp[key] = data
        with open(path, "w") as f:
            json.dump(allp, f, ensure_ascii=False, indent=2)
    except Exception as e:
        logger.error(f"save_progress error: {e}")

def load_progress(path: str, key: str) -> Dict[str, Any]:
    try:
        with open(path, "r") as f:
            allp = json.load(f)
        return allp.get(key, {})
    except Exception:
        return {}
