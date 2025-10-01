"""
Utility functions for the Bluesky bot
"""

import re
import json
import logging
from typing import Optional, Dict, Any, List
from urllib.parse import urlparse

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
    Resolve actual post URI and CID from a Bluesky post URL using the API.
    يحاول أولاً بطريقة get_posts (كما في كودك الأصلي)،
    وإن فشل، يعمل Fallback أدق عبر resolve_handle + get_record.
    """
    logger = logging.getLogger(__name__)
    
    try:
        # Extract basic info from URL
        post_info = extract_post_info(post_url)
        if not post_info:
            return None
        
        username = post_info['username']
        post_id = post_info['post_id']

        # 1) الطريقة الأصلية: get_profile + get_posts
        try:
            profile = client.app.bsky.actor.get_profile({'actor': username})
            did = profile.did
            post_uri = f"at://{did}/app.bsky.feed.post/{post_id}"

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
            logger.warning("Primary resolution via get_posts returned no posts; trying fallback...")
        except Exception as e:
            logger.warning(f"Primary resolution failed ({e}); trying fallback...")

        # 2) Fallback أدق: resolve_handle → get_record
        try:
            did_obj = client.com.atproto.identity.resolve_handle({'handle': username})
            did = did_obj.did
            rec = client.com.atproto.repo.get_record({
                'repo': did,
                'collection': 'app.bsky.feed.post',
                'rkey': post_id,
            })
            cid = rec.cid
            post_uri = f"at://{did}/app.bsky.feed.post/{post_id}"
            return {
                'uri': post_uri,
                'cid': cid,
                'username': username,
                'post_id': post_id,
                'did': did
            }
        except Exception as e:
            logger.error(f"Fallback resolution failed for {username}/{post_id}: {e}")
            return None
            
    except Exception as e:
        logger.error(f"Error resolving post from URL {post_url}: {e}")
        return None

def save_progress(progress_file: str, post_url: str, progress_data: Dict[str, Any]):
    """Save progress to a JSON file"""
    logger = logging.getLogger(__name__)
    
    try:
        # Load existing progress
        try:
            with open(progress_file, 'r', encoding='utf-8') as f:
                all_progress = json.load(f)
        except (FileNotFoundError, json.JSONDecodeError):
            all_progress = {}
        
        # Update progress for this post
        all_progress[post_url] = progress_data
        
        # Save back to file
        with open(progress_file, 'w', encoding='utf-8') as f:
            json.dump(all_progress, f, indent=2, ensure_ascii=False)
        
        logger.debug(f"Progress saved for {post_url}")
        
    except Exception as e:
        logger.error(f"Failed to save progress: {e}")

def load_progress(progress_file: str, post_url: str) -> Dict[str, Any]:
    """Load progress from a JSON file"""
    logger = logging.getLogger(__name__)
    
    try:
        with open(progress_file, 'r', encoding='utf-8') as f:
            all_progress = json.load(f)
        
        return all_progress.get(post_url, {})
        
    except (FileNotFoundError, json.JSONDecodeError):
        logger.debug(f"No existing progress found for {post_url}")
        return {}
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


# =========================
# New helpers for audience & checkpoint
# =========================

def get_likers(client, post_uri: str) -> List[Dict[str, str]]:
    """
    يجلب جميع المعجبين لبوست معيّن (مع ترقيم الصفحات).
    يرجع قائمة قوامها dict: {handle, display_name, did}
    """
    logger = logging.getLogger(__name__)
    likers: List[Dict[str, str]] = []
    cursor = None

    try:
        while True:
            params = {'uri': post_uri, 'limit': 100}
            if cursor:
                params['cursor'] = cursor
            res = client.app.bsky.feed.get_likes(params)
            if not getattr(res, 'likes', None):
                break
            for like in res.likes:
                actor = like.actor
                likers.append({
                    'handle': actor.handle,
                    'display_name': getattr(actor, 'display_name', None),
                    'did': actor.did
                })
            cursor = getattr(res, 'cursor', None)
            if not cursor:
                break
        logger.info(f"👍 get_likers: {len(likers)} users")
    except Exception as e:
        logger.error(f"get_likers failed: {e}")
    return likers

def get_reposters(client, post_uri: str, post_cid: str) -> List[Dict[str, str]]:
    """
    يجلب جميع مُعيدي النشر لبوست معيّن (مع ترقيم الصفحات).
    يرجع قائمة قوامها dict: {handle, display_name, did}
    """
    logger = logging.getLogger(__name__)
    reposters: List[Dict[str, str]] = []
    cursor = None

    try:
        while True:
            params = {'uri': post_uri, 'cid': post_cid, 'limit': 100}
            if cursor:
                params['cursor'] = cursor
            res = client.app.bsky.feed.get_reposted_by(params)
            if not getattr(res, 'reposted_by', None):
                break
            for user in res.reposted_by:
                reposters.append({
                    'handle': user.handle,
                    'display_name': getattr(user, 'display_name', None),
                    'did': user.did
                })
            cursor = getattr(res, 'cursor', None)
            if not cursor:
                break
        logger.info(f"🔁 get_reposters: {len(reposters)} users")
    except Exception as e:
        logger.error(f"get_reposters failed: {e}")
    return reposters

def dedupe_users(users: List[Dict[str, str]]) -> List[Dict[str, str]]:
    """
    إزالة التكرار حسب handle مع الحفاظ على الترتيب الأول.
    """
    seen = set()
    out: List[Dict[str, str]] = []
    for u in users:
        h = u.get('handle')
        if h and h not in seen:
            seen.add(h)
            out.append(u)
    return out

def ensure_progress_schema(progress: Dict[str, Any]) -> Dict[str, Any]:
    """
    يضمن وجود الحقول القياسية في سجل التقدم لواجهة HTML والكود.
    """
    if not isinstance(progress, dict):
        progress = {}
    progress.setdefault('processed_handles', [])
    progress.setdefault('last_processed', None)
    progress.setdefault('total_processed', len(progress.get('processed_handles', [])))
    progress.setdefault('total', 0)
    return progress

def append_processed_handle(progress_file: str, post_url: str, handle: str) -> Dict[str, Any]:
    """
    يضيف handle إلى قائمة المعالَجين ويحدّث last_processed و total_processed فورًا.
    يرجع الكائن المحدث (للاستخدام في الواجهة إن لزم).
    """
    progress = load_progress(progress_file, post_url)
    progress = ensure_progress_schema(progress)

    processed = set(progress.get('processed_handles', []))
    if handle not in processed:
        processed.add(handle)
        progress['processed_handles'] = list(processed)
        progress['last_processed'] = handle
        progress['total_processed'] = len(processed)
        save_progress(progress_file, post_url, progress)

    return progress

def set_total_audience(progress_file: str, post_url: str, total: int) -> Dict[str, Any]:
    """
    يحدّث إجمالي الجمهور (total) لعرض أدق في الواجهة.
    """
    progress = load_progress(progress_file, post_url)
    progress = ensure_progress_schema(progress)
    progress['total'] = int(total)
    save_progress(progress_file, post_url, progress)
    return progress
