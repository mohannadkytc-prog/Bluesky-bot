# -*- coding: utf-8 -*-
import os
import json
import time
import random
import logging
from types import SimpleNamespace
from threading import Thread, Event
from typing import Dict, List, Optional

from flask import Flask, render_template, request, jsonify

# atproto
from atproto import Client

# مسارات البيانات (يدعم /data أو /tmp)
DATA_DIR = "/data" if os.path.exists("/data") else "/tmp"
os.makedirs(DATA_DIR, exist_ok=True)
PROGRESS_PATH = os.path.join(DATA_DIR, "progress.json")

# الحالة داخل الذاكرة
RUNTIME_TASKS: Dict[str, dict] = {}
SHOULD_STOP: Dict[str, Event] = {}

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("bluesky-bot")

# ===== utils =====
from utils import (
    resolve_post_from_url,
    save_progress_for_key,
    load_progress_for_key,
    validate_message_template,
)

# ===== Flask =====
app = Flask(__name__, template_folder="templates")

# ---------- مساعدات الدخول ----------
def login_bluesky(handle: str, app_password: str) -> Client:
    client = Client()
    client.login(handle, app_password)
    return client

# ---------- جلب الجمهور مرتباً ----------
def fetch_audience_sorted(client: Client, post_uri: str, mode: str) -> List[Dict]:
    """
    mode: 'likers' أو 'reposters'
    يعيد قائمة مرتبة من الأقدم إلى الأحدث.
    """
    audience = []
    seen = set()
    cursor = None

    while True:
        if mode == "likers":
            resp = client.app.bsky.feed.get_likes({"uri": post_uri, "cursor": cursor, "limit": 100})
            items = getattr(resp, "likes", []) or []
            for it in items:
                actor = getattr(it, "actor", None)
                if not actor or not getattr(actor, "did", None):
                    continue
                if actor.did in seen:
                    continue
                seen.add(actor.did)
                audience.append({
                    "did": actor.did,
                    "handle": getattr(actor, "handle", None),
                    "indexedAt": getattr(it, "createdAt", None)
                })
            cursor = getattr(resp, "cursor", None)

        elif mode == "reposters":
            resp = client.app.bsky.feed.get_reposted_by({"uri": post_uri, "cursor": cursor, "limit": 100})
            items = getattr(resp, "repostedBy", []) or []
            for actor in items:
                if not actor or not getattr(actor, "did", None):
                    continue
                if actor.did in seen:
                    continue
                seen.add(actor.did)
                audience.append({
                    "did": actor.did,
                    "handle": getattr(actor, "handle", None),
                    "indexedAt": getattr(actor, "createdAt", None),  # قد تكون None
                })
            cursor = getattr(resp, "cursor", None)
        else:
            raise ValueError("processing_type must be 'likers' or 'reposters'")

        if not cursor:
            break

    # أقدم → أحدث (لو لا يوجد indexedAt نضع قيمة عالية لضمان بقاءهم في النهاية)
    audience.sort(key=lambda x: (x.get("indexedAt") or "9999-12-31T00:00:00Z"))
    return audience

# ---------- آخر منشور-جذر للمستخدم ----------
def get_latest_root_post(client: Client, actor: str) -> Optional[Dict]:
    try:
        feed = client.app.bsky.feed.get_author_feed({
            "actor": actor,
            "limit": 25,
            "filter": "posts_no_replies"
        })
        for it in getattr(feed, "feed", []) or []:
            post = getattr(it, "post", None)
            if not post:
                continue
            if not getattr(post, "reply", None):
                return {"uri": post.uri, "cid": post.cid}
    except Exception as e:
        logger.warning(f"get_latest_root_post failed for {actor}: {e}")
    return None

# ---------- إرسال الرد ----------
def post_reply(client: Client, repo_did: str, parent_uri: str, parent_cid: str, text: str) -> bool:
    try:
        record = {
            "$type": "app.bsky.feed.post",
            "text": text,
            "createdAt": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "reply": {
                "root": {"uri": parent_uri, "cid": parent_cid},
                "parent": {"uri": parent_uri, "cid": parent_cid},
            },
        }
        client.com.atproto.repo.create_record({
            "repo": repo_did,
            "collection": "app.bsky.feed.post",
            "record": record
        })
        return True
    except Exception as e:
        logger.error(f"post_reply failed: {e}")
        return False

# ---------- العامل (التنفيذ المتسلسل) ----------
def run_worker(account_key: str):
    task = RUNTIME_TASKS.get(account_key)
    if not task:
        return

    # حمّلي الإعدادات المخزنة
    messages: List[str] = task["messages"]
    cfg = SimpleNamespace(**task["cfg"])  # min_delay, max_delay, handle, password
    processing_type: str = task["processing_type"]
    post_uri: str = task["post_uri"]
    audience: List[Dict] = task["audience"]

    # login
    client = login_bluesky(cfg.bluesky_handle, cfg.bluesky_password)
    me_did = client.me.did

    # تقدّم سابق
    prog = load_progress_for_key(PROGRESS_PATH, account_key) or {}
    i = int(prog.get("last_index", 0))
    done = int(prog.get("done", 0))
    failed = int(prog.get("failed", 0))
    skipped = int(prog.get("skipped", 0))

    stop_flag = SHOULD_STOP.get(account_key)

    while i < len(audience) and (not stop_flag or not stop_flag.is_set()):
        target = audience[i]
        i += 1

        # آخر منشور-جذر للمستخدم
        latest = get_latest_root_post(client, target["did"])
        if not latest:
            skipped += 1
            save_progress_for_key(PROGRESS_PATH, account_key, {
                **prog, "last_index": i, "done": done, "failed": failed, "skipped": skipped
            })
            continue

        parent_uri, parent_cid = latest["uri"], latest["cid"]

        # اختاري رسالة عشوائية سليمة
        msg = random.choice(messages).strip() if messages else "👋"
        if not validate_message_template(msg):
            failed += 1
            save_progress_for_key(PROGRESS_PATH, account_key, {
                **prog, "last_index": i, "done": done, "failed": failed, "skipped": skipped
            })
            continue

        ok = post_reply(client, me_did, parent_uri, parent_cid, msg)
        if ok:
            done += 1
        else:
            failed += 1

        save_progress_for_key(PROGRESS_PATH, account_key, {
            **prog, "post_uri": post_uri, "mode": processing_type,
            "last_index": i, "done": done, "failed": failed, "skipped": skipped,
            "total": len(audience)
        })

        wait_for = random.randint(cfg.min_delay, cfg.max_delay)
        time.sleep(wait_for)

    save_progress_for_key(PROGRESS_PATH, account_key, {
        **prog, "finished": True, "last_index": i, "done": done,
        "failed": failed, "skipped": skipped, "total": len(audience)
    })
    # انتهت المهمة
    SHOULD_STOP.pop(account_key, None)
    RUNTIME_TASKS.pop(account_key, None)

# ================== المسارات ==================

@app.route("/", methods=["GET"])
def index():
    return render_template("persistent.html")

@app.route("/start", methods=["POST"])
def start_task():
    """
    المدخلات JSON:
    - handle, app_password
    - post_url
    - messages (نص متعدد الأسطر)
    - processing_type: 'likers' أو 'reposters'
    - min_delay, max_delay (ثوانٍ)
    """
    data = request.get_json(force=True)

    handle = (data.get("handle") or "").strip()
    password = (data.get("app_password") or "").strip()
    post_url = (data.get("post_url") or "").strip()
    processing_type = data.get("processing_type", "likers")
    min_delay = int(data.get("min_delay", 200))
    max_delay = int(data.get("max_delay", 250))

    # رسائل
    raw_msgs = data.get("messages", "")
    messages = [m.strip() for m in raw_msgs.splitlines() if m.strip()]
    if not messages:
        messages = ["👋"]

    # حلّ الرابط
    try:
        tmp_client = login_bluesky(handle, password)
    except Exception as e:
        return jsonify({"ok": False, "error": f"Login failed: {e}"}), 400

    resolved = resolve_post_from_url(tmp_client, post_url)
    if not resolved:
        return jsonify({"ok": False, "error": "تعذّر حلّ رابط البوست"}), 400

    post_uri = resolved["uri"]
    account_key = f"{handle}|{post_uri}|{processing_type}"

    # اجلب الجمهور مرتباً
    audience = fetch_audience_sorted(tmp_client, post_uri, processing_type)

    # خزن إعدادات المهمة
    RUNTIME_TASKS[account_key] = {
        "cfg": {
            "bluesky_handle": handle,
            "bluesky_password": password,
            "min_delay": min_delay,
            "max_delay": max_delay,
        },
        "messages": messages,
        "processing_type": processing_type,
        "post_uri": post_uri,
        "audience": audience,
    }

    # ابدأ مؤشر التقدم
    prog = load_progress_for_key(PROGRESS_PATH, account_key) or {}
    save_progress_for_key(PROGRESS_PATH, account_key, {
        **prog, "post_uri": post_uri, "mode": processing_type, "total": len(audience),
        "last_index": prog.get("last_index", 0), "done": prog.get("done", 0),
        "failed": prog.get("failed", 0), "skipped": prog.get("skipped", 0),
    })

    # شغّلي العامل
    SHOULD_STOP[account_key] = Event()
    t = Thread(target=run_worker, args=(account_key,), daemon=True)
    t.start()

    return jsonify({"ok": True, "key": account_key, "total": len(audience)})

@app.route("/stop", methods=["POST"])
def stop_task():
    data = request.get_json(force=True)
    handle = data.get("handle")
    post_url = data.get("post_url")
    processing_type = data.get("processing_type", "likers")

    # نحتاج post_uri من الرابط
    # تسجيل دخول مؤقت بأي كلمة مرور؟ لا، يتوفر لدينا progress، قد يحوي post_uri
    # لو لم نجد، نحل الرابط بالحساب الحالي من الطلب (لو موجود)
    key = None
    # حاول استخراج من progress
    for k, v in (json.load(open(PROGRESS_PATH)) if os.path.exists(PROGRESS_PATH) else {}).items():
        if k.startswith(f"{handle}|") and v.get("mode") == processing_type:
            # اختبري أن v يحمل نفس post_uri (لو نعرفه من الواجهة)
            key = k
    if not key and post_url and data.get("app_password"):
        client = login_bluesky(handle, data["app_password"])
        resolved = resolve_post_from_url(client, post_url)
        if resolved:
            key = f"{handle}|{resolved['uri']}|{processing_type}"

    if not key:
        return jsonify({"ok": False, "error": "لم يتم العثور على مهمة نشطة لهذا الحساب/الرابط"}), 404

    ev = SHOULD_STOP.get(key)
    if ev:
        ev.set()

    return jsonify({"ok": True})

@app.route("/resume", methods=["POST"])
def resume_task():
    """
    استئناف نفس المفتاح (handle|post_uri|mode) من حيث توقف.
    يحتاج نفس المدخلات الأساسية لنعيد تهيئة الـ runtime (كلمات السر/الرسائل/التأخيرات).
    """
    data = request.get_json(force=True)

    handle = (data.get("handle") or "").strip()
    password = (data.get("app_password") or "").strip()
    post_url = (data.get("post_url") or "").strip()
    processing_type = data.get("processing_type", "likers")
    min_delay = int(data.get("min_delay", 200))
    max_delay = int(data.get("max_delay", 250))
    raw_msgs = data.get("messages", "")
    messages = [m.strip() for m in raw_msgs.splitlines() if m.strip()]
    if not messages:
        messages = ["👋"]

    # نحل الرابط ونبني المفتاح
    client = login_bluesky(handle, password)
    resolved = resolve_post_from_url(client, post_url)
    if not resolved:
        return jsonify({"ok": False, "error": "تعذّر حلّ رابط البوست"}), 400

    post_uri = resolved["uri"]
    account_key = f"{handle}|{post_uri}|{processing_type}"

    # لو المهمة منتهية بالكامل ممكن نعيد audience من جديد
    audience = fetch_audience_sorted(client, post_uri, processing_type)

    RUNTIME_TASKS[account_key] = {
        "cfg": {
            "bluesky_handle": handle,
            "bluesky_password": password,
            "min_delay": min_delay,
            "max_delay": max_delay,
        },
        "messages": messages,
        "processing_type": processing_type,
        "post_uri": post_uri,
        "audience": audience,
    }

    SHOULD_STOP[account_key] = Event()
    t = Thread(target=run_worker, args=(account_key,), daemon=True)
    t.start()

    return jsonify({"ok": True, "key": account_key})

@app.route("/status", methods=["POST"])
def status():
    """
    ترجع حالة التقدم لهذا الحساب/الرابط/الوضع.
    """
    data = request.get_json(force=True)
    handle = (data.get("handle") or "").strip()
    post_url = (data.get("post_url") or "").strip()
    processing_type = data.get("processing_type", "likers")

    client = None
    post_uri = None
    if post_url and data.get("app_password"):
        try:
            client = login_bluesky(handle, data["app_password"])
            resolved = resolve_post_from_url(client, post_url)
            if resolved:
                post_uri = resolved["uri"]
        except Exception:
            pass

    # إن لم نحلّ post_uri نحاول إيجاده من progress
    key = None
    if post_uri:
        key = f"{handle}|{post_uri}|{processing_type}"
    else:
        # ابحث عن أول تطابق بالهاندل + الوضع
        prog_all = json.load(open(PROGRESS_PATH)) if os.path.exists(PROGRESS_PATH) else {}
        for k, v in prog_all.items():
            if k.startswith(f"{handle}|") and v.get("mode") == processing_type:
                key = k
                break

    prog = load_progress_for_key(PROGRESS_PATH, key) if key else {}
    running = key in RUNTIME_TASKS
    current_task = "Processing audience" if running else "—"

    return jsonify({
        "ok": True,
        "running": running,
        "current_task": current_task,
        "progress": prog or {
            "total": 0, "done": 0, "failed": 0, "skipped": 0, "last_index": 0
        }
    })


# ---------- WSGI ----------
def app_factory():
    return app

if __name__ == "__main__":
    # للتشغيل المحلي
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", "10000")))
