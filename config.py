"""Runtime paths & defaults (works on /data if present, otherwise /tmp)."""
import os
from typing import Optional

# استخدم /data إذا كان موجود (خطة مدفوعة مع Disk)، وإلا /tmp (Starter)
DATA_DIR = "/data" if os.path.exists("/data") else "/tmp"
os.makedirs(DATA_DIR, exist_ok=True)

# مسارات الملفات/المجلدات
PROGRESS_PATH = os.path.join(DATA_DIR, "progress.json")
TASKS_DIR = os.path.join(DATA_DIR, "tasks")
os.makedirs(TASKS_DIR, exist_ok=True)

# قِيم افتراضية للتأخير (يمكن تغييرها من الواجهة أو Env)
DEFAULT_MIN_DELAY = int(os.getenv("DEFAULT_MIN_DELAY", "200"))
DEFAULT_MAX_DELAY = int(os.getenv("DEFAULT_MAX_DELAY", "250"))

class Config:
    """Configuration class for bot settings (credentials & timing)."""

    def __init__(
        self,
        bluesky_handle: Optional[str] = None,
        bluesky_password: Optional[str] = None,
        min_delay: Optional[int] = None,
        max_delay: Optional[int] = None,
    ):
        # بيانات الدخول: إما من الواجهة أو من متغيرات البيئة
        self.bluesky_handle: Optional[str] = (
            bluesky_handle or os.getenv("BLUESKY_HANDLE") or os.getenv("BSKY_HANDLE")
        )
        self.bluesky_password: Optional[str] = (
            bluesky_password or os.getenv("BLUESKY_PASSWORD") or os.getenv("BSKY_PASSWORD")
        )

        # التأخيرات: من الواجهة أو الافتراضي
        self.min_delay: int = int(min_delay if min_delay is not None else DEFAULT_MIN_DELAY)
        self.max_delay: int = int(max_delay if max_delay is not None else DEFAULT_MAX_DELAY)

        # إعدادات إضافية عامة
        self.api_timeout: int = int(os.getenv("API_TIMEOUT", "30"))
        self.max_retries: int = int(os.getenv("MAX_RETRIES", "3"))

        self._validate_config()

    def _validate_config(self):
        if self.min_delay < 0 or self.max_delay < 0:
            raise ValueError("Delay values must be positive")
        if self.min_delay > self.max_delay:
            raise ValueError("Minimum delay cannot be greater than maximum delay")
        if self.api_timeout < 1:
            raise ValueError("API timeout must be at least 1 second")
        if self.max_retries < 1:
            raise ValueError("Max retries must be at least 1")

    def is_valid(self) -> bool:
        return bool(self.bluesky_handle and self.bluesky_password)

    def __str__(self) -> str:
        return (
            f"Config(handle={self.bluesky_handle}, "
            f"delay={self.min_delay}-{self.max_delay}s, data_dir={DATA_DIR})"
        )
