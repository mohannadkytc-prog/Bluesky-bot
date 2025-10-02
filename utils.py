# utils.py
import re
import time
import random
import requests
from typing import List, Dict, Tuple, Optional

PUBLIC_API = "https://public.api.bsky.app/xrpc"
WRITE_API  = "https://bsky.social/xrpc"


# -----------------------------
# جلسة مصادقة للكتابة (ردود)
# -----------------------------
class BskySession:
    """
    جلسة مصادقة لعمليات الكتابة على Bluesky.
    - login(handle, password): يطلب توكن ودالة الـ DID ويجهز الهيدر.
    - session: requests.Session مع Authorization جاهز.
    - did: DID الخاص بالحساب.
    """
    def __init__(self, timeout: int = 30):
        self.session = requests.Session()
        self.session.headers.update({"Content-Type": "application/json"})
        self.timeout = timeout
        self.did: Optional[str] = None
        self.access_jwt: Optional[str] = None

    def login(self, handle: str, password: str):
        url = f"{WRITE_API}/com.atproto.server.createSession"
        payload = {"identifier": handle.strip(), "password": password}
        r = self.session.post(url, json=payload, timeout=self.timeout)
        if r.status_code != 200:
            raise RuntimeError(f"فشل تسجيل الدخول: {r.status_code} {r.text}")

        data = r.json()
        self.access_jwt = data.get("accessJwt")
        self.did = data.get("did")
        if not self.access_jwt or not self.did:
            raise RuntimeError("استجابة تسجيل الدخول ناقصة (accessJwt/did مفقود).")

        # أضف Authorization لجميع الطلبات اللاحقة
        self.session.headers["Authorization"] = f"Bearer {self.access_jwt}"

    # التفاف GET/POST باستخدام نفس السشن
    def get(self, url, **kw):
        kw.setdefault("timeout", self.timeout)
        return self.session.get(url, **kw)

    def post(self, url, **kw):
        kw.setdefault("timeout", self.timeout)
        return self.session.post(url, **kw)


# -----------------------------------
# أدوات مساعدة: تحليل الروابط والحساب
# -----------------------------------
def resolve_handle_to_did(handle: str) -> str:
    """يحوّل handle إلى DID باستخدام public api."""
    url = f"{PUBLIC_API}/com.atproto.identity.resolveHandle"
    r = requests.get(url, params={"handle": handle}, timeout=30)
    if r.status_code != 200:
        raise RuntimeError(f"فشل resolveHandle: {r.status_code} {r.text}")
    did = r.json().get("did")
    if not did:
        raise RuntimeError("لم يتم إيجاد DID لهذا الـ handle")
    return did


def parse_bsky_post_url(url: str) -> Tuple[str, str]:
    """
    يُرجع (handle, rkey) من رابط بوست bsky.app
    مثال:
    https://bsky.app/profile/USER.handle/post/3m25wgllyis2c
    """
    m = re.search(r"/profile/([^/]+)/post/([^/?#]+)", url)
    if not m:
        raise ValueError("رابط البوست غير صالح. يجب أن يكون من شكل bsky.app/profile/.../post/...")
    handle = m.group(1)
    rkey = m.group(2)
    return handle, rkey


def resolve_post_from_url(post_url: str) -> Tuple[str, str, str]:
    """
    يحوّل رابط البوست إلى (uri, did, handle)
    - uri على شكل: at://did/app.bsky.feed.post/rkey
    """
    handle, rkey = parse_bsky_post_url(post_url.strip())
    did = resolve_handle_to_did(handle)
    uri = f"at://{did}/app.bsky.feed.post/{rkey}"
    return uri, did, handle


# -----------------------------------
# جلب الجمهور (معجبين/معيدي نشر) + فلترة لا يملك بوستات
# -----------------------------------
def get_likers(uri: str, limit: int = 100) -> List[Dict]:
    """
    يُرجع قائمة المعجبين (dict) كما تعيدها واجهة Bluesky العامة.
    """
    url = f"{PUBLIC_API}/app.bsky.feed.getLikes"
    # سنجلب بصفحة واحدة كافية لمعظم الحالات. يمكن لاحقاً دعم pagination
    r = requests.get(url, params={"uri": uri, "limit": limit}, timeout=30)
    if r.status_code != 200:
        raise RuntimeError(f"getLikes فشل: {r.status_code} {r.text}")
    items = r.json().get("likes", [])
    # نحولها لصيغة أبسط: did/handle
    result = []
    for it in items:
        actor = it.get("actor", {})
        result.append({
            "did": actor.get("did"),
            "handle": actor.get("handle"),
            "displayName": actor.get("displayName", ""),
        })
    return result


def get_reposters(uri: str, limit: int = 100) -> List[Dict]:
    """
    يُرجع قائمة معيدي النشر (dict) كما تعيدها واجهة Bluesky العامة.
    """
    url = f"{PUBLIC_API}/app.bsky.feed.getRepostedBy"
    r = requests.get(url, params={"uri": uri, "limit": limit}, timeout=30)
    if r.status_code != 200:
        raise RuntimeError(f"getRepostedBy فشل: {r.status_code} {r.text}")
    items = r.json().get("repostedBy", [])
    result = []
    for actor in items:
        result.append({
            "did": actor.get("did"),
            "handle": actor.get("handle"),
            "displayName": actor.get("displayName", ""),
        })
    return result


def has_posts(did: str) -> bool:
    """
    يفحص إذا كان المستخدم لديه أي منشورات.
    """
    url = f"{PUBLIC_API}/app.bsky.feed.getAuthorFeed"
    r = requests.get(url, params={"actor": did, "limit": 1}, timeout=30)
    if r.status_code != 200:
        # لو فشل الاستعلام نعتبره لا يملك بوستات لكي لا نوقف البوت
        return False
    feed = r.json().get("feed", [])
    return len(feed) > 0


# -----------------------------------
# الرد على آخر منشور لكل مستخدم
# -----------------------------------
def reply_to_latest_post(session: BskySession, did: str, messages: List[str], handle_for_log: str = "") -> Tuple[bool, str]:
    """
    يرد على آخر منشور للمستخدم DID المُعطى.
    - session: BskySession بعد login()
    - messages: قائمة رسائل نصية؛ يُختار منها عشوائياً
    يرجع (success, message)
    """
    try:
        # 1) نجيب آخر بوست من public api
        feed_url = f"{PUBLIC_API}/app.bsky.feed.getAuthorFeed"
        fr = session.get(feed_url, params={"actor": did, "limit": 1})
        if fr.status_code != 200:
            return False, f"فشل جلب بوستات {handle_for_log or did}: {fr.text}"

        feed_data = fr.json()
        feed = feed_data.get("feed", [])
        if not feed:
            return False, f"{handle_for_log or did}: لا يملك منشورات."

        latest_post = feed[0].get("post", {})
        post_uri = latest_post.get("uri")
        post_cid = latest_post.get("cid")
        if not post_uri or not post_cid:
            return False, f"{handle_for_log or did}: لا يوجد URI/CID للبوست."

        # 2) نجهز رسالة
        if not messages:
            return False, "قائمة الرسائل فارغة."
        message = random.choice(messages).strip()
        if not message:
            return False, "رسالة فارغة."

        # 3) نرسل الرد باستخدام write api (يتطلب Authorization)
        reply_url = f"{WRITE_API}/com.atproto.repo.createRecord"
        payload = {
            "repo": session.did,
            "collection": "app.bsky.feed.post",
            "record": {
                "$type": "app.bsky.feed.post",
                "text": message,
                "reply": {
                    "root": {"cid": post_cid, "uri": post_uri},
                    "parent": {"cid": post_cid, "uri": post_uri},
                },
                "createdAt": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
            }
        }
        rr = session.post(reply_url, json=payload)
        if rr.status_code == 200:
            return True, f"تم الرد على {handle_for_log or did}"
        else:
            return False, f"فشل الرد على {handle_for_log or did}: {rr.status_code} {rr.text}"

    except Exception as e:
        return False, f"استثناء عند {handle_for_log or did}: {e}"


# -----------------------------------
# أداة مساعدة: تجهيز الجمهور حسب النوع مع فلترة من لا يملك بوستات
# -----------------------------------
def gather_audience(process_type: str, post_url: str, limit: int = 100, ignore_no_posts: bool = True) -> Tuple[List[Dict], str]:
    """
    يُعيد (audience_list, uri)
    - process_type: "Likers" أو "Reposters"
    - post_url: رابط البوست من bsky.app
    - يقوم بفلترة المستخدمين الذين لا يملكون منشورات إذا ignore_no_posts=True
    - يحافظ على الترتيب كما يعود من الـ API (من الأعلى للأسفل)
    """
    uri, _, _ = resolve_post_from_url(post_url)
    if process_type.lower().startswith("liker"):
        users = get_likers(uri, limit=limit)
    else:
        users = get_reposters(uri, limit=limit)

    if ignore_no_posts:
        filtered = []
        for u in users:
            did = u.get("did")
            if did and has_posts(did):
                filtered.append(u)
        users = filtered

    return users, uri
