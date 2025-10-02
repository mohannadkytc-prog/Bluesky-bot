# -*- coding: utf-8 -*-
"""
Bluesky Bot - Flask app + worker
- يجلب المعجبين أو معيدي النشر من رابط بوست محدد (بالترتيب).
- يرد على آخر منشور لكل مستخدم.
- يحفظ التقدم ويتيح الإيقاف/الاستئناف.
"""

import os
import json
import time
import random
import threading
from typing import Dict, Any, List, Optional

from flask import Flask, request, jsonify, render_template_string
from atproto import Client, models as bsky_models

# ===== مسارات التخزين (DATA أولاً ثم TMP) =====
DATA_DIR = "/data" if os.path.exists("/data") else "/tmp"
os.makedirs(DATA_DIR, exist_ok=True)
PROGRESS_PATH = os.path.join(DATA_DIR, "progress.json")

# ===== تطبيق Flask (مطلوب لـ gunicorn) =====
app = Flask(__name__)

# ===== حالة الجلسة (مشتركة) =====
state_lock = threading.Lock()
state: Dict[str, Any] = {
    "running": False,
    "paused": False,
    "thread": None,               # type: Optional[threading.Thread]
    "cfg": {},                    # إعدادات التشغيل الحالية
    "stats": {"total": 0, "done": 0, "fail": 0},
    "current": {"index": 0, "did": None, "task": "-"},
    "last_error": "",
    "per_user": {}                # did -> {"ok": bool, "error": str | None}
}

# ===== قوالب HTML صغيرة (بسيطة) =====
# تستخدم render_template_string لتعمل دون ملف خارجي؛
# لو تحب، انقلها إلى templates/index.html واستبدل الاستدعاء بـ render_template('index.html')
INDEX_HTML = """
<!doctype html>
<html lang="ar" dir="rtl">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Bluesky Bot</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<style>
 body{font-family:system-ui,-apple-system,Segoe UI,Roboto; background:#f6f7fb; margin:0; padding:0;}
 .wrap{max-width:980px;margin:32px auto;padding:0 12px}
 .card{background:#fff;border-radius:14px;box-shadow:0 6px 20px rgba(0,0,0,.08);padding:18px 18px;margin:14px 0}
 h1{margin:0 0 12px}
 .row{display:flex;gap:12px;flex-wrap:wrap}
 .col{flex:1;min-width:240px}
 label{display:block;margin:10px 0 6px;font-weight:600}
 input[type=text],input[type=password],input[type=number],textarea,select{
   width:100%;padding:10px 12px;border:1px solid #ddd;border-radius:10px;background:#fafafa;outline:none
 }
 textarea{min-height:140px;white-space:pre}
 button{cursor:pointer;border:0;border-radius:10px;padding:10px 14px;font-weight:700}
 .btn-go{background:#16a34a;color:#fff}
 .btn-stop{background:#ef4444;color:#fff}
 .btn-resume{background:#3b82f6;color:#fff}
 .btn-refresh{background:#f59e0b;color:#fff}
 .kpi{display:grid;grid-template-columns:repeat(auto-fit,minmax(180px,1fr));gap:12px}
 .kpi .box{background:#fff;border-radius:12px;padding:14px 14px;box-shadow:0 4px 14px rgba(0,0,0,.06)}
 .box h3{margin:4px 0 6px;font-size:14px;color:#555}
 .box .v{font-size:22px;font-weight:900}
 pre{background:#0b1220;color:#d1e7ff;border-radius:12px;padding:12px;overflow:auto}
 small{color:#777}
</style>
</head>
<body>
<div class="wrap">
  <div class="card">
    <h1>Bluesky Bot</h1>
    <div class="row">
      <div class="col">
        <label>حساب بلوسكاي (handle)</label>
        <input id="handle" type="text" placeholder="you.bsky.social">
      </div>
      <div class="col">
        <label>كلمة مرور التطبيق (App Password)</label>
        <input id="password" type="password" placeholder="xxxx-xxxx-xxxx-xxxx">
      </div>
    </div>

    <div class="row">
      <div class="col">
        <label>رابط البوست الهدف (لجلب الجمهور منه)</label>
        <input id="target_url" type="text" placeholder="https://bsky.app/profile/.../post/...">
      </div>
      <div class="col">
        <label>نوع المعالجة</label>
        <select id="mode">
          <option value="likers">المعجبون فقط (Likers)</option>
          <option value="reposters">إعادة نشر فقط (Reposters)</option>
        </select>
      </div>
    </div>

    <div class="row">
      <div class="col">
        <label>الحد الأدنى للتأخير (ثوان)</label>
        <input id="min_delay" type="number" value="200">
      </div>
      <div class="col">
        <label>الحد الأقصى للتأخير (ثوان)</label>
        <input id="max_delay" type="number" value="250">
      </div>
    </div>

    <div class="row">
      <div class="col">
        <label>الرسائل (سطر لكل رسالة؛ سيختار البوت عشوائيًا لكل مستخدم)</label>
        <textarea id="messages" placeholder="أدخل كل رسالة في سطر منفصل"></textarea>
      </div>
    </div>

    <div class="row" style="gap:10px">
      <button class="btn-go" onclick="startTask()">بدء المهمة ✅</button>
      <button class="btn-stop" onclick="stopTask()">إيقاف ⛔</button>
      <button class="btn-resume" onclick="resumeTask()">استئناف ▶️</button>
      <button class="btn-refresh" onclick="refresh()">تحديث الحالة 🔄</button>
    </div>

    <small>سيُحفظ التقدم تلقائيًا في ملف: <code>{{progress_path}}</code></small>
  </div>

  <div class="card">
    <h2>حالة التشغيل</h2>
    <div class="kpi">
      <div class="box">
        <h3>الحالة</h3><div class="v" id="k_state">Idle</div>
      </div>
      <div class="box">
        <h3>المهمة الحالية</h3><div class="v" id="k_task">—</div>
      </div>
      <div class="box">
        <h3>إجمالي الجمهور</h3><div class="v" id="k_total">0</div>
      </div>
      <div class="box">
        <h3>منجز</h3><div class="v" id="k_done">0</div>
      </div>
      <div class="box">
        <h3>فشل</h3><div class="v" id="k_fail">0</div>
      </div>
    </div>

    <h3>آخر خطأ</h3>
    <pre id="k_error">(لا يوجد)</pre>

    <h3>ملخص التقدم (حسب المستخدم)</h3>
    <pre id="k_users">{}</pre>
  </div>
</div>

<script>
async function startTask(){
  const body = {
    handle: document.getElementById('handle').value.trim(),
    password: document.getElementById('password').value.trim(),
    target_url: document.getElementById('target_url').value.trim(),
    mode: document.getElementById('mode').value,
    min_delay: parseInt(document.getElementById('min_delay').value||'200'),
    max_delay: parseInt(document.getElementById('max_delay').value||'250'),
    messages: document.getElementById('messages').value.split('\\n').map(x=>x.trim()).filter(x=>x.length>0)
  };
  const r = await fetch('/start', {method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)});
  alert((await r.json()).msg || 'تم البدء');
  refresh();
}
async function stopTask(){
  const r = await fetch('/stop', {method:'POST'});
  alert((await r.json()).msg || 'تم الإيقاف');
  refresh();
}
async function resumeTask(){
  const r = await fetch('/resume', {method:'POST'});
  alert((await r.json()).msg || 'تم الاستئناف');
  refresh();
}
async function refresh(){
  const r = await fetch('/status'); const s = await r.json();
  document.getElementById('k_state').textContent = s.running ? (s.paused ? 'Paused' : 'Running') : 'Idle';
  document.getElementById('k_task').textContent = s.current?.task || '—';
  document.getElementById('k_total').textContent = s.stats?.total ?? 0;
  document.getElementById('k_done').textContent = s.stats?.done ?? 0;
  document.getElementById('k_fail').textContent = s.stats?.fail ?? 0;
  document.getElementById('k_error').textContent = s.last_error || '(لا يوجد)';
  document.getElementById('k_users').textContent = JSON.stringify(s.per_user||{}, null, 2);
}
setInterval(()=>{ refresh().catch(()=>{}); }, 5000);
window.addEventListener('load', refresh);
</script>
</body>
</html>
"""

# ===== أدوات مساعدة للتخزين =====
def save_progress(blob: Dict[str, Any]) -> None:
    try:
        with open(PROGRESS_PATH, "w", encoding="utf-8") as f:
            json.dump(blob, f, ensure_ascii=False, indent=2)
    except Exception as e:
        with state_lock:
            state["last_error"] = f"save_progress: {e}"

def load_progress() -> Dict[str, Any]:
    if not os.path.exists(PROGRESS_PATH):
        return {}
    try:
        with open(PROGRESS_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception as e:
        with state_lock:
            state["last_error"] = f"load_progress: {e}"
        return {}

# ===== أدوات Bluesky =====
def login_client(handle: str, password: str) -> Client:
    c = Client()
    c.login(handle, password)
    return c

def resolve_post_from_url(client: Client, url: str) -> bsky_models.AppBskyFeedDefs.PostView:
    """
    يحوّل رابط التطبيق إلى uri/cid ثم يجلب بيانات البوست
    """
    # روابط bsky.app تكون عادة: https://bsky.app/profile/{actor}/post/{rkey}
    try:
        parts = url.rstrip("/").split("/")
        rkey = parts[-1]
        actor = parts[-3]
        # resolve handle -> did
        did = client.resolve_handle(actor).did
        # get post thread to obtain uri/cid
        thread = client.app.bsky.feed.get_post_thread(params={"uri": f"at://{did}/app.bsky.feed.post/{rkey}", "depth": 0})
        post = thread.thread.post  # PostView
        return post
    except Exception as e:
        raise RuntimeError(f"تعذّر تفسير الرابط/جلب البوست: {e}")

def get_audience_ordered(client: Client, target_url: str, mode: str) -> List[str]:
    """
    يعيد قائمة DIDs بالترتيب (كما يظهر في التطبيق: الأعلى ثم الأدنى).
    mode in {"likers","reposters"}
    """
    post = resolve_post_from_url(client, target_url)
    uri = post.uri

    dids: List[str] = []
    cursor = None
    try:
        if mode == "likers":
            while True:
                resp = client.app.bsky.feed.get_likes(params={"uri": uri, "cursor": cursor, "limit": 100})
                for it in resp.likes:
                    dids.append(it.actor.did)
                cursor = getattr(resp, "cursor", None)
                if not cursor:
                    break
        else:  # reposters
            while True:
                resp = client.app.bsky.feed.get_reposted_by(params={"uri": uri, "cursor": cursor, "limit": 100})
                for it in resp.reposted_by:
                    dids.append(it.did)
                cursor = getattr(resp, "cursor", None)
                if not cursor:
                    break
    except Exception as e:
        raise RuntimeError(f"فشل جلب الجمهور: {e}")

    # إزالة التكرارات مع الحفاظ على الترتيب (لو تكرّر DID لسبب ما)
    seen = set()
    ordered = []
    for d in dids:
        if d not in seen:
            seen.add(d)
            ordered.append(d)
    return ordered

def reply_to_latest_post(client: Client, target_did: str, text: str) -> None:
    """
    يرد على آخر منشور للمستخدم (إن وجد). إن لم يوجد منشورات => raise Skip
    """
    # feed المؤلف
    feed = client.app.bsky.feed.get_author_feed(params={"actor": target_did, "limit": 1})
    if not feed.feed:
        raise RuntimeError("skipped_no_posts")

    item = feed.feed[0]
    post_view = item.post
    if not getattr(post_view, "uri", None):
        raise RuntimeError("skipped_no_posts")

    # send reply
    client.send_post(text=text, reply_to=post_view)

# ===== الـ Worker =====
def worker_loop():
    while True:
        with state_lock:
            running = state["running"]
            paused = state["paused"]
            cfg = dict(state["cfg"])
            idx = state["current"]["index"]
        if not running:
            break
        if paused:
            time.sleep(1.0)
            continue

        try:
            handle = cfg["handle"]
            password = cfg["password"]
            target_url = cfg["target_url"]
            mode = cfg["mode"]
            min_delay = int(cfg["min_delay"])
            max_delay = int(cfg["max_delay"])
            messages = list(cfg.get("messages") or [])
            if not messages:
                messages = ["Hello!"]

            # login مرة واحدة لكل دورة كاملة
            client = login_client(handle, password)

            # audience محملة مسبقًا في cfg (إن لم تكن، اجلبها)
            audience = cfg.get("audience")
            if not audience:
                audience = get_audience_ordered(client, target_url, mode)
                with state_lock:
                    state["cfg"]["audience"] = audience
                    state["stats"]["total"] = len(audience)
                    save_progress({"cfg": state["cfg"], "stats": state["stats"], "current": state["current"], "per_user": state["per_user"]})

            total = len(audience)
            while idx < total:
                did = audience[idx]
                msg = random.choice(messages)

                with state_lock:
                    state["current"]["did"] = did
                    state["current"]["task"] = f"{'Reply'} to {did} ({idx+1}/{total})"
                try:
                    reply_to_latest_post(client, did, msg)
                    with state_lock:
                        state["stats"]["done"] += 1
                        state["per_user"][did] = {"ok": True, "error": None}
                        state["current"]["index"] = idx + 1
                        save_progress({"cfg": state["cfg"], "stats": state["stats"], "current": state["current"], "per_user": state["per_user"]})
                except Exception as e:
                    err = str(e)
                    with state_lock:
                        state["stats"]["fail"] += 1
                        state["last_error"] = err
                        state["per_user"][did] = {"ok": False, "error": err}
                        state["current"]["index"] = idx + 1
                        save_progress({"cfg": state["cfg"], "stats": state["stats"], "current": state["current"], "per_user": state["per_user"]})

                # تأخير بين كل مستخدم
                delay = random.uniform(min_delay, max_delay)
                for _ in range(int(delay)):
                    with state_lock:
                        if not state["running"] or state["paused"]:
                            break
                    time.sleep(1.0)
                if not state["running"] or state["paused"]:
                    break

                idx += 1

            # انتهت الدورة
            with state_lock:
                state["running"] = False
                state["paused"] = False
                state["current"]["task"] = "—"
                save_progress({"cfg": state["cfg"], "stats": state["stats"], "current": state["current"], "per_user": state["per_user"]})

        except Exception as e:
            with state_lock:
                state["last_error"] = f"worker_loop: {e}"
            time.sleep(2.0)

# ===== واجهات الويب =====
@app.route("/", methods=["GET"])
def index():
    return render_template_string(INDEX_HTML, progress_path=PROGRESS_PATH)

@app.route("/start", methods=["POST"])
def start():
    body = request.get_json(force=True)

    # تحقق أساسي
    req_keys = ["handle", "password", "target_url", "mode", "min_delay", "max_delay"]
    for k in req_keys:
        if not body.get(k):
            return jsonify({"ok": False, "msg": f"حقل {k} مطلوب"}), 400

    # تهيئة الحالة
    with state_lock:
        if state["running"]:
            return jsonify({"ok": False, "msg": "المهمة قيد التشغيل بالفعل"}), 400

        state["running"] = True
        state["paused"] = False
        state["cfg"] = {
            "handle": body["handle"],
            "password": body["password"],
            "target_url": body["target_url"],
            "mode": body["mode"],
            "min_delay": int(body["min_delay"]),
            "max_delay": int(body["max_delay"]),
            "messages": body.get("messages", []),
            "audience": []  # ستمتلئ عند التشغيل
        }
        state["stats"] = {"total": 0, "done": 0, "fail": 0}
        state["per_user"] = {}
        state["current"] = {"index": 0, "did": None, "task": "Booting"}
        state["last_error"] = ""
        save_progress({"cfg": state["cfg"], "stats": state["stats"], "current": state["current"], "per_user": state["per_user"]})

        t = threading.Thread(target=worker_loop, daemon=True)
        state["thread"] = t
        t.start()

    return jsonify({"ok": True, "msg": "تم بدء المهمة"})

@app.route("/stop", methods=["POST"])
def stop():
    with state_lock:
        state["running"] = False
        state["paused"] = False
        save_progress({"cfg": state["cfg"], "stats": state["stats"], "current": state["current"], "per_user": state["per_user"]})
    return jsonify({"ok": True, "msg": "تم إيقاف المهمة"})

@app.route("/resume", methods=["POST"])
def resume():
    with state_lock:
        # استئناف من الملف لو كنا متوقفين
        if state["running"]:
            state["paused"] = False
            return jsonify({"ok": True, "msg": "تم استئناف المهمة الحالية"})

        snap = load_progress()
        cfg = snap.get("cfg")
        current = snap.get("current", {"index": 0})
        stats = snap.get("stats", {"total": 0, "done": 0, "fail": 0})
        per_user = snap.get("per_user", {})

        if not cfg:
            return jsonify({"ok": False, "msg": "لا يوجد تقدم محفوظ للاستئناف"}), 400

        state["cfg"] = cfg
        state["stats"] = stats
        state["per_user"] = per_user
        state["current"] = current
        state["running"] = True
        state["paused"] = False
        state["last_error"] = ""

        t = threading.Thread(target=worker_loop, daemon=True)
        state["thread"] = t
        t.start()

    return jsonify({"ok": True, "msg": "تم استئناف المهمة من حيث توقفت"})

@app.route("/status", methods=["GET"])
def status():
    with state_lock:
        return jsonify({
            "running": state["running"],
            "paused": state["paused"],
            "cfg": {
                "mode": state["cfg"].get("mode"),
                "min_delay": state["cfg"].get("min_delay"),
                "max_delay": state["cfg"].get("max_delay"),
            },
            "stats": state["stats"],
            "current": state["current"],
            "last_error": state["last_error"],
            "per_user": state["per_user"]
        })

# ===== تشغيل محلي (اختياري) =====
if __name__ == "__main__":
    # للتجربة محليًا:
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", "8000")))
