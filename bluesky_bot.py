# bluesky_bot.py
import os
import json
import random
import threading
import time
from typing import Dict, Any, List
from flask import Flask, request, jsonify, render_template_string

# استيراد الدوال من utils.py (كما أرسلتها لك سابقًا)
from utils import (
    resolve_post_from_url,
    get_likers,
    get_reposters,
    has_posts,
    reply_to_latest_post,
)

# ===== مسارات التخزين (/data إن وُجدت، وإلا /tmp) =====
DATA_DIR = "/data" if os.path.exists("/data") else "/tmp"
os.makedirs(DATA_DIR, exist_ok=True)
PROGRESS_PATH = os.path.join(DATA_DIR, "progress.json")

# حالة وتشغيل
app = Flask(__name__)
lock = threading.Lock()
runner_thread: threading.Thread = None
RUNNING = False
STOP_REQUESTED = False

state: Dict[str, Any] = {
    "status": "Idle",
    "mode": "likers",  # "likers" or "reposters"
    "min_delay": 200,
    "max_delay": 250,
    "messages": [],
    "post_url": "",
    "total_audience": 0,
    "done": 0,
    "fail": 0,
    "last_error": "-",
    "summary": {},  # per DID: {"status": "...", "error": "..."}
    "task": "",
}

def save_state():
    with lock:
        tmp = state.copy()
    try:
        with open(PROGRESS_PATH, "w", encoding="utf-8") as f:
            json.dump(tmp, f, ensure_ascii=False, indent=2)
    except Exception:
        pass

def load_state():
    if os.path.exists(PROGRESS_PATH):
        try:
            with open(PROGRESS_PATH, "r", encoding="utf-8") as f:
                data = json.load(f)
            with lock:
                state.update(data)
        except Exception:
            pass

load_state()

# ============== نواة المعالجة ==============
def process_audience(handle: str, password: str):
    global RUNNING, STOP_REQUESTED, runner_thread

    try:
        headers, repo_did = get_api(handle, password)
    except Exception as e:
        with lock:
            state["status"] = "Idle"
            state["last_error"] = f"فشل تسجيل الدخول: {e}"
        save_state()
        return

    # تحليل الرابط والحصول على URI
    try:
        _, _, uri = resolve_post_from_url(state["post_url"])
    except Exception as e:
        with lock:
            state["status"] = "Idle"
            state["last_error"] = f"رابط غير صالح/فشل التحليل: {e}"
        save_state()
        return

    # جلب الجمهور حسب النوع (من أعلى لأسفل)
    try:
        if state["mode"] == "likers":
            audience = get_likers(uri)
        else:
            audience = get_reposters(uri)
    except Exception as e:
        with lock:
            state["status"] = "Idle"
            state["last_error"] = f"تعذّر جلب الجمهور: {e}"
        save_state()
        return

    with lock:
        state["total_audience"] = len(audience)
        state["status"] = "Running"
        state["done"] = 0 if state["task"] == "" else state["done"]
        state["fail"] = 0 if state["task"] == "" else state["fail"]
        state["summary"] = state["summary"] if state["task"] else {}
        state["task"] = state["post_url"]
    save_state()

    # دالة مساعده لاختيار رسالة عشوائية (سطر لكل رسالة)
    def pick_message() -> str:
        msgs = [m.strip() for m in state["messages"] if m.strip()]
        if not msgs:
            return "🙏"  # رسالة بسيطة لو القائمة فارغة
        return random.choice(msgs)

    # نمشي على القائمة من الأعلى للأسفل
    first = True
    for did in audience:
        with lock:
            if STOP_REQUESTED:
                state["status"] = "Idle"
                STOP_REQUESTED = False
                RUNNING = False
                save_state()
                return

        # أول محاولة: بدون انتظار
        if not first:
            delay = random.randint(int(state["min_delay"]), int(state["max_delay"]))
            for _ in range(delay):
                # نوم تفاعلي قصير مع حفظ الحالة
                time.sleep(1)
        first = False

        # تجاهل من ليس لديه منشورات (حسب طلبك)
        try:
            if not has_posts(did):
                with lock:
                    state["summary"][did] = {"status": "skipped_no_posts"}
                save_state()
                continue
        except Exception as e:
            with lock:
                state["fail"] += 1
                state["last_error"] = f"فحص منشورات {did} فشل: {e}"
                state["summary"][did] = {"status": "error", "error": str(e)}
            save_state()
            continue

        # إرسال الرد على آخر منشور
        try:
            ok = reply_to_latest_post(headers, repo_did, did, pick_message())
            if ok:
                with lock:
                    state["done"] += 1
                    state["summary"][did] = {"status": "ok"}
            else:
                with lock:
                    state["fail"] += 1
                    state["summary"][did] = {"status": "error", "error": "send_failed"}
            save_state()
        except Exception as e:
            with lock:
                state["fail"] += 1
                state["last_error"] = f"إرسال رد لـ {did} فشل: {e}"
                state["summary"][did] = {"status": "error", "error": str(e)}
            save_state()

    with lock:
        state["status"] = "Idle"
        RUNNING = False
    save_state()


# ============== واجهة الويب ==============
INDEX_HTML = """
<!doctype html>
<html dir="rtl" lang="ar">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>Bluesky لوحة تحكم بوت</title>
  <style>
    body{font-family:system-ui,-apple-system,Segoe UI,Roboto;max-width:900px;margin:24px auto;padding:0 12px;}
    .card{border:1px solid #ddd;border-radius:12px;padding:16px;margin:12px 0;}
    input,select,textarea{width:100%;padding:10px;border:1px solid #ccc;border-radius:10px;margin:8px 0;}
    .row{display:grid;grid-template-columns:1fr 1fr;gap:12px}
    .btn{border:0;border-radius:10px;padding:10px 14px;margin:4px 6px;cursor:pointer;font-weight:600}
    .green{background:#10b981;color:#fff}
    .red{background:#ef4444;color:#fff}
    .blue{background:#3b82f6;color:#fff}
    .muted{color:#666;font-size:13px}
    pre{white-space:pre-wrap;word-break:break-word;background:#f6f7f8;padding:10px;border-radius:10px}
    .grid4{display:grid;grid-template-columns:repeat(4,1fr);gap:12px}
    .stat{border:1px solid #eee;border-radius:10px;padding:14px;text-align:center}
    .stat b{display:block;font-size:22px;margin-top:4px}
  </style>
</head>
<body>
  <h2>Bluesky لوحة تحكم بوت</h2>

  <div class="card">
    <div class="row">
      <div>
        <label>حساب Bluesky (handle)</label>
        <input id="handle" placeholder="name.bsky.social" />
      </div>
      <div>
        <label>كلمة المرور</label>
        <input id="password" type="password" />
      </div>
    </div>

    <label>رابط المنشور (لجلب الجمهور)</label>
    <input id="post_url" placeholder="https://bsky.app/profile/.../post/..." />

    <label>نوع المعالجة</label>
    <select id="mode">
      <option value="likers">المعجبون (Likers)</option>
      <option value="reposters">معيدو النشر (Reposters)</option>
    </select>

    <div class="row">
      <div>
        <label>الحد الأدنى للتأخير (ثوان)</label>
        <input id="min_delay" type="number" value="200" />
      </div>
      <div>
        <label>الحد الأقصى للتأخير (ثوان)</label>
        <input id="max_delay" type="number" value="250" />
      </div>
    </div>

    <label>الرسائل (سطر لكل رسالة؛ سيتم اختيار عشوائيًا لكل مستخدم)</label>
    <textarea id="messages" rows="6" placeholder="سطر = رسالة"></textarea>

    <div>
      <button class="btn green" onclick="startTask()">بدء المهمة ✅</button>
      <button class="btn red" onclick="stopTask()">إيقاف ⛔</button>
      <button class="btn blue" onclick="refresh()">تحديث الحالة 🔄</button>
    </div>
    <div class="muted">يحفظ التقدم تلقائيًا في ملف <code>{{progress_path}}</code>.</div>
  </div>

  <div class="card">
    <h3>حالة التشغيل</h3>
    <div class="grid4">
      <div class="stat">الحالة<b id="st_status">-</b></div>
      <div class="stat">إجمالي الجمهور<b id="st_total">0</b></div>
      <div class="stat">منجز<b id="st_done">0</b></div>
      <div class="stat">فشل<b id="st_fail">0</b></div>
    </div>
    <div class="row">
      <div class="stat">مهمة<b id="st_task">-</b></div>
      <div class="stat">آخر خطأ<b id="st_err">-</b></div>
    </div>
    <label>ملخص لكل مستخدم</label>
    <pre id="st_summary">{}</pre>
  </div>

<script>
async function startTask(){
  const payload = {
    handle: document.getElementById('handle').value.trim(),
    password: document.getElementById('password').value.trim(),
    post_url: document.getElementById('post_url').value.trim(),
    mode: document.getElementById('mode').value,
    min_delay: parseInt(document.getElementById('min_delay').value || "200"),
    max_delay: parseInt(document.getElementById('max_delay').value || "250"),
    messages: document.getElementById('messages').value.split('\\n')
  };
  const r = await fetch('/start', {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify(payload)});
  const j = await r.json();
  alert(j.msg || 'تم');
  refresh()
}
async function stopTask(){
  const r = await fetch('/stop', {method:'POST'});
  const j = await r.json();
  alert(j.msg || 'تم');
  refresh()
}
async function refresh(){
  const r = await fetch('/status');
  const st = await r.json();
  document.getElementById('st_status').innerText = st.status;
  document.getElementById('st_total').innerText = st.total_audience;
  document.getElementById('st_done').innerText = st.done;
  document.getElementById('st_fail').innerText = st.fail;
  document.getElementById('st_err').innerText = st.last_error || '-';
  document.getElementById('st_task').innerText = st.task || '-';
  document.getElementById('st_summary').innerText = JSON.stringify(st.summary || {}, null, 2);
}
refresh();
setInterval(refresh, 4000);
</script>
</body>
</html>
"""

@app.get("/")
def index():
    return render_template_string(INDEX_HTML, progress_path=PROGRESS_PATH)

@app.get("/status")
def get_status():
    with lock:
        return jsonify(state)

@app.post("/stop")
def stop():
    global STOP_REQUESTED
    with lock:
        STOP_REQUESTED = True
    return jsonify({"ok": True, "msg": "تم طلب إيقاف المهمة"})

@app.post("/start")
def start():
    global RUNNING, STOP_REQUESTED, runner_thread

    data = request.get_json(force=True)
    handle = data.get("handle", "").strip()
    password = data.get("password", "").strip()
    post_url = data.get("post_url", "").strip()
    mode = data.get("mode", "likers")
    min_delay = int(data.get("min_delay", 200))
    max_delay = int(data.get("max_delay", 250))
    messages = data.get("messages") or []

    if not handle or not password or not post_url:
        return jsonify({"ok": False, "msg": "يرجى إدخال الحساب وكلمة المرور والرابط"}), 400
    if min_delay < 0 or max_delay < 0 or min_delay > max_delay:
        return jsonify({"ok": False, "msg": "قيم التأخير غير صحيحة"}), 400
    if mode not in ("likers", "reposters"):
        mode = "likers"

    with lock:
        state["mode"] = mode
        state["min_delay"] = min_delay
        state["max_delay"] = max_delay
        state["messages"] = messages
        state["post_url"] = post_url
        state["last_error"] = "-"
        state["status"] = "Starting"
        # إعادة العدادات لهذه المهمة
        state["done"] = 0
        state["fail"] = 0
        state["summary"] = {}
        state["task"] = ""
    save_state()

    if RUNNING:
        return jsonify({"ok": False, "msg": "مهمة قيد التشغيل بالفعل"}), 400

    STOP_REQUESTED = False
    RUNNING = True
    runner_thread = threading.Thread(target=process_audience, args=(handle, password), daemon=True)
    runner_thread.start()
    return jsonify({"ok": True, "msg": "بدأت المهمة"})

# تطبيق WSGI
app = app
