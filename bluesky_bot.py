import random
import threading
import time
from typing import Dict, Any, List, Optional

from flask import Flask, render_template, request, jsonify

from atproto import Client, models

from config import Config, PROGRESS_PATH
from utils import (
    extract_post_info,
    load_progress,
    save_progress,
    save_progress_for_key,
    validate_message_template,
)

app = Flask(__name__)

# حالة الخدمة في الذاكرة
state: Dict[str, Any] = {
    "running": False,
    "current_task": None,       # dict يُخزن آخر مدخلات الشاشة
    "stop_event": threading.Event(),
    "lock": threading.Lock(),
}

# --------- Helpers: Bluesky ----------
def login_client(handle: str, password: str) -> Client:
    client = Client()
    client.login(handle, password)
    return client

def resolve_uri_from_post_url(client: Client, post_url: str) -> Optional[str]:
    """تحويل رابط bsky إلى at://did/app.bsky.feed.post/<rkey>"""
    info = extract_post_info(post_url)
    if not info:
        return None
    profile = client.app.bsky.actor.get_profile({"actor": info["username"]})
    did = profile.did
    return f"at://{did}/app.bsky.feed.post/{info['post_id']}"

def get_likers_in_order(client: Client, post_uri: str) -> List[str]:
    """إرجاع DIDs للمعجبين بالترتيب المعروض (الأحدث أولاً كما تعيد API)."""
    dids: List[str] = []
    cursor = None
    while True:
        resp = client.app.bsky.feed.get_likes({"uri": post_uri, "cursor": cursor, "limit": 100})
        for it in resp.likes:
            if it.actor and it.actor.did:
                dids.append(it.actor.did)
        cursor = resp.cursor
        if not cursor:
            break
    return dids

def get_reposters_in_order(client: Client, post_uri: str) -> List[str]:
    """إرجاع DIDs لمعيدي النشر بالترتيب المعروض (الأحدث أولاً)."""
    dids: List[str] = []
    cursor = None
    while True:
        resp = client.app.bsky.feed.get_reposted_by({"uri": post_uri, "cursor": cursor, "limit": 100})
        for it in resp.repostedBy:
            if it.did:
                dids.append(it.did)
        cursor = getattr(resp, "cursor", None)
        if not cursor:
            break
    return dids

def latest_post_uri_for_user(client: Client, did_or_handle: str) -> Optional[str]:
    """إحضار آخر بوست لحساب (إن لم يوجد يرجع None)."""
    feed = client.app.bsky.feed.get_author_feed({"actor": did_or_handle, "limit": 1})
    if not feed.feed:
        return None
    post = feed.feed[0].post
    return post.uri if post and post.uri else None

def send_reply(client: Client, parent_uri: str, text: str):
    """إرسال رد على بوست معيّن."""
    post = client.app.bsky.feed.get_posts({"uris": [parent_uri]}).posts[0]
    reply_ref = models.app.bsky.feed.post.ReplyRef(
        parent=models.com.atproto.repo.strong_ref.Main(uri=post.uri, cid=post.cid),
        root=models.com.atproto.repo.strong_ref.Main(uri=post.uri, cid=post.cid),
    )
    client.app.bsky.feed.post.create(
        models.ComAtprotoRepoCreateRecord.Data(
            repo=client.me.did,
            collection="app.bsky.feed.post",
            record=models.AppBskyFeedPost.Main(
                text=text,
                createdAt=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                reply=reply_ref,
            ),
        )
    )

# --------- Progress helpers for this task ----------
def progress_key(handle: str, post_url: str, mode: str) -> str:
    return f"{handle}|{post_url}|{mode}"

def build_queue_once(client: Client, post_url: str, mode: str) -> List[str]:
    post_uri = resolve_uri_from_post_url(client, post_url)
    if not post_uri:
        return []
    if mode == "likes":
        return get_likers_in_order(client, post_uri)
    elif mode == "reposts":
        return get_reposters_in_order(client, post_uri)
    else:
        return []

# --------- Worker ----------
def worker(task: Dict[str, Any]):
    """
    task: {
      handle, password, post_url, mode ('likes'|'reposts'),
      messages (List[str]), min_delay, max_delay
    }
    """
    handle = task["handle"]
    password = task["password"]
    post_url = task["post_url"]
    mode = task["mode"]
    messages: List[str] = task["messages"]
    min_delay, max_delay = int(task["min_delay"]), int(task["max_delay"])

    # سجّل مفتاح التقدم
    key = progress_key(handle, post_url, mode)

    # حمّل التقدم
    progress = load_progress()
    job = progress.get(key) or {
        "queue": [],          # list of DIDs المرتبة
        "index": 0,           # أين وصلنا
        "processed": [],      # قائمة DIDs تمّت
        "skipped_no_posts": [],
        "failures": [],
        "done": False,
        "stats": {"ok": 0, "fail": 0}
    }

    try:
        client = login_client(handle, password)

        # حضّر قائمة المستخدمين مرة واحدة فقط إذا فارغة
        if not job["queue"]:
            q = build_queue_once(client, post_url, mode)
            job["queue"] = q
            job["index"] = 0
            save_progress_for_key(key, job)

        # حلقة التنفيذ
        while job["index"] < len(job["queue"]) and not state["stop_event"].is_set():
            did = job["queue"][job["index"]]

            # تجاهل المُعالَجين
            if did in job["processed"]:
                job["index"] += 1
                continue

            # آخر بوست للمستخدم
            try:
                target_uri = latest_post_uri_for_user(client, did)
            except Exception:
                # لو فشل جلب الفيد نعدّه “لا بوست”
                target_uri = None

            if not target_uri:
                job["skipped_no_posts"].append(did)
                job["processed"].append(did)
                job["index"] += 1
                save_progress_for_key(key, job)
                # لا داعي للتأخير إذا لم نرسل شيئًا
                continue

            # اختر رسالة صالحة
            valid_msgs = [m.strip() for m in messages if validate_message_template(m.strip())]
            if not valid_msgs:
                valid_msgs = ["Thank you! 🙏"]  # أمان احتياطي

            msg = random.choice(valid_msgs)

            # أرسل الرد
            try:
                send_reply(client, target_uri, msg)
                job["stats"]["ok"] += 1
            except Exception as e:
                job["stats"]["fail"] += 1
                job["failures"].append({"did": did, "error": str(e)})

            job["processed"].append(did)
            job["index"] += 1
            save_progress_for_key(key, job)

            # تأخير بين المستخدمين
            if job["index"] < len(job["queue"]) and not state["stop_event"].is_set():
                delay = random.randint(min_delay, max_delay)
                for _ in range(delay):
                    if state["stop_event"].is_set():
                        break
                    time.sleep(1)

        # انتهى الدور
        if job["index"] >= len(job["queue"]):
            job["done"] = True
            save_progress_for_key(key, job)

    except Exception as e:
        # سجّل الفشل العام
        job.setdefault("failures", []).append({"did": None, "error": f"worker-fatal: {e}"})
        save_progress_for_key(key, job)
    finally:
        # إعادة حالة التشغيل
        with state["lock"]:
            state["running"] = False
            state["current_task"] = None
            state["stop_event"].clear()

# --------- Routes (UI) ----------
@app.route("/", methods=["GET"])
def home():
    # عرض الصفحة مع آخر حالة
    p = load_progress()
    return render_template("persistent.html",
                           running=state["running"],
                           progress=p,
                           progress_path=PROGRESS_PATH)

@app.route("/start", methods=["POST"])
def start_task():
    if state["running"]:
        return jsonify({"ok": False, "msg": "هناك مهمة تعمل فعلًا"}), 400

    handle = request.form.get("handle", "").strip()
    password = request.form.get("password", "").strip()
    post_url = request.form.get("post_url", "").strip()
    mode = request.form.get("mode", "likes").strip()  # likes | reposts
    min_delay = int(request.form.get("min_delay", "200"))
    max_delay = int(request.form.get("max_delay", "250"))
    # الرسائل: كل سطر = رسالة
    messages_text = request.form.get("messages", "").strip()
    messages = [m for m in [x.strip() for x in messages_text.split("\n")] if m]

    cfg = Config(handle, password, min_delay, max_delay)
    if not cfg.is_valid():
        return jsonify({"ok": False, "msg": "أدخل اليوزر + App Password"}), 400
    if not post_url:
        return jsonify({"ok": False, "msg": "أدخل رابط البوست"}), 400
    if mode not in ("likes", "reposts"):
        return jsonify({"ok": False, "msg": "نوع المعالجة غير صحيح"}), 400

    task = {
        "handle": cfg.bluesky_handle,
        "password": cfg.bluesky_password,
        "post_url": post_url,
        "mode": mode,
        "messages": messages,
        "min_delay": cfg.min_delay,
        "max_delay": cfg.max_delay,
    }

    with state["lock"]:
        state["running"] = True
        state["current_task"] = task
        state["stop_event"].clear()
        threading.Thread(target=worker, args=(task,), daemon=True).start()

    return jsonify({"ok": True})

@app.route("/stop", methods=["POST"])
def stop_task():
    state["stop_event"].set()
    return jsonify({"ok": True})

@app.route("/resume", methods=["POST"])
def resume_task():
    if state["running"]:
        return jsonify({"ok": False, "msg": "مهمة قيد التشغيل"}), 400
    # يعيد تشغيل آخر مهمة محفوظة في الذاكرة (من آخر /start ناجح)
    task = state.get("current_task")
    if not task:
        # حاول الاسترجاع من الواجهة مباشرة (نفس حقول /start)
        handle = request.form.get("handle", "").strip()
        password = request.form.get("password", "").strip()
        post_url = request.form.get("post_url", "").strip()
        mode = request.form.get("mode", "likes").strip()
        min_delay = int(request.form.get("min_delay", "200"))
        max_delay = int(request.form.get("max_delay", "250"))
        messages_text = request.form.get("messages", "").strip()
        messages = [m for m in [x.strip() for x in messages_text.split("\n")] if m]
        task = {
            "handle": handle,
            "password": password,
            "post_url": post_url,
            "mode": mode,
            "messages": messages,
            "min_delay": min_delay,
            "max_delay": max_delay,
        }

    with state["lock"]:
        state["running"] = True
        state["stop_event"].clear()
        threading.Thread(target=worker, args=(task,), daemon=True).start()

    return jsonify({"ok": True})

@app.route("/status", methods=["GET"])
def status():
    p = load_progress()
    running = state["running"]
    return jsonify({"running": running, "progress": p, "progress_path": PROGRESS_PATH})

# WSGI entry
def app_factory():
    return app
