"""
Utility functions for the Bluesky bot
(يبقى مستقل ويستقبل مسار ملف التقدم من caller حتى ما يصير تعارض استيراد)
"""

import re
import json
import logging
from typing import Optional, Dict, Any
from urllib.parse import urlparse

logger = logging.getLogger(__name__)

def extract_post_info(post_url: str) -> Optional[Dict[str, str]]:
    """
    Extract post URI parts from a Bluesky post URL.

    Example URL: https://bsky.app/profile/username.bsky.social/post/3k4duaz5vfs2b
    """
    try:
        parsed = urlparse(post_url)
        if parsed.netloc not in ["bsky.app", "staging.bsky.app"]:
            logger.error(f"Invalid Bluesky URL domain: {parsed.netloc}")
            return None

        parts = parsed.path.strip("/").split("/")
        # Expected: profile/<user>/post/<post_id>
        if len(parts) != 4 or parts[0] != "profile" or parts[2] != "post":
            logger.error(f"Invalid URL format: {post_url}")
            return None

        username = parts[1]
        post_id = parts[3]
        logger.info(f"utils:Extracted username={username}, post_id={post_id}")
        return {"username": username, "post_id": post_id, "url": post_url}
    except Exception as e:
        logger.error(f"Error extracting post info from URL {post_url}: {e}")
        return None


def resolve_post_from_url(client, post_url: str) -> Optional[Dict[str, str]]:
    """
    Resolve actual AT URI and CID using the Bluesky API.
    """
    try:
        info = extract_post_info(post_url)
        if not info:
            return None

        username = info["username"]
        post_id = info["post_id"]

        try:
            profile = client.app.bsky.actor.get_profile({"actor": username})
            did = profile.did
        except Exception as e:
            logger.error(f"Failed to resolve DID for {username}: {e}")
            return None

        at_uri = f"at://{did}/app.bsky.feed.post/{post_id}"

        try:
            resp = client.app.bsky.feed.get_posts({"uris": [at_uri]})
            if resp.posts:
                post = resp.posts[0]
                cid = post.cid
                return {"uri": at_uri, "cid": cid, "username": username, "post_id": post_id, "did": did}
        except Exception as e:
            logger.error(f"Failed to fetch post {at_uri}: {e}")
            return None
    except Exception as e:
        logger.error(f"Error resolving post from URL {post_url}: {e}")
        return None


def save_progress(progress_file: str, post_url: str, progress_data: Dict[str, Any]) -> None:
    """Save progress to a JSON file at progress_file."""
    try:
        try:
            with open(progress_file, "r") as f:
                all_progress = json.load(f)
        except (FileNotFoundError, json.JSONDecodeError):
            all_progress = {}

        all_progress[post_url] = progress_data

        with open(progress_file, "w") as f:
            json.dump(all_progress, f, indent=2)

        logger.debug(f"Progress saved for {post_url}")
    except Exception as e:
        logger.error(f"Failed to save progress: {e}")


def load_progress(progress_file: str, post_url: str) -> Dict[str, Any]:
    """Load progress dict for a specific post_url from progress_file."""
    try:
        with open(progress_file, "r") as f:
            all_progress = json.load(f)
        return all_progress.get(post_url, {})
    except (FileNotFoundError, json.JSONDecodeError):
        logger.debug(f"No existing progress found for {post_url}")
        return {}
    except Exception as e:
        logger.error(f"Failed to load progress: {e}")
        return {}


def validate_message_template(template: str) -> bool:
    """Basic validation for a single message template."""
    if not template or not isinstance(template, str):
        return False
    if len(template) > 280:
        return False

    dangerous = [r"<script", r"javascript:", r"data:", r"vbscript:", r"on\w+="]
    for pat in dangerous:
        if re.search(pat, template, re.IGNORECASE):
            return False
    return True


def format_duration(seconds: int) -> str:
    """Turn seconds into a human readable string."""
    if seconds < 60:
        return f"{seconds}s"
    if seconds < 3600:
        return f"{seconds // 60}m {seconds % 60}s"
    hours = seconds // 3600
    minutes = (seconds % 3600) // 60
    return f"{hours}h {minutes}m"
