# utils.py
import re
import time
from typing import List, Tuple, Optional, Dict
import requests

BASE = "https://public.api.bsky.app/xrpc"
TIMEOUT = 30


# =========================
# Auth
# =========================
def get_api(handle: str, password: str) -> Tuple[Dict[str, str], str]:
    """
    يسجل الدخول ويعيد:
      - headers: بها الـ Bearer token
      - repo_did: الـ DID الخاص بحساب البوت (لاستخدامه كـ repo في createRecord)
    """
    url = f"{BASE}/com.atproto.server.createSession"
    r = requests.post(url, json={"identifier": handle, "password": password}, timeout=TIMEOUT)
    r.raise_for_status()
    data = r.json()
    access = data["accessJwt"]
    repo_did = data["did"]
    headers = {
        "Authorization": f"Bearer {access}",
        "Content-Type": "application/json",
    }
    return headers, repo_did


# =========================
# Helpers: Post URL parsing
# =========================
def resolve_post_from_url(post_url: str) -> Tuple[str, str, str]:
    """
    يأخذ رابط بوست بالشكل:
      https://bsky.app/profile/{handle}/post/{rkey}
    ويعيد:
      - did لصاحب البوست
      - rkey
      - uri بصيغة at://did/app.bsky.feed.post/rkey
    """
    m = re.search(r"bsky\.app/profile/([^/]+)/post/([^/?#]+)", post_url)
    if not m:
        raise ValueError("رابط غير صالح: يجب أن يكون من bsky.app وفيه /profile/.../post/...")
    handle = m.group(1)
    rkey = m.group(2)

    # resolve handle to DID
    res = requests.get(
        f"{BASE}/com.atproto.identity.resolveHandle",
        params={"handle": handle},
        timeout=TIMEOUT,
    )
    res.raise_for_status()
    did = res.json()["did"]

    uri = f"at://{did}/app.bsky.feed.post/{rkey}"
    return did, rkey, uri


# =========================
# Audience: Likers / Reposters (with pagination)
# =========================
def _paged_get(url: str, array_key: str, item_path: List[str]) -> List[str]:
    """
    مساعد داخلي للترقيم (cursor). يرجّع قائمة DIDs.
    - url: endpoint الكامل مع query عدا cursor
    - array_key: اسم المصفوفة في الرد (likes, repostedBy)
    - item_path: المسار داخل العنصر للوصول إلى الحقل "did"
      مثال: ["actor", "did"] أو ["did"]
    """
    out: List[str] = []
    cursor = None
    while True:
        params = {}
        if cursor:
            params["cursor"] = cursor
        r = requests.get(url, params=params, timeout=TIMEOUT)
        r.raise_for_status()
        data = r.json()
        arr = data.get(array_key, [])
        for it in arr:
            node = it
            for key in item_path:
                node = node[key]
            out.append(node)
        cursor = data.get("cursor")
        if not cursor:
            break
        # خفّة على الـ API
        time.sleep(0.15)
    return out


def get_likers(uri: str) -> List[str]:
    """يعيد DIDs لكل من أعجب بالبوست (مرتّبة من أعلى إلى أسفل حسب واجهة الـ API)."""
    url = f"{BASE}/app.bsky.feed.getLikes?uri={uri}"
    return _paged_get(url, "likes", ["actor", "did"])


def get_reposters(uri: str) -> List[str]:
    """يعيد DIDs لكل من أعاد نشر البوست (مرتّبة من أعلى إلى أسفل)."""
    url = f"{BASE}/app.bsky.feed.getRepostedBy?uri={uri}"
    return _paged_get(url, "repostedBy", ["did"])


# =========================
# Author feed helpers
# =========================
def has_posts(did: str) -> bool:
    """يتحقق إن كان للمستخدم أي منشورات عامة."""
    r = requests.get(
        f"{BASE}/app.bsky.feed.getAuthorFeed",
        params={"actor": did, "limit": 1},
        timeout=TIMEOUT,
    )
    r.raise_for_status()
    return len(r.json().get("feed", [])) > 0


def get_latest_post_uri(did: str) -> Optional[str]:
    """يعيد at:// URI لآخر منشور للمستخدم، أو None لو لم يوجد."""
    r = requests.get(
        f"{BASE}/app.bsky.feed.getAuthorFeed",
        params={"actor": did, "limit": 1},
        timeout=TIMEOUT,
    )
    r.raise_for_status()
    feed = r.json().get("feed", [])
    if not feed:
        return None
    return feed[0]["post"]["uri"]


# =========================
# Reply helpers
# =========================
def _get_cid_for_uri(uri: str) -> Optional[str]:
    """يجلب CID لبوست عبر getPosts."""
    r = requests.get(
        f"{BASE}/app.bsky.feed.getPosts",
        params={"uris": uri},
        timeout=TIMEOUT,
    )
    r.raise_for_status()
    posts = r.json().get("posts", [])
    if not posts:
        return None
    return posts[0].get("cid")


def reply_to_post(headers: Dict[str, str], repo_did: str, parent_uri: str, text: str) -> bool:
    """
    يرد على بوست معيّن (parent_uri). يجلب CID ويبني record الرد.
    """
    cid = _get_cid_for_uri(parent_uri)
    if not cid:
        return False

    payload = {
        "repo": repo_did,
        "collection": "app.bsky.feed.post",
        "record": {
            "$type": "app.bsky.feed.post",
            "text": text,
            "createdAt": __import__("datetime").datetime.utcnow().isoformat() + "Z",
            "reply": {
                "root": {"uri": parent_uri, "cid": cid},
                "parent": {"uri": parent_uri, "cid": cid},
            },
        },
    }

    r = requests.post(
        f"{BASE}/com.atproto.repo.createRecord",
        headers=headers,
        json=payload,
        timeout=TIMEOUT,
    )
    # 200 أو 201 كلاهما نجاح مقبول
    return r.status_code in (200, 201)


def reply_to_latest_post(headers: Dict[str, str], repo_did: str, target_did: str, text: str) -> bool:
    """
    يرد على آخر منشور للمستخدم target_did.
    يتجاهل المستخدم إن لم يكن لديه منشورات.
    """
    parent_uri = get_latest_post_uri(target_did)
    if not parent_uri:
        return False
    return reply_to_post(headers, repo_did, parent_uri, text)
