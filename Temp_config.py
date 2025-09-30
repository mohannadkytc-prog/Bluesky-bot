""" Configuration management for the Bluesky bot """
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
