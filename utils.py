import requests
import re

API_BASE = "https://public.api.bsky.app/xrpc"

def resolve_post_from_url(url):
    """
    يحوّل رابط البوست من bsky.app إلى بيانات (uri, cid)
    """
    match = re.search(r"profile/([^/]+)/post/([a-z0-9]+)", url)
    if not match:
        raise ValueError("رابط غير صالح")

    handle = match.group(1)
    rkey = match.group(2)

    # استدعاء API للحصول على البوست
    resp = requests.get(
        f"{API_BASE}/app.bsky.feed.getPostThread",
        params={"uri": f"at://{handle}/app.bsky.feed.post/{rkey}"}
    )

    if resp.status_code != 200:
        raise RuntimeError(f"فشل جلب البوست: {resp.text}")

    data = resp.json()
    post = data.get("thread", {}).get("post")
    if not post:
        raise RuntimeError("لم يتم العثور على البوست")

    return {
        "uri": post["uri"],
        "cid": post["cid"]
    }


def get_likers(uri, cid):
    """
    جلب قائمة المعجبين بالبوست
    """
    resp = requests.get(
        f"{API_BASE}/app.bsky.feed.getLikes",
        params={"uri": uri, "cid": cid}
    )
    if resp.status_code != 200:
        raise RuntimeError(f"فشل جلب المعجبين: {resp.text}")

    data = resp.json()
    return [item["actor"]["handle"] for item in data.get("likes", [])]


def get_reposters(uri, cid):
    """
    جلب قائمة معيدي النشر
    """
    resp = requests.get(
        f"{API_BASE}/app.bsky.feed.getRepostedBy",
        params={"uri": uri, "cid": cid}
    )
    if resp.status_code != 200:
        raise RuntimeError(f"فشل جلب معيدي النشر: {resp.text}")

    data = resp.json()
    return [item["handle"] for item in data.get("repostedBy", [])]


def has_posts(session, actor):
    """
    التأكد إذا المستخدم عنده بوستات (لأخذ آخر بوست للرد عليه)
    """
    resp = session.get(
        f"{API_BASE}/app.bsky.feed.getAuthorFeed",
        params={"actor": actor, "limit": 1}
    )
    if resp.status_code != 200:
        return False
    data = resp.json()
    return bool(data.get("feed"))
