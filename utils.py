# utils.py
import os
import re
import time
import json
from typing import Dict, List, Tuple, Optional

# ========= اختيارياً: PostgreSQL عبر psycopg =========
try:
    import psycopg  # psycopg[binary]
    from contextlib import closing
except Exception:  # لو المكتبة غير متوفرة
    psycopg = None
    from contextlib import closing  # موجودة بس علشان التايبينغ

from atproto import Client, models as M


# ---------- جلسة العميل ----------
def make_client(handle: str, password: str) -> Client:
    """يسجّل الدخول ويعيد Client جاهز."""
    c = Client()
    # مهم: استخدمي App Password (وليس كلمة السر العادية)
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
            resp = client.app.bsky.feed.get_likes(
                {"uri": post_at_uri, "cursor": cursor, "limit": 100}
            )
            for item in resp.likes or []:
                actor = item.actor
                audience.append({"did": actor.did, "handle": actor.handle})
            cursor = getattr(resp, "cursor", None)
            if not cursor:
                break

    elif mode == "reposters":
        while True:
            resp = client.app.bsky.feed.get_reposted_by(
                {"uri": post_at_uri, "cursor": cursor, "limit": 100}
            )
            # الحقل الصحيح في الاستجابة:
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


# ---------- أدوات داخلية للفلترة ----------
def _is_repost(item) -> bool:
    """يعيد True إذا كان هذا العنصر عبارة عن إعادة نشر (reason موجود)."""
    return bool(getattr(item, "reason", None))


def _get_author_did_from_post(post) -> Optional[str]:
    """يحاول استخراج DID لصاحب البوست من الحقول الشائعة."""
    if hasattr(post, "author") and getattr(post.author, "did", None):
        return post.author.did
    # احتياط لو تغيّر الشكل
    if hasattr(post, "uri"):
        # at://did/app.bsky.feed.post/rkey
        parts = str(post.uri).split("/")
        if len(parts) >= 4 and parts[2].startswith("did:"):
            return parts[2]
    return None


# ---------- هل للحساب منشورات (أصلية) ----------
def has_posts(client: Client, did_or_handle: str) -> bool:
    """
    يتحقق من وجود منشور/رد أصلي للمستخدم (غير إعادة نشر).
    نمشي على صفحات قليلة للتأكد.
    """
    cursor: Optional[str] = None
    for _ in range(3):  # صفحات محدودة لتقليل الاستهلاك
        resp = client.app.bsky.feed.get_author_feed(
            {"actor": did_or_handle, "limit": 10, "cursor": cursor, "filter": "posts_with_replies"}
        )
        if not resp.feed:
            return False
        for item in resp.feed:
            if _is_repost(item):
                continue
            post = item.post
            author_did = _get_author_did_from_post(post)
            if author_did and author_did == did_or_handle:
                return True
        cursor = getattr(resp, "cursor", None)
        if not cursor:
            break
    return False


# ---------- آخر منشور للمستخدم (أصلي فقط) ----------
def latest_post_uri(client: Client, did_or_handle: str) -> Optional[str]:
    """
    يرجع آخر بوست/رد أصلي للمستخدم نفسه (NOT a repost).
    يمر على صفحات حتى يجد أو يعيد None.
    """
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
            author_did = _get_author_did_from_post(post)
            if author_did and author_did == did_or_handle:
                return post.uri  # at://did/app.bsky.feed.post/rkey

        cursor = getattr(resp, "cursor", None)
        if not cursor:
            break

    return None


# ---------- إرسال رد ----------
def reply_to_post(client: Client, target_post_uri: str, text: str) -> str:
    """
    يرد على بوست محدد بـ target_post_uri.
    نبني مراجع root/parent كـ dict يحوي uri و cid (بدون StrongRef).
    """
    posts = client.app.bsky.feed.get_posts({"uris": [target_post_uri]})
    if not posts.posts:
        raise RuntimeError("تعذر جلب معلومات البوست الهدف")

    parent = posts.posts[0]

    # المرجع الأساسي (parent) كـ dict
    parent_ref = {"uri": parent.uri, "cid": parent.cid}

    # المرجع الجذري (root) – إن لم يوجد نستخدم parent
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
#                         تخزين التقدّم (DB أو JSON)
# ======================================================================

# URL القاعدة: نقبل أكثر من اسم متغير بيئة لمرونة أعلى
SUPABASE_DB_URL = (
    os.getenv("DB_URL") or
    os.getenv("SUPABASE_DB_URL") or
    os.getenv("DATABASE_URL") or
    ""
)

# إجبار استخدام قاعدة البيانات حتى لو في JSON
FORCE_PG = os.getenv("FORCE_PG", "").strip() in ("1", "true", "True", "YES", "yes")

# مفتاح يميّز كل بوت داخل الجدول
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


def _db_enabled() -> bool:
    """
    هل الاتصال بقاعدة البيانات مفعّل؟
    - لازم يكون عندنا URL
    - ومكتبة psycopg متاحة
    - ولو FORCE_PG=1 بنرجّح DB ولو في حلول ثانية
    """
    if not SUPABASE_DB_URL:
        return False
    if psycopg is None:
        # لو مجبَرة DB والباكدج مش موجود، نطبع تحذير
        if FORCE_PG:
            print("[progress][warn] FORCE_PG=1 مفعّل لكن psycopg غير متوفر — سيُستخدم JSON كحل مؤقت.")
        return False
    return True


def _db_init_if_needed() -> None:
    """ينشئ جدول progress والفهرس لو غير موجودين (idempotent)."""
    if not _db_enabled():
        return
    try:
        with closing(psycopg.connect(SUPABASE_DB_URL)) as conn, conn.cursor() as cur:
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
        print(f"[progress][db] ready (bot_key={BOT_KEY})")
    except Exception as e:
        print(f"[progress][db][error] init failed: {e}")


def _db_load_progress() -> Dict:
    _db_init_if_needed()
    with closing(psycopg.connect(SUPABASE_DB_URL)) as conn, conn.cursor() as cur:
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
            # أنشئ صف جديد بالقيم الافتراضية
            cur.execute(
                """
                insert into progress (bot_key, state, task, audience, idx, stats, per_user, last_error)
                values (%s, %s, %s, %s, %s, %s, %s, %s);
                """,
                (
                    BOT_KEY,
                    _DEFAULT_PROGRESS["state"],
                    psycopg.types.json.Json(_DEFAULT_PROGRESS["task"]),
                    psycopg.types.json.Json(_DEFAULT_PROGRESS["audience"]),
                    _DEFAULT_PROGRESS["index"],
                    psycopg.types.json.Json(_DEFAULT_PROGRESS["stats"]),
                    psycopg.types.json.Json(_DEFAULT_PROGRESS["per_user"]),
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
    # دمج آمن مع الافتراضي
    merged = dict(_DEFAULT_PROGRESS)
    merged.update(data or {})
    with closing(psycopg.connect(SUPABASE_DB_URL)) as conn, conn.cursor() as cur:
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
                psycopg.types.json.Json(merged.get("task", {})),
                psycopg.types.json.Json(merged.get("audience", [])),
                int(merged.get("index", 0)),
                psycopg.types.json.Json(merged.get("stats", {"ok": 0, "fail": 0, "total": 0})),
                psycopg.types.json.Json(merged.get("per_user", {})),
                merged.get("last_error", "-"),
            ),
        )
        conn.commit()
    print(f"[progress][db] saved (state={merged.get('state')}, idx={merged.get('index')}, ok={merged.get('stats',{}).get('ok')}, fail={merged.get('stats',{}).get('fail')})")


# ---------- واجهة التحميل/الحفظ المستخدمة في باقي البرنامج ----------
def load_progress(path: str) -> Dict:
    """
    لو كان DB مُفعَّل (أو مُجبَر عبر FORCE_PG) => نقرأ من Postgres،
    وإلا نقرأ من ملف JSON كما كان سابقًا.
    في حال فشل الاتصال بقاعدة البيانات، نطبع تحذير ونرجع للملف.
    """
    use_db = _db_enabled() or FORCE_PG
    if use_db and _db_enabled():
        try:
            return _db_load_progress()
        except Exception as e:
            print(f"[progress][warn] DB load failed, fallback to file: {e}")
            # fallback للملف
            try:
                with open(path, "r", encoding="utf-8") as f:
                    return json.load(f)
            except Exception:
                return dict(_DEFAULT_PROGRESS)
    else:
        # JSON فقط
        try:
            with open(path, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return dict(_DEFAULT_PROGRESS)


def save_progress(path: str, data: Dict) -> None:
    """
    لو DB مُفعَّل (أو مُجبَر) => نخزّن في Postgres + نكتب نسخة ملف احتياطية،
    وإلا نخزّن في ملف JSON فقط.
    """
    use_db = _db_enabled() or FORCE_PG
    if use_db and _db_enabled():
        try:
            _db_save_progress(data)
        except Exception as e:
            print(f"[progress][warn] DB save failed, fallback to file: {e}")
            # حتى لو فشل DB، نكتب نسخة ملف
            try:
                with open(path, "w", encoding="utf-8") as f:
                    json.dump(data, f, ensure_ascii=False, indent=2)
            except Exception:
                pass
        else:
            # نسخة احتياطية ملف (اختياري)
            try:
                with open(path, "w", encoding="utf-8") as f:
                    json.dump(data, f, ensure_ascii=False, indent=2)
            except Exception:
                pass
    else:
        # JSON فقط
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
