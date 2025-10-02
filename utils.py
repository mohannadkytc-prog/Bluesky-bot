# utils.py
import re
import time
import random
import json
from typing import Dict, List, Tuple, Optional

from atproto import Client, models as M, client_utils as CU

# ---------- جلسة العميل ----------
def make_client(handle: str, password: str) -> Client:
    """يسجّل الدخول ويعيد Client جاهز."""
    c = Client()
    # ملاحظة: استخدمي App Password (وليس الرقم السري العادي) لحساب Bluesky
    c.login(handle, password)
    return c

# ---------- تحليل رابط البوست ----------
def _parse_bsky_post_url(url: str) -> Tuple[str, str]:
    """
    يُعيد (actor, rkey) من رابط مثل:
    https://bsky.app/profile/{actor}/post/{rkey}
    - actor قد يكون handle أو did:plc:...
    """
    m = re.search(r"/profile/([^/]+)/post/([^/?#]+)", url)
    if not m:
        raise ValueError("رابط غير صالح لبوست Bluesky")
    actor = m.group(1)
    rkey = m.group(2)
    return actor, rkey

def resolve_post_from_url(client: Client, url: str) -> Tuple[str, str, str]:
    """
    من رابط التطبيق يرجع (did, rkey, at_uri)
    at_uri = at://{did}/app.bsky.feed.post/{rkey}
    """
    actor, rkey = _parse_bsky_post_url(url)

    if actor.startswith("did:"):
        did = actor
    else:
        did = client.com.atproto.identity.resolve_handle({"handle": actor}).did

    at_uri = f"at://{did}/app.bsky.feed.post/{rkey}"
    return did, rkey, at_uri

# ---------- جلب الجمهور ----------
def fetch_audience(client: Client, mode: str, post_at_uri: str) -> List[Dict]:
    """
    يرجع قائمة مرتبة من الحسابات (dict لكل مستخدم يحتوي did, handle).
    mode: 'likers' | 'reposters'
    """
    audience: List[Dict] = []
    cursor: Optional[str] = None

    if mode == "likers":
        while True:
            resp = client.app.bsky.feed.get_likes({"uri": post_at_uri, "cursor": cursor, "limit": 100})
            for item in resp.likes or []:
                actor = item.actor
                audience.append({"did": actor.did, "handle": actor.handle})
            cursor = getattr(resp, "cursor", None)
            if not cursor:
                break

    elif mode == "reposters":
        while True:
            resp = client.app.bsky.feed.get_reposted_by({"uri": post_at_uri, "cursor": cursor, "limit": 100})
            # ملاحظة: الحقل الصحيح هو reposted_by
            for actor in resp.reposted_by or []:
                audience.append({"did": actor.did, "handle": actor.handle})
            cursor = getattr(resp, "cursor", None)
            if not cursor:
                break
    else:
        raise ValueError("mode يجب أن يكون likers أو reposters")

    # إزالة التكرار مع الحفاظ على الترتيب
    seen = set()
    unique = []
    for a in audience:
        if a["did"] not in seen:
            seen.add(a["did"])
            unique.append(a)
    return unique

# ---------- هل للحساب منشورات ----------
def has_posts(client: Client, did_or_handle: str) -> bool:
    resp = client.app.bsky.feed.get_author_feed(
        {"actor": did_or_handle, "limit": 1, "filter": "posts_no_replies"}
    )
    return len(resp.feed or []) > 0

# ---------- آخر منشور للمستخدم ----------
def latest_post_uri(client: Client, did_or_handle: str) -> Optional[str]:
    resp = client.app.bsky.feed.get_author_feed(
        {"actor": did_or_handle, "limit": 1, "filter": "posts_no_replies"}
    )
    if not resp.feed:
        return None
    post = resp.feed[0].post
    return post.uri  # at://did/app.bsky.feed.post/rkey

# ---------- إرسال رد ----------
def reply_to_post(client: Client, target_post_uri: str, text: str) -> str:
    """
    يرد على بوست محدد بـ target_post_uri.
    يصلح خطأ create_strong_ref عبر تمرير dict يحوي uri و cid.
    """
    posts = client.app.bsky.feed.get_posts({"uris": [target_post_uri]})
    if not posts.posts:
        raise RuntimeError("تعذر جلب معلومات البوست الهدف")

    parent = posts.posts[0]

    # ✅ الإصلاح: create_strong_ref يتوقع dict بالشكل {"uri": ..., "cid": ...}
    parent_ref = CU.create_strong_ref({"uri": parent.uri, "cid": parent.cid})

    # الجذر = إن كان للبوست root، استعمله، غير ذلك parent نفسه
    root_ref = parent_ref
    try:
        root = getattr(getattr(parent, "record", None), "reply", None)
        root = getattr(root, "root", None)
        if root and getattr(root, "uri", None) and getattr(root, "cid", None):
            root_ref = CU.create_strong_ref({"uri": root.uri, "cid": root.cid})
    except Exception:
        # لو أي خطأ، نستخدم parent كـ root
        root_ref = parent_ref

    record = M.AppBskyFeedPost.Record(
        text=text,
        reply=M.AppBskyFeedPost.ReplyRef(parent=parent_ref, root=root_ref),
        created_at=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        langs=["en"],
    )

    res = client.com.atproto.repo.create_record(
        {"collection": "app.bsky.feed.post", "repo": client.me.did, "record": record}
    )
    return res.uri

# ---------- حفظ/تحميل التقدم ----------
def load_progress(path: str) -> Dict:
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {
            "state": "Idle",
            "task": {},
            "audience": [],
            "index": 0,
            "stats": {"ok": 0, "fail": 0, "total": 0},
            "per_user": {},
            "last_error": "-",
        }

def save_progress(path: str, data: Dict) -> None:
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
