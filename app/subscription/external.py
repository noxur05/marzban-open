"""Appending other-panel ("external") hosts to a generated subscription.

Where the pieces live:

  * the registry (`external_subscriptions`) is sudo-authored config in MySQL;
  * the Rust poller fetches each remote subscription once per format, verifies
    every host's country from its IP, rewrites the remarks, and publishes
    render-ready payloads to Redis (24h staleness TTL, refreshed each
    successful cycle; deleted outright when the remote account reports itself
    expired or out of quota);
  * this module resolves "which payloads does *this* user get" and merges them
    into an already-generated local config. Each subscription is assigned to a
    set of admins (`external_subscription_admins`), so a user receives exactly
    the subscriptions their owning admin was granted — no assignment means no
    hosts, never "all hosts".

Why the remote panel is asked for each format separately (`/sub/<token>/v2ray`,
`…/clash-meta`, …) instead of one payload being converted here: the remote
panels are the same fork, so their own subscription route does the conversion.
That is what lets this feature support every output format **without ever
parsing a proxy URI** — the only per-format work left is merging two configs of
the same shape, which is what `merge_into` does.

Failure model: every step degrades to "serve the local config only". A missing
payload is indistinguishable from a not-yet-polled one, so it can never be
treated as an error — the poller rewrites unconditionally every cycle, which is
what makes Redis-only storage safe.
"""
import base64
import binascii
import logging
from typing import TYPE_CHECKING, List

if TYPE_CHECKING:
    from app.models.user import UserResponse

logger = logging.getLogger(__name__)

# NOTE: every app-level import in this module is deliberately function-local.
# `share.py` imports this module, and `app.models.user` imports `share.py`,
# which is itself reached from `app.db.crud` — so a module-level
# `from app.db import crud` (or `from app.utils import cache`, which imports
# crud) would close that existing chain into a real cycle whose resolution
# depends on which module the interpreter happened to reach first. Keeping the
# imports local costs one dict lookup per call and removes the question
# entirely.

# Formats whose merge is implemented. A format outside this set is served
# untouched — see merge_into's docstring for what each remaining one needs.
#
# SAFETY INVARIANT: the poller filters unroutable and domestic (.tm) hosts out
# of the `v2ray` payload only, because that filter is line-based and the other
# formats are JSON/YAML documents it does not parse. Those payloads are still
# mirrored into Redis, so anyone adding a format here MUST first add host
# filtering for it on the poller side (stats-collector/src/poller/mod.rs,
# `relabel`), or that format will carry hosts this panel is supposed to drop.
MERGEABLE_FORMATS = ("v2ray",)


def _subscription_ids_for(admin_id: int) -> List[int]:
    """Enabled subscription ids assigned to this admin, Redis first.

    The cache carries no TTL and is dropped on every write that can change the
    mapping, so a steady-state subscription fetch makes NO database query at
    all. A miss costs one indexed query, which is then cached.
    """
    from app.db import GetDB, crud
    from app.utils import cache

    cached = cache.get_cached_external_ids_for_admin(admin_id)
    if cached is not None:
        return cached
    try:
        with GetDB() as db:
            ids = crud.get_external_subscription_ids_for_admin(db, admin_id)
    except Exception:
        # A DB hiccup here must not take the subscription down with it.
        logger.exception("failed to read external subscription assignments")
        return []
    cache.set_cached_external_ids_for_admin(admin_id, ids)
    return ids


def payloads_for(user: "UserResponse", config_format: str) -> List[str]:
    """External payloads this user should receive, in registry order.

    Resolved from the user's *current* owner, so a sudo set-owner transfer takes
    effect on the user's next fetch with no extra bookkeeping. A user with no
    admin row is never eligible: there is nobody the foreign traffic belongs to.
    """
    from app.utils import cache

    if config_format not in MERGEABLE_FORMATS:
        return []
    admin_id = user.admin.id if user.admin else None
    if admin_id is None:
        return []
    ids = _subscription_ids_for(admin_id)
    if not ids:
        return []
    return cache.get_external_payloads(ids, config_format)


def merge_into(config: str, config_format: str, payloads: List[str]) -> str:
    """Merge external payloads into a locally generated config.

    Implemented:

      * ``v2ray`` — both sides are newline-separated proxy URIs, so this is a
        line append. The payload is stored DECODED for exactly this reason;
        `generate_subscription` base64-encodes the result afterwards when the
        client wants it encoded.

    Not implemented, and each needs more than concatenation:

      * ``v2ray-json`` — two JSON arrays of outbounds; append the elements.
      * ``clash`` / ``clash-meta`` — YAML: extend `proxies`, then add the new
        proxy *names* into every `proxy-groups` entry, or the client parses
        them and shows nothing.
      * ``sing-box`` — JSON: extend `outbounds`, then add the new tags to the
        selector/urltest outbounds, same reason as clash.
      * ``outline`` — a single-key config with no list to extend; and
        `outline_enabled` is mutually exclusive with `device_limit` anyway, so
        no device-limited user can reach it.

    A format not listed above returns `config` untouched, which is why
    `payloads_for` refuses to fetch for it in the first place.
    """
    if not payloads or config_format != "v2ray":
        return config

    lines = [line for line in config.split("\n") if line.strip()]
    for payload in payloads:
        text = decode_payload(payload)
        lines.extend(line for line in text.split("\n") if line.strip())
    return "\n".join(lines)


def decode_payload(raw: str) -> str:
    """Normalise a payload that may or may not be base64.

    The poller stores `v2ray` payloads decoded, but a payload can also arrive
    here from a hand-seeded Redis key or a future poller version, and a remote
    panel serves that format base64-encoded. Detect and decode rather than
    appending an opaque blob into a user's config, which would break the whole
    subscription rather than just the foreign part.
    """
    stripped = raw.strip()
    if not stripped:
        return ""
    if "://" in stripped:
        return stripped
    try:
        # validate=False: the remote may or may not pad, and may wrap lines.
        decoded = base64.b64decode(stripped + "=" * (-len(stripped) % 4)).decode()
    except (binascii.Error, UnicodeDecodeError, ValueError):
        return ""
    return decoded if "://" in decoded else ""
