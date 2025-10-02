import os
import time
import random
from flask import Flask, request, jsonify, render_template_string
from utils import get_likers, get_reposters, reply_to_latest_post

app = Flask(__name__)

# حالة التشغيل
status = {
    "state": "Idle",
    "total_users": 0,
    "done": 0,
    "fail": 0,
    "errors": []
}

@app.route("/")
def index():
    return render_template_string("""
    <html dir="rtl">
    <head><title>Bluesky Bot</title></head>
    <body>
        <h2>واجهة تحكم البوت</h2>
        <form action="/start" method="post">
            رابط البوست: <input type="text" name="post_url"><br><br>
            نوع المعالجة:
            <select name="mode">
                <option value="likers">معجبين</option>
                <option value="reposters">معيدين نشر</option>
            </select><br><br>
            من: <input type="number" name="min_delay" value="200"> ثانية
            إلى: <input type="number" name="max_delay" value="250"> ثانية<br><br>
            <textarea name="messages" rows="5" cols="50">اكتب هنا الرسائل (سطر لكل رسالة)</textarea><br><br>
            <button type="submit">بدء المهمة</button>
        </form>
        <br>
        <form action="/stop" method="post"><button type="submit">إيقاف</button></form>
        <form action="/resume" method="post"><button type="submit">استئناف</button></form>
        <form action="/status" method="get"><button type="submit">تحديث الحالة</button></form>
        <hr>
        <h3>حالة التشغيل الحالية</h3>
        <p>الحالة: {{state}}</p>
        <p>إجمالي الجمهور: {{total_users}}</p>
        <p>المنجز: {{done}}</p>
        <p>الفشل: {{fail}}</p>
        <p>الأخطاء:</p>
        <ul>
        {% for err in errors %}
            <li>{{err}}</li>
        {% endfor %}
        </ul>
    </body>
    </html>
    """, **status)

@app.route("/start", methods=["POST"])
def start():
    global status
    post_url = request.form["post_url"]
    mode = request.form["mode"]
    min_delay = int(request.form["min_delay"])
    max_delay = int(request.form["max_delay"])
    messages = [m.strip() for m in request.form["messages"].split("\n") if m.strip()]

    # جلب القائمة حسب الاختيار
    if mode == "likers":
        users = get_likers(post_url)
    else:
        users = get_reposters(post_url)

    status = {
        "state": "Running",
        "total_users": len(users),
        "done": 0,
        "fail": 0,
        "errors": []
    }

    # تنفيذ الردود بالتسلسل
    for user in users:
        if status["state"] == "Stopped":
            break
        try:
            msg = random.choice(messages)
            reply_to_latest_post(user, msg)
            status["done"] += 1
        except Exception as e:
            status["fail"] += 1
            status["errors"].append(f"{user}: {str(e)}")
        delay = random.randint(min_delay, max_delay)
        time.sleep(delay)

    status["state"] = "Idle"
    return jsonify({"message": "تم بدء المهمة", "users": len(users)})

@app.route("/stop", methods=["POST"])
def stop():
    global status
    status["state"] = "Stopped"
    return jsonify({"message": "تم إيقاف المهمة"})

@app.route("/resume", methods=["POST"])
def resume():
    global status
    if status["state"] == "Stopped":
        status["state"] = "Running"
        return jsonify({"message": "تم الاستئناف"})
    return jsonify({"message": "لا يوجد مهمة موقوفة"})

@app.route("/status")
def get_status():
    return jsonify(status)

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)))
