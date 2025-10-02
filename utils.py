"""Bluesky helpers — أسماء الدوال ثابتة لتفادي ImportError."""
from typing import List, Dict, Tuple, Optional
import re

# ============== اربط عميل Bluesky هنا ==============
# إذا عندك عميل مُجهّز (atproto مثلاً)، عرّف get_api() ليرجعه.
def get_api(handle: str, password: str):
    """
    TODO: اكتب هنا تهيئة عميل Bluesky الحقيقي وأعده.
    لازم ترجع كائن فيه دوال:
      - get_likes(post_uri)            -> dict
      - get_reposted_by(post_uri)      -> dict
      - get_author_feed(did, limit=1)  -> dict
      - send_post(text, reply_to_uri)  -> None/raise
    مؤقتاً نرجع None ونتعامل دفاعياً في الدوال الأخرى.
    """
    return None
# ====================================================


# ----解析/استخراج URI من رابط منشور Bluesky----
def resolve_post_from_url(url: str) -> Optional[str]:
    """
    يقبل روابط مثل:
      https://bsky.app/profile/<did OR handle>/post/<rkey>
    ويعيد post URI مثل: at://<did>/app.bsky.feed.post/<rkey>
    ملاحظة: للحصول على did الحقيقي من handle يلزم استعلام API؛
    إن لم يتوفر، نعيد None ويظهر خطأ مفهوم بالواجهة.
    """
    m = re.search(r"/profile/([^/]+)/post/([A-Za-z0-9]+)", url)
    if not m:
        return None
    did_or_handle, rkey = m.group(1), m.group(2)
    # إن كان معك did مباشر فالمعادلة سهلة:
    if did_or_handle.startswith("did:"):
        return f"at://{did_or_handle}/app.bsky.feed.post/{rkey}"
    # لو كان handle ستحتاج تحويله إلى did عبر API حقيقي.
    # مؤقتاً نعيد None ليظهر خطأ واضح.
    return None


# ---- معجبين ----
def get_likers(api, post_uri: str) -> List[Dict]:
    """
    يعيد قائمة بالترتيب من الأعلى للأسفل.
    كل عنصر على الأقل يحتوي {'did': '<did>'}
    """
    try:
        if api is None:
            return []
        resp = api.get_likes(post_uri)  # اكتب ترابطك الحقيقي هنا
        items = resp.get("likes", []) if isinstance(resp, dict) else []
        out: List[Dict] = []
        for it in items:
            did = (it.get("actor") or {}).get("did") or it.get("did")
            if did:
                out.append({"did": did})
        return out
    except Exception as e:
        print(f"[get_likers] {e}")
        return []


# ---- معيدو النشر ----
def get_reposters(api, post_uri: str) -> List[Dict]:
    try:
        if api is None:
            return []
        resp = api.get_reposted_by(post_uri)  # اكتب ترابطك الحقيقي هنا
        items = resp.get("repostedBy", []) if isinstance(resp, dict) else []
        out: List[Dict] = []
        for it in items:
            did = it.get("did")
            if did:
                out.append({"did": did})
        return out
    except Exception as e:
        print(f"[get_reposters] {e}")
        return []


# ---- التحقق من وجود منشورات ----
def has_posts(api, did: str) -> bool:
    try:
        if api is None:
            return False
        feed = api.get_author_feed(did, limit=1)  # اكتب ترابطك الحقيقي هنا
        return bool(feed and isinstance(feed, dict) and feed.get("feed"))
    except Exception as e:
        print(f"[has_posts] {e}")
        return False


# ---- الرد على آخر منشور ----
def reply_to_latest_post(api, did: str, message: str) -> bool:
    try:
        if api is None:
            return False
        feed = api.get_author_feed(did, limit=1)  # اكتب ترابطك الحقيقي هنا
        if not feed or "feed" not in feed or not feed["feed"]:
            return False
        latest_uri = feed["feed"][0]["post"]["uri"]
        api.send_post(text=message, reply_to=latest_uri)  # اكتب ترابطك الحقيقي هنا
        return True
    except Exception as e:
        print(f"[reply_to_latest_post] {e}")
        return False
