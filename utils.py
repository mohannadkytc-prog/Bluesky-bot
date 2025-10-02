import re
import requests

# تحويل رابط المتصفح إلى URI
def convert_url_to_uri(url):
    """
    يحول الرابط العادي من bsky.app إلى uri صحيح
    """
    match = re.match(r"https:\/\/bsky\.app\/profile\/([^\/]+)\/post\/([^\/]+)", url)
    if not match:
        raise ValueError("الرابط غير صحيح")
    handle = match.group(1)
    rkey = match.group(2)
    # نستخدم resolveHandle للحصول على DID
    did = resolve_handle(handle)
    return f"at://{did}/app.bsky.feed.post/{rkey}"

def resolve_handle(handle):
    """
    جلب DID من الـ handle (اليوزر)
    """
    resp = requests.get(f"https://bsky.social/xrpc/com.atproto.identity.resolveHandle?handle={handle}")
    data = resp.json()
    if "did" not in data:
        raise ValueError("فشل في جلب did من handle")
    return data["did"]

# جلب قائمة المعجبين
def get_likers(session, uri):
    resp = session.get("app.bsky.feed.getLikes", params={"uri": uri})
    data = resp.json()
    return [item["actor"]["did"] for item in data.get("likes", [])]

# جلب قائمة معيدي النشر
def get_reposters(session, uri):
    resp = session.get("app.bsky.feed.getRepostedBy", params={"uri": uri})
    data = resp.json()
    return [item["did"] for item in data.get("repostedBy", [])]

# جلب آخر بوست للمستخدم
def get_latest_post(session, did):
    resp = session.get("app.bsky.feed.getAuthorFeed", params={"actor": did, "limit": 1})
    feed = resp.json().get("feed", [])
    if not feed:
        return None
    return feed[0]["post"]["uri"]

# الرد على بوست
def reply_to_post(session, post_uri, text):
    session.post("app.bsky.feed.post", json={
        "text": text,
        "reply": {"parent": {"uri": post_uri}}
    })
