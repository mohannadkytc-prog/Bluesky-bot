import os
import time
import json
import random
import threading
from typing import List, Dict, Optional

from flask import Flask, render_template, request, jsonify
from atproto import Client
from atproto.exceptions import AtProtocolError

from config import Config, PROGRESS_PATH, DATA_DIR
from utils import save_progress, load_progress, resolve_post_from_url

# ================= Flask app & runtime state =================
app = Flask(__name__)

state = {
    "status": "Idle",
    "current_task": None,
    "success": 0,
    "fails": 0,
    "attempts": 0,
    "started_at": None,
    "last_update": None,
}

cfg = Config()
progress_db: Dict[str, dict] = load_progress(PROGRESS_PATH) or {}

# ================= Helpers =================
def _login() -> Client:
    c = Client()
    c.login(cfg.bluesky_handle, cfg.bluesky_password)
    return c

def _sort_by_time(items: List[dict], key_name: str = "createdAt") -> List[dict]:
    # نضمن ترتيب ثابت من الأقدم إلى الأحدث
    def _key(it):
        return it.get(key_name) or it.get("indexedAt") or it.get("created_at") or ""
    return sorted(items, key=_key)

def fetch_audience(client: Client, post_url: str, source: str) -> List[dict]:
    """
    source: 'likers' | 'reposters'
    يرجّع قائمة مستخدمين مرتّبة زمنياً (أقدم -> أحدث) بدون تكرار.
    """
    ref = resolve_post_from_url(client, post_url)
    if not ref:
        raise ValueError("لم أستطع حلّ رابط المنشور.")

    uri = ref["uri"]

    users: List[dict] = []

    if source == "likers":
        # get_likes ترجع عناصر فيها actor
        resp = client.app.bsky.feed.get_likes({"uri": uri, "limit": 100})
        items = []
        # قد نحتاج صفحات إضافية؛ نبقيه بسيطاً بـ 100 أولاً
        items.extend(resp.likes or [])
        items = _sort_by_time(items, "createdAt")
        for it in items:
            actor = it.actor
            users.append({"did": actor.did, "handle": actor.handle})

    elif source == "reposters":
        resp = client.app.bsky.feed.get_reposted_by({"uri": uri, "limit": 100})
        items = []
        items.extend(resp.reposted_by or [])
        items = _sort_by_time(items, "indexedAt")
        for actor in items:
            users.append({"did": actor.did, "handle": actor.handle})
    else:
        raise ValueError("source يجب أن يكون likers أو reposters")

    # إزالة التكرار مع الحفاظ على الترتيب
    seen = set()
    unique = []
    for u in users:
        if u["did"] not in seen:
            seen.add(u["did"])
            unique.append(u)

    return unique

def reply_to_users_last_post(client: Client, user_handle: str, msg: str) -> bool:
    """
    يرد على آخر منشور عند المستخدم المحدّد.
    """
    try:
        feed = client.app.bsky.feed.get_author_feed({"actor": user_handle, "limit": 1})
        posts = feed.feed or []
        if not posts:
            return False

        post = posts[0].post
        uri = post.uri
        cid = post.cid

        client.com.atproto.repo.create_record({
            "repo": client.me.did,
            "collection": "app.bsky.feed.post",
            "record": {
                "$type": "app.bsky.feed.post",
                "text": msg,
                "createdAt": client.get_current_time_iso(),
                "reply": {
                    "root": {"uri": uri, "cid": cid},
                    "parent": {"uri": uri, "cid": cid},
                },
            },
        })
        return True
    except Exception:
        return False

# ================= Worker =================
def run_worker(post_url: str,
               messages: List[str],
               source: str,
               min_delay: int,
               max_delay: int):
    """
    source = 'likers' أو 'reposters'
    """
    state.update({
        "status": "Running",
        "current_task": f"Processing {source}",
        "success": 0,
        "fails": 0,
        "attempts": 0,
        "started_at": int(time.time()),
        "last_update": int(time.time()),
    })

    # إعدادات التأخير على مستوى العملية الحالية
    cfg.min_delay = min_delay
    cfg.max_delay = max_delay

    client = _login()

    try:
        audience = fetch_audience(client, post_url, source)
    except Exception as e:
        state.update({"status": "Idle", "current_task": None})
        return

    # استرجاع تقدّم سابق لهذا الرابط (للاستئناف)
    post_key = f"{post_url}:{source}"
    done_index = 0
    if progress_db.get(post_key) and isinstance(progress_db[post_key].get("done_index"), int):
        done_index = progress_db[post_key]["done_index"]

    for idx in range(done_index, len(audience)):
        # قد يتم إيقاف العملية من الواجهة
        if state.get("status") != "Running":
            break

        user = audience[idx]
        state["attempts"] += 1
        msg = random.choice(messages) if messages else "🙏"

        ok = reply_to_users_last_post(client, user["handle"], msg)
        if ok:
            state["success"] += 1
        else:
            state["fails"] += 1

        # تحديث التقدّم (نحفظ index الذي تمّ إنهاؤه)
        progress_db[post_key] = {
            "done_index": idx + 1,
            "total": len(audience),
            "last_user": user["handle"],
            "updated_at": int(time.time()),
        }
        save_progress(PROGRESS_PATH, progress_db)

        state["last_update"] = int(time.time())

        # تأخير بين المستخدمين
        delay = random.randint(cfg.min_delay, cfg.max_delay)
        time.sleep(delay)

    state.update({"status": "Idle", "current_task": None})

worker_thread: Optional[threading.Thread] = None

# ================= Routes =================
@app.get("/")
def index():
    return render_template("persistent.html",
                           state=state,
                           cfg=cfg,
                           data_dir=DATA_DIR)

@app.post("/queue_task")
def queue_task():
    global worker_thread

    payload = request.get_json(force=True)
    post_url = (payload.get("post_url") or "").strip()
    messages = [m.strip() for m in (payload.get("messages") or []) if m and m.strip()]
    source = (payload.get("audience_source") or "likers").lower()  # 'likers' | 'reposters'
    min_delay = int(payload.get("min_delay", cfg.min_delay))
    max_delay = int(payload.get("max_delay", cfg.max_delay))

    # بيانات الدخول يمكن تمريرها في الطلب أو تبقى من env
    handle = payload.get("bluesky_handle")
    password = payload.get("bluesky_password")
    if handle:
        cfg.bluesky_handle = handle
    if password:
        cfg.bluesky_password = password

    if not (cfg.bluesky_handle and cfg.bluesky_password and post_url and messages):
        return jsonify({"error": "بيانات ناقصة: الحساب/كلمة المرور/الرابط/الرسائل"}), 400

    if source not in ("likers", "reposters"):
        return jsonify({"error": "audience_source يجب أن يكون likers أو reposters"}), 400

    # شغّل العامل بالخلفية
    if worker_thread and worker_thread.is_alive():
        return jsonify({"error": "هناك مهمة قيد التشغيل بالفعل"}), 409

    worker_thread = threading.Thread(
        target=run_worker,
        args=(post_url, messages, source, min_delay, max_delay),
        daemon=True,
    )
    worker_thread.start()
    return jsonify({"status": "started"})

@app.post("/stop_task")
def stop_task():
    state["status"] = "Idle"
    state["current_task"] = None
    return jsonify({"status": "stopped"})

@app.get("/detailed_progress")
def detailed_progress():
    return jsonify({
        "state": state,
        "progress": progress_db
    })

# ===== Aliases مختصرة =====
@app.post("/queue")
def _queue_alias():
    return queue_task()

@app.get("/progress")
def _progress_alias():
    return detailed_progress()

if __name__ == "__main__":
    port = int(os.getenv("PORT", "10000"))
    app.run(host="0.0.0.0", port=port)
