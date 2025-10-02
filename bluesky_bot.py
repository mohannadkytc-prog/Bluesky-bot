from flask import Flask, render_template, request, jsonify
import json, os, random, threading, time
from typing import Dict, Any, List
from config import Config, PROGRESS_PATH, DATA_DIR
from utils import (
    get_api, resolve_post_from_url, get_likers, get_reposters,
    has_posts, reply_to_latest_post
)

app = Flask(__name__, template_folder="templates")

STATE = {
    "running": False,
    "paused": False,
    "thread": None,
    "summary": {},   # progress per audience key
    "stats": {"total": 0, "done": 0, "fail": 0, "last_error": ""},
    "current_task": "",
}

LOCK = threading.Lock()


def load_progress() -> Dict[str, Any]:
    if os.path.exists(PROGRESS_PATH):
        try:
            with open(PROGRESS_PATH, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return {}
    return {}


def save_progress(progress: Dict[str, Any]) -> None:
    os.makedirs(os.path.dirname(PROGRESS_PATH), exist_ok=True)
    with open(PROGRESS_PATH, "w", encoding="utf-8") as f:
        json.dump(progress, f, ensure_ascii=False, indent=2)


def audience_key(link: str, handle: str) -> str:
    return f"{handle.strip().lower()}|{link.strip()}"


def worker(cfg: Config, link: str, msgs: List[str]):
    with LOCK:
        STATE["running"] = True
        STATE["paused"] = False
        STATE["stats"] = {"total": 0, "done": 0, "fail": 0, "last_error": ""}
        STATE["summary"] = {}
        STATE["current_task"] = audience_key(link, cfg.bluesky_handle)

    # 1) API & post uri
    api = get_api(cfg.bluesky_handle, cfg.bluesky_password)
    post_uri = resolve_post_from_url(link)
    if not post_uri:
        with LOCK:
            STATE["stats"]["last_error"] = "رابط غير صالح، أو بحاجة لاشتقاق did من handle."
            STATE["running"] = False
        return

    # 2) audience (ordered)
    audience = get_likers(api, post_uri) if cfg.processing_type == "likers" else get_reposters(api, post_uri)
    with LOCK:
        STATE["stats"]["total"] = len(audience)

    # 3) progress file (resume-aware)
    progress = load_progress()
    key = audience_key(link, cfg.bluesky_handle)
    done_set = set(progress.get(key, {}).get("done", []))

    for idx, it in enumerate(audience):
        with LOCK:
            if not STATE["running"]:
                break
        # Pause support
        while True:
            with LOCK:
                paused = STATE["paused"]
                still_running = STATE["running"]
            if not still_running:
                break
            if not paused:
                break
            time.sleep(0.5)

        did = it.get("did")
        if not did:
            with LOCK:
                STATE["stats"]["fail"] += 1
                STATE["stats"]["last_error"] = "عنصر بدون DID"
            continue

        # skip done
        if did in done_set:
            continue

        # ignore users without posts
        if not has_posts(api, did):
            with LOCK:
                STATE["summary"][did] = "skipped_no_posts"
            continue

        # pick random message
        message = random.choice(msgs).strip() if msgs else ""
        ok = reply_to_latest_post(api, did, message)

        with LOCK:
            if ok:
                STATE["stats"]["done"] += 1
                STATE["summary"][did] = "ok"
                done_set.add(did)
            else:
                STATE["stats"]["fail"] += 1
                STATE["summary"][did] = "fail"

        # persist incremental progress
        progress[key] = {
            "link": link,
            "handle": cfg.bluesky_handle,
            "done": list(done_set),
        }
        save_progress(progress)

        # delay
        time.sleep(random.randint(cfg.min_delay, cfg.max_delay))

    with LOCK:
        STATE["running"] = False


@app.route("/", methods=["GET"])
def home():
    return render_template("index.html",
                           data_dir=DATA_DIR,
                           default_min_delay=str(Config().min_delay),
                           default_max_delay=str(Config().max_delay))


@app.route("/start", methods=["POST"])
def start():
    payload = request.get_json(force=True)
    handle = payload.get("handle", "").strip()
    password = payload.get("password", "")
    link = payload.get("link", "").strip()
    processing_type = payload.get("processing_type", "likers").lower()
    messages_text = payload.get("messages", "").strip()
    min_delay = int(payload.get("min_delay", Config().min_delay))
    max_delay = int(payload.get("max_delay", Config().max_delay))

    msgs = [ln for ln in messages_text.split("\n") if ln.strip()]
    cfg = Config(
        bluesky_handle=handle,
        bluesky_password=password,
        min_delay=min_delay,
        max_delay=max_delay,
        processing_type=processing_type,
    )

    if not cfg.is_valid():
        return jsonify({"ok": False, "error": "بيانات الدخول ناقصة."}), 400
    if not link:
        return jsonify({"ok": False, "error": "الرجاء إدخال رابط المنشور."}), 400
    with LOCK:
        if STATE["running"]:
            return jsonify({"ok": False, "error": "مهمة قيد التشغيل بالفعل."}), 409
        t = threading.Thread(target=worker, args=(cfg, link, msgs), daemon=True)
        STATE["thread"] = t
        t.start()
    return jsonify({"ok": True})


@app.route("/stop", methods=["POST"])
def stop():
    with LOCK:
        STATE["running"] = False
        STATE["paused"] = False
    return jsonify({"ok": True})


@app.route("/resume", methods=["POST"])
def resume():
    with LOCK:
        if not STATE["running"]:
            return jsonify({"ok": False, "error": "لا توجد مهمة قيد التشغيل."}), 400
        STATE["paused"] = False
    return jsonify({"ok": True})


@app.route("/pause", methods=["POST"])
def pause():
    with LOCK:
        if not STATE["running"]:
            return jsonify({"ok": False, "error": "لا توجد مهمة قيد التشغيل."}), 400
        STATE["paused"] = True
    return jsonify({"ok": True})


@app.route("/status", methods=["GET"])
def status():
    with LOCK:
        data = {
            "running": STATE["running"],
            "paused": STATE["paused"],
            "stats": STATE["stats"],
            "summary": STATE["summary"],
            "current_task": STATE["current_task"],
        }
    return jsonify({"ok": True, "state": data})


# ====== تشغيل عبر gunicorn ======
# Procfile لديك يجب أن يشير إلى:
# web: gunicorn bluesky_bot:app --workers 1 --timeout 120
if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", "10000")))
