"""Bluesky helpers — أسماء الدوال ثابتة لتفادي ImportError."""
from typing import List, Dict, Optional
import re
import requests  # NEW: نستخدمه لتحويل الـ handle إلى DID مع XRPC

# ============== اربط عميل Bluesky هنا (اختياري لاحقاً) ==============
def get_api(handle: str, password: str):
    # TODO: اربط عميل Bluesky الحقيقي إذا رغبتِ.
    return None
# ====================================================================


def _normalize_url(url: str) -> str:
    """نضمن وجود https:// في بداية الرابط."""
    url = (url or "").strip()
    if url and not url.startswith("http"):
        # يسمح بإدخال 'profile/..' فنكمّلها
        if url.startswith("profile/"):
            url = "https://bsky.app/" + url
        else:
            url = "https://" + url
    return url


def resolve_handle_to_did(handle: str) -> Optional[str]:
    """
    يحوّل handle (مثل user.bsky.social) إلى DID باستخدام:
    https://bsky.social/xrpc/com.atproto.identity.resolveHandle?handle=...
    لا يحتاج توثيق.
    """
    try:
        h = handle.strip()
        # لو أُدخل كـ did أصلاً
        if h.startswith("did:"):
            return h
        resp = requests.get(
            "https://bsky.social/xrpc/com.atproto.identity.resolveHandle",
            params={"handle": h},
            timeout=10,
        )
        if resp.ok:
            data = resp.json()
            did = data.get("did")
            return did
    except Exception as e:
        print(f"[resolve_handle_to_did] {e}")
    return None


def resolve_post_from_url(url: str) -> Optional[str]:
    """
    يقبل روابط مثل:
      https://bsky.app/profile/<did OR handle>/post/<rkey>
    ويعيد post URI: at://<did>/app.bsky.feed.post/<rkey>
    الآن يدعم الـ handle تلقائياً (نحوّل إلى DID).
    """
    url = _normalize_url(url)
    m = re.search(r"/profile/([^/]+)/post/([A-Za-z0-9]+)", url)
    if not m:
        return None
    did_or_handle, rkey = m.group(1), m.group(2)

    # لو كان المعطى did نعيد مباشرة
    if did_or_handle.startswith("did:"):
        return f"at://{did_or_handle}/app.bsky.feed.post/{rkey}"

    # لو كان handle نحوله إلى DID عبر XRPC
    did = resolve_handle_to_did(did_or_handle)
    if not did:
        return None
    return f"at://{did}/app.bsky.feed.post/{rkey}"


def get_likers(api, post_uri: str) -> List[Dict]:
    try:
        if api is None:
            return []
        resp = api.get_likes(post_uri)
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


def get_reposters(api, post_uri: str) -> List[Dict]:
    try:
        if api is None:
            return []
        resp = api.get_reposted_by(post_uri)
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


def has_posts(api, did: str) -> bool:
    try:
        if api is None:
            return False
        feed = api.get_author_feed(did, limit=1)
        return bool(feed and isinstance(feed, dict) and feed.get("feed"))
    except Exception as e:
        print(f"[has_posts] {e}")
        return False


def reply_to_latest_post(api, did: str, message: str) -> bool:
    try:
        if api is None:
            return False
        feed = api.get_author_feed(did, limit=1)
        if not feed or "feed" not in feed or not feed["feed"]:
            return False
        latest_uri = feed["feed"][0]["post"]["uri"]
        api.send_post(text=message, reply_to=latest_uri)
        return True
    except Exception as e:
        print(f"[reply_to_latest_post] {e}")
        return False
