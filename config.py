"""Runtime paths & defaults (works on /data if present, otherwise /tmp)."""
import os
from typing import Optional

# استخدم /data إن وُجد (خطة مدفوعة مع Disk)، وإلا /tmp (Starter)
DATA_DIR = "/data" if os.path.exists("/data") else "/tmp"
os.makedirs(DATA_DIR, exist_ok=True)

# مسارات التخزين
PROGRESS_PATH = os.path.join(DATA_DIR, "progress.json")
TASKS_DIR = os.path.join(DATA_DIR, "tasks")
os.makedirs(TASKS_DIR, exist_ok=True)

# قِيم افتراضية للتأخير
DEFAULT_MIN_DELAY = int(os.getenv("DEFAULT_MIN_DELAY", "200"))
DEFAULT_MAX_DELAY = int(os.getenv("DEFAULT_MAX_DELAY", "250"))


class Config:
    """إعدادات البوت (بيانات الدخول + التأخير + نوع المعالجة)"""

    def __init__(
        self,
        bluesky_handle: Optional[str] = None,
        bluesky_password: Optional[str] = None,
        min_delay: Optional[int] = None,
        max_delay: Optional[int] = None,
        processing_mode: Optional[str] = None,  # LIKES أو REPOSTS
    ):
        self.bluesky_handle: Optional[str] = (
            bluesky_handle or os.getenv("BLUESKY_HANDLE") or os.getenv("BSKY_HANDLE")
        )
        self.bluesky_password: Optional[str] = (
            bluesky_password or os.getenv("BLUESKY_PASSWORD") or os.getenv("BSKY_PASSWORD")
        )

        self.min_delay: int = int(min_delay if min_delay is not None else DEFAULT_MIN_DELAY)
        self.max_delay: int = int(max_delay if max_delay is not None else DEFAULT_MAX_DELAY)

        # LIKES أو REPOSTS (افتراضي LIKES)
        self.processing_mode: str = (processing_mode or os.getenv("PROCESSING_MODE", "LIKES")).upper()

        self.api_timeout: int = int(os.getenv("API_TIMEOUT", "30"))
        self.max_retries: int = int(os.getenv("MAX_RETRIES", "3"))

        self._validate_config()

    def _validate_config(self):
        if self.min_delay < 0 or self.max_delay < 0:
            raise ValueError("Delay values must be positive")
        if self.min_delay > self.max_delay:
            raise ValueError("Minimum delay cannot be greater than maximum delay")
        if self.processing_mode not in ("LIKES", "REPOSTS"):
            raise ValueError("Processing mode must be either 'LIKES' or 'REPOSTS'")

    def is_valid(self) -> bool:
        return bool(self.bluesky_handle and self.bluesky_password)

    def __str__(self) -> str:
        return f"Config(handle={self.bluesky_handle}, mode={self.processing_mode}, delay={self.min_delay}-{self.max_delay}s, data_dir={os.path.dirname(PROGRESS_PATH)})"
