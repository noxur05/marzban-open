"""Appending operator-authored ("manual") hosts to a generated subscription.

The manual, poller-free twin of `app/subscription/external.py`. A sudo operator
pastes a full share link (vless://, vmess://, trojan://, ss://) into the
`manual_hosts` registry and assigns it to a set of admins
(`manual_host_admins`). This module resolves which links *this* user should get
(from their current owning admin) and hands them to `external.merge_into`, which
appends them to an already-generated v2ray config exactly as it does for
external-subscription payloads — a plain URI-line append, no parsing.

Failure model, like external subs: every step degrades to "serve the local
config only". No assignment means no hosts, never "all hosts".

Only the v2ray family is supported (`external.MERGEABLE_FORMATS`); for any other
format `payloads_for` returns nothing.
"""
import logging
from typing import TYPE_CHECKING, List

if TYPE_CHECKING:
    from app.models.user import UserResponse

logger = logging.getLogger(__name__)

# NOTE: app-level imports are function-local for the same reason as in
# external.py — `share.py` imports this module, `app.models.user` imports
# `share.py`, and that chain is reachable from `app.db.crud`; a module-level
# `from app.db import crud` would close it into a real import cycle.


def _links_for_admin(admin_id: int) -> List[str]:
    """Enabled manual-host links assigned to this admin, Redis first.

    Cached per admin with invalidate-on-write, so a steady-state subscription
    fetch makes NO database query. A miss costs one indexed query, then cached.
    """
    from app.db import GetDB, crud
    from app.utils import cache

    cached = cache.get_cached_manual_links_for_admin(admin_id)
    if cached is not None:
        return cached
    try:
        with GetDB() as db:
            links = crud.get_manual_host_links_for_admin(db, admin_id)
    except Exception:
        # A DB hiccup here must not take the subscription down with it.
        logger.exception("failed to read manual-host assignments")
        return []
    cache.set_cached_manual_links_for_admin(admin_id, links)
    return links


def payloads_for(user: "UserResponse", config_format: str) -> List[str]:
    """Manual-host links this user should receive, in registry order.

    Resolved from the user's *current* owner, so a sudo set-owner transfer takes
    effect on the user's next fetch. A user with no admin row is never eligible.
    """
    from app.subscription.external import MERGEABLE_FORMATS

    if config_format not in MERGEABLE_FORMATS:
        return []
    admin_id = user.admin.id if user.admin else None
    if admin_id is None:
        return []
    return _links_for_admin(admin_id)
