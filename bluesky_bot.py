# bluesky_bot.py
import os
import random
import threading
import time
from typing import List, Dict

from flask import Flask, request, jsonify, render_template_string

from config import Config, PROGRESS_PATH, DEFAULT_MIN_DELAY, DEFAULT_MAX_DELAY
from utils import (
    make_client,
    resolve_post_from_url,
    fetch_audience,
    has_posts,
    latest_post_uri,
    reply_to_post,
    load_progress,
    save_progress,
)

app = Flask(__name__)

# حالة المهمة (داخل الذاكرة)
_worker_thread: threading.Thread | None = None
_stop_flag = threading.Event()
_lock = threading.Lock()

# قالب الواجهة (HTML داخل الملف لتفادي مشاكل المسارات)
INDEX_HTML = """
<!doctype html><html lang="ar" dir="rtl"><head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>لوحة تحكم بوت Bluesky</title>
<link rel="preconnect" href="https://fonts.googleapis.com"><link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Tajawal:wght@400;600&display=swap" rel="stylesheet">
<style>
  body{font-family:Tajawal,system-ui,Arial;background:#0f1221;color:#e9ecf1;margin:0;padding:0}
  .card{max-width:880px;margin:28px auto;background:#151936;border:1px solid #2b2f55;border-radius:14px;padding:16px 18px;box-shadow:0 6px 24px rgba(0,0,0,.25)}
  h1{font-size:22px;margin:6px 0 14px}
  label{font-size:14px;color:#b7c0de;margin:6px 2px;display:block}
  input,select,textarea{width:100%;padding:10px 12px;border-radius:10px;border:1px solid #2e335f;background:#101433;color:#e9ecf1;outline:none}
  textarea{min-height:160px;white-space:pre}
  .row{display:grid;grid-template-columns:1fr 1fr;gap:12px}
  .btns{display:flex;gap:10px;flex-wrap:wrap;margin-top:10px}
  button{border:0;border-radius:10px;padding:10px 14px;cursor:pointer}
  .start{background:#1db954;color:#04120a;font-weight:700}
  .stop{background:#ff4d4d;color:#1e0707;font-weight:700}
  .resume{background:#3ea2ff;color:#02131f;font-weight:700}
  .ghost{background:#2a315f;color:#d1d9ff}
  .pill{display:inline-block;background:#1f2347;border:1px solid #323a77;padding:8px 10px;border-radius:10px;font-size:14px;margin:2px 0}
  .muted{color:#93a1c5;font-size:13px}
  pre{background:#0e1230;border:1px solid #2b2f55;border-radius:10px;padding:12px;max-height:200px;overflow:auto}
</style>
</head><body>
<div class="card">
  <h1>لوحة تحكم بوت <b>Bluesky</b></h1>
  <div class="row">
    <div>
      <label>حساب Bluesky (handle)</label>
      <input id="handle" placeholder="user.bsky.social">

      <label>كلمة المرور (App Password)</label>
      <input id="password" type="password" placeholder="xxxx-xxxx-xxxx-xxxx">

      <label>رابط المنشور (لجلب الجمهور)</label>
      <input id="post_url" placeholder="https://bsky.app/profile/handle/post/rkey">

      <label>نوع المعالجة</label>
      <select id="mode">
        <option value="likers">المعجبون (Likers)</option>
        <option value="reposters">معيدو النشر (Reposters)</option>
      </select>

      <div class="row">
        <div>
          <label>الحد الأدنى للتأخير (ثوان)</label>
          <input id="min_delay" type="number" min="0" value="{{min_delay}}">
        </div>
        <div>
          <label>الحد الأقصى للتأخير (ثوان)</label>
          <input id="max_delay" type="number" min="0" value="{{max_delay}}">
        </div>
      </div>

      <label>الرسائل (سطر لكل رسالة، سيُختار عشوائياً لكل مستخدم)</label>
      <textarea id="messages" placeholder="اكتب كل رسالة في سطر مستقل."></textarea>

      <div class="btns">
        <button class="start" onclick="startTask()">بدء المهمة ✓</button>
        <button class="stop" onclick="stopTask()">إيقاف ⛔</button>
        <button class="resume" onclick="resumeTask()">استئناف ▶️</button>
        <button class="ghost" onclick="refreshStatus()">تحديث الحالة 🔄</button>
      </div>
      <p class="muted">يحفظ التقدّم تلقائياً في ملف <code>{{progress_path}}</code>.</p>
    </div>

    <div>
      <label>حالة التشغيل</label>
      <div class="row">
        <div class="pill">الحالة: <span id="state">-</span></div>
        <div class="pill">إجمالي الجمهور: <span id="total">0</span></div>
        <div class="pill">منجز: <span id="ok">0</span></div>
        <div class="pill">فشل: <span id="fail">0</span></div>
      </div>
      <label>آخر خطأ</label>
      <div class="pill" style="width:100%"><span id="last_error">-</span></div>

      <label>ملخّص لكل مستخدم (did → الحالة)</label>
      <pre id="per_user">{}</pre>
    </div>
  </div>
</div>

<script>
async function startTask(){
  const body = {
    handle: document.getElementById('handle').value.trim(),
    password: document.getElementById('password').value.trim(),
    post_url: document.getElementById('post_url').value.trim(),
    mode: document.getElementById('mode').value,
    min_delay: Number(document.getElementById('min_delay').value),
    max_delay: Number(document.getElementById('max_delay').value),
    messages: document.getElementById('messages').value
  };
  const r = await fetch('/start', {method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify(body)});
  const j = await r.json(); alert(j.msg || j.error || 'ok'); refreshStatus();
}
async function stopTask(){
  const r = await fetch('/stop', {method:'POST'}); const j = await r.json();
  alert(j.msg || j.error || 'ok'); refreshStatus();
}
async function resumeTask(){
  const r = await fetch('/resume', {method:'POST'}); const j = await r.json();
  alert(j.msg || j.error || 'ok'); refreshStatus();
}
async function refreshStatus(){
  const r = await fetch('/status'); const s = await r.json();
  document.getElementById('state').innerText = s.state;
  document.getElementById('total').innerText = s.stats.total;
  document.getElementById('ok').innerText = s.stats.ok;
  document.getElementById('fail').innerText = s.stats.fail;
  document.getElementById('last_error').innerText = s.last_error || '-';
  document.getElementById('per_user').innerText = JSON.stringify(s.per_user || {}, null, 2);
}
refreshStatus();
</script>
</body></html>
"""

# -------------- صفحة رئيسية --------------
@app.get("/")
def index():
    return render_template_string(
        INDEX_HTML,
        min_delay=DEFAULT_MIN_DELAY,
        max_delay=DEFAULT_MAX_DELAY,
        progress_path=PROGRESS_PATH,
    )

# -------------- APIs --------------
@app.get("/status")
def status():
    return jsonify(load_progress(PROGRESS_PATH))

def _run_worker(cfg: Config, post_url: str, mode: str, messages: List[str]):
    progress = load_progress(PROGRESS_PATH)
    progress["state"] = "Running"
    progress["task"] = {
        "handle": cfg.bluesky_handle,
        "mode": mode,
        "min_delay": cfg.min_delay,
        "max_delay": cfg.max_delay,
        "post_url": post_url,
    }
    progress["last_error"] = "-"
    save_progress(PROGRESS_PATH, progress)

    try:
        client = make_client(cfg.bluesky_handle, cfg.bluesky_password)
        did, rkey, post_uri = resolve_post_from_url(client, post_url)

        # الجمهور (حسب النوع) بالترتيب
        audience = fetch_audience(client, mode, post_uri)
        # تصفية من لا يملك منشورات
        filtered = []
        for a in audience:
            try:
                if has_posts(client, a["did"]):
                    filtered.append(a)
            except Exception:
                # تجاهل بصمت
                pass

        with _lock:
            progress["audience"] = filtered
            progress["index"] = progress.get("index", 0)
            progress["stats"]["total"] = len(filtered)
            save_progress(PROGRESS_PATH, progress)

        # التنفيذ
        while True:
            # التوقف؟
            if _stop_flag.is_set():
                with _lock:
                    progress["state"] = "Idle"
                    save_progress(PROGRESS_PATH, progress)
                return

            with _lock:
                i = progress.get("index", 0)
                if i >= len(progress["audience"]):
                    progress["state"] = "Idle"
                    save_progress(PROGRESS_PATH, progress)
                    return
                user = progress["audience"][i]

            # إرسال رد على آخر منشور للمستخدم
            try:
                target_uri = latest_post_uri(client, user["did"])
                if not target_uri:
                    raise RuntimeError("skipped_no_posts")

                msg = random.choice(messages).strip()
                if not msg:
                    raise RuntimeError("empty_message")

                reply_to_post(client, target_uri, msg)

                with _lock:
                    progress["per_user"][user["did"]] = "ok"
                    progress["stats"]["ok"] += 1
                    progress["index"] = i + 1
                    progress["last_error"] = "-"
                    save_progress(PROGRESS_PATH, progress)

            except Exception as e:
                with _lock:
                    progress["per_user"][user["did"]] = f"fail: {e}"
                    progress["stats"]["fail"] += 1
                    progress["index"] = i + 1
                    progress["last_error"] = str(e)
                    save_progress(PROGRESS_PATH, progress)

            # انتظار بين المستخدمين
            delay = random.randint(cfg.min_delay, cfg.max_delay)
            for _ in range(delay):
                if _stop_flag.is_set():
                    with _lock:
                        progress["state"] = "Idle"
                        save_progress(PROGRESS_PATH, progress)
                    return
                time.sleep(1)

    except Exception as e:
        with _lock:
            progress["state"] = "Idle"
            progress["last_error"] = f"Client Error: {e}"
            save_progress(PROGRESS_PATH, progress)

@app.post("/start")
def start():
    global _worker_thread
    body = request.get_json(force=True)
    handle = (body.get("handle") or "").strip()
    password = (body.get("password") or "").strip()
    post_url = (body.get("post_url") or "").strip()
    mode = (body.get("mode") or "likers").strip().lower()
    min_delay = int(body.get("min_delay") or DEFAULT_MIN_DELAY)
    max_delay = int(body.get("max_delay") or DEFAULT_MAX_DELAY)
    messages_raw = body.get("messages") or ""
    messages = [m.strip() for m in messages_raw.splitlines() if m.strip()]

    if not (handle and password and post_url and messages):
        return jsonify(error="الرجاء تعبئة الحقول (الحساب/كلمة المرور/الرابط/الرسائل)"), 400
    if mode not in ("likers", "reposters"):
        return jsonify(error="نوع المعالجة يجب أن يكون likers أو reposters"), 400
    if min_delay > max_delay:
        min_delay, max_delay = max_delay, min_delay

    cfg = Config(handle, password, min_delay, max_delay)

    # إعادة تهيئة المؤشرات
    progress = load_progress(PROGRESS_PATH)
    progress.update({
        "state": "Queued",
        "task": {"handle": handle, "mode": mode, "min_delay": min_delay, "max_delay": max_delay, "post_url": post_url},
        "audience": [],
        "index": 0,
        "stats": {"ok": 0, "fail": 0, "total": 0},
        "per_user": {},
        "last_error": "-",
    })
    save_progress(PROGRESS_PATH, progress)

    # شغل الخيط
    _stop_flag.clear()
    if _worker_thread and _worker_thread.is_alive():
        return jsonify(error="المهمة تعمل بالفعل"), 400

    _worker_thread = threading.Thread(
        target=_run_worker, args=(cfg, post_url, mode, messages), daemon=True
    )
    _worker_thread.start()
    return jsonify(msg="تم بدء المهمة")

@app.post("/stop")
def stop():
    _stop_flag.set()
    return jsonify(msg="تم إرسال أمر الإيقاف")

@app.post("/resume")
def resume():
    global _worker_thread
    progress = load_progress(PROGRESS_PATH)

    task = progress.get("task") or {}
    handle = task.get("handle")
    post_url = task.get("post_url")
    mode = task.get("mode")
    min_delay = task.get("min_delay") or DEFAULT_MIN_DELAY
    max_delay = task.get("max_delay") or DEFAULT_MAX_DELAY

    if not (handle and post_url and mode):
        return jsonify(error="لا توجد مهمة محفوظة لاستئنافها"), 400

    # الرسائل: نعيد استخدام الرسائل السابقة إن وُجدت ضمن per_user؟ الأفضل نطلبها من جديد
    # لتبسيط الاستئناف سنستخدم رسائل افتراضية إن لم تُرسل هذه المرة
    messages = request.get_json(silent=True) or {}
    msgs_raw = (messages.get("messages") or "").strip()
    if not msgs_raw:
        msgs_raw = "Thanks for reading.\nAppreciate your support.\n"
    msgs = [m.strip() for m in msgs_raw.splitlines() if m.strip()]

    cfg = Config(handle, os.getenv("BSKY_PASSWORD") or "", min_delay, max_delay)
    # عند الاستئناف نحتاج كلمة مرور؛ لتجنب ظهورها بالواجهة نخزنها من البداية فقط بالرام.
    # فلو غيرتي السيرفس قد يلزم إدخالها مجدداً عبر /start.
    if not cfg.bluesky_password:
        return jsonify(error="لا يمكن الاستئناف بدون كلمة المرور. ابدئي من جديد عبر بدء المهمة."), 400

    _stop_flag.clear()
    if _worker_thread and _worker_thread.is_alive():
        return jsonify(error="المهمة تعمل بالفعل"), 400

    _worker_thread = threading.Thread(
        target=_run_worker, args=(cfg, post_url, mode, msgs), daemon=True
    )
    _worker_thread.start()
    return jsonify(msg="تم استئناف المهمة")

# --------- نقطة دخول WSGI ---------
if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", "10000")))
