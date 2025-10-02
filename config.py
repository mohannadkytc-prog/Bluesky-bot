"""Runtime paths & defaults (works on /data if present, otherwise /tmp)."""
import os

# استخدم /data إذا كان موجود (Starter/مدفوع مع Disk)، وإلا /tmp
DATA_DIR = "/data" if os.path.exists("/data") else "/tmp"
os.makedirs(DATA_DIR, exist_ok=True)

# مسارات الملفات
PROGRESS_PATH = os.path.join(DATA_DIR, "progress.json")

# قِيم افتراضية للتأخير (من الواجهة أو Env)
DEFAULT_MIN_DELAY = int(os.getenv("DEFAULT_MIN_DELAY", "200"))
DEFAULT_MAX_DELAY = int(os.getenv("DEFAULT_MAX_DELAY", "250"))

from typing import Optional

class Config:
    """Configuration class for bot settings (credentials & timing)."""

    def __init__(
        self,
        bluesky_handle: Optional[str] = None,
        bluesky_password: Optional[str] = None,
        min_delay: Optional[int] = None,
        max_delay: Optional[int] = None,
    ):
        self.bluesky_handle: Optional[str] = bluesky_handle or os.getenv("BLUESKY_HANDLE")
        self.bluesky_password: Optional[str] = bluesky_password or os.getenv("BLUESKY_PASSWORD")

        self.min_delay: int = int(min_delay if min_delay is not None else DEFAULT_MIN_DELAY)
        self.max_delay: int = int(max_delay if max_delay is not None else DEFAULT_MAX_DELAY)

        self.api_timeout: int = int(os.getenv("API_TIMEOUT", "30"))
        self.max_retries: int = int(os.getenv("MAX_RETRIES", "3"))

    def is_valid(self) -> bool:
        return bool(self.bluesky_handle and self.bluesky_password)

    def __str__(self) -> str:
        return f"Config(handle={self.bluesky_handle}, delay={self.min_delay}-{self.max_delay}s, data_dir={DATA_DIR})"
