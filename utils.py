"""
Utility functions for the Bluesky bot
"""
import re
import json
import logging
from typing import Optional, Dict, Any, List, Tuple
from urllib.parse import urlparse

logger = logging.getLogger(__name__)

def extract_post_info(post_url: str) -> Optional[Dict[str, str]]:
    """
    Extract post URI parts from a Bluesky post URL
    Example: https://bsky.app/profile/username.bsky.social/post/3k4duaz5vfs2b
    """
    try:
        parsed = urlparse(post_url)
        if parsed.netloc not in ['bsky.app', 'staging.bsky.app']:
            logger.error(f"Invalid Bluesky URL domain: {parsed.netloc}")
            return None
        path_parts = parsed.path.strip('/').split('/')
        if len(path_parts) != 4 or path_parts[0] != 'profile' or path_parts[2] != 'post':
            logger.error(f"Invalid URL format: {post_url}")
            return None
        username = path_parts[1]
        post_id = path_parts[3]
        logger.info(f"Extracted username: {username}, post_id: {post_id}")
        return {'username': username, 'post_id': post_id, 'url': post_url}
    except Exception as e:
        logger.error(f"Error extracting post info from URL {post_url}: {e}")
        return None

def resolve_post_from_url(client, post_url: str) -> Optional[Dict[str, str]]:
    """
    Resolve actual post URI and CID from a Bluesky post URL using the API
    """
    try:
        info = extract_post_info(post_url)
        if not info:
            return None
        username = info['username']
        post_id = info['post_id']
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
                return {
                    'uri': post_uri,
                    'cid': post.cid,
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

# ------------ التخزين ------------
def save_progress(progress_file: str, post_url: str, progress_data: Dict[str, Any]):
    """Save progress to a JSON file keyed by post_url"""
    try:
        try:
            with open(progress_file, 'r') as f:
                all_progress = json.load(f)
        except (FileNotFoundError, json.JSONDecodeError):
            all_progress = {}
        all_progress[post_url] = progress_data
        with open(progress_file, 'w') as f:
            json.dump(all_progress, f, indent=2, ensure_ascii=False)
        logger.debug(f"Progress saved for {post_url}")
    except Exception as e:
        logger.error(f"Failed to save progress: {e}")

def load_progress(progress_file: str, post_url: str) -> Dict[str, Any]:
    """Load progress by post_url from JSON file"""
    try:
        with open(progress_file, 'r') as f:
            all_progress = json.load(f)
        return all_progress.get(post_url, {})
    except (FileNotFoundError, json.JSONDecodeError):
        logger.debug(f"No existing progress found for {post_url}")
        return {}
    except Exception as e:
        logger.error(f"Failed to load progress: {e}")
        return {}

# ------------ التحقق والتهيئة ------------
def validate_message_template(template: str) -> bool:
    """Validate that the message template is safe and well-formed"""
    if not template or not isinstance(template, str):
        return False
    if len(template) > 280:
        return False
    dangerous_patterns = [r'<script', r'javascript:', r'data:', r'vbscript:', r'onclick', r'onerror']
    for pattern in dangerous_patterns:
        if re.search(pattern, template, re.IGNORECASE):
            return False
    return True

def format_duration(seconds: int) -> str:
    if seconds < 60:
        return f"{seconds}s"
    elif seconds < 3600:
        return f"{seconds // 60}m {seconds % 60}s"
    else:
        hours = seconds // 3600
        minutes = (seconds % 3600) // 60
        return f"{hours}h {minutes}m"

# ------------ مساعدات للجمهور بالترتيب ------------
def _merge_unique_ordered(
    likers: List[Tuple[str, str]],
    reposters: List[Tuple[str, str]],
    mode: str
) -> List[Tuple[str, str]]:
    """
    likers/reposters: قوائم من (actor, indexedAt)
    mode: 'likers' | 'reposters' | 'both'
    نرجّع قائمة مرتبة تصاعدياً حسب indexedAt مع إزالة التكرار (نفضّل أول ظهور).
    """
    combined: List[Tuple[str, str]] = []
    if mode == 'likers':
        combined = likers
    elif mode == 'reposters':
        combined = reposters
    else:
        combined = likers + reposters
    # sort by indexedAt asc (الأقدم أولاً)
    combined.sort(key=lambda x: x[1])
    # dedupe by actor
    seen = set()
    ordered: List[Tuple[str, str]] = []
    for actor, ts in combined:
        if actor not in seen:
            seen.add(actor)
            ordered.append((actor, ts))
    return ordered
