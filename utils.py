"""
Utility functions for the Bluesky bot
"""

import re
import json
import logging
from typing import Optional, Dict, Any
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

def save_progress(progress_file: str, post_url: str, progress_data: Dict[str, Any]):
    """Save progress to a JSON file"""
    logger = logging.getLogger(__name__)
    
    try:
        # Load existing progress
        try:
            with open(progress_file, 'r') as f:
                all_progress = json.load(f)
        except (FileNotFoundError, json.JSONDecodeError):
            all_progress = {}
        
        # Update progress for this post
        all_progress[post_url] = progress_data
        
        # Save back to file
        with open(progress_file, 'w') as f:
            json.dump(all_progress, f, indent=2)
        
        logger.debug(f"Progress saved for {post_url}")
        
    except Exception as e:
        logger.error(f"Failed to save progress: {e}")

def load_progress(progress_file: str, post_url: str) -> Dict[str, Any]:
    """Load progress from a JSON file"""
    logger = logging.getLogger(__name__)
    
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
