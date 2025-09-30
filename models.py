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
