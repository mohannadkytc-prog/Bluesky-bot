import requests

def get_likers(post_uri, session):
    """يرجع قائمة المستخدمين اللي عملوا لايك للبوست."""
    url = f"https://bsky.social/xrpc/app.bsky.feed.getLikes?uri={post_uri}"
    resp = session.get(url)
    resp.raise_for_status()
    data = resp.json()
    return [like["actor"]["did"] for like in data.get("likes", [])]

def get_reposters(post_uri, session):
    """يرجع قائمة المستخدمين اللي عملوا ريبوسـت للبوست."""
    url = f"https://bsky.social/xrpc/app.bsky.feed.getRepostedBy?uri={post_uri}"
    resp = session.get(url)
    resp.raise_for_status()
    data = resp.json()
    return [repost["did"] for repost in data.get("repostedBy", [])]

def reply_to_latest_post(user_did, message, session):
    """يرد على آخر بوست لليوزر المحدد (بـ DID)."""
    feed_url = f"https://bsky.social/xrpc/app.bsky.feed.getAuthorFeed?actor={user_did}&limit=1"
    feed_resp = session.get(feed_url)
    feed_resp.raise_for_status()
    feed_data = feed_resp.json()
    posts = feed_data.get("feed", [])
    
    if not posts:
        return {"status": "skipped_no_posts", "user": user_did}
    
    post = posts[0]["post"]
    post_uri = post["uri"]
    post_cid = post["cid"]

    reply_url = "https://bsky.social/xrpc/com.atproto.repo.createRecord"
    reply_payload = {
        "repo": session.did,
        "collection": "app.bsky.feed.post",
        "record": {
            "text": message,
            "reply": {
                "root": {"uri": post_uri, "cid": post_cid},
                "parent": {"uri": post_uri, "cid": post_cid},
            }
        }
    }

    r = session.post(reply_url, json=reply_payload)
    if r.status_code == 200:
        return {"status": "ok", "user": user_did}
    else:
        return {"status": "fail", "user": user_did, "error": r.text}
