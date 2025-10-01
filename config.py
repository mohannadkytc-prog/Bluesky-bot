"""
App configuration & writable paths (Render Starter-safe)
"""
import os

# ============ Writable storage ============
# على Render Starter لا يمكن الكتابة تحت /var/...
# نستخدم /tmp/data (ram-disk مؤقت) وهو مسموح الكتابة فيه
DATA_DIR = os.getenv("DATA_DIR", "/tmp/data")
os.makedirs(DATA_DIR, exist_ok=True)

PROGRESS_PATH = os.path.join(DATA_DIR, "progress.json")
TASKS_DIR = os.path.join(DATA_DIR, "tasks")
os.makedirs(TASKS_DIR, exist_ok=True)

# ============ Defaults ============
DEFAULT_MIN_DELAY = int(os.getenv("DEFAULT_MIN_DELAY", "200"))
DEFAULT_MAX_DELAY = int(os.getenv("DEFAULT_MAX_DELAY", "250"))

# ============ Config object ============
from typing import Optional

class Config:
    """Runtime config for the bot"""

    def __init__(
        self,
        bluesky_handle: Optional[str] = None,
        bluesky_password: Optional[str] = None,
        min_delay: Optional[int] = None,
        max_delay: Optional[int] = None,
    ):
        # credentials: من الواجهة أولاً ثم env كاحتياطي
        self.bluesky_handle: Optional[str] = (
            bluesky_handle
            or os.getenv("BLUESKY_HANDLE")
            or os.getenv("BSKY_HANDLE")
        )
        self.bluesky_password: Optional[str] = (
            bluesky_password
            or os.getenv("BLUESKY_PASSWORD")
            or os.getenv("BSKY_PASSWORD")
        )

        # delays
        self.min_delay: int = int(min_delay if min_delay is not None else DEFAULT_MIN_DELAY)
        self.max_delay: int = int(max_delay if max_delay is not None else DEFAULT_MAX_DELAY)

        # networking / retries
        self.api_timeout: int = int(os.getenv("API_TIMEOUT", "30"))
        self.max_retries: int = int(os.getenv("MAX_RETRIES", "3"))

        self._validate()

    def _validate(self):
        if self.min_delay < 0 or self.max_delay < 0:
            raise ValueError("Delay values must be positive")
        if self.min_delay > self.max_delay:
            raise ValueError("min_delay cannot be greater than max_delay")
        if self.api_timeout < 1:
            raise ValueError("API timeout must be >= 1")
        if self.max_retries < 1:
            raise ValueError("MAX_RETRIES must be >= 1")

    def is_valid(self) -> bool:
        return bool(self.bluesky_handle and self.bluesky_password)

    def __repr__(self) -> str:
        return f"Config(handle={self.bluesky_handle}, delay={self.min_delay}-{self.max_delay}s)"
