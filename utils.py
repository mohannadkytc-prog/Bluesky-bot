# utils.py
import os
import re
import time
import json
from typing import Dict, List, Tuple, Optional
from contextlib import closing

import requests  # لا تحتاجين مكتبة supabase؛ نستخدم REST مباشرة.

# ========= تحكم بأنماط التخزين =========
FORCE_PG = os.getenv("FORCE_PG", "").strip().lower() in {"1", "true", "yes"}

# ---- إعداد psycopg v3 / psycopg2 إن وُجد (لخيار DB المباشر) ----
_psycopg_kind = "none"
try:
    import psycopg  # type: ignore

    def _connect(url: str):
        return psycopg.connect(url)

    def _json_param(v):
        return psycopg.types.json.Json(v)

    _psycopg_kind = "psycopg3"
except Exception:
    try:
        import psycopg2 as psycopg  # type: ignore
        import psycopg2.extras as _pg2extras  # type: ignore

        def _connect(url: str):
            return psycopg.connect(url)

        def _json_param(v):
            return _pg2extras.Json(v)

        _psycopg_kind = "psycopg2"
    except Exception:
        psycopg = None  # type: ignore

        def _connect(url: str):
            raise RuntimeError("psycopg/psycopg2 not installed")

        def _json_param(v):
            return v

if FORCE_PG and _psycopg_kind == "none":
    print("[progress][warn] FORCE_PG=1 مفعّل لكن psycopg/psycopg2 غير متوفر — سيتم استخدام REST/JSON حسب المتاح.")
elif _psycopg_kind != "none":
    print(f"[progress][info] PostgreSQL via {_psycopg_kind} مفعّل.")

from atproto import Client, models as M

# ---------- جلسة العميل ----------
def make_client(handle: str, password: str) -> Client:
    c = Client()
    c.login(handle, password)  # App Password
    return c


# ---------- تحليل رابط البوست ----------
def _parse_bsky_post_url(url: str) -> Tuple[str, str]:
    m = re.search(r"/profile/([^/]+)/post/([^/?#]+)", url)
    if not m:
        raise ValueError("رابط غير صالح لبوست Bluesky")
    return m.group(1), m.group(2)


def resolve_post_from_url(client: Client, url: str) -> Tuple[str, str, str]:
    actor, rkey = _parse_bsky_post_url(url)
    if actor.startswith("did:"):
        did = actor
    else:
        did = client.com.atproto.identity.resolve_handle({"handle": actor}).did
    return did, rkey, f"at://{did}/app.bsky.feed.post/{rkey}"


# ---------- جلب الجمهور ----------
def fetch_audience(client: Client, mode: str, post_at_uri: str) -> List[Dict]:
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
            for actor in resp.reposted_by or []:
                audience.append({"did": actor.did, "handle": actor.handle})
            cursor = getattr(resp, "cursor", None)
            if not cursor:
                break
    else:
        raise ValueError("mode يجب أن يكون likers أو reposters")

    # إزالة التكرار مع الحفاظ على الترتيب
    seen, unique = set(), []
    for a in audience:
        if a["did"] not in seen:
            seen.add(a["did"])
            unique.append(a)
    return unique


# ---------- أدوات داخلية ----------
def _is_repost(item) -> bool:
    return bool(getattr(item, "reason", None))


def _get_author_did_from_post(post) -> Optional[str]:
    if hasattr(post, "author") and getattr(post.author, "did", None):
        return post.author.did
    if hasattr(post, "uri"):
        parts = str(post.uri).split("/")
        if len(parts) >= 4 and parts[2].startswith("did:"):
            return parts[2]
    return None


def has_posts(client: Client, did_or_handle: str) -> bool:
    cursor: Optional[str] = None
    for _ in range(3):
        resp = client.app.bsky.feed.get_author_feed(
            {"actor": did_or_handle, "limit": 10, "cursor": cursor, "filter": "posts_with_replies"}
        )
        if not resp.feed:
            return False
        for item in resp.feed:
            if _is_repost(item):
                continue
            post = item.post
            if _get_author_did_from_post(post) == did_or_handle:
                return True
        cursor = getattr(resp, "cursor", None)
        if not cursor:
            break
    return False


def latest_post_uri(client: Client, did_or_handle: str) -> Optional[str]:
    cursor: Optional[str] = None
    while True:
        resp = client.app.bsky.feed.get_author_feed(
            {"actor": did_or_handle, "limit": 25, "cursor": cursor, "filter": "posts_with_replies"}
        )
        if not resp.feed:
            return None
        for item in resp.feed:
            if _is_repost(item):
                continue
            post = item.post
            if _get_author_did_from_post(post) == did_or_handle:
                return post.uri
        cursor = getattr(resp, "cursor", None)
        if not cursor:
            break
    return None


def reply_to_post(client: Client, target_post_uri: str, text: str) -> str:
    posts = client.app.bsky.feed.get_posts({"uris": [target_post_uri]})
    if not posts.posts:
        raise RuntimeError("تعذر جلب معلومات البوست الهدف")

    parent = posts.posts[0]
    parent_ref = {"uri": parent.uri, "cid": parent.cid}
    root_ref = parent_ref
    try:
        root = getattr(getattr(parent, "record", None), "reply", None)
        root = getattr(root, "root", None)
        if root and getattr(root, "uri", None) and getattr(root, "cid", None):
            root_ref = {"uri": root.uri, "cid": root.cid}
    except Exception:
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


# ======================================================================
#                         تخزين التقدّم (REST / DB / JSON)
# ======================================================================

# REST (Supabase PostgREST)
SUPABASE_URL = (os.getenv("SUPABASE_URL") or "").rstrip("/")
SUPABASE_KEY = os.getenv("SUPABASE_SERVICE_ROLE") or os.getenv("SUPABASE_ANON_KEY") or ""
REST_ENABLED = bool(SUPABASE_URL and SUPABASE_KEY)

def _rest_headers():
    return {
        "apikey": SUPABASE_KEY,
        "Authorization": f"Bearer {SUPABASE_KEY}",
        "Content-Type": "application/json",
        "Accept": "application/json",
        "Prefer": "return=representation",
    }

def _rest_table_url(table: str) -> str:
    return f"{SUPABASE_URL}/rest/v1/{table}"

# DB مباشر
SUPABASE_DB_URL = os.getenv("DB_URL") or os.getenv("SUPABASE_DB_URL") or os.getenv("DATABASE_URL") or ""

# مفتاح البوت
BOT_KEY = (
    os.getenv("BOT_KEY")
    or os.getenv("RENDER_SERVICE_NAME")
    or os.getenv("FLY_APP_NAME")
    or os.getenv("RAILWAY_SERVICE_NAME")
    or "default-bot"
)

_DEFAULT_PROGRESS: Dict = {
    "state": "Idle",
    "task": {},
    "audience": [],
    "index": 0,
    "stats": {"ok": 0, "fail": 0, "total": 0},
    "per_user": {},
    "last_error": "-",
}

# ---------- REST helpers ----------
def _rest_get_progress() -> Optional[Dict]:
    """يرجع صف progress للبوت إن وجد، وإلا None."""
    url = _rest_table_url("progress")
    # نفلتر bot_key بالضبط (case-sensitive) لتجنّب مشكلة lower(bot_key)
    params = {"select": "state,task,audience,idx,stats,per_user,last_error", "bot_key": f"eq.{BOT_KEY}", "limit": "1"}
    r = requests.get(url, headers=_rest_headers(), params=params, timeout=20)
    r.raise_for_status()
    rows = r.json()
    if rows:
        row = rows[0]
        return {
            "state": row.get("state", "Idle"),
            "task": row.get("task") or {},
            "audience": row.get("audience") or [],
            "index": row.get("idx") or 0,
            "stats": row.get("stats") or {"ok": 0, "fail": 0, "total": 0},
            "per_user": row.get("per_user") or {},
            "last_error": row.get("last_error") or "-",
        }
    return None

def _rest_insert_default() -> Dict:
    """يدخل صف افتراضي للبوت ويعيده."""
    url = _rest_table_url("progress")
    payload = [{
        "bot_key": BOT_KEY,
        "state": _DEFAULT_PROGRESS["state"],
        "task": _DEFAULT_PROGRESS["task"],
        "audience": _DEFAULT_PROGRESS["audience"],
        "idx": _DEFAULT_PROGRESS["index"],
        "stats": _DEFAULT_PROGRESS["stats"],
        "per_user": _DEFAULT_PROGRESS["per_user"],
        "last_error": _DEFAULT_PROGRESS["last_error"],
    }]
    r = requests.post(url, headers={**_rest_headers(), "Prefer": "return=representation"}, json=payload, timeout=20)
    r.raise_for_status()
    print(f"[progress][rest] created default row for {BOT_KEY}")
    return _DEFAULT_PROGRESS.copy()

def _rest_save_progress(data: Dict) -> None:
    """تحديث أو إدخال حسب وجود الصف."""
    existing = _rest_get_progress()
    url = _rest_table_url("progress")
    merged = dict(_DEFAULT_PROGRESS); merged.update(data or {})
    if existing is None:
        # insert
        body = [{
            "bot_key": BOT_KEY,
            "state": merged["state"],
            "task": merged["task"],
            "audience": merged["audience"],
            "idx": int(merged["index"]),
            "stats": merged["stats"],
            "per_user": merged["per_user"],
            "last_error": merged["last_error"],
        }]
        r = requests.post(url, headers=_rest_headers(), json=body, timeout=20)
        r.raise_for_status()
    else:
        # update
        params = {"bot_key": f"eq.{BOT_KEY}"}
        body = {
            "state": merged["state"],
            "task": merged["task"],
            "audience": merged["audience"],
            "idx": int(merged["index"]),
            "stats": merged["stats"],
            "per_user": merged["per_user"],
            "last_error": merged["last_error"],
            "updated_at": "now()",
        }
        r = requests.patch(url, headers=_rest_headers(), params=params, json=body, timeout=20)
        r.raise_for_status()
    print(f"[progress][rest] saved (state={merged.get('state')}, idx={merged.get('index')})")

# ---------- DB مباشر (كما كان) ----------
def _db_enabled() -> bool:
    if not SUPABASE_DB_URL:
        return False
    if _psycopg_kind == "none":
        if FORCE_PG:
            print("[progress][warn] FORCE_PG=1 مفعّل لكن لا توجد مكتبة psycopg/psycopg2 — سيتم استخدام REST/JSON.")
        return False
    return True

def _db_init_if_needed() -> None:
    if not _db_enabled():
        return
    try:
        with closing(_connect(SUPABASE_DB_URL)) as conn, conn.cursor() as cur:
            cur.execute(
                """
                create table if not exists progress (
                    id          bigserial primary key,
                    bot_key     text not null,
                    state       text not null default 'Idle',
                    task        jsonb not null default '{}'::jsonb,
                    audience    jsonb not null default '[]'::jsonb,
                    idx         integer not null default 0,
                    stats       jsonb not null default '{"ok":0,"fail":0,"total":0}'::jsonb,
                    per_user    jsonb not null default '{}'::jsonb,
                    last_error  text not null default '-',
                    updated_at  timestamptz not null default now()
                );
                create unique index if not exists progress_bot_key_unique
                    on progress (lower(bot_key));
                """
            )
            conn.commit()
        print(f"[progress][db] ready (bot_key={BOT_KEY}) via {_psycopg_kind}")
    except Exception as e:
        print(f"[progress][db][error] init failed: {e}")

def _db_load_progress() -> Dict:
    _db_init_if_needed()
    with closing(_connect(SUPABASE_DB_URL)) as conn, conn.cursor() as cur:
        cur.execute(
            """
            select state, task, audience, idx, stats, per_user, last_error
            from progress
            where lower(bot_key)=lower(%s)
            limit 1;
            """,
            (BOT_KEY,),
        )
        row = cur.fetchone()
        if not row:
            cur.execute(
                """
                insert into progress (bot_key, state, task, audience, idx, stats, per_user, last_error)
                values (%s, %s, %s, %s, %s, %s, %s, %s);
                """,
                (
                    BOT_KEY,
                    _DEFAULT_PROGRESS["state"],
                    _json_param(_DEFAULT_PROGRESS["task"]),
                    _json_param(_DEFAULT_PROGRESS["audience"]),
                    _DEFAULT_PROGRESS["index"],
                    _json_param(_DEFAULT_PROGRESS["stats"]),
                    _json_param(_DEFAULT_PROGRESS["per_user"]),
                    _DEFAULT_PROGRESS["last_error"],
                ),
            )
            conn.commit()
            print(f"[progress][db] created default row for {BOT_KEY}")
            return dict(_DEFAULT_PROGRESS)
        state, task, audience, idx, stats, per_user, last_error = row
        print(f"[progress][db] loaded row for {BOT_KEY} (state={state}, idx={idx})")
        return {
            "state": state,
            "task": task or {},
            "audience": audience or [],
            "index": idx or 0,
            "stats": stats or {"ok": 0, "fail": 0, "total": 0},
            "per_user": per_user or {},
            "last_error": last_error or "-",
        }

def _db_save_progress(data: Dict) -> None:
    _db_init_if_needed()
    merged = dict(_DEFAULT_PROGRESS); merged.update(data or {})
    with closing(_connect(SUPABASE_DB_URL)) as conn, conn.cursor() as cur:
        cur.execute(
            """
            insert into progress (bot_key, state, task, audience, idx, stats, per_user, last_error, updated_at)
            values (%s, %s, %s, %s, %s, %s, %s, %s, now())
            on conflict (lower(bot_key)) do update set
                state = excluded.state,
                task = excluded.task,
                audience = excluded.audience,
                idx = excluded.idx,
                stats = excluded.stats,
                per_user = excluded.per_user,
                last_error = excluded.last_error,
                updated_at = now();
            """,
            (
                BOT_KEY,
                merged.get("state", "Idle"),
                _json_param(merged.get("task", {})),
                _json_param(merged.get("audience", [])),
                int(merged.get("index", 0)),
                _json_param(merged.get("stats", {"ok": 0, "fail": 0, "total": 0})),
                _json_param(merged.get("per_user", {})),
                merged.get("last_error", "-"),
            ),
        )
        conn.commit()
    print(f"[progress][db] saved (state={merged.get('state')}, idx={merged.get('index')})")


# ---------- API موحّد لقراءة/حفظ التقدّم ----------
def load_progress(path: str) -> Dict:
    """
    الأولوية: REST إذا متاح → DB مباشر إذا مُجبر/متاح → JSON.
    """
    # 1) REST
    if REST_ENABLED and not FORCE_PG:
        try:
            row = _rest_get_progress()
            if row is None:
                return _rest_insert_default()
            return row
        except Exception as e:
            print(f"[progress][rest][warn] load REST failed, fallback: {e}")

    # 2) DB مباشر
    use_db = (_db_enabled() and FORCE_PG) or (_db_enabled() and not REST_ENABLED)
    if use_db:
        try:
            return _db_load_progress()
        except Exception as e:
            print(f"[progress][db][warn] load DB failed, fallback: {e}")

    # 3) JSON
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return dict(_DEFAULT_PROGRESS)


def save_progress(path: str, data: Dict) -> None:
    """
    الأولوية: REST إذا متاح → DB مباشر إذا مُجبر/متاح → JSON.
    تُكتب نسخة JSON دائمًا كنسخة احتياطية عندما ينجح REST/DB.
    """
    # 1) REST
    if REST_ENABLED and not FORCE_PG:
        try:
            _rest_save_progress(data)
            try:
                with open(path, "w", encoding="utf-8") as f:
                    json.dump(data, f, ensure_ascii=False, indent=2)
            except Exception:
                pass
            return
        except Exception as e:
            print(f"[progress][rest][warn] save REST failed, fallback: {e}")

    # 2) DB مباشر
    use_db = (_db_enabled() and FORCE_PG) or (_db_enabled() and not REST_ENABLED)
    if use_db:
        try:
            _db_save_progress(data)
            try:
                with open(path, "w", encoding="utf-8") as f:
                    json.dump(data, f, ensure_ascii=False, indent=2)
            except Exception:
                pass
            return
        except Exception as e:
            print(f"[progress][db][warn] save DB failed, fallback: {e}")

    # 3) JSON
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
