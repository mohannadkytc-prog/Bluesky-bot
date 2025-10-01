# -*- coding: utf-8 -*-
"""
Utility functions for the Bluesky bot
"""

import re
import json
import logging
from typing import Optional, Dict, Any
from urllib.parse import urlparse

# نحاول استيراد PROGRESS_PATH الافتراضي (لاستخدامه إن لم يُمرَّر مسار)
try:
    from config import PROGRESS_PATH as DEFAULT_PROGRESS_PATH  # type: ignore
except Exception:
    DEFAULT_PROGRESS_PATH = "/tmp/progress.json"


def extract_post_info(post_url: str) -> Optional[Dict[str, str]]:
    """
    Extract post URI and CID from a Bluesky post URL

    Example URL: https://bsky.app/profile/username.bsky.social/post/3k4duaz5vfs2b
    """
    logger = logging.getLogger(__name__)

    try:
        parsed = urlparse(post_url)

        if parsed.netloc not in ['bsky.app', 'staging.bsky.app']:
            logger.error(f"Invalid Bluesky URL domain: {parsed.netloc}")
            return None

        path_parts = parsed.path.strip('/').split('/')

        # Expected format: profile/username/post/post_id
        if len(path_parts) != 4 or path_parts[0] != 'profile' or path_parts[2] != 'post':
            logger.error(f"Invalid URL format: {post_url}")
            return None

        username = path_parts[1]
        post_id = path_parts[3]

        logger.info(f"Extracted username: {username}, post_id: {post_id}")

        return {
            'username': username,
            'post_id': post_id,
            'url': post_url
        }

    except Exception as e:
        logger.error(f"Error extracting post info from URL {post_url}: {e}")
        return None


def resolve_post_from_url(client, post_url: str) -> Optional[Dict[str, str]]:
    """
    Resolve actual post URI and CID from a Bluesky post URL using the API
    """
    logger = logging.getLogger(__name__)

    try:
        post_info = extract_post_info(post_url)
        if not post_info:
            return None

        username = post_info['username']
        post_id = post_info['post_id']

        try:
            profile = client.app.bsky.actor.get_profile({'actor': username})
            did = profile.did
        except Exception as e:
            logger.error(f"Failed to resolve DID for {username}: {e}")
            return None

        post_uri = f"at://{did}/app.bsky.feed.post/{post_id}"

        try:
            post_response = client.app.bsky.feed.get_posts({'uris': [post_uri]})
            if post_response.posts:
                post = post_response.posts[0]
                post_cid = post.cid

                return {
                    'uri': post_uri,
                    'cid': post_cid,
                    'username': username,
                    'post_id': post_id,
                    'did': did
                }
        except Exception as e:
            logger.error(f"Failed to fetch post {post_uri}: {e}")
            return None

    except Exception as e:
        logger.error(f"Error resolving post from URL {post_url}: {e}")
        return None


def _load_all_progress(progress_file: str) -> Dict[str, Any]:
    try:
        with open(progress_file, 'r') as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}
    except Exception:
        return {}


def save_progress(progress_file: Optional[str], key: str, progress_data: Dict[str, Any]):
    """Save progress to a JSON file. If progress_file is None, use DEFAULT_PROGRESS_PATH."""
    logger = logging.getLogger(__name__)
    progress_file = progress_file or DEFAULT_PROGRESS_PATH

    try:
        all_progress = _load_all_progress(progress_file)
        all_progress[key] = progress_data

        # تأكد من وجود المجلد
        import os
        os.makedirs(os.path.dirname(progress_file) or ".", exist_ok=True)

        with open(progress_file, 'w') as f:
            json.dump(all_progress, f, indent=2)

        logger.debug(f"Progress saved for {key} -> {progress_file}")
    except Exception as e:
        logger.error(f"Failed to save progress: {e}")


def load_progress(progress_file: Optional[str], key: str) -> Dict[str, Any]:
    """Load progress from a JSON file. If progress_file is None, use DEFAULT_PROGRESS_PATH."""
    logger = logging.getLogger(__name__)
    progress_file = progress_file or DEFAULT_PROGRESS_PATH

    try:
        all_progress = _load_all_progress(progress_file)
        return all_progress.get(key, {})
    except Exception as e:
        logger.error(f"Failed to load progress: {e}")
        return {}


def validate_message_template(template: str) -> bool:
    """Validate that the message template is safe and well-formed"""
    if not template or not isinstance(template, str):
        return False

    # Bluesky عادةً 300 حرف تقريبًا (خلّينا حد 280 آمن)
    if len(template) > 280:
        return False

    dangerous_patterns = [
        r'<script',
        r'javascript:',
        r'data:',
        r'vbscript:',
        r'onclick',
        r'onerror'
    ]

    for pattern in dangerous_patterns:
        if re.search(pattern, template, re.IGNORECASE):
            return False

    return True


def format_duration(seconds: int) -> str:
    """Format duration in seconds to human-readable format"""
    if seconds < 60:
        return f"{seconds}s"
    elif seconds < 3600:
        return f"{seconds // 60}m {seconds % 60}s"
    else:
        hours = seconds // 3600
        minutes = (seconds % 3600) // 60
        return f"{hours}h {minutes}m"
