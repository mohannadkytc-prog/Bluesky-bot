# bluesky_bot.py
import os
import re
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
    # === جديد لإدارة تقدّم كل حساب ===
    load_progress_for, save_progress_for, progress_path_for, _fp,
)

app = Flask(__name__)

# حالة المهمة (داخل الذاكرة)
_worker_thread: threading.Thread | None = None
_stop_flag = threading.Event()
_lock = threading.Lock()

# ---------------- ضبط فترات التشغيل/الراحة من متغيرات البيئة ----------------
def _env_minutes(name: str, default_min: int | None) -> int | None:
    try:
        v = os.getenv(name)
        if not v:
            return default_min
        x = int(v)
        return x if x > 0 else None
    except Exception:
        return default_min

RUN_MIN = _env_minutes("RUN_MINUTES", None)      # مثال: 60
REST_MIN = _env_minutes("REST_MINUTES", None)    # مثال: 20 أو 25

DATA_DIR = os.getenv("DATA_DIR", "/tmp")

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
      <textarea id="messages" placeholder="اكتب كل رسالة في سطر مستقل. يمكن استخدام {EMOJI} لوضع الإيموجي في مكان محدد."></textarea>

      <label>قائمة الإيموجي (افصل بينهم بمسافة أو فاصلة)</label>
      <input id="emojis" placeholder="💙 💔 🙏 ✨, 🕊️, 🌟">

      <div class="btns">
        <button class="start" onclick="startTask()">بدء المهمة ✓</button>
        <button class="stop" onclick="stopTask()">إيقاف ⛔</button>
        <button class="resume" onclick="resumeTask()">استئناف ▶️</button>
        <button class="ghost" onclick="refreshStatus()">تحديث الحالة 🔄</button>
      </div>
      <p class="muted">سيتم حفظ التقدّم لكل حساب في مجلد <code>{{data_dir}}</code> باسم <code>progress_&lt;handle&gt;.json</code>.</p>
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
    messages: document.getElementById('messages').value,
    emojis: document.getElementById('emojis').value
  };
  const r = await fetch('/start', {method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify(body)});
  const j = await r.json(); alert(j.msg || j.error || 'ok'); refreshStatus();
}
async function stopTask(){
  const r = await fetch('/stop', {method:'POST'}); const j = await r.json();
  alert(j.msg || j.error || 'ok'); refreshStatus();
}
async function resumeTask(){
  const body = {
    handle: document.getElementById('handle').value.trim(),
    password: document.getElementById('password').value.trim(),
    messages: document.getElementById('messages').value,
    emojis: document.getElementById('emojis').value
  };
  const r = await fetch('/resume', {method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify(body)});
  const j = await r.json(); alert(j.msg || j.error || 'ok'); refreshStatus();
}
async function refreshStatus(){
  const h = document.getElementById('handle').value.trim();
  const qs = h ? ('?handle=' + encodeURIComponent(h)) : '';
  const r = await fetch('/status' + qs); const s = await r.json();
  document.getElementById('state').innerText = s.state;
  document.getElementById('total').innerText = (s.stats && s.stats.total) || 0;
  document.getElementById('ok').innerText = (s.stats && s.stats.ok) || 0;
  document.getElementById('fail').innerText = (s.stats && s.stats.fail) || 0;
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
        data_dir=DATA_DIR,
    )

# -------------- APIs --------------
@app.get("/status")
def status():
    handle = (request.args.get("handle") or "").strip()
    if handle:
        return jsonify(load_progress_for(handle))
    return jsonify(load_progress(PROGRESS_PATH))

def _split_emojis(s: str) -> List[str]:
    # نفصل على مسافات أو فواصل، ونحذف الفراغات والتكرارات مع الحفاظ على الترتيب
    raw = [x.strip() for x in re.split(r"[\s,]+", (s or "").strip()) if x.strip()]
    seen, out = set(), []
    for e in raw:
        if e not in seen:
            seen.add(e)
            out.append(e)
    return out

def _compose_with_emoji(base_msg: str, emojis: List[str]) -> str:
    if not emojis:
        return base_msg.strip()
    e = random.choice(emojis)
    if "{EMOJI}" in base_msg:
        txt = base_msg.replace("{EMOJI}", e)
    else:
        txt = f"{base_msg.strip()} {e}"
    return re.sub(r"\s+", " ", txt).strip()

def _run_worker(cfg: Config, post_url: str, mode: str, messages: List[str], progress_path: str, emojis: List[str]):
    progress = load_progress(progress_path)
    progress["state"] = "Running"
    progress["task"] = {
        "handle": cfg.bluesky_handle,
        "mode": mode,
        "min_delay": cfg.min_delay,
        "max_delay": cfg.max_delay,
        "post_url": post_url,
        "messages": "\n".join(messages),
        "emojis": " ".join(emojis),  # نخزنها نصًا
        "pw_fp": _fp(cfg.bluesky_password),
    }
    progress["last_error"] = "-"
    save_progress(progress_path, progress)

    try:
        client = make_client(cfg.bluesky_handle, cfg.bluesky_password)
        did, rkey, post_uri = resolve_post_from_url(client, post_url)

        audience = fetch_audience(client, mode, post_uri)
        filtered = []
        for a in audience:
            try:
                if has_posts(client, a["did"]):
                    filtered.append(a)
            except Exception:
                pass

        with _lock:
            progress["audience"] = filtered
            progress["index"] = progress.get("index", 0)
            progress["stats"]["total"] = len(filtered)
            save_progress(progress_path, progress)

        run_secs = (RUN_MIN or 0) * 60
        rest_secs = (REST_MIN or 0) * 60
        cycle_start = time.time()

        while True:
            if _stop_flag.is_set():
                with _lock:
                    progress["state"] = "Idle"
                    save_progress(progress_path, progress)
                return

            if run_secs > 0 and rest_secs > 0:
                elapsed = time.time() - cycle_start
                if elapsed >= run_secs:
                    with _lock:
                        progress["state"] = f"Resting ({REST_MIN}m)"
                        save_progress(progress_path, progress)
                    for _ in range(rest_secs):
                        if _stop_flag.is_set():
                            with _lock:
                                progress["state"] = "Idle"
                                save_progress(progress_path, progress)
                            return
                        time.sleep(1)
                    cycle_start = time.time()
                    with _lock:
                        progress["state"] = "Running"
                        save_progress(progress_path, progress)

            with _lock:
                i = progress.get("index", 0)
                if i >= len(progress["audience"]):
                    progress["state"] = "Idle"
                    save_progress(progress_path, progress)
                    return
                user = progress["audience"][i]

            try:
                target_uri = latest_post_uri(client, user["did"])
                if not target_uri:
                    raise RuntimeError("skipped_no_own_posts")

                base_msg = random.choice(messages).strip()
                if not base_msg:
                    raise RuntimeError("empty_message")

                final_msg = _compose_with_emoji(base_msg, _split_emojis(progress["task"].get("emojis", "")))

                reply_to_post(client, target_uri, final_msg)

                with _lock:
                    progress["per_user"][user["did"]] = "ok"
                    progress["stats"]["ok"] += 1
                    progress["index"] = i + 1
                    progress["last_error"] = "-"
                    save_progress(progress_path, progress)

            except Exception as e:
                with _lock:
                    progress["per_user"][user["did"]] = f"fail: {e}"
                    progress["stats"]["fail"] += 1
                    progress["index"] = i + 1
                    progress["last_error"] = str(e)
                    save_progress(progress_path, progress)

            delay = random.randint(cfg.min_delay, cfg.max_delay)
            for _ in range(delay):
                if _stop_flag.is_set():
                    with _lock:
                        progress["state"] = "Idle"
                        save_progress(progress_path, progress)
                    return
                time.sleep(1)

    except Exception as e:
        with _lock:
            progress["state"] = "Idle"
            progress["last_error"] = f"Client Error: {e}"
            save_progress(progress_path, progress)

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
    emojis_raw = body.get("emojis") or ""
    messages = [m.strip() for m in messages_raw.splitlines() if m.strip()]
    emojis = _split_emojis(emojis_raw)

    if not (handle and password and post_url and messages):
        return jsonify(error="الرجاء تعبئة الحقول (الحساب/كلمة المرور/الرابط/الرسائل)"), 400
    if mode not in ("likers", "reposters"):
        return jsonify(error="نوع المعالجة يجب أن يكون likers أو reposters"), 400
    if min_delay > max_delay:
        min_delay, max_delay = max_delay, min_delay

    cfg = Config(handle, password, min_delay, max_delay)

    progress = load_progress_for(handle)
    progress.update({
        "state": "Queued",
        "task": {
            "handle": handle,
            "mode": mode,
            "min_delay": min_delay,
            "max_delay": max_delay,
            "post_url": post_url,
            "messages": "\n".join(messages),
            "emojis": " ".join(emojis),
            "pw_fp": _fp(password),
        },
        "audience": [],
        "index": 0,
        "stats": {"ok": 0, "fail": 0, "total": 0},
        "per_user": {},
        "last_error": "-",
    })
    save_progress_for(handle, progress)

    _stop_flag.clear()
    if _worker_thread and _worker_thread.is_alive():
        return jsonify(error="المهمة تعمل بالفعل"), 400

    progress_path = progress_path_for(handle)
    _worker_thread = threading.Thread(
        target=_run_worker, args=(cfg, post_url, mode, messages, progress_path, emojis), daemon=True
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
    body = request.get_json(silent=True) or {}

    ui_handle = (body.get("handle") or "").strip()
    if not ui_handle:
        prior = load_progress(PROGRESS_PATH)
        ui_handle = (prior.get("task", {}).get("handle") or "").strip()
    if not ui_handle:
        return jsonify(error="لا يمكن الاستئناف: يرجى إدخال الحساب في الحقل ثم الضغط على استئناف."), 400

    progress = load_progress_for(ui_handle)
    task = progress.get("task") or {}

    # الرسائل
    msgs_raw = (body.get("messages") or "").strip()
    if not msgs_raw:
        saved = (task.get("messages") or "").strip()
        if saved:
            msgs_raw = saved
    if not msgs_raw:
        return jsonify(error="لا توجد رسائل محفوظة للاستئناف. ابدئي المهمة من جديد أو مرّري messages إلى /resume."), 400
    messages = [m.strip() for m in msgs_raw.splitlines() if m.strip()]

    # الإيموجي
    emojis_raw = (body.get("emojis") or "").strip()
    if not emojis_raw:
        emojis_raw = (task.get("emojis") or "").strip()
    emojis = _split_emojis(emojis_raw)

    # كلمة المرور
    ui_password = (body.get("password") or "").strip()
    password = ui_password or (os.getenv("BSKY_PASSWORD") or "").strip()
    if not password:
        return jsonify(error="لا يمكن الاستئناف بدون كلمة المرور. ارسلي password مع /resume أو ضعي BSKY_PASSWORD."), 400

    post_url = task.get("post_url")
    mode = (task.get("mode") or "likers").strip().lower()
    min_delay = int(task.get("min_delay") or DEFAULT_MIN_DELAY)
    max_delay = int(task.get("max_delay") or DEFAULT_MAX_DELAY)
    if not (post_url and mode):
        return jsonify(error="لا توجد مهمة محفوظة مكتملة المعطيات لهذا الحساب."), 400

    # حدّث بصمة الاعتماد و/أو قائمة الإيموجي لو تغيّرت
    old_fp = (task.get("pw_fp") or "").strip()
    new_fp = _fp(password)
    if old_fp != new_fp or (task.get("emojis") or "") != " ".join(emojis):
        task["pw_fp"] = new_fp
        task["handle"] = ui_handle
        task["emojis"] = " ".join(emojis)
        progress["task"] = task
        for k in ("session", "access_jwt", "refresh_jwt"):
            if k in progress:
                progress.pop(k)
        save_progress_for(ui_handle, progress)

    cfg = Config(ui_handle, password, min_delay, max_delay)

    _stop_flag.clear()
    if _worker_thread and _worker_thread.is_alive():
        return jsonify(error="المهمة تعمل بالفعل"), 400

    progress_path = progress_path_for(ui_handle)
    _worker_thread = threading.Thread(
        target=_run_worker, args=(cfg, post_url, mode, messages, progress_path, emojis), daemon=True
    )
    _worker_thread.start()
    return jsonify(msg="تم استئناف المهمة")

# --------- نقطة دخول WSGI ---------
if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", "10000")))
