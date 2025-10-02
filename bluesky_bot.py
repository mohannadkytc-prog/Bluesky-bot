from flask import Flask, request, jsonify, render_template_string
import time, random, threading
import requests
from config import Config, PROGRESS_PATH
from utils import get_likers, get_reposters, reply_to_latest_post

app = Flask(__name__)

# الحالة العامة للبوت
bot_state = {
    "status": "Idle",
    "current_task": None,
    "total": 0,
    "done": 0,
    "failed": 0,
    "errors": []
}

# HTML بسيط للواجهة
HTML_PAGE = """
<!DOCTYPE html>
<html lang="ar">
<head>
<meta charset="UTF-8">
<title>Bluesky Bot</title>
</head>
<body>
<h2>إعدادات البوت</h2>
<form method="post" action="/start">
الرابط: <input type="text" name="post_uri"><br>
الرسالة: <textarea name="message"></textarea><br>
نوع المعالجة:
<select name="mode">
  <option value="likers">معجبين</option>
  <option value="reposters">معيدي النشر</option>
</select><br>
الحد الأدنى للتأخير: <input type="number" name="min_delay" value="200"><br>
الحد الأقصى للتأخير: <input type="number" name="max_delay" value="250"><br>
<button type="submit">بدء المهمة</button>
</form>

<h2>حالة التشغيل</h2>
<p>الحالة: {{status}}</p>
<p>إجمالي الجمهور: {{total}}</p>
<p>تم إنجازه: {{done}}</p>
<p>فشل: {{failed}}</p>
<p>أخطاء: {{errors}}</p>

<form method="post" action="/stop"><button type="submit">إيقاف</button></form>
</body>
</html>
"""

def bot_job(post_uri, message, mode, config):
    global bot_state
    session = requests.Session()

    # جلب القائمة
    if mode == "likers":
        users = get_likers(post_uri, session)
    else:
        users = get_reposters(post_uri, session)

    bot_state["total"] = len(users)
    bot_state["done"] = 0
    bot_state["failed"] = 0
    bot_state["errors"] = []
    bot_state["status"] = "Running"

    for user in users:
        if bot_state["status"] == "Stopped":
            break

        result = reply_to_latest_post(user, message, session)
        if result["status"] == "ok":
            bot_state["done"] += 1
        elif result["status"] == "fail":
            bot_state["failed"] += 1
            bot_state["errors"].append(result.get("error", "unknown error"))

        time.sleep(random.randint(config.min_delay, config.max_delay))

    bot_state["status"] = "Idle"

@app.route("/")
def index():
    return render_template_string(HTML_PAGE, **bot_state)

@app.route("/start", methods=["POST"])
def start():
    global bot_state
    config = Config(
        min_delay=request.form.get("min_delay"),
        max_delay=request.form.get("max_delay"),
    )
    post_uri = request.form.get("post_uri")
    message = request.form.get("message")
    mode = request.form.get("mode")

    threading.Thread(target=bot_job, args=(post_uri, message, mode, config)).start()
    return jsonify({"status": "started"})

@app.route("/stop", methods=["POST"])
def stop():
    global bot_state
    bot_state["status"] = "Stopped"
    return jsonify({"status": "stopping"})

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=10000)
