import requests
import re

BASE_URL = "https://public.api.bsky.app/xrpc"

# =====================
# Auth Helper
# =====================
def get_api(handle, password):
    """Login and return auth headers"""
    url = f"{BASE_URL}/com.atproto.server.createSession"
    r = requests.post(url, json={"identifier": handle, "password": password})
    r.raise_for_status()
    data = r.json()
    return {
        "Authorization": f"Bearer {data['accessJwt']}",
        "Content-Type": "application/json"
    }

# =====================
# Extract Post Info
# =====================
def resolve_post_from_url(post_url):
    """
    Input: https://bsky.app/profile/{handle}/post/{rkey}
    Output: (did, rkey)
    """
    match = re.search(r"bsky\.app/profile/([^/]+)/post/([^/?#]+)", post_url)
    if not match:
        raise ValueError("رابط غير صالح: يجب أن يكون من bsky.app")
    handle = match.group(1)
    rkey = match.group(2)

    # resolve handle -> DID
    url = f"{BASE_URL}/com.atproto.identity.resolveHandle?handle={handle}"
    r = requests.get(url)
    r.raise_for_status()
    did = r.json().get("did")
    return did, rkey

# =====================
# Get Likers
# =====================
def get_likers(uri):
    url = f"{BASE_URL}/app.bsky.feed.getLikes?uri={uri}"
    r = requests.get(url)
    r.raise_for_status()
    out = []
    for item in r.json().get("likes", []):
        out.append(item["actor"]["did"])
    return out

# =====================
# Get Reposters
# =====================
def get_reposters(uri):
    url = f"{BASE_URL}/app.bsky.feed.getRepostedBy?uri={uri}"
    r = requests.get(url)
    r.raise_for_status()
    out = []
    for item in r.json().get("repostedBy", []):
        out.append(item["did"])
    return out

# =====================
# Get latest post by DID
# =====================
def get_latest_post(did):
    url = f"{BASE_URL}/app.bsky.feed.getAuthorFeed?actor={did}&limit=1"
    r = requests.get(url)
    r.raise_for_status()
    feed = r.json().get("feed", [])
    if not feed:
        return None
    return feed[0]["post"]["uri"]

# =====================
# Reply to a post
# =====================
def reply_to_latest_post(headers, target_did, message):
    latest_post = get_latest_post(target_did)
    if not latest_post:
        return False

    url = f"{BASE_URL}/com.atproto.repo.createRecord"
    data = {
        "collection": "app.bsky.feed.post",
        "repo": headers["Authorization"].split(" ")[1],  # jwt contains repo DID
        "record": {
            "text": message,
            "reply": {
                "parent": {"uri": latest_post, "cid": ""},
                "root": {"uri": latest_post, "cid": ""}
            },
            "createdAt": __import__("datetime").datetime.utcnow().isoformat() + "Z"
        }
    }
    r = requests.post(url, headers=headers, json=data)
    return r.status_code == 200
