"""Redis cache for the subscription hot path: announcements + device-block.

The DB is the single source of truth; Redis is a best-effort accelerator.
**Every** operation degrades gracefully — any Redis error/timeout (or Redis
not being installed/reachable at all) makes the caller fall through to the
DB, so subscriptions never break and a block never "fails open" beyond the
read-through model's bounded window.

Device-block: read-through, **both outcomes cached**, 30-min TTL. An admin
block writes the key to "blocked" immediately (invalidate + add in one), so
the very next fetch rejects from cache. A manual unblock (no endpoint) is
bounded by the TTL.

Announcement: read-through with a safety TTL; correctness comes from
write-through invalidation on every post/disable.
"""
import json
import logging
from typing import Dict, List, Optional

try:  # redis is optional at import time — absence just disables the cache
    import redis as _redislib
except Exception:  # pragma: no cover
    _redislib = None

from app.db import crud
from config import REDIS_URL

logger = logging.getLogger(__name__)

_BLOCK_TTL = 1800      # 30 min, for both blocked and not-blocked entries
_ANNOUNCE_TTL = 1800   # safety net; primary correctness is invalidate-on-write
_NONE = "\x00none"     # negative-cache sentinel ("looked up, nothing usable")


def _make_client():
    if not REDIS_URL or _redislib is None:
        return None
    try:
        # Short timeouts: a slow/dead Redis must never stall the single-worker
        # request path — it fast-fails and the caller uses the DB.
        return _redislib.Redis.from_url(
            REDIS_URL,
            decode_responses=True,
            socket_connect_timeout=0.2,
            socket_timeout=0.2,
            health_check_interval=30,
        )
    except Exception:
        logger.exception("redis client init failed; caching disabled")
        return None


_client = _make_client()


# ---------------------------------------------------------------------------
# Device-block cache (read-through, both outcomes, 30-min TTL)
# ---------------------------------------------------------------------------

def _blk_key(user_id: int, kind: str, val: str) -> str:
    return f"blk:{user_id}:{kind}:{val}"


def _is_blocked_cached(db, user_id: int, kind: str, val: str, db_check) -> bool:
    key = _blk_key(user_id, kind, val)
    if _client is not None:
        try:
            cached = _client.get(key)
            if cached is not None:
                return cached == "1"
        except Exception:
            pass  # fall through to DB
    blocked = db_check(db, user_id, val)
    if _client is not None:
        try:
            _client.set(key, "1" if blocked else "0", ex=_BLOCK_TTL)
        except Exception:
            pass
    return blocked


def is_hwid_blocked(db, user_id: int, hwid: str) -> bool:
    return _is_blocked_cached(db, user_id, "hwid", hwid, crud.is_hwid_blocked)


def is_device_key_blocked(db, user_id: int, device_key: str) -> bool:
    """Block check for an exact identity. 'hwid:' keys keep the 'hwid' cache
    kind (so existing entries stay valid); Micky 'pk:' keys use 'key'."""
    if device_key.startswith(crud.DEVICE_KEY_HWID_PREFIX):
        return is_hwid_blocked(db, user_id, device_key[len(crud.DEVICE_KEY_HWID_PREFIX):])
    return _is_blocked_cached(db, user_id, "key", device_key, crud.is_device_key_blocked)


def is_model_blocked(db, user_id: int, model: str) -> bool:
    return _is_blocked_cached(db, user_id, "model", model, crud.is_model_blocked)


def cache_block(user_id: int, *, hwid: Optional[str] = None,
                model: Optional[str] = None, device_key: Optional[str] = None) -> None:
    """Called when an admin blocks a device: write the key to blocked now so
    the next fetch rejects from cache (overwrites any stale not-blocked)."""
    if _client is None:
        return
    try:
        if hwid:
            _client.set(_blk_key(user_id, "hwid", hwid), "1", ex=_BLOCK_TTL)
        if model:
            _client.set(_blk_key(user_id, "model", model), "1", ex=_BLOCK_TTL)
        if device_key:
            _client.set(_blk_key(user_id, "key", device_key), "1", ex=_BLOCK_TTL)
    except Exception:
        pass


def invalidate_user_blocks(user_ids: List[int]) -> None:
    """Drop every cached block verdict for these users — used when the users
    themselves are deleted, where the specific hwid/model values are no longer
    known so `uncache_block` cannot be called.

    Both outcomes are cached under `blk:<user_id>:<kind>:<val>` with a 30-min
    TTL and no other expiry path, so without this a purge leaves verdicts
    behind that describe users that no longer exist. That only becomes visible
    if the ids are reused — which MySQL 8 does not do (AUTO_INCREMENT is
    persisted across restarts) but SQLite does, freely. Cheap enough to do
    unconditionally rather than reason about per deployment.
    """
    if _client is None or not user_ids:
        return
    try:
        for user_id in user_ids:
            keys = list(_client.scan_iter(match=f"blk:{user_id}:*", count=500))
            if keys:
                _client.delete(*keys)
    except Exception:
        pass


def uncache_block(user_id: int, *, hwid: Optional[str] = None,
                  model: Optional[str] = None) -> None:
    """Clear a key (e.g. on an unblock) so the next fetch re-reads the DB."""
    if _client is None:
        return
    try:
        keys = []
        if hwid:
            keys.append(_blk_key(user_id, "hwid", hwid))
        if model:
            keys.append(_blk_key(user_id, "model", model))
        if keys:
            _client.delete(*keys)
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Announcement cache (read-through + invalidate-on-write)
# ---------------------------------------------------------------------------

def _ann_key(admin_id: Optional[int]) -> str:
    return "announce:global" if admin_id is None else f"announce:admin:{admin_id}"


def _latest_usable_text(db, admin_id: Optional[int]) -> Optional[str]:
    """Latest row for admin_id/global, returning its text only if that latest
    row is enabled (else None — a disabled latest must fall through). Cached
    per key with a safety TTL; the _NONE sentinel caches 'nothing usable'."""
    key = _ann_key(admin_id)
    if _client is not None:
        try:
            c = _client.get(key)
            if c is not None:
                return None if c == _NONE else c
        except Exception:
            pass
    latest = crud.get_latest_announcement(db, admin_id)
    text = latest.text if (latest is not None and latest.enabled) else None
    if _client is not None:
        try:
            _client.set(key, text if text is not None else _NONE, ex=_ANNOUNCE_TTL)
        except Exception:
            pass
    return text


def get_effective_announcement(db, admin_id: Optional[int]) -> Optional[str]:
    """Resolved announcement for an admin's users (cache-backed): the admin's
    latest row if enabled, else the latest global row if enabled, else None."""
    if admin_id is not None:
        own = _latest_usable_text(db, admin_id)
        if own is not None:
            return own
    return _latest_usable_text(db, None)


def invalidate_announcement(admin_id: Optional[int]) -> None:
    """Drop the cached entry for an admin (or global) after a post/disable."""
    if _client is None:
        return
    try:
        _client.delete(_ann_key(admin_id))
    except Exception:
        pass


# ---------------------------------------------------------------------------
# External subscriptions (other-panel host mirroring)
#
# THIS IS THE CONTRACT WITH THE RUST POLLER (stats-collector/src/poller/).
# Keys, value shapes and the no-TTL rule are duplicated in store.rs — change
# both together.
#
#   extsub:payload:<sub_id>:<format>  render-ready payload, 24h TTL refreshed
#                                     on every successful poll cycle — the
#                                     staleness bound: hosts nothing could
#                                     verify for a day stop being served.
#                                     For `v2ray` this is the DECODED,
#                                     newline-joined URI list — the panel has
#                                     to merge before base64-encoding, so
#                                     storing it encoded would mean decode,
#                                     merge, re-encode on every fetch.
#   extsub:status:<sub_id>            JSON, last poll outcome. Panel UI only.
#   extsub:identity                   JSON {hwid, user_agent} the poller uses.
#
# Unlike every other cache here there is no DB fallback, because there is no
# DB copy: payloads are cache-only by design and heal on the next poll cycle
# (the poller rewrites unconditionally every cycle). A miss therefore means
# "not polled yet, evicted, stale past the 24h bound, or deleted by the
# poller because the remote account reported itself expired/out of quota" —
# and in every one of those cases the caller MUST degrade to serving only
# local hosts. `_log_payload_miss` exists so a *permanent* hole is visible in
# the log instead of looking identical to "nothing registered yet".
# ---------------------------------------------------------------------------

# Formats the poller may publish. Kept here (rather than imported from the
# subscription layer) because this module must stay importable without pulling
# in the generators — `app.utils.cache` is imported by routers that never
# render a subscription.
EXTERNAL_FORMATS = (
    "v2ray", "v2ray-json", "clash", "clash-meta", "sing-box", "outline",
)


# Which enabled subscriptions each admin receives, cached per admin. Read on
# every subscription fetch by an eligible user, so it must not cost a query.
#
# Correctness comes from invalidate-on-write; the TTL is a CEILING on how long a
# mistake can last, not the mechanism. It exists because invalidation is
# best-effort by construction — a non-atomic SCAN+DEL that swallows Redis errors
# while the write still returns 200, and a read-through that can re-populate a
# pre-write value if it races a DEL. Without a ceiling, one dropped DEL leaves a
# revoked admin serving foreign hosts (or a granted one serving none) FOREVER.
# Five minutes costs at most one indexed query per admin per five minutes, which
# leaves "no SQL in steady state" intact.
_EXTSUB_ADMIN_TTL = 300
_EXTSUB_ADMIN_KEY_PREFIX = "extsub:admin:"


def _extsub_admin_key(admin_id: int) -> str:
    return f"{_EXTSUB_ADMIN_KEY_PREFIX}{admin_id}"


def get_cached_external_ids_for_admin(admin_id: int) -> Optional[List[int]]:
    """This admin's enabled subscription ids, or None on a miss.

    None and `[]` mean different things: `[]` is a cached "this admin is
    assigned nothing" and must NOT send the caller to the database.
    """
    if _client is None:
        return None
    try:
        raw = _client.get(_extsub_admin_key(admin_id))
    except Exception:
        return None
    if raw is None:
        return None
    try:
        parsed = json.loads(raw)
    except Exception:
        return None
    if not isinstance(parsed, list):
        return None
    return [int(i) for i in parsed if isinstance(i, int)]


def set_cached_external_ids_for_admin(admin_id: int, ids: List[int]) -> None:
    if _client is None:
        return
    try:
        _client.set(_extsub_admin_key(admin_id), json.dumps(ids), ex=_EXTSUB_ADMIN_TTL)
    except Exception:
        pass


def invalidate_external_admin_ids() -> None:
    """Drop every per-admin mapping.

    Called after any registry or assignment write. Dropping all of them rather
    than the ones that changed is deliberate: an assignment edit can add and
    remove admins in one request, enabling/disabling or deleting a subscription
    moves it for every admin at once, and the set is tiny (one small key per
    admin). Rebuilding is one query per admin, on their next fetch.
    """
    if _client is None:
        return
    try:
        keys = list(_client.scan_iter(match=f"{_EXTSUB_ADMIN_KEY_PREFIX}*", count=500))
        if keys:
            _client.delete(*keys)
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Manual hosts (operator-authored links). Unlike external subscriptions there
# is no poller, so this cache stores the resolved LINK STRINGS per admin
# directly (not ids pointing at poller-written payloads). Read on every
# subscription fetch by an eligible user; correctness comes from
# invalidate-on-write, the TTL is only a ceiling on a dropped invalidation.
# ---------------------------------------------------------------------------
_MANUAL_ADMIN_TTL = 300
_MANUAL_ADMIN_KEY_PREFIX = "manualhosts:admin:"


def _manual_admin_key(admin_id: int) -> str:
    return f"{_MANUAL_ADMIN_KEY_PREFIX}{admin_id}"


def get_cached_manual_links_for_admin(admin_id: int) -> Optional[List[str]]:
    """This admin's enabled manual-host links, or None on a miss.

    None and `[]` mean different things: `[]` is a cached "assigned nothing"
    and must NOT send the caller to the database.
    """
    if _client is None:
        return None
    try:
        raw = _client.get(_manual_admin_key(admin_id))
    except Exception:
        return None
    if raw is None:
        return None
    try:
        parsed = json.loads(raw)
    except Exception:
        return None
    if not isinstance(parsed, list):
        return None
    return [s for s in parsed if isinstance(s, str)]


def set_cached_manual_links_for_admin(admin_id: int, links: List[str]) -> None:
    if _client is None:
        return
    try:
        _client.set(_manual_admin_key(admin_id), json.dumps(links), ex=_MANUAL_ADMIN_TTL)
    except Exception:
        pass


def invalidate_manual_admin_links() -> None:
    """Drop every per-admin manual-host mapping. Called after any manual-host
    registry or assignment write. Wholesale like invalidate_external_admin_ids
    and for the same reasons."""
    if _client is None:
        return
    try:
        keys = list(_client.scan_iter(match=f"{_MANUAL_ADMIN_KEY_PREFIX}*", count=500))
        if keys:
            _client.delete(*keys)
    except Exception:
        pass


def _extsub_payload_key(sub_id: int, fmt: str) -> str:
    return f"extsub:payload:{sub_id}:{fmt}"


def get_external_payloads(sub_ids: List[int], fmt: str) -> List[str]:
    """Payloads for the given registry ids in the given format, in id order.

    Missing/empty entries are dropped, so the return value is only the
    payloads that are actually available right now. One MGET — this runs on
    the subscription hot path.
    """
    if _client is None or not sub_ids:
        return []
    keys = [_extsub_payload_key(i, fmt) for i in sub_ids]
    try:
        values = _client.mget(keys)
    except Exception:
        return []

    payloads = []
    missing = []
    for sub_id, value in zip(sub_ids, values):
        if value:
            payloads.append(value)
        else:
            missing.append(sub_id)
    if missing:
        _log_payload_miss(missing, fmt)
    return payloads


def _log_payload_miss(sub_ids: List[int], fmt: str) -> None:
    """Debug-level: this fires legitimately for a newly registered row and for
    any format the poller does not mirror, so it must not be noisy. It is here
    so that "eligible users silently lost their external hosts" is greppable."""
    logger.debug("external subscription payload miss for %s (format=%s)",
                 sub_ids, fmt)


def get_external_status(sub_ids: List[int]) -> Dict[int, dict]:
    """{sub_id -> last poll outcome dict} for the sudo UI. Missing = not yet
    reported. Never raises: an unreachable Redis just yields {}."""
    if _client is None or not sub_ids:
        return {}
    try:
        values = _client.mget([f"extsub:status:{i}" for i in sub_ids])
    except Exception:
        return {}

    out = {}
    for sub_id, raw in zip(sub_ids, values):
        if not raw:
            continue
        try:
            parsed = json.loads(raw)
        except Exception:
            continue
        if isinstance(parsed, dict):
            out[sub_id] = parsed
    return out


def get_external_poller_identity() -> dict:
    """The static Happ identity the poller reports it is using.

    Read from Redis rather than kept as a constant here so the UI shows the
    HWID actually in use — that string is what the operator pre-fills as a
    device slot on each remote panel's integration user.
    """
    if _client is None:
        return {}
    try:
        raw = _client.get("extsub:identity")
    except Exception:
        return {}
    if not raw:
        return {}
    try:
        parsed = json.loads(raw)
    except Exception:
        return {}
    return parsed if isinstance(parsed, dict) else {}


def drop_external_subscription_cache(sub_id: int) -> None:
    """Delete every key the poller owns for one registry row.

    Payload keys carry a 24h TTL — a bound on *silent* staleness, not a
    substitute for this: a deleted or retargeted registration must not serve a
    dead row's hosts for up to a day. Called from the delete endpoint.
    """
    if _client is None:
        return
    try:
        keys = [f"extsub:status:{sub_id}"]
        keys += [_extsub_payload_key(sub_id, f) for f in EXTERNAL_FORMATS]
        # The discovered dialect describes the OLD target, so it has to go too —
        # otherwise a retargeted row keeps probing the previous panel's segment
        # vocabulary first.
        keys += [f"extsub:dialect:{sub_id}:{f}" for f in EXTERNAL_FORMATS]
        _client.delete(*keys)
    except Exception:
        pass
