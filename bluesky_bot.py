import os
import time
import json
import random
import logging
import threading
from datetime import datetime
from typing import List, Dict, Any, Optional

from flask import Flask, render_template, request, jsonify
from atproto import Client, models as at_models

# مسارات التخزين: /data إن وُجد وإلا /tmp
DATA_DIR = "/data" if os.path.exists("/data") else "/tmp"
os.makedirs(DATA_DIR, exist_ok=True)
PROGRESS_PATH = os.path.join(DATA_DIR, "progress.json")

# لوج
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s: %(message)s")
log = logging.getLogger("bluesky-bot")

# استيراد أدواتنا
from utils import (
    resolve_post_from_url,
    save_progress,
    load_progress,
    validate_message_template,
)

# حالة التشغيل العامة
STATE: Dict[str, Any] = {
    "status": "Idle",               # Idle | Running | Paused | Stopped
    "current_task": None,           # وصف المهمة
    "started_at": None,
    "success": 0,
    "failed": 0,
    "total": 0,
    "last_update": None,
    "key": None,                    # مفتاح التقدم لهذه المهمة
}

# أعلام إيقاف/استئناف
STOP_EVENT = threading.Event()
PAUSE_EVENT = threading.Event()

app = Flask(__name__, template_folder="templates")


# ==== أدوات Bluesky ====

def bs_login(handle: str, app_password: str) -> Client:
    client = Client()
    client.login(handle, app_password)
    return client


def get_audience_ordered(client: Client, post_uri: str, mode: str) -> List[Dict[str, str]]:
    """
    يرجّع قائمة مرتبة من الأعلى للأسفل لمستخدمي:
      - المعجبين (mode='likes')
      - مُعيدي النشر (mode='reposts')
    كل عنصر: {"did": "...", "handle": "..."}
    """
    audience: List[Dict[str, str]] = []
    cursor = None

    if mode == "likes":
        # app.bsky.feed.get_likes
        while True:
            resp = client.app.bsky.feed.get_likes({"uri": post_uri, "cursor": cursor, "limit": 100})
            for it in resp.likes:
                actor = it.actor
                audience.append({"did": actor.did, "handle": actor.handle})
            cursor = getattr(resp, "cursor", None)
            if not cursor:
                break

    elif mode == "reposts":
        # app.bsky.feed.get_reposted_by
        while True:
            resp = client.app.bsky.feed.get_reposted_by({"uri": post_uri, "cursor": cursor, "limit": 100})
            for it in resp.reposted_by:
                audience.append({"did": it.did, "handle": it.handle})
            cursor = getattr(resp, "cursor", None)
            if not cursor:
                break
    else:
        raise ValueError("Invalid mode (expected 'likes' or 'reposts').")

    # القائمة بالأصل مرتبة زمنياً من الأحدث للأقدم؛ نريد من الأعلى للأسفل كما تظهر:
    # عادةً الأعلى = الأحدث، فإذا أردتِ من الأقدم للأحدث، افعلي audience.reverse()
    # هنا سنُبقيها كما تأتي من الـ API (الأحدث أولاً).
    return audience


def get_latest_post_of_user(client: Client, actor: str) -> Optional[Dict[str, str]]:
    """
    يرجّع آخر بوست (uri, cid) لحساب (did أو handle).
    يتجاهل الحسابات بلا منشورات.
    """
    try:
        feed = client.app.bsky.feed.get_author_feed({"actor": actor, "limit": 10})
        for item in getattr(feed, "feed", []):
            post = getattr(item, "post", None)
            if post and getattr(post, "uri", None) and getattr(post, "cid", None):
                return {"uri": post.uri, "cid": post.cid}
        return None
    except Exception:
        return None


def reply_to_post(client: Client, parent_uri: str, parent_cid: str, text: str) -> bool:
    """
    ينشئ رد على منشور معيّن.
    """
    try:
        now_iso = datetime.utcnow().isoformat() + "Z"
        record = at_models.AppBskyFeedPost.Record(
            text=text,
            created_at=now_iso,
            reply=at_models.AppBskyFeedPost.ReplyRef(
                parent=at_models.ComAtprotoRepoStrongRef.Main(cid=parent_cid, uri=parent_uri),
                root=at_models.ComAtprotoRepoStrongRef.Main(cid=parent_cid, uri=parent_uri),
            ),
            langs=["ar", "en"],
        )
        client.com.atproto.repo.create_record(
            at_models.ComAtprotoRepoCreateRecord.Data(
                repo=client.me.did,
                collection="app.bsky.feed.post",
                record=record,
            )
        )
        return True
    except Exception as e:
        log.warning(f"reply_to_post failed: {e}")
        return False


# ==== تقدم و مفتاح المهمة ====

def make_progress_key(handle: str, post_url: str, mode: str) -> str:
    return f"{handle}::{mode}::{post_url}"


def save_state_progress(key: str, audience: List[Dict[str, str]], next_index: int) -> None:
    progress = {
        "audience": audience,   # قائمة ثابتة محفوظة
        "next_index": next_index,
        "success": STATE["success"],
        "failed": STATE["failed"],
        "total": STATE["total"],
        "updated_at": datetime.utcnow().isoformat() + "Z",
    }
    save_progress(PROGRESS_PATH, key, progress)


def load_state_progress(key: str) -> Dict[str, Any]:
    return load_progress(PROGRESS_PATH, key)


# ==== ثريد المهمة ====

def worker_task(cfg: Dict[str, Any]) -> None:
    """
    ينفَّذ في ثريد مستقل عند الضغط على "بدء المهمة" أو "استئناف".
    """
    handle      = cfg["handle"]
    app_pass    = cfg["password"]
    post_url    = cfg["post_url"]
    mode        = cfg["mode"]  # "likes" | "reposts"
    min_delay   = int(cfg["min_delay"])
    max_delay   = int(cfg["max_delay"])
    messages    = [m.strip() for m in cfg["messages"] if m.strip()]

    # لوجين
    client = bs_login(handle, app_pass)
    resolved = resolve_post_from_url(client, post_url)
    if not resolved:
        STATE["status"] = "Idle"
        return

    post_uri = resolved["uri"]
    key = make_progress_key(handle, post_url, mode)
    STATE["key"] = key

    # حمّلي التقدم إن وجد
    progress_db = load_state_progress(key)
    if progress_db and "audience" in progress_db:
        audience = progress_db["audience"]
        next_i   = int(progress_db.get("next_index", 0))
        STATE["success"] = int(progress_db.get("success", 0))
        STATE["failed"]  = int(progress_db.get("failed", 0))
        STATE["total"]   = int(progress_db.get("total", len(audience)))
        log.info(f"Resuming from index={next_i} / total={len(audience)}")
    else:
        # اجلب القائمة بالترتيب وحفظها
        audience = get_audience_ordered(client, post_uri, mode)
        STATE["success"] = 0
        STATE["failed"]  = 0
        STATE["total"]   = len(audience)
        next_i = 0
        save_state_progress(key, audience, next_i)

    STATE["status"] = "Running"
    STATE["started_at"] = time.time()
    STATE["current_task"] = f"{mode} on {post_url}"

    for idx in range(next_i, len(audience)):
        if STOP_EVENT.is_set():
            STATE["status"] = "Stopped"
            save_state_progress(key, audience, idx)
            return

        while PAUSE_EVENT.is_set():
            STATE["status"] = "Paused"
            time.sleep(1)

        STATE["status"] = "Running"

        user = audience[idx]
        actor = user.get("did") or user.get("handle")

        # احصل على آخر بوست، ولو مافي → تجاهل
        last = get_latest_post_of_user(client, actor)
        if not last:
            STATE["failed"] += 1
            save_state_progress(key, audience, idx + 1)
            continue

        # رسالة عشوائية مع تحقّق سريع
        text = random.choice(messages) if messages else ""
        if not validate_message_template(text):
            text = text[:280]

        ok = reply_to_post(client, last["uri"], last["cid"], text)
        if ok:
            STATE["success"] += 1
        else:
            STATE["failed"] += 1

        STATE["last_update"] = time.time()
        save_state_progress(key, audience, idx + 1)

        # تأخير عشوائي
        delay = random.randint(min_delay, max_delay)
        for _ in range(delay):
            if STOP_EVENT.is_set():
                break
            time.sleep(1)
        if STOP_EVENT.is_set():
            STATE["status"] = "Stopped"
            return

    STATE["status"] = "Idle"
    save_state_progress(key, audience, len(audience))


# ==== واجهات الويب ====

@app.route("/", methods=["GET"])
def index():
    # عرض الواجهة
    return render_template("persistent.html")


@app.route("/start", methods=["POST"])
def start():
    """
    body(JSON):
      handle, password, post_url,
      mode: "likes"|"reposts",
      min_delay, max_delay,
      messages: [..]
    """
    data = request.get_json(force=True)

    # نظّف الأعلام
    STOP_EVENT.clear()
    PAUSE_EVENT.clear()

    # صفري الحالة
    STATE.update({
        "status": "Idle",
        "current_task": None,
        "started_at": None,
        "success": 0,
        "failed": 0,
        "total": 0,
        "last_update": None,
        "key": None,
    })

    t = threading.Thread(target=worker_task, args=(data,), daemon=True)
    t.start()
    return jsonify({"ok": True, "status": "Running"})


@app.route("/stop", methods=["POST"])
def stop():
    STOP_EVENT.set()
    PAUSE_EVENT.clear()
    return jsonify({"ok": True, "status": "Stopping"})


@app.route("/resume", methods=["POST"])
def resume():
    # استئناف = إزالة pause فقط (لو كانت المهمة محفوظة ستكمل من حيث توقفت)
    PAUSE_EVENT.clear()
    if STATE["status"] == "Paused":
        STATE["status"] = "Running"
    return jsonify({"ok": True, "status": STATE["status"]})


@app.route("/pause", methods=["POST"])
def pause():
    # زر اختياري (غير مطلوب) — لا نعرضه في الواجهة الآن
    PAUSE_EVENT.set()
    return jsonify({"ok": True, "status": "Paused"})


@app.route("/status", methods=["GET"])
def status():
    out = {
        "status": STATE["status"],
        "current_task": STATE["current_task"],
        "started_at": STATE["started_at"],
        "success": STATE["success"],
        "failed": STATE["failed"],
        "total": STATE["total"],
        "last_update": STATE["last_update"],
        "key": STATE["key"],
    }
    return jsonify(out)


# بووت
app_name = os.environ.get("RENDER_SERVICE_NAME", "bluesky-bot")
log.info(f"{app_name} ready (data_dir={DATA_DIR})")

def app_factory():
    return app

if __name__ == "__main__":
    # للتشغيل المحلي
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", "10000")))
