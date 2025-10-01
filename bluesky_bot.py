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
