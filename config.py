"""Runtime paths & configuration (DATA_DIR fallback to /tmp)."""
import os
from typing import Optional

# ===== مسار التخزين: /data إن وُجد، وإلا /tmp =====
DATA_DIR = "/data" if os.path.exists("/data") else "/tmp"
os.makedirs(DATA_DIR, exist_ok=True)

PROGRESS_PATH = os.path.join(DATA_DIR, "progress.json")
TASKS_DIR = os.path.join(DATA_DIR, "tasks")
os.makedirs(TASKS_DIR, exist_ok=True)

# تأخيرات افتراضية (تتغير من الواجهة)
DEFAULT_MIN_DELAY = int(os.getenv("DEFAULT_MIN_DELAY", "200"))
DEFAULT_MAX_DELAY = int(os.getenv("DEFAULT_MAX_DELAY", "250"))

# نوع المعالجة الافتراضي من البيئة (likers أو reposters)
DEFAULT_PROCESSING = os.getenv("DEFAULT_PROCESSING", "likers").lower()


class Config:
    """إعدادات الاعتماد والتأخير ونوع المعالجة."""

    def __init__(
        self,
        bluesky_handle: Optional[str] = None,
        bluesky_password: Optional[str] = None,
        min_delay: Optional[int] = None,
        max_delay: Optional[int] = None,
        processing_type: Optional[str] = None,  # "likers" | "reposters"
    ):
        self.bluesky_handle = (
            bluesky_handle or os.getenv("BLUESKY_HANDLE") or os.getenv("BSKY_HANDLE")
        )
        self.bluesky_password = (
            bluesky_password
            or os.getenv("BLUESKY_PASSWORD")
            or os.getenv("BSKY_PASSWORD")
        )

        self.min_delay: int = int(min_delay if min_delay is not None else DEFAULT_MIN_DELAY)
        self.max_delay: int = int(max_delay if max_delay is not None else DEFAULT_MAX_DELAY)

        self.processing_type: str = (processing_type or DEFAULT_PROCESSING).lower()
        if self.processing_type not in {"likers", "reposters"}:
            self.processing_type = "likers"

        self.api_timeout: int = int(os.getenv("API_TIMEOUT", "30"))
        self.max_retries: int = int(os.getenv("MAX_RETRIES", "3"))

        self._validate()

    def _validate(self) -> None:
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
        return f"Config(handle={self.bluesky_handle}, delay={self.min_delay}-{self.max_delay}, processing={self.processing_type}, data_dir={DATA_DIR})"
