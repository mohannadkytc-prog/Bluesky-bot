import json
import os
import re
from typing import Dict, Any, Optional
from urllib.parse import urlparse

from config import PROGRESS_PATH

# ---------- Progress helpers ----------
def load_progress(path: str = PROGRESS_PATH) -> Dict[str, Any]:
    """تحميل حالة التقدم من ملف JSON."""
    if not os.path.exists(path):
        return {}
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}

def save_progress(data: Dict[str, Any], path: str = PROGRESS_PATH):
    """حفظ حالة التقدم إلى ملف JSON."""
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)

def save_progress_for_key(key: str, value: Any, path: str = PROGRESS_PATH):
    """حفظ قيمة محددة (key) في ملف التقدم."""
    progress = load_progress(path)
    progress[key] = value
    save_progress(progress, path)

# ---------- URL helpers ----------
def extract_post_info(post_url: str) -> Optional[Dict[str, str]]:
    """
    يتحقق أن الرابط من bsky.app ويستخرج: username و post_id
    مثال: https://bsky.app/profile/handle.bsky.social/post/3k4duaz5vfs2b
    """
    try:
        parsed = urlparse(post_url)
        if parsed.netloc not in ("bsky.app", "staging.bsky.app"):
            return None
        parts = parsed.path.strip("/").split("/")
        if len(parts) != 4 or parts[0] != "profile" or parts[2] != "post":
            return None
        return {"username": parts[1], "post_id": parts[3]}
    except Exception:
        return None

# ---------- Validation ----------
def validate_message_template(template: str) -> bool:
    if not template or not isinstance(template, str):
        return False
    if len(template) > 280:
        return False
    # منع سكربتات
    dangerous = [r"<script", r"javascript:", r"data:", r"vbscript:", r"on\w+="]
    return not any(re.search(p, template, re.IGNORECASE) for p in dangerous)
