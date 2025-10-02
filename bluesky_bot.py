"""
Core bot runner: sequentially reply to latest post of each DID from the given audience list.
Respects stop/resume and persists progress per (handle|mode|post_url).
"""

import os
import time
import random
import logging
import threading
from typing import Dict, Any, List, Optional

from atproto import Client, models
from utils import (
    resolve_post_from_url,
    get_audience_list,
    load_progress,
    save_progress,
    task_key,
    validate_message_template,
    DATA_DIR,
)

logger = logging.getLogger(__name__)
logging.basicConfig(level=logging.INFO)

# حالة المهمة العامة (لكل خدمة)
task_state = {
    "thread": None,           # Thread object
    "running": False,         # is loop running
    "paused": False,          # allow 'stop' that can be resumed
    "key": "",                # current task key
    "status": "Idle",         # Idle/Running/Stopped
    "last_error": "",
    "current_did": "",
    "mode": "likers",         # likers|reposters
    "post_url": "",
    "handle": "",
    "min_delay": 180,
    "max_delay": 300,
}


def _login(handle: str, password: str) -> Client:
    client = Client()
    client.login(handle, password)
    return client


def _get_latest_post_uri_cid(client: Client, actor: str) -> Optional[Dict[str, str]]:
    """
    Returns {'uri': .., 'cid': ..} for the latest author post; None if no posts.
    """
    try:
        feed = client.app.bsky.feed.get_author_feed({"actor": actor, "limit": 1})
        if not feed.feed:
            return None
        post = feed.feed[0].post
        return {"uri": post.uri, "cid": post.cid}
    except Exception:
        return None


def _send_reply(client: Client, parent_uri: str, parent_cid: str, text: str) -> bool:
    """
    Send reply to parent post.
    """
    try:
        record = models.AppBskyFeedPost.Record(
            created_at=models.datetime_now(),
            text=text,
            reply=models.AppBskyFeedPost.ReplyRef(parent={"uri": parent_uri, "cid": parent_cid})
        )
        client.com.atproto.repo.create_record(
            repo=client.me.did,
            collection=models.ids.AppBskyFeedPost,
            record=record
        )
        return True
    except Exception as e:
        logger.exception(f"reply failed: {e}")
        return False


def _pick_message(msg_lines: List[str]) -> str:
    lines = [ln.strip() for ln in msg_lines if ln.strip()]
    if not lines:
        return ""
    # اختر سطرًا عشوائيًا من القائمة، مع فحص أمان الرسالة
    text = random.choice(lines)
    return text if validate_message_template(text) else ""


def run_task(
    handle: str,
    password: str,
    post_url: str,
    mode: str,
    min_delay: int,
    max_delay: int,
    messages_text: str,
):
    """
    الحلقة الأساسية: تجلب الجمهور، وترد على آخر منشور لكل حساب بالتسلسل.
    تُحافظ على التقدّم وتدعم الإيقاف والاستئناف.
    """
    task_state.update({
        "running": True,
        "paused": False,
        "status": "Running",
        "last_error": "",
        "handle": handle,
        "post_url": post_url,
        "mode": mode,
        "min_delay": min_delay,
        "max_delay": max_delay,
    })

    key = task_key(handle, mode, post_url)
    task_state["key"] = key

    # تسجيل الدخول
    try:
        client = _login(handle, password)
    except Exception as e:
        task_state["last_error"] = f"Login failed: {e}"
        task_state["running"] = False
        task_state["status"] = "Idle"
        return

    # حلّ الـ URI/CID للرابط المُدخل
    meta = resolve_post_from_url(client, post_url)
    if not meta:
        task_state["last_error"] = "Bad post URL (cannot resolve)."
        task_state["running"] = False
        task_state["status"] = "Idle"
        return

    # حمّل التقدّم السابق
    prog = load_progress(key)

    # إذا أول مرة، جهّز قائمة الجمهور بالترتيب
    if not prog["queue"] and not prog["processed"]:
        dids = get_audience_list(client, meta["uri"], mode=mode)
        prog["queue"] = dids[:]          # قائمة العمل
        prog["stats"]["total"] = len(dids)
        prog["processed"] = {}
        prog["stats"]["ok"] = 0
        prog["stats"]["fail"] = 0
        prog["last_error"] = ""
        prog["cursor"] = 0
        save_progress(key, prog)

    # نصوص الرسائل من TextArea (سطر لكل رسالة)
    msg_lines = messages_text.splitlines()

    # المعالجة بالتسلسل (من الأعلى للأسفل)
    while task_state["running"] and not task_state["paused"]:
        # إنتهت القائمة
        if prog["cursor"] >= len(prog["queue"]):
            task_state["status"] = "Idle"
            task_state["running"] = False
            break

        did = prog["queue"][prog["cursor"]]
        task_state["current_did"] = did

        # اجلب آخر منشور لصاحب الـ DID
        # ملاحظة: نحتاج الـ handle الخاص به؛ يمكن تمرير الـ did مباشرة في author_feed
        parent = _get_latest_post_uri_cid(client, did)
        if not parent:
            # تجاهل من لا يملك منشورات
            prog["processed"][did] = {"ok": False, "err": "skipped_no_posts"}
            prog["stats"]["fail"] += 1
            prog["cursor"] += 1
            save_progress(key, prog)
            continue

        # اختر رسالة
        msg = _pick_message(msg_lines) or ""
        if not msg:
            prog["processed"][did] = {"ok": False, "err": "empty_or_invalid_msg"}
            prog["stats"]["fail"] += 1
            prog["cursor"] += 1
            save_progress(key, prog)
            continue

        # أرسل الرد
        ok = _send_reply(client, parent["uri"], parent["cid"], msg)
        if ok:
            prog["processed"][did] = {"ok": True, "err": None}
            prog["stats"]["ok"] += 1
        else:
            prog["processed"][did] = {"ok": False, "err": "reply_failed"}
            prog["stats"]["fail"] += 1

        prog["cursor"] += 1
        save_progress(key, prog)

        # تأخير بين المستخدمين
        delay = random.randint(min_delay, max_delay)
        for _ in range(delay):
            if not task_state["running"] or task_state["paused"]:
                break
            time.sleep(1)

    task_state["status"] = "Idle"
    task_state["running"] = False
    task_state["current_did"] = ""


# ====== تحكّم خارجي من الواجهة ======
def start_task(**kwargs):
    if task_state.get("running"):
        return False, "Already running."
    th = threading.Thread(target=run_task, kwargs=kwargs, daemon=True)
    task_state["thread"] = th
    th.start()
    return True, "Started."


def stop_task():
    task_state["paused"] = True
    task_state["running"] = False
    task_state["status"] = "Idle"
    return True, "Stopped."


def resume_task(handle: str, password: str, post_url: str, mode: str, min_delay: int, max_delay: int, messages_text: str):
    """
    يستأنف اعتمادًا على نفس الـ key و الـ cursor المخزّن.
    (نفس start لكن سيقرأ الحركة من progress.json)
    """
    return start_task(
        handle=handle,
        password=password,
        post_url=post_url,
        mode=mode,
        min_delay=min_delay,
        max_delay=max_delay,
        messages_text=messages_text,
    )


def read_status() -> Dict[str, Any]:
    st = {
        "status": task_state["status"],
        "running": task_state["running"],
        "mode": task_state["mode"],
        "post_url": task_state["post_url"],
        "handle": task_state["handle"],
        "current_did": task_state["current_did"],
        "min_delay": task_state["min_delay"],
        "max_delay": task_state["max_delay"],
        "last_error": task_state["last_error"],
        "data_dir": DATA_DIR,
    }
    # أرقام من progress
    if task_state["key"]:
        prog = load_progress(task_state["key"])
        st["stats"] = prog.get("stats", {})
        st["cursor"] = prog.get("cursor", 0)
        st["last_error_saved"] = prog.get("last_error", "")
        st["processed"] = prog.get("processed", {})
        st["remaining"] = max(0, prog.get("stats", {}).get("total", 0) - st.get("cursor", 0))
    else:
        st["stats"] = {"ok": 0, "fail": 0, "total": 0}
        st["cursor"] = 0
        st["remaining"] = 0
    return st
