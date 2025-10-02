# -*- coding: utf-8 -*-
import os, time, threading, random, logging
from typing import Dict, Any, List, Optional
from flask import Flask, render_template, request, jsonify
from atproto import Client, models

from config import PROGRESS_PATH, Config  # يستخدم DATA_DIR/PROGRESS_PATH من config.py
from utils import (
    extract_post_info,
    resolve_post_from_url,
    save_progress,
    load_progress,
    validate_message_template,
)

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("bluesky-bot")

app = Flask(__name__)

# =========================
# إدارة التشغيل والحالة
# =========================
RUNNERS: Dict[str, Dict[str, Any]] = {}  # per-key: {"thread": Thread, "stop": Event}

def progress_key(handle: str, post_url: str, mode: str) -> str:
    return f"{handle}|{post_url}|{mode}"

def _load(handle: str, post_url: str, mode: str) -> Dict[str, Any]:
    return load_progress(PROGRESS_PATH, progress_key(handle, post_url, mode))

def _save(handle: str, post_url: str, mode: str, data: Dict[str, Any]):
    save_progress(PROGRESS_PATH, progress_key(handle, post_url, mode), data)

# =========================
# وظائف البلوسكاي
# =========================

def get_client(cfg: Config) -> Client:
    client = Client()
    client.login(cfg.bluesky_handle, cfg.bluesky_password)
    return client

def fetch_audience(client: Client, post_uri: str, mode: str) -> List[str]:
    """جلب قائمة المعجبين أو معيدي النشر (DIDs) بالترتيب من الأقدم للأحدث."""
    dids: List[str] = []
    cursor = None
    while True:
        if mode == "likes":
            resp = client.app.bsky.feed.get_likes({"uri": post_uri, "cursor": cursor, "limit": 100})
            items = resp.likes or []
            for i in items:
                if i.actor and i.actor.did:
                    dids.append(i.actor.did)
            cursor = getattr(resp, "cursor", None)
        else:  # reposts
            resp = client.app.bsky.feed.get_reposted_by({"uri": post_uri, "cursor": cursor, "limit": 100})
            items = resp.repostedBy or []
            for a in items:
                if a.did:
                    dids.append(a.did)
            cursor = getattr(resp, "cursor", None)

        if not cursor:
            break

    # الأقدم -> الأحدث: نريد البدء من الأعلى، لذا لا نعكس
    return dids

def latest_post_uri_for_user(client: Client, did_or_handle: str) -> Optional[str]:
    """إرجاع آخر بوست كتبه هذا المستخدم نفسه (ليس ريبوست/محتوى غيره)."""
    cursor = None
    while True:
        feed = client.app.bsky.feed.get_author_feed({"actor": did_or_handle, "limit": 50, "cursor": cursor})
        if not feed.feed:
            return None
        for item in feed.feed:
            post = item.post
            if post and post.author and post.author.did == did_or_handle and post.uri:
                return post.uri
        cursor = getattr(feed, "cursor", None)
        if not cursor:
            break
    return None

def send_reply(client: Client, parent_uri: str, text: str):
    """إرسال رد على بوست معيّن (بسيط ومُتوافق)."""
    post = client.app.bsky.feed.get_posts({"uris": [parent_uri]}).posts[0]
    reply_ref = models.app.bsky.feed.post.ReplyRef(
        parent=models.com.atproto.repo.strong_ref.Main(uri=post.uri, cid=post.cid),
        root=models.com.atproto.repo.strong_ref.Main(uri=post.uri, cid=post.cid),
    )
    client.app.bsky.feed.post.create(
        repo=client.me.did,
        record=models.AppBskyFeedPost.Main(
            text=text,
            createdAt=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            reply=reply_ref,
        ),
    )

# =========================
# حلقة التنفيذ
# =========================

def runner_loop(cfg: Config, post_url: str, mode: str, messages: List[str]):
    """حلقة الخلفية: تتابع الرد على القائمة حسب الترتيب مع الانتظار بين المستخدمين."""
    key = progress_key(cfg.bluesky_handle, post_url, mode)
    stop_event: threading.Event = RUNNERS[key]["stop"]

    # تحميل/تهيئة التقدم
    progress = _load(cfg.bluesky_handle, post_url, mode) or {}
    progress.setdefault("stats", {"ok": 0, "fail": 0})
    progress.setdefault("failures", [])
    progress.setdefault("skipped_no_posts", [])
    progress.setdefault("settings", {
        "handle": cfg.bluesky_handle,
        "mode": mode,
        "min_delay": cfg.min_delay,
        "max_delay": cfg.max_delay,
    })
    progress.setdefault("last_error", None)

    client = None
    try:
        client = get_client(cfg)
    except Exception as e:
        progress["last_error"] = f"Login failed: {e}"
        _save(cfg.bluesky_handle, post_url, mode, progress)
        return

    # حل الـ URI من الرابط
    resolved = resolve_post_from_url(client, post_url)
    if not resolved or "uri" not in resolved:
        progress["last_error"] = "Cannot resolve post URL."
        _save(cfg.bluesky_handle, post_url, mode, progress)
        return

    # بناء الجمهور إن لم يكن موجوداً
    if "audience" not in progress or not isinstance(progress["audience"], list):
        try:
            audience = fetch_audience(client, resolved["uri"], "likes" if mode == "likes" else "reposts")
            progress["audience"] = audience
            progress["index"] = 0
            progress["total"] = len(audience)
            progress["last_error"] = None
            _save(cfg.bluesky_handle, post_url, mode, progress)
        except Exception as e:
            progress["last_error"] = f"Fetch audience failed: {e}"
            _save(cfg.bluesky_handle, post_url, mode, progress)
            return

    audience: List[str] = progress.get("audience", [])
    idx = int(progress.get("index", 0))

    # المعالجة بالتسلسل من الأعلى للأسفل
    while not stop_event.is_set() and idx < len(audience):
        did = audience[idx]
        try:
            # جلب آخر بوست للمستخدم
            target_uri = latest_post_uri_for_user(client, did)
            if not target_uri:
                # لا منشورات: تخطّي
                progress["skipped_no_posts"].append(did)
            else:
                # اختيار رسالة عشوائية صحيحة
                msg = random.choice(messages).strip()
                if not validate_message_template(msg):
                    raise ValueError("Message failed validation")
                send_reply(client, target_uri, msg)
                progress["stats"]["ok"] += 1

            idx += 1
            progress["index"] = idx
            progress["last_error"] = None
            _save(cfg.bluesky_handle, post_url, mode, progress)

            # الانتظار بين المستخدمين
            sleep_s = random.randint(cfg.min_delay, cfg.max_delay)
            for _ in range(sleep_s):
                if stop_event.is_set():
                    break
                time.sleep(1)

        except Exception as e:
            progress["stats"]["fail"] += 1
            progress.setdefault("failures", []).append({"did": did, "error": str(e)})
            progress["last_error"] = str(e)
            idx += 1
            progress["index"] = idx
            _save(cfg.bluesky_handle, post_url, mode, progress)

    # انتهت الحلقة أو تم إيقافها
    RUNNERS.pop(key, None)


# =========================
# الواجهات
# =========================

@app.route("/", methods=["GET"])
def home():
    return render_template("persistent.html")

@app.route("/start", methods=["POST"])
def start():
    handle = request.form.get("handle", "").strip()
    password = request.form.get("password", "").strip()
    post_url = request.form.get("post_url", "").strip()
    mode = request.form.get("process_type", "likes")  # likes | reposts
    min_delay = int(request.form.get("min_delay") or 200)
    max_delay = int(request.form.get("max_delay") or 250)
    messages_text = request.form.get("messages", "").strip()
    messages = [m for m in messages_text.splitlines() if m.strip()]

    cfg = Config(handle, password)
    cfg.min_delay = min_delay
    cfg.max_delay = max_delay

    if not (cfg.is_valid() and post_url and messages):
        return jsonify({"ok": False, "error": "المدخلات غير مكتملة"}), 400

    key = progress_key(cfg.bluesky_handle, post_url, mode)
    if key in RUNNERS:
        return jsonify({"ok": False, "error": "مهمة قيد التشغيل"}), 400

    # تهيئة التقدم الأساسي
    base = {
        "stats": {"ok": 0, "fail": 0},
        "failures": [],
        "skipped_no_posts": [],
        "index": 0,
        "total": 0,
        "audience": [],
        "settings": {
            "handle": cfg.bluesky_handle,
            "mode": mode,
            "min_delay": cfg.min_delay,
            "max_delay": cfg.max_delay,
        },
        "last_error": None,
    }
    _save(cfg.bluesky_handle, post_url, mode, base)

    # تشغيل الخيط
    stop_event = threading.Event()
    t = threading.Thread(target=runner_loop, args=(cfg, post_url, mode, messages), daemon=True)
    RUNNERS[key] = {"thread": t, "stop": stop_event}
    t.start()
    return jsonify({"ok": True})

@app.route("/stop", methods=["POST"])
def stop():
    handle = request.form.get("handle", "").strip()
    post_url = request.form.get("post_url", "").strip()
    mode = request.form.get("process_type", "likes")
    key = progress_key(handle, post_url, mode)
    runner = RUNNERS.get(key)
    if runner:
        runner["stop"].set()
        return jsonify({"ok": True})
    return jsonify({"ok": False, "error": "لا توجد مهمة عاملة"}), 404

@app.route("/resume", methods=["POST"])
def resume():
    # مثل start لكن لا يصفر التقدم
    handle = request.form.get("handle", "").strip()
    password = request.form.get("password", "").strip()
    post_url = request.form.get("post_url", "").strip()
    mode = request.form.get("process_type", "likes")
    min_delay = int(request.form.get("min_delay") or 200)
    max_delay = int(request.form.get("max_delay") or 250)
    messages_text = request.form.get("messages", "").strip()
    messages = [m for m in messages_text.splitlines() if m.strip()]

    cfg = Config(handle, password)
    cfg.min_delay = min_delay
    cfg.max_delay = max_delay

    if not (cfg.is_valid() and post_url and messages):
        return jsonify({"ok": False, "error": "المدخلات غير مكتملة"}), 400

    key = progress_key(cfg.bluesky_handle, post_url, mode)
    if key in RUNNERS:
        return jsonify({"ok": False, "error": "مهمة قيد التشغيل"}), 400

    stop_event = threading.Event()
    t = threading.Thread(target=runner_loop, args=(cfg, post_url, mode, messages), daemon=True)
    RUNNERS[key] = {"thread": t, "stop": stop_event}
    t.start()
    return jsonify({"ok": True})

@app.route("/status", methods=["GET"])
def status():
    # نعرض ملخصًا لكل مفتاح (قد يكون لديك أكثر من مهمة محفوظة)
    try:
        # حمّل الملف الكامل
        if os.path.exists(PROGRESS_PATH):
            import json
            with open(PROGRESS_PATH, "r") as f:
                all_prog = json.load(f)
        else:
            all_prog = {}
    except Exception:
        all_prog = {}

    # هو قيد التشغيل إذا كان له Runner حيّ
    running_keys = list(RUNNERS.keys())
    any_running = bool(running_keys)

    # حساب حقول الملخص المطلوبة
    summary: Dict[str, Dict[str, Any]] = {}
    for k, prog in all_prog.items():
        stats = prog.get("stats", {"ok": 0, "fail": 0})
        skipped = prog.get("skipped_no_posts", [])
        total = int(prog.get("total", len(prog.get("audience", []))))
        done = stats.get("ok", 0) + stats.get("fail", 0) + len(skipped)
        success = stats.get("ok", 0)
        fail = stats.get("fail", 0)
        last_error = prog.get("last_error")
        summary[k] = {
            "total_audience": total,
            "done": done,
            "ok": success,
            "fail": fail,
            "skipped": len(skipped),
            "last_error": last_error,
        }

    return jsonify({"running": any_running, "progress": all_prog, "summary": summary})

# نقطة دخول غونيكورن
app = app
