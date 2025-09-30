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

# تأكدي أن هذه الاستيرادات موجودة لديك
from bluesky_bot import BlueSkyBot
from config import Config
from models import init_db, BotRun, TaskConfig, SavedCredentials, db


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

app = Flask(__name__)
app.secret_key = 'always_on_bot_secret_key_2025'

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


# ----------------------------------------------------------------------
# 🚀 الدوال الأساسية
# ----------------------------------------------------------------------

worker_thread = None

def bot_worker_loop():
    """The main loop that processes tasks from the queue"""
    global bot_queue, current_task, is_processing, bot_progress, stop_event
    logger.info("Worker loop started.")
    while True:
        # Check for stop event
        if stop_event.is_set():
            time.sleep(1)
            continue
            
        # Check for empty queue
        if not bot_queue:
            bot_progress['status'] = 'idle'
            time.sleep(5)
            continue
            
        # 🚀 بدء معالجة المهمة الفعلية
        current_task = bot_queue.pop(0)
        is_processing = True
        bot_progress['status'] = 'processing'
        bot_progress['current_task_id'] = current_task['id']
        
        try:
            logger.info(f"Starting actual task processing: {current_task['id']}")
            
            # 1. تهيئة البوت وتسجيل الدخول
            # ⚠️ التعديل الأخير: تم حذف app_context=app لتجنب خطأ الانهيار
            bot = BlueSkyBot(
                current_task['bluesky_handle'],
                current_task['bluesky_password']
            )
            
            # 2. تشغيل المهمة الفعلية (هذا هو كود المعالجة الخاص بكِ)
            # يجب أن يكون الكود هنا مثل: bot.run_task(current_task, bot_progress, stop_event) 
            
            logger.info("Executing main bot logic (Placeholder/Actual logic)")
            time.sleep(15) # محاكاة عمل البوت
            
            # 3. إنهاء المهمة وتحديث الحالة
            bot_progress['status'] = 'completed'
            logger.info(f"Task {current_task['id']} completed successfully.")
            
        except Exception as e:
            bot_progress['status'] = 'failed'
            logger.error(f"❌ Critical error during task {current_task['id']}: {e}")
        
        finally:
            current_task = None
            is_processing = False
            bot_progress['queue_size'] = len(bot_queue)
            stop_event.clear() # جاهز للتعامل مع مهمة جديدة

def start_background_worker():
    """Starts the worker thread if it's not already running"""
    global worker_thread
    if worker_thread is None or not worker_thread.is_alive():
        logger.info("Starting background worker thread...")
        worker_thread = Thread(target=bot_worker_loop, daemon=True)
        worker_thread.start()
        logger.info("Background worker thread started successfully.")

def save_credentials_to_database(user_session, bluesky_handle, bluesky_password, post_urls, message_templates):
    """Placeholder for saving credentials function"""
    pass

# ⚠️ التعديل الحاسم: دالة الاستئناف التلقائي معطلة بالكامل
def auto_resume_from_persistence():
    """(معطلة) Automatically resume tasks from saved progress on startup"""
    pass 
    
# ----------------------------------------------------------------------
# التهيئة وبدء التشغيل 
# ----------------------------------------------------------------------

# Initialize database
init_db(app)

# 🚀 **بدء تشغيل العامل الخلفي (تم الترتيب ليتجنب NameError)**
start_background_worker() 

# ----------------------------------------------------------------------
# الدوال الأساسية لبقية التطبيق (مسارات الواجهة API)
# ----------------------------------------------------------------------

@app.route('/')
def index():
    """Web interface for the always-on bot"""
    return render_template('persistent.html')

@app.route('/health')
def health_check():
    """Health check endpoint"""
    return jsonify({
        'status': 'healthy',
        'service': 'always-on-bot',
        'is_processing': is_processing,
        'queue_size': len(bot_queue),
        'current_task': current_task['id'] if current_task else None
    })

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
        processing_type = data.get('processing_type', 'likers')
        min_delay = data.get('min_delay', 300)
        max_delay = data.get('max_delay', 360)
        
        # Validation
        post_urls = [url.strip() for url in post_urls if url.strip()]
        if not post_urls or not bluesky_handle or not bluesky_password or max_delay < min_delay:
             return jsonify({'error': 'Invalid input parameters'}), 400
        
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
        
        # Auto-save credentials (placeholder)
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
                'current_task_runtime': 0
            }
        }
        
        return jsonify(detailed_stats)
    except Exception as e:
        return jsonify({'error': str(e)}), 500
        
# 🛑 **الجزء المحذوف:** تم حذف الأسطر التي تبدأ بـ `if __name__ == '__main__':`
# ----------------------------------------------------------------------
