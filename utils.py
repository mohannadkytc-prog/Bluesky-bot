# utils.py
# -*- coding: utf-8 -*-

import os
import re
import json
import time
import random
import datetime
from typing import Dict, List, Tuple, Optional

import requests

# ------------------------------
# إعدادات عامة وملفات التقدّم
# ------------------------------
API_BASE = "https://bsky.social/xrpc"

# إن كان لديك config.py يعرّف هذه المسارات سيعمل الاستيراد؛
# وإلا نوفّر قيم افتراضية آمنة.
try:
    from config import PROGRESS_PATH, DATA_DIR
except Exception:
    DATA_DIR = "/data" if os.path.exists("/data") else "/tmp"
    os.makedirs(DATA_DIR, exist_ok=True)
    PROGRESS_PATH = os.path.join(DATA_DIR, "progress.json")


# -------------- أدوات ملف التقدم --------------
def load_progress() -> Dict:
    if os.path.exists(PROGRESS_PATH):
        try:
            with open(PROGRESS_PATH, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass
    return {
        "state": "Idle",
        "task": "",
        "total": 0,
        "done": 0,
        "fail": 0,
        "last_error": "-",
        "per_user": {},  # did -> {"status": "...", "note": "..."}
        "stats": {"ok": 0, "fail": 0},
    }


def save_progress(p: Dict) -> None:
    try:
        with open(PROGRESS_PATH, "w", encoding="utf-8") as f:
            json.dump(p, f, ensure_ascii=False, indent=2)
    except Exception:
        # لا نعطّل المهمة بسبب خطأ كتابة التقدم
        pass


def save_progress_for_key(did: str, status: str, note: str = "") -> None:
    p = load_progress()
    per = p.get("per_user", {})
    per[did] = {"status": status, "note": note}
    p["per_user"] = per
    save_progress(p)


# -------------- أدوات طلبات HTTP --------------
def _get(url: str, params: Dict = None, headers: Dict = None) -> requests.Response:
    return requests.get(url, params=params or {}, headers=headers or {}, timeout=30)


def _post(url: str, json_body: Dict, headers: Dict = None) -> requests.Response:
    return requests.post(url, json=json_body, headers=headers or {}, timeout=30)


# -------------- تسجيل الدخول / التوكن --------------
def get_api(handle: str, password: str) -> Dict:
    """
    يُرجع dict يحوي accessJwt و did و headers الجاهزة للنداءات.
    """
    url = f"{API_BASE}/com.atproto.server.createSession"
    body = {"identifier": handle, "password": password}
    r = _post(url, body)
    if r.status_code != 200:
        raise Exception(f"فشل تسجيل الدخول: {r.status_code} {r.text}")

    data = r.json()
    access = data["accessJwt"]
    did = data["did"]
    headers = {"Authorization": f"Bearer {access}"}
    return {"did": did, "accessJwt": access, "headers": headers}


# -------------- أدوات تحليل رابط البوست --------------
POST_URL_RE = re.compile(
    r"https?://(?:www\.)?bsky\.app/profile/(?P<handle>[^/]+)/post/(?P<rkey>[A-Za-z0-9]+)"
)


def parse_post_url(post_url: str) -> Tuple[str, str]:
    """
    يُعيد (handle, rkey) من رابط app.bsky كما في:
    https://bsky.app/profile/<handle>/post/<rkey>
    """
    m = POST_URL_RE.search(post_url.strip())
    if not m:
        raise ValueError("رابط البوست غير صالح، تأكد من الشكل: https://bsky.app/profile/<handle>/post/<rkey>")
    return m.group("handle"), m.group("rkey")


def resolve_handle_to_did(handle: str) -> str:
    url = f"{API_BASE}/com.atproto.identity.resolveHandle"
    r = _get(url, params={"handle": handle})
    if r.status_code != 200:
        raise Exception(f"فشل resolveHandle: {r.status_code} {r.text}")
    return r.json()["did"]


def build_post_uri(author_did: str, rkey: str) -> str:
    return f"at://{author_did}/app.bsky.feed.post/{rkey}"


# -------------- جلب الجمهور (مرتب من الأعلى للأسفل) --------------
def get_likers(post_uri: str, headers: Dict) -> List[Dict]:
    """
    يُرجع قائمة من العناصر: {"did": ..., "handle": ...}
    مرتبة بنفس ترتيب API (عادةً من الأحدث للأقدم)،
    وبما أنك طلبت "من الأعلى للأسفل" فهذا الترتيب مناسب مباشرة.
    """
    url = f"{API_BASE}/app.bsky.feed.getLikes"
    cursor = None
    out: List[Dict] = []
    while True:
        params = {"uri": post_uri, "limit": 100}
        if cursor:
            params["cursor"] = cursor
        r = _get(url, params=params, headers=headers)
        if r.status_code != 200:
            raise Exception(f"فشل getLikes: {r.status_code} {r.text}")
        data = r.json()
        for it in data.get("likes", []):
            actor = it.get("actor", {})
            out.append({"did": actor.get("did"), "handle": actor.get("handle")})
        cursor = data.get("cursor")
        if not cursor:
            break
    return out


def get_reposters(post_uri: str, headers: Dict) -> List[Dict]:
    url = f"{API_BASE}/app.bsky.feed.getRepostedBy"
    cursor = None
    out: List[Dict] = []
    while True:
        params = {"uri": post_uri, "limit": 100}
        if cursor:
            params["cursor"] = cursor
        r = _get(url, params=params, headers=headers)
        if r.status_code != 200:
            raise Exception(f"فشل getRepostedBy: {r.status_code} {r.text}")
        data = r.json()
        for it in data.get("repostedBy", []):
            out.append({"did": it.get("did"), "handle": it.get("handle")})
        cursor = data.get("cursor")
        if not cursor:
            break
    return out


# -------------- التحقق إن الحساب لديه منشورات --------------
def has_posts(actor_did: str, headers: Dict) -> bool:
    url = f"{API_BASE}/app.bsky.feed.getAuthorFeed"
    r = _get(url, params={"actor": actor_did, "limit": 1}, headers=headers)
    if r.status_code != 200:
        # لو فشل، نعتبره لا يصلح مؤقتاً
        return False
    items = r.json().get("feed", [])
    return len(items) > 0


def get_latest_post_uri_and_cid(actor_did: str, headers: Dict) -> Optional[Tuple[str, str]]:
    """
    يُعيد (latest_post_uri, latest_post_cid) أو None إذا لا يوجد منشورات.
    """
    url = f"{API_BASE}/app.bsky.feed.getAuthorFeed"
    r = _get(url, params={"actor": actor_did, "limit": 1}, headers=headers)
    if r.status_code != 200:
        return None
    feed = r.json().get("feed", [])
    if not feed:
        return None

    post = feed[0].get("post", {})
    uri = post.get("uri")
    cid = post.get("cid")
    if uri and cid:
        return uri, cid
    return None


# -------------- إرسال الرد --------------
def reply_to_latest_post(api: Dict, target_did: str, text: str) -> None:
    """
    يرد على آخر منشور لدى المستخدم target_did بالنص text.
    """
    headers = api["headers"]

    latest = get_latest_post_uri_and_cid(target_did, headers)
    if not latest:
        raise Exception("المستخدم لا يملك منشورات (أو تعذر جلبها)")

    parent_uri, parent_cid = latest

    # إنشاء record الرد
    now_iso = datetime.datetime.utcnow().replace(microsecond=0).isoformat() + "Z"
    record = {
        "text": text,
        "createdAt": now_iso,
        "reply": {
            "root": {"uri": parent_uri, "cid": parent_cid},
            "parent": {"uri": parent_uri, "cid": parent_cid},
        },
        "$type": "app.bsky.feed.post",
        "langs": ["en"],  # غيّرها لو بدك
    }

    url = f"{API_BASE}/com.atproto.repo.createRecord"
    body = {
        "repo": api["did"],  # حسابنا نحن
        "collection": "app.bsky.feed.post",
        "record": record,
    }
    r = _post(url, body, headers=headers)
    if r.status_code != 200:
        raise Exception(f"فشل إنشاء الرد: {r.status_code} {r.text}")


# -------------- مُسهّلات من رابط بوست إلى قائمة جمهور مرتبة --------------
def audience_from_post_url(
    post_url: str,
    mode: str,  # "likers" | "reposters"
    headers: Dict,
) -> Tuple[str, str, str, List[Dict]]:
    """
    يُرجع: (author_handle, author_did, post_uri, audience_list)
    audience_list عناصرها {"did": ..., "handle": ...} مرتبة من الأعلى للأسفل.
    """
    author_handle, rkey = parse_post_url(post_url)
    author_did = resolve_handle_to_did(author_handle)
    post_uri = build_post_uri(author_did, rkey)

    if mode.lower() in ("likers", "likes", "liker"):
        audience = get_likers(post_uri, headers)
    else:
        audience = get_reposters(post_uri, headers)

    return author_handle, author_did, post_uri, audience


# -------------- حلقة تنفيذ (اختياري إن كنت تستدعيها من الواجهة) --------------
def run_sequential_replies(
    handle: str,
    password: str,
    post_url: str,
    mode: str,  # "likers" or "reposters"
    messages: List[str],
    min_delay: int,
    max_delay: int,
    stop_flag_fn=None,   # callable يرجع True لإيقاف المهمة
) -> None:
    """
    يمشي على الجمهور من الأعلى للأسفل، ويتحقق أن لكل حساب بوست،
    ثم يرد على آخر بوست لديه، مع تأخير عشوائي بين min/max.
    يحفظ خطة التقدّم في progress.json.
    """
    api = get_api(handle, password)
    headers = api["headers"]

    # الجمهور
    _, _, _, audience = audience_from_post_url(post_url, mode, headers)

    p = load_progress()
    p.update({
        "state": "Running",
        "task": post_url,
        "total": len(audience),
        "done": 0,
        "fail": 0,
        "last_error": "-",
        "stats": {"ok": 0, "fail": 0},
        "per_user": {},
    })
    save_progress(p)

    for idx, actor in enumerate(audience, start=1):
        if stop_flag_fn and stop_flag_fn():
            p["state"] = "Idle"
            save_progress(p)
            return

        did = actor.get("did")
        if not did:
            p["fail"] += 1
            p["stats"]["fail"] += 1
            save_progress_for_key("unknown", "error", "missing did")
            save_progress(p)
            continue

        try:
            # تجاوز الحسابات بلا منشورات
            if not has_posts(did, headers):
                save_progress_for_key(did, "skipped", "no_posts")
                # لا نزيد done/fail
            else:
                msg = random.choice(messages).strip()
                reply_to_latest_post(api, did, msg)
                p["done"] += 1
                p["stats"]["ok"] += 1
                save_progress_for_key(did, "ok", "")
        except Exception as e:
            p["fail"] += 1
            p["stats"]["fail"] += 1
            p["last_error"] = str(e)[:500]
            save_progress_for_key(did, "fail", p["last_error"])

        # حدّث التقدّم بعد كل مستخدم
        save_progress(p)

        # تأخير آمن بين المستخدمين
        sleep_for = random.randint(int(min_delay), int(max_delay))
        time.sleep(max(1, sleep_for))

    p["state"] = "Idle"
    save_progress(p)
