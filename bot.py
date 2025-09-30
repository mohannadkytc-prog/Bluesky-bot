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
    app.run(host='0.0.0.0', port=5000, debug=False
            
            



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
