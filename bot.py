#!/usr/bin/env python3
"""
Always-on bot that runs persistently in the background
Stores tasks and continues running even when interface is closed
"""

import os
import sys
import time
import json
import logging
from datetime import datetime
from threading import Thread, Event
from flask import Flask, request, jsonify, render_template

# Add current directory to path
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from bluesky_bot import BlueSkyBot
from config import Config
from models import init_db, BotRun, TaskConfig, SavedCredentials, db

app = Flask(__name__)
app.secret_key = 'always_on_bot_secret_key_2025'

# Initialize database
init_db(app)

# Global bot state
bot_queue = []
current_task = None
is_processing = False
stop_event = Event()
bot_progress = {
    'current': 0,
    'total': 0,
    'current_post': '-',
    'percentage': 0,
    'last_processed': '',
    'status': 'idle',
    'queue_size': 0,
    'total_reposters': 0,
    'already_processed': 0,
    'remaining': 0,
    'total_mentions_sent': 0,
    'total_posts_processed': 0,
    'session_start_time': datetime.utcnow().isoformat(),
    'current_task_id': None,
    'current_post_index': 0,
    'total_posts_in_task': 0,
    'errors_count': 0,
    'success_rate': 0.0
}

def auto_resume_from_persistence():
    """Automatically resume tasks from saved progress on startup"""
    global bot_queue, current_task, bot_progress, is_processing
    
    try:
        # Try to load from always_on_progress.json
        if os.path.exists('always_on_progress.json'):
            logger.info("🔄 Found saved progress - attempting automatic resume...")
            
            with open('always_on_progress.json', 'r', encoding='utf-8') as f:
                saved_data = json.load(f)
            
            saved_task = saved_data.get('current_task')
            saved_progress = saved_data.get('bot_progress', {})
            
            if saved_task and saved_progress.get('status') in ['processing', 'queued']:
                # Restore the task to the queue
                logger.info(f"📋 Restoring task: {saved_task['id']}")
                logger.info(f"📊 Progress: {saved_progress.get('current', 0)}/{saved_progress.get('total', 0)} ({saved_progress.get('percentage', 0)}%)")
                
                # Add to queue
                bot_queue.append(saved_task)
                
                # Restore progress data
                bot_progress.update({
                    'current': saved_progress.get('current', 0),
                    'total': saved_progress.get('total', 0),
                    'current_post': saved_progress.get('current_post', '1/1'),
                    'percentage': saved_progress.get('percentage', 0),
                    'last_processed': saved_progress.get('last_processed', ''),
                    'status': 'queued',
                    'queue_size': len(bot_queue),
                    'total_reposters': saved_progress.get('total_reposters', 0),
                    'already_processed': saved_progress.get('already_processed', 0),
                    'remaining': saved_progress.get('remaining', 0),
                    'total_mentions_sent': saved_progress.get('total_mentions_sent', 0),
                    'current_task_id': saved_task['id'],
                    'current_post_index': saved_progress.get('current_post_index', 0),
                    'total_posts_in_task': saved_progress.get('total_posts_in_task', 1)
                })
                
                logger.info("✅ Task successfully restored to queue - bot will resume automatically")
                logger.info(f"🎯 Resume point: {bot_progress['last_processed']} (mention #{bot_progress['current']})")
                return True
            else:
                logger.info("ℹ️ No active task found in saved progress")
        else:
            logger.info("ℹ️ No saved progress file found")
            
    except Exception as e:
        logger.error(f"❌ Error during automatic resume: {e}")
    
    return False

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler('always_on_bot.log'),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger(__name__)

@app.route('/')
def index():
    """Web interface for the always-on bot"""
    return render_template('persistent.html')

@app.route('/health')
def health_check():
    """Health check endpoint"""
    return {
        'status': 'healthy',
        'service': 'always-on-bot',
        'is_processing': is_processing,
        'queue_size': len(bot_queue),
        'current_task': current_task is not None
    }, 200

@app.route('/stop_current_task', methods=['POST'])
def stop_current_task():
    """Stop the current running task"""
    global current_task, is_processing, stop_event
    try:
        if current_task:
            logger.info(f"Stopping current task: {current_task['id']}")
            stop_event.set()
            current_task = None
            is_processing = False
            bot_progress['status'] = 'stopped'
            # Clear stop_event after stopping so worker can process next task
            stop_event.clear()
            logger.info("Stop event cleared - worker can now process next queued task")
            return jsonify({'success': True, 'message': 'Task stopped successfully'})
        else:
            return jsonify({'success': False, 'message': 'No active task to stop'})
    except Exception as e:
        logger.error(f"Error stopping task: {e}")
        return jsonify({'error': str(e)}), 500

@app.route('/queue_task', methods=['POST'])
def queue_task():
    """Add a task to the bot queue"""
    try:
        data = request.get_json()
        
        # Extract task data
        post_urls = data.get('post_urls', [])
        message_templates = data.get('message_templates', [])
        bluesky_handle = data.get('bluesky_handle', '')
        bluesky_password = data.get('bluesky_password', '')
        processing_type = data.get('processing_type', 'likers')  # Default to likers
        min_delay = data.get('min_delay', 300)  # Default 5 minutes (300 seconds)
        max_delay = data.get('max_delay', 360)  # Default 6 minutes (360 seconds)
        
        # Validate inputs
        post_urls = [url.strip() for url in post_urls if url.strip()]
        if not post_urls:
            return jsonify({'error': 'At least one post URL is required'}), 400
        
        if not bluesky_handle or not bluesky_password:
            return jsonify({'error': 'Bluesky handle and password are required'}), 400
        
        # Validate timing parameters
        if min_delay < 1 or max_delay < 1:
            return jsonify({'error': 'Minimum and maximum delay must be greater than 0'}), 400
        
        if max_delay < min_delay:
            return jsonify({'error': 'Maximum delay must be greater than or equal to minimum delay'}), 400
        
        # Create task
        task = {
            'id': f"task_{int(time.time())}_{len(bot_queue)}",
            'post_urls': post_urls,
            'message_templates': message_templates,
            'bluesky_handle': bluesky_handle,
            'bluesky_password': bluesky_password,
            'processing_type': processing_type,
            'min_delay': min_delay,
            'max_delay': max_delay,
            'queued_at': datetime.utcnow().isoformat(),
            'status': 'queued'
        }
        
        # Add to queue
        bot_queue.append(task)
        bot_progress['queue_size'] = len(bot_queue)
        
        logger.info(f"Queued task {task['id']} with {len(post_urls)} posts")
        
        # Ensure background worker is running
        start_background_worker()
        
        # Auto-save credentials and task configuration
        user_session = request.headers.get('X-User-Session', 'default_session')
        save_credentials_to_database(user_session, bluesky_handle, bluesky_password, post_urls, message_templates)
        
        return jsonify({
            'success': True,
            'task_id': task['id'],
            'queue_position': len(bot_queue),
            'message': 'Task queued successfully'
        }), 200
        
    except Exception as e:
        logger.error(f"Error queuing task: {e}")
        return jsonify({'error': str(e)}), 500

@app.route('/start_bot', methods=['POST'])
def start_bot():
    """Compatibility endpoint - redirects to queue_task"""
    return queue_task()

@app.route('/status')
def get_status():
    """Get current bot status"""
    return jsonify({
        'is_processing': is_processing,
        'queue_size': len(bot_queue),
        'current_task': current_task['id'] if current_task else None,
        'status': bot_progress['status']
    })

@app.route('/progress')
def get_progress():
    """Get current progress"""
    return jsonify(bot_progress)

@app.route('/detailed_progress')
def detailed_progress():
    """Get detailed progress with statistics"""
    try:
        # Get database statistics
        with app.app_context():
            total_bot_runs = BotRun.query.count()
            completed_runs = BotRun.query.filter_by(status='completed').count()
            failed_runs = BotRun.query.filter_by(status='failed').count()
            
        detailed_stats = {
            **bot_progress,
            'database_stats': {
                'total_bot_runs': total_bot_runs,
                'completed_runs': completed_runs,
                'failed_runs': failed_runs,
                'success_rate': (completed_runs / total_bot_runs * 100) if total_bot_runs > 0 else 0
            },
            'runtime_stats': {
                'session_uptime': (datetime.utcnow() - datetime.fromisoformat(bot_progress['session_start_time'])).total_seconds(),
                'current_task_runtime': 0  # Will be calculated if task is running
            }
        }
        
        return jsonify(detailed_stats)
    except Exception as e:
        return jsonify({'error': str(e)}), 500

# باقي الكود موجود في الملف الأصلي...
# [باقي الوظائف والمسارات]

if __name__ == '__main__':
    # Try to auto-resume on startup
    auto_resume_from_persistence()
    
    # Start background worker
    start_background_worker()
    
    logger.info("Starting Always-On Bot Service on port 5000")
    app.run(host='0.0.0.0', port=5000, debug=False)
  """
Bluesky Bot Implementation
Handles the core logic for fetching reposters and mentioning them
"""

import time
import random
import json
import logging
import re
from typing import List, Dict, Optional
from atproto import Client, client_utils
from atproto.exceptions import AtProtocolError
from utils import extract_post_info, resolve_post_from_url, save_progress, load_progress

class BlueSkyBot:
    """Main bot class for handling Bluesky interactions"""
    
    def __init__(self, config):
        """Initialize the bot with configuration"""
        self.config = config
        self.client = Client()
        self.logger = logging.getLogger(__name__)
        self.progress_file = "progress.json"
        self.bot_run = None
        self.message_counter = 0  # Counter for sequential message selection
        
    def set_bot_run(self, bot_run):
        """Set the database record for this bot run"""
        self.bot_run = bot_run
    
    def update_progress(self, current, total, last_processed="", total_reposters=0, already_processed=0):
        """Update progress - this is a default implementation that can be overridden"""
        # Default implementation just logs progress
        remaining = total - current
        percentage = (current / total * 100) if total > 0 else 0
        
        progress_info = f"Progress: {current}/{total} ({percentage:.1f}%) - Last: {last_processed}"
        if total_reposters > 0:
            progress_info += f" - Total reposters: {total_reposters}"
        
        self.logger.info(progress_info)
        
        # Note: This method is designed to be overridden by the calling code
        # (like always_on_bot) with a proper callback function
    
    def add_message_variations(self, message: str) -> str:
        """Add random variations to make each message unique"""
        # Random emojis as requested by user
        symbols = ['🙏', '💔', '🥺', '🌸', '🍉', '💙', '❤️', '💚', '💖', '🦋', '🌺', '🤎', '🌹']
        dots = ['...', '..', '.', '']
        spaces = [' ', '  ']
        
        # Randomly add symbols
        if random.random() < 0.7:  # 70% chance to add symbol
            symbol = random.choice(symbols)
            message = message.rstrip('.!?') + ' ' + symbol
        
        # Randomly add dots
        if random.random() < 0.5:  # 50% chance to add dots
            dots_choice = random.choice(dots)
            message = message.rstrip('.!?') + dots_choice
        
        # Randomly add extra spaces (subtle variation)
        if random.random() < 0.3:  # 30% chance
            space_choice = random.choice(spaces)
            message = message.replace(' ', space_choice, 1)  # Replace first space
        
        return message
    
    def select_sequential_message(self, message_templates: List[str]) -> str:
        """Select message in sequence and add variations"""
        if not message_templates:
            # Updated message templates as requested by user
            default_messages = [
                "Hope feels distant, but your support can bring it back to life. Every act of kindness restores dignity. The link is the bridge, help us cross it.",
                "I don't ask for pity, I ask for a chance to rise again. Your support is strength, your compassion is survival. The link is how you can make it real.",
                "Many turn away, but I believe in those who still care. Your help can turn despair into hope. The link is the path—please walk it with us.",
                "My truth is shared with courage, not shame. Support given with love lifts more than burdens—it restores faith in humanity. Please visit the link.",
                "A small gift can mean a new beginning. My request is simple: dignity through support. The link holds the power to change a life.",
                "Behind my words is real struggle. Behind your kindness is real impact. Together, we can transform pain into hope. It starts with the link.",
                "Asking for help is not weakness—it is trust in humanity. Your hand can steady me, your support can carry us. The link is where we stand together.",
                "Silence has hidden my pain, but today I speak. I need help to carry this burden with dignity. The link matters, please don't look away.",
                "I don't seek charity, I seek solidarity. Compassion shared is strength multiplied. The link is your chance to stand with us.",
                "Hardship can break, but kindness can rebuild. Your support is not just aid, it is hope reborn. The link is where healing begins."
            ]
            message_templates = default_messages
        
        # Select message in sequence
        base_message = message_templates[self.message_counter % len(message_templates)]
        self.message_counter += 1
        
        # Add variations with random emoji
        varied_message = self.add_message_variations(base_message)
        
        return varied_message
        
    def authenticate(self) -> bool:
        """Authenticate with Bluesky"""
        try:
            self.logger.info("Authenticating with Bluesky...")
            profile = self.client.login(
                self.config.bluesky_handle, 
                self.config.bluesky_password
            )
            self.logger.info(f"Successfully authenticated as {profile.display_name}")
            return True
        except AtProtocolError as e:
            self.logger.error(f"Authentication failed: {e}")
            return False
        except Exception as e:
            self.logger.error(f"Unexpected authentication error: {e}")
            return False
    
    def get_reposters(self, post_uri: str, post_cid: str) -> List[Dict]:
        """Fetch the list of users who reposted the given post"""
        try:
            self.logger.info(f"Fetching reposters for post: {post_uri}")
            
            reposters = []
            cursor = None
            
            while True:
                params = {
                    'uri': post_uri,
                    'cid': post_cid,
                    'limit': 100
                }
                
                if cursor:
                    params['cursor'] = cursor
                
                response = self.client.app.bsky.feed.get_reposted_by(params)
                
                if not response.reposted_by:
                    break
                
                for reposter in response.reposted_by:
                    reposters.append({
                        'handle': reposter.handle,
                        'display_name': reposter.display_name,
                        'did': reposter.did
                    })
                
                # Check if there are more pages
                if hasattr(response, 'cursor') and response.cursor:
                    cursor = response.cursor
                else:
                    break
            
            self.logger.info(f"Found {len(reposters)} reposters")
            return reposters
            
        except AtProtocolError as e:
            self.logger.error(f"Failed to fetch reposters: {e}")
            return []
        except Exception as e:
            self.logger.error(f"Unexpected error fetching reposters: {e}")
            return []
    
    def fetch_latest_post(self, handle: str) -> Dict:
        """Fetch the latest post or reply from a user"""
        try:
            self.logger.info(f"Fetching latest post for @{handle}")
            
            # Get the user's feed (author feed)
            response = self.client.app.bsky.feed.get_author_feed({
                'actor': handle,
                'filter': 'posts_with_replies',
                'includePins': False,
                'limit': 20  # Get recent posts to find the latest one
            })
            
            if not response.feed:
                self.logger.warning(f"No posts found for @{handle}")
                return None
            
            # Find the most recent post (not a repost)
            for item in response.feed:
                post = item.post
                # Skip reposts - we want original posts or replies only
                if hasattr(item, 'reason') and item.reason:
                    continue
                    
                # This is an original post or reply
                post_data = {
                    'uri': post.uri,
                    'cid': post.cid,
                    'text': post.record.text if hasattr(post.record, 'text') else '',
                    'created_at': post.record.created_at if hasattr(post.record, 'created_at') else '',
                    'author_handle': post.author.handle,
                    'author_did': post.author.did
                }
                
                self.logger.info(f"Found latest post for @{handle}: {post_data['uri']}")
                return post_data
                
            self.logger.warning(f"No suitable posts found for @{handle}")
            return None
            
        except AtProtocolError as e:
            self.logger.error(f"Failed to fetch latest post for @{handle}: {e}")
            return None
        except Exception as e:
            self.logger.error(f"Unexpected error fetching latest post for @{handle}: {e}")
            return None
    
    def reply_to_post(self, post_data: Dict, user_data: Dict, message_templates: List[str]) -> bool:
        """Reply to a specific post with a message"""
        try:
            from datetime import datetime, timezone
            
            handle = user_data['handle']
            
            # Select message in sequence and add variations
            selected_message = self.select_sequential_message(message_templates)
            
            self.logger.info(f"Replying to @{handle}'s post: {selected_message}")
            
            # Create the reply with proper Bluesky record format
            reply_data = {
                '$type': 'app.bsky.feed.post',
                'text': selected_message,
                'createdAt': datetime.now(timezone.utc).isoformat(),
                'reply': {
                    'root': {
                        'uri': post_data['uri'],
                        'cid': post_data['cid']
                    },
                    'parent': {
                        'uri': post_data['uri'], 
                        'cid': post_data['cid']
                    }
                }
            }
            
            # Send the reply using the correct API format
            response = self.client.com.atproto.repo.create_record({
                'repo': self.client.me.did,
                'collection': 'app.bsky.feed.post',
                'record': reply_data
            })
            
            self.logger.info(f"Successfully replied to @{handle}'s post")
            return True
            
        except AtProtocolError as e:
            self.logger.error(f"Failed to reply to @{handle}'s post: {e}")
            return False
        except Exception as e:
            self.logger.error(f"Unexpected error replying to @{handle}'s post: {e}")
            return False
    
    def random_delay(self):
        """Wait for a random delay between min and max delay"""
        delay = random.randint(self.config.min_delay, self.config.max_delay)
        self.logger.info(f"Waiting {delay} seconds before next reply...")
        
        # Show countdown
        for remaining in range(delay, 0, -1):
            if remaining % 10 == 0 or remaining <= 10:
                print(f"⏳ {remaining} seconds remaining...")
            time.sleep(1)

    def process_reposters_with_replies(self, post_url: str, message_templates: List[str]) -> bool:
        """Main method to process all reposters with replies to their latest posts"""
        try:
            # Authenticate first
            if not self.authenticate():
                return False
            
            # Extract post information from URL
            post_info = extract_post_info(post_url)
            if not post_info:
                self.logger.error("Failed to extract post information from URL")
                return False
            
            # Resolve the actual post URI and CID using the API
            resolved_info = resolve_post_from_url(self.client, post_url)
            if not resolved_info:
                self.logger.error("Failed to resolve post information from Bluesky API")
                return False
            
            post_uri = resolved_info['uri']
            post_cid = resolved_info['cid']
            
            # Get reposters
            reposters = self.get_reposters(post_uri, post_cid)
            if not reposters:
                self.logger.warning("No reposters found for this post")
                self.update_progress(0, 0, "", 0, 0)
                return True
            
            # Load previous progress
            progress = load_progress(self.progress_file, post_url)
            processed_handles = set(progress.get('processed_handles', []))
            
            # Calculate actual processed count based on real progress
            already_processed = len(processed_handles)  # Real number of processed replies
            
            # Filter out already processed reposters  
            remaining_reposters = [
                r for r in reposters 
                if r['handle'] not in processed_handles
            ]
            
            if not remaining_reposters:
                self.logger.info("All reposters have already been processed")
                return True
            
            total_reposters = len(reposters)
            remaining_count = len(remaining_reposters)
            
            print(f"\n🚀 STARTING REPLY MODE FROM #{already_processed + 1}")
            print(f"   First person to reply to: @{remaining_reposters[0]['handle'] if remaining_reposters else 'None'}")
            
            print(f"\n📊 Processing Summary:")
            print(f"   Total reposters found: {total_reposters}")
            print(f"   Already processed: {already_processed}")
            print(f"   Remaining to process: {remaining_count}")
            print(f"   Progress: {already_processed}/{total_reposters} ({(already_processed/total_reposters*100):.1f}%)")
            
            # Update initial progress
            self.update_progress(already_processed, total_reposters, "", total_reposters, already_processed)
            
            if remaining_count == 0:
                print(f"\n🎉 All reposters have been processed!")
                self.update_progress(total_reposters, total_reposters, "All completed", total_reposters, total_reposters)
                return True
            
            # Process each remaining reposter with reply system
            for i, reposter in enumerate(remaining_reposters, 1):
                handle = reposter['handle']
                display_name = reposter['display_name']
                current_total = already_processed + i
                
                print(f"\n[{current_total}/{total_reposters}] Processing @{handle} ({display_name})")
                print(f"   📈 Progress: {current_total}/{total_reposters} ({(current_total/total_reposters*100):.1f}%)")
                print(f"   ⏳ Remaining: {total_reposters - current_total}")
                
                # Fetch user's latest post
                latest_post = self.fetch_latest_post(handle)
                
                if not latest_post:
                    print(f"⚠️ No recent posts found for @{handle}, skipping...")
                    # Still mark as processed to avoid retrying
                    processed_handles.add(handle)
                    progress['processed_handles'] = list(processed_handles)
                    progress['last_processed'] = handle
                    progress['total_processed'] = len(processed_handles)
                    save_progress(self.progress_file, post_url, progress)
                    continue
                
                # Reply to their latest post instead of mentioning
                success = self.reply_to_post(latest_post, reposter, message_templates)
                
                if success:
                    # Update progress 
                    processed_handles.add(handle)
                    progress['processed_handles'] = list(processed_handles)
                    progress['last_processed'] = handle
                    progress['total_processed'] = len(processed_handles)
                    progress['last_replied_post'] = latest_post['uri']  # Track last replied post
                    
                    save_progress(self.progress_file, post_url, progress)
                    
                    # Update web interface progress
                    current_total = already_processed + i
                    self.update_progress(current_total, total_reposters, f"@{handle}", total_reposters, already_processed)
                    
                    print(f"✅ Successfully replied to @{handle}'s post")
                    
                    # Add delay between replies (except for the last one)
                    if i < remaining_count:
                        self.random_delay()
                else:
                    print(f"❌ Failed to reply to @{handle}'s post")
                    
                    # Ask user if they want to continue
                    if i < remaining_count:
                        continue_choice = input("Continue with next reposter? (y/N): ").strip().lower()
                        if continue_choice != 'y':
                            print("Stopping process as requested.")
                            return False
            
            print(f"\n🎉 Successfully processed all reposters with replies!")
            return True
            
        except Exception as e:
            self.logger.error(f"Error processing reposters with replies: {e}")
            return False
          """
Configuration management for the Bluesky bot
"""

import os
from typing import Optional

class Config:
    """Configuration class for bot settings"""
    
    def __init__(self, bluesky_handle: Optional[str] = None, bluesky_password: Optional[str] = None):
        """Initialize configuration from parameters or environment variables"""
        
        # Bluesky credentials - use provided values or fall back to environment
        self.bluesky_handle: Optional[str] = bluesky_handle or os.getenv('BLUESKY_HANDLE')
        self.bluesky_password: Optional[str] = bluesky_password or os.getenv('BLUESKY_PASSWORD')
        
        # Timing configuration - 3-5 minutes
        self.min_delay: int = int(os.getenv('MIN_DELAY', '180'))  # 180 seconds (3 minutes)
        self.max_delay: int = int(os.getenv('MAX_DELAY', '300'))  # 300 seconds (5 minutes)
        
        # API configuration
        self.api_timeout: int = int(os.getenv('API_TIMEOUT', '30'))  # 30 seconds
        self.max_retries: int = int(os.getenv('MAX_RETRIES', '3'))  # 3 retries
        
        # Validation
        self._validate_config()
    
    def _validate_config(self):
        """Validate configuration values"""
        if self.min_delay < 0 or self.max_delay < 0:
            raise ValueError("Delay values must be positive")
        
        if self.min_delay > self.max_delay:
            raise ValueError("Minimum delay cannot be greater than maximum delay")
        
        if self.api_timeout < 1:
            raise ValueError("API timeout must be at least 1 second")
        
        if self.max_retries < 1:
            raise ValueError("Max retries must be at least 1")
    
    def is_valid(self) -> bool:
        """Check if the configuration is valid for running the bot"""
        return bool(self.bluesky_handle and self.bluesky_password)
    
    def __str__(self) -> str:
        """String representation of configuration (without sensitive data)"""
        return f"Config(handle={self.bluesky_handle}, delay={self.min_delay}-{self.max_delay}s)"
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
      """
Database models for the Bluesky bot
"""

import os
import json
from datetime import datetime
from flask import Flask
from flask_sqlalchemy import SQLAlchemy
from sqlalchemy.orm import DeclarativeBase


class Base(DeclarativeBase):
    pass


db = SQLAlchemy(model_class=Base)


class BotRun(db.Model):
    """Track each bot run session"""
    __tablename__ = 'bot_runs'
    
    id = db.Column(db.Integer, primary_key=True)
    post_url = db.Column(db.String(500), nullable=False)
    message_template = db.Column(db.Text, nullable=False)
    bluesky_handle = db.Column(db.String(100), nullable=False)
    status = db.Column(db.String(20), default='running')  # running, completed, failed
    total_reposters = db.Column(db.Integer, default=0)
    processed_count = db.Column(db.Integer, default=0)
    started_at = db.Column(db.DateTime, default=datetime.utcnow)
    completed_at = db.Column(db.DateTime, nullable=True)
    error_message = db.Column(db.Text, nullable=True)
    
    # Relationships
    mentions = db.relationship('Mention', backref='bot_run', lazy=True, cascade='all, delete-orphan')
    reposters = db.relationship('Reposter', backref='bot_run', lazy=True, cascade='all, delete-orphan')


class Reposter(db.Model):
    """Track users who reposted"""
    __tablename__ = 'reposters'
    
    id = db.Column(db.Integer, primary_key=True)
    bot_run_id = db.Column(db.Integer, db.ForeignKey('bot_runs.id'), nullable=False)
    handle = db.Column(db.String(100), nullable=False)
    display_name = db.Column(db.String(200), nullable=True)
    did = db.Column(db.String(200), nullable=False)
    found_at = db.Column(db.DateTime, default=datetime.utcnow)
    
    # Relationships
    mentions = db.relationship('Mention', backref='reposter', lazy=True)


class Mention(db.Model):
    """Track mentions sent to users"""
    __tablename__ = 'mentions'
    
    id = db.Column(db.Integer, primary_key=True)
    bot_run_id = db.Column(db.Integer, db.ForeignKey('bot_runs.id'), nullable=False)
    reposter_id = db.Column(db.Integer, db.ForeignKey('reposters.id'), nullable=False)
    handle = db.Column(db.String(100), nullable=False)
    message_sent = db.Column(db.Text, nullable=False)
    status = db.Column(db.String(20), default='pending')  # pending, sent, failed
    post_uri = db.Column(db.String(500), nullable=True)  # Bluesky post URI
    sent_at = db.Column(db.DateTime, default=datetime.utcnow)
    error_message = db.Column(db.Text, nullable=True)


class TaskConfig(db.Model):
    """Store complete task configurations for resumption"""
    __tablename__ = 'task_configs'
    
    id = db.Column(db.Integer, primary_key=True)
    task_id = db.Column(db.String(100), nullable=False, unique=True)
    bluesky_handle = db.Column(db.String(100), nullable=False)
    bluesky_password = db.Column(db.String(200), nullable=False)
    post_urls = db.Column(db.Text, nullable=False)  # JSON array of URLs
    message_templates = db.Column(db.Text, nullable=False)  # JSON array of templates
    current_post_index = db.Column(db.Integer, default=0)
    total_posts = db.Column(db.Integer, default=0)
    current_progress = db.Column(db.Integer, default=0)
    total_reposters = db.Column(db.Integer, default=0)
    last_processed_handle = db.Column(db.String(100), nullable=True)
    status = db.Column(db.String(20), default='queued')  # queued, processing, paused, completed, failed
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
    completed_at = db.Column(db.DateTime, nullable=True)
    
    def to_dict(self):
        """Convert task config to dictionary for JSON serialization"""
        return {
            'task_id': self.task_id,
            'bluesky_handle': self.bluesky_handle,
            'bluesky_password': self.bluesky_password,
            'post_urls': json.loads(self.post_urls) if self.post_urls else [],
            'message_templates': json.loads(self.message_templates) if self.message_templates else [],
            'current_post_index': self.current_post_index,
            'total_posts': self.total_posts,
            'current_progress': self.current_progress,
            'total_reposters': self.total_reposters,
            'last_processed_handle': self.last_processed_handle,
            'status': self.status,
            'created_at': self.created_at.isoformat() if self.created_at else None,
            'updated_at': self.updated_at.isoformat() if self.updated_at else None,
            'completed_at': self.completed_at.isoformat() if self.completed_at else None
        }


class SavedCredentials(db.Model):
    """Store user credentials and preferences for auto-loading"""
    __tablename__ = 'saved_credentials'
    
    id = db.Column(db.Integer, primary_key=True)
    user_session = db.Column(db.String(100), nullable=False)  # Browser session identifier
    bluesky_handle = db.Column(db.String(100), nullable=False)
    bluesky_password = db.Column(db.String(200), nullable=False)
    default_post_urls = db.Column(db.Text, nullable=True)  # JSON array of frequently used URLs
    default_message_templates = db.Column(db.Text, nullable=True)  # JSON array of templates
    last_used_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    
    def to_dict(self):
        """Convert credentials to dictionary for JSON serialization"""
        return {
            'user_session': self.user_session,
            'bluesky_handle': self.bluesky_handle,
            'bluesky_password': self.bluesky_password,
            'default_post_urls': json.loads(self.default_post_urls) if self.default_post_urls else [],
            'default_message_templates': json.loads(self.default_message_templates) if self.default_message_templates else [],
            'last_used_at': self.last_used_at.isoformat() if self.last_used_at else None,
            'created_at': self.created_at.isoformat() if self.created_at else None
        }


def init_db(app: Flask):
    """Initialize database with Flask app"""
    app.config["SQLALCHEMY_DATABASE_URI"] = os.environ.get("DATABASE_URL")
    app.config["SQLALCHEMY_ENGINE_OPTIONS"] = {
        "pool_recycle": 300,
        "pool_pre_ping": True,
    }
    
    db.init_app(app)
    
    with app.app_context():
        db.create_all()
        
    return db
<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>🤖 Persistent Bluesky Bot</title>
    <style>
        /* CSS styles for the interface */
        * {
            margin: 0;
            padding: 0;
            box-sizing: border-box;
        }
        
        body {
            font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif;
            background: linear-gradient(135deg, #667eea 0%, #764ba2 100%);
            min-height: 100vh;
            padding: 20px;
            color: #333;
        }
        
        .container {
            max-width: 800px;
            margin: 0 auto;
            background: rgba(255, 255, 255, 0.95);
            border-radius: 20px;
            padding: 40px;
            box-shadow: 0 20px 40px rgba(0,0,0,0.1);
        }
        
        /* باقي الأنماط... */
    </style>
</head>
<body>
    <div class="container">
        <div class="header">
            <h1>🤖 Persistent Bluesky Bot</h1>
            <p>يعمل بشكل مستمر في الخلفية، حتى عند إغلاق الصفحة</p>
        </div>
        
        <!-- باقي محتوى واجهة المستخدم... -->
        
        <form id="botForm">
            <!-- نموذج إدخال البيانات -->
            <div class="form-group">
                <label for="bluesky_handle">حساب بلوسكاي</label>
                <input type="text" id="bluesky_handle" name="bluesky_handle" 
                       placeholder="StillAlive15.bsky.social" value="StillAlive15.bsky.social">
            </div>
            
            <div class="form-group">
                <label for="bluesky_password">كلمة المرور</label>
                <input type="password" id="bluesky_password" name="bluesky_password" 
                       placeholder="Your app password" value="2bgo-7sdw-ovnk-wh7j">
            </div>
            
            <!-- باقي الحقول... -->
        </form>
    </div>

    <script>
        // JavaScript للتحكم في البوت
        function queueTask() {
            // إرسال المهمة للبوت
            fetch('/queue_task', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({
                    post_urls: [document.getElementById('post_url_1').value],
                    message_templates: getMessageTemplates(),
                    bluesky_handle: document.getElementById('bluesky_handle').value,
                    bluesky_password: document.getElementById('bluesky_password').value,
                    processing_type: 'reposts',
                    min_delay: 180,
                    max_delay: 300
                })
            })
            .then(response => response.json())
            .then(data => {
                console.log('Task queued:', data);
            });
        }
        
        // تحديث الحالة كل 5 ثوان
        setInterval(updateStatus, 5000);
        
        function updateStatus() {
            fetch('/detailed_progress')
                .then(response => response.json())
                .then(data => {
                    // تحديث واجهة المستخدم بالتقدم
                    document.getElementById('currentProgress').textContent = 
                        `${data.current}/${data.total} (${data.percentage}%)`;
                });
        }
    </script>
</body>
</html>
{
  "https://bsky.app/profile/cara.city/post/3lzobwjgyfs27": {
    "processed_handles": [
      "keliali.bsky.social",
      "zet237.bsky.social",
      "nishanetta.bsky.social",
      "backlitgalaxy.bsky.social",
      "kazanmasilverwind.bsky.social"
    ],
    "last_processed": "kazanmasilverwind.bsky.social",
    "total_processed": 101
  }
}
{
  "timestamp": "2025-09-29T17:11:53.200640",
  "bot_progress": {
    "current": 100,
    "total": 726,
    "current_post": "1/1",
    "percentage": 13,
    "last_processed": "@2amwakeupcall.com",
    "status": "processing",
    "queue_size": 0,
    "total_reposters": 726,
    "already_processed": 98,
    "remaining": 626,
    "total_mentions_sent": 8528,
    "current_task_id": "task_1759165610_0"
  },
  "current_task": {
    "id": "task_1759165610_0",
    "post_urls": ["https://bsky.app/profile/cara.city/post/3lzobwjgyfs27"],
    "message_templates": [
      "Hope feels distant, but your support can bring it back to life...",
      "I don't ask for pity, I ask for a chance to rise again...",
      "Many turn away, but I believe in those who still care..."
    ],
    "bluesky_handle": "StillAlive15.bsky.social",
    "bluesky_password": "2bgo-7sdw-ovnk-wh7j",
    "processing_type": "reposts",
    "min_delay": 180,
    "max_delay": 300
  }
}
pip install atproto flask flask-sqlalchemy
python always_on_bot.py
