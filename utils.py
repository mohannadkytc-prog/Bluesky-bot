"""
Utility functions for the Bluesky bot
- متوافق مع Config (اختياري)
- استخدام /data للتخزين على Render بشكل آمن
"""

import os
import re
import json
import logging
from typing import Optional, Dict, Any
from urllib.parse import urlparse

# ===== إعداد التخزين الافتراضي (/data) =====
DATA_DIR = os.getenv("DATA_DIR", "/data")
os.makedirs(DATA_DIR, exist_ok=True)

DEFAULT_PROGRESS_PATH = os.path.join(DATA_DIR, "progress.json")
TASKS_DIR = os.path.join(DATA_DIR, "tasks")
os.makedirs(TASKS_DIR, exist_ok=True)


def _get_paths(config: Optional[Any] = None) -> Dict[str, str]:
    """
    إرجاع مسارات التخزين (تستخدم Config إن وُجدت، وإلا /data الافتراضي)
    - config.progress_path (لو متوفر)
    - otherwise DEFAULT_PROGRESS_PATH
    """
    progress_path = DEFAULT_PROGRESS_PATH
    tasks_dir = TASKS_DIR

    # لو مرّ كائن Config وفيه خصائص المسارات، نستخدمها
    try:
        if config is not None:
            if hasattr(config, "data_dir") and config.data_dir:
                os.makedirs(config.data_dir, exist_ok=True)
            if hasattr(config, "tasks_dir") and config.tasks_dir:
                tasks_dir = config.tasks_dir
                os.makedirs(tasks_dir, exist_ok=True)
            if hasattr(config, "progress_path") and config.progress_path:
                progress_path = config.progress_path
                # تأكّد من وجود المجلد
                os.makedirs(os.path.dirname(progress_path) or DATA_DIR, exist_ok=True)
    except Exception as e:
        logging.getLogger(__name__).warning(
            f"Falling back to defaults for storage paths: {e}"
        )

    return {"progress_path": progress_path, "tasks_dir": tasks_dir}


def extract_post_info(post_url: str) -> Optional[Dict[str, str]]:
    """
    Extract post URI and CID from a Bluesky post URL

    Example URL: https://bsky.app/profile/username.bsky.social/post/3k4duaz5vfs2b
    """
    logger = logging.getLogger(__name__)

    try:
        # Parse the URL
        parsed = urlparse(post_url)

        # Check if it's a valid Bluesky URL
        if parsed.netloc not in ['bsky.app', 'staging.bsky.app']:
            logger.error(f"Invalid Bluesky URL domain: {parsed.netloc}")
            return None

        # Extract path components
        path_parts = parsed.path.strip('/').split('/')

        # Expected format: profile/username/post/post_id
        if len(path_parts) != 4 or path_parts[0] != 'profile' or path_parts[2] != 'post':
            logger.error(f"Invalid URL format: {post_url}")
            return None

        username = path_parts[1]
        post_id = path_parts[3]

        logger.info(f"Extracted username: {username}, post_id: {post_id}")

        # Return basic info - we'll resolve the actual URI/CID using the API
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
    This is a more robust approach that actually fetches the post data
    """
    logger = logging.getLogger(__name__)

    try:
        # Extract basic info from URL
        post_info = extract_post_info(post_url)
        if not post_info:
            return None

        username = post_info['username']
        post_id = post_info['post_id']

        # Resolve the handle to get the actual DID
        try:
            profile = client.app.bsky.actor.get_profile({'actor': username})
            did = profile.did
        except Exception as e:
            logger.error(f"Failed to resolve DID for {username}: {e}")
            return None

        # Construct the proper AT URI
        post_uri = f"at://{did}/app.bsky.feed.post/{post_id}"

        # Fetch the actual post to get the CID
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


def _load_all_progress(file_path: str) -> Dict[str, Any]:
    """تحميل كل التقدم من ملف واحد."""
    try:
        with open(file_path, 'r', encoding='utf-8') as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}
    except Exception as e:
        logging.getLogger(__name__).error(f"Failed to read progress file: {e}")
        return {}


def _save_all_progress(file_path: str, payload: Dict[str, Any]) -> None:
    """حفظ كل التقدم في ملف واحد بشكل آمن (atomic write بسيط)."""
    logger = logging.getLogger(__name__)
    tmp_path = f"{file_path}.tmp"

    try:
        # اكتب لملف مؤقت أولاً
        with open(tmp_path, 'w', encoding='utf-8') as f:
            json.dump(payload, f, indent=2, ensure_ascii=False)
        # ثم استبدل الملف الأساسي
        os.replace(tmp_path, file_path)
        logger.debug("Progress file updated successfully.")
    except Exception as e:
        logger.error(f"Failed to save progress: {e}")
        # نظّف الملف المؤقت إن وُجد
        try:
            if os.path.exists(tmp_path):
                os.remove(tmp_path)
        except Exception:
            pass


def save_progress(progress_file: Optional[str], post_url: str, progress_data: Dict[str, Any], config: Optional[Any] = None):
    """
    Save progress to a JSON file.
    - لو مرّرت progress_file نستخدمه
    - لو ما مرّ، نأخذ المسار من Config (إن وجد) وإلا من /data
    """
    logger = logging.getLogger(__name__)

    paths = _get_paths(config)
    file_path = progress_file or paths["progress_path"]

    try:
        all_progress = _load_all_progress(file_path)
        all_progress[post_url] = progress_data
        _save_all_progress(file_path, all_progress)
        logger.debug(f"Progress saved for {post_url} -> {file_path}")
    except Exception as e:
        logger.error(f"Failed to save progress: {e}")


def load_progress(progress_file: Optional[str], post_url: str, config: Optional[Any] = None) -> Dict[str, Any]:
    """
    Load progress for a specific post_url.
    - يتعامل نفس طريقة save_progress في اختيار المسار
    """
    logger = logging.getLogger(__name__)

    paths = _get_paths(config)
    file_path = progress_file or paths["progress_path"]

    try:
        all_progress = _load_all_progress(file_path)
        return all_progress.get(post_url, {})
    except Exception as e:
        logger.error(f"Failed to load progress: {e}")
        return {}


def validate_message_template(template: str) -> bool:
    """Validate that the message template is safe and well-formed"""
    if not template or not isinstance(template, str):
        return False

    # Check for reasonable length
    if len(template) > 280:  # Bluesky character limit
        return False

    # Check for basic safety (no obvious injection attempts)
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
