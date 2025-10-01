import os, time, random, threading, logging
from typing import List, Optional, Dict
from flask import Flask, render_template, request, jsonify
from atproto import Client
from atproto.exceptions import AtProtocolError

from config import Config, PROGRESS_PATH
from utils import (
    resolve_post_from_url,
    get_likers,
    get_reposters,
    get_latest_post,
    reply_to_post,
    save_progress,
    load_progress,
)

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("bluesky-bot")

app = Flask(__name__)

# حالة الخدمة
runtime_stats = {"status": "Idle", "current_task": None, "session_uptime": "0s"}
bot_progress = {"total": 0, "done": 0, "failed": 0, "success_rate": 0.0}
_worker: Optional[threading.Thread] = None

def _update_progress():
    total = bot_progress["total"]
    done = bot_progress["done"]
    failed = bot_progress["failed"]
    bot_progress["success_rate"] = (done / total) if total else 0.0

def _login(client: Client, cfg: Config):
    log.info(f"🔑 Logging in as {cfg.bluesky_handle}")
    client.login(cfg.bluesky_handle, cfg.bluesky_password)

def _process(cfg: Config, post_url: str, messages: List[str]):
    start = time.time()
    runtime_stats["status"] = "Running"
    runtime_stats["current_task"] = "Processing audience"

    client = Client()
    _login(client, cfg)

    ref = resolve_post_from_url(client, post_url)
    if not ref:
        log.error("❌ Failed to resolve post url")
        runtime_stats["status"] = "Idle"
        runtime_stats["current_task"] = None
        return

    uri, cid = ref["uri"], ref["cid"]

    # 1) audience ordered
    if cfg.processing_mode == "LIKES":
        audience = get_likers(client, uri)
    else:
        audience = get_reposters(client, uri)

    bot_progress["total"] = len(audience)
    bot_progress["done"] = 0
    bot_progress["failed"] = 0
    _update_progress()
    log.info(f"🎯 Audience size: {len(audience)} [{cfg.processing_mode}]")

    # 2) loop users (top to bottom)
    for handle in audience:
        try:
            latest = get_latest_post(client, handle)
            if not latest:
                log.warning(f"⏭️ No latest post for @{handle}")
                bot_progress["failed"] += 1
                _update_progress()
                continue

            parent_uri, parent_cid = latest
            msg = random.choice(messages) if messages else "🙏"
            reply_to_post(client, parent_uri, parent_cid, msg)
            log.info(f"💬 Replied to @{handle}: {msg[:50]}")

            bot_progress["done"] += 1
            _update_progress()

            # حفظ تقدم بسيط
            save_progress(PROGRESS_PATH, key=cfg.bluesky_handle or "default", data={
                "last_user": handle,
                "done": bot_progress["done"],
                "failed": bot_progress["failed"],
                "total": bot_progress["total"],
                "mode": cfg.processing_mode,
                "post": post_url,
            })

            # delay
            delay = random.randint(cfg.min_delay, cfg.max_delay)
            log.info(f"⏳ Sleeping {delay}s")
            time.sleep(delay)

        except Exception as e:
            log.error(f"⚠️ Error for @{handle}: {e}")
            bot_progress["failed"] += 1
            _update_progress()

    runtime_stats["status"] = "Idle"
    runtime_stats["current_task"] = None
    runtime_stats["session_uptime"] = f"{int(time.time() - start)}s"
    log.info("✅ Done")

# ================== Flask Routes ==================
@app.route("/", methods=["GET"])
def index():
    return render_template("persistent.html")

@app.route("/queue_task", methods=["POST"])
def queue_task():
    global _worker

    data = request.get_json(force=True)
    handle = data.get("bluesky_handle") or os.getenv("BLUESKY_HANDLE")
    password = data.get("bluesky_password") or os.getenv("BLUESKY_PASSWORD")
    post_url = data.get("post_url") or (data.get("post_urls") or [None])[0]
    messages = data.get("messages") or data.get("message_templates") or []
    min_delay = int(data.get("min_delay", 200))
    max_delay = int(data.get("max_delay", 250))

    mode = (data.get("processing_mode") or data.get("processing_type") or "LIKES").upper()
    if mode not in ("LIKES", "REPOSTS"):
        return jsonify({"error": "processing_mode must be LIKES or REPOSTS"}), 400

    cfg = Config(
        bluesky_handle=handle,
        bluesky_password=password,
        min_delay=min_delay,
        max_delay=max_delay,
        processing_mode=mode,
    )
    if not (cfg.is_valid() and post_url):
        return jsonify({"error": "missing handle/password/post_url"}), 400
    if not messages:
        messages = ["🙏 Thank you for your support."]

    # شغّل بالخلفية
    _worker = threading.Thread(target=_process, args=(cfg, post_url, messages), daemon=True)
    _worker.start()
    return jsonify({"status": "started"})

@app.route("/detailed_progress")
def detailed_progress():
    return jsonify({"runtime_stats": runtime_stats, "bot_progress": bot_progress})

@app.route("/stop_task", methods=["POST"])
def stop_task():
    runtime_stats["status"] = "Stopped"
    return jsonify({"status": "stopped"})

@app.route("/resume_task", methods=["POST"])
def resume_task():
    runtime_stats["status"] = "Running"
    return jsonify({"status": "resumed"})

# Aliases
@app.post("/queue")
def queue_alias(): return queue_task()

@app.get("/progress")
def progress_alias(): return detailed_progress()

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", "10000")))
