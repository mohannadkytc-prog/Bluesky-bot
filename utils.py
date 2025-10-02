# -*- coding: utf-8 -*-
"""
Utility functions for the Bluesky bot
"""
import re
import json
import logging
from typing import Optional, Dict, Any, Tuple
from urllib.parse import urlparse

logger = logging.getLogger(__name__)

def extract_post_info(post_url: str) -> Optional[Dict[str, str]]:
    try:
        parsed = urlparse(post_url)
        if parsed.netloc not in ['bsky.app', 'staging.bsky.app']:
            logger.error(f"Invalid Bluesky URL domain: {parsed.netloc}")
            return None
        parts = parsed.path.strip('/').split('/')
        if len(parts) != 4 or parts[0] != 'profile' or parts[2] != 'post':
            logger.error(f"Invalid URL format: {post_url}")
            return None
        return {"username": parts[1], "post_id": parts[3], "url": post_url}
    except Exception as e:
        logger.error(f"Error extracting post info: {e}")
        return None

def resolve_post_from_url(client, post_url: str) -> Optional[Dict[str, str]]:
    try:
        info = extract_post_info(post_url)
        if not info:
            return None
        username = info['username']
        post_id = info['post_id']

        profile = client.app.bsky.actor.get_profile({'actor': username})
        did = profile.did
        post_uri = f"at://{did}/app.bsky.feed.post/{post_id}"

        post_response = client.app.bsky.feed.get_posts({'uris': [post_uri]})
        if post_response.posts:
            post = post_response.posts[0]
            return {
                'uri': post_uri,
                'cid': post.cid,
                'username': username,
                'post_id': post_id,
                'did': did
            }
        return None
    except Exception as e:
        logger.error(f"resolve_post_from_url failed: {e}")
        return None

# ---------- التخزين ----------
def _read_all(path: str) -> Dict[str, Any]:
    try:
        with open(path, "r") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}

def _write_all(path: str, payload: Dict[str, Any]) -> None:
    with open(path, "w") as f:
        json.dump(payload, f, indent=2, ensure_ascii=False)

def save_progress_for_key(progress_file: str, key: str, progress_data: Dict[str, Any]):
    all_p = _read_all(progress_file)
    all_p[key] = progress_data
    _write_all(progress_file, all_p)

def load_progress_for_key(progress_file: str, key: str) -> Dict[str, Any]:
    return _read_all(progress_file).get(key, {})

# ---------- فحص الرسالة ----------
def validate_message_template(template: str) -> bool:
    if not template or not isinstance(template, str):
        return False
    if len(template) > 280:
        return False
    dangerous = [r'<script', r'javascript:', r'data:', r'vbscript:', r'onclick', r'onerror']
    for p in dangerous:
        if re.search(p, template, re.IGNORECASE):
            return False
    return True
