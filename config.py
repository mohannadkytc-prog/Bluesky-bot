import os

# مسار التخزين الآمن في Render (var/data)
DATA_DIR = os.getenv("DATA_DIR", "/var/data")
os.makedirs(DATA_DIR, exist_ok=True)

# ملف/مجلدات التقدم والمهام
PROGRESS_PATH = os.path.join(DATA_DIR, "progress.json")
TASKS_DIR = os.path.join(DATA_DIR, "tasks")
os.makedirs(TASKS_DIR, exist_ok=True)

# قيَم افتراضية للتأخير (يمكن تغييرها من الواجهة أو env)
DEFAULT_MIN_DELAY = int(os.getenv("DEFAULT_MIN_DELAY", "200"))
DEFAULT_MAX_DELAY = int(os.getenv("DEFAULT_MAX_DELAY", "250"))
