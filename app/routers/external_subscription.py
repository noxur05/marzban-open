"""Sudo-only registry for subscriptions that live on OTHER panels.

A row here is the subscription URL of an ordinary user on panel #2/#3. The
out-of-process Rust poller (`stats-collector/src/poller/`) fetches each one,
verifies every host's country from its IP, relabels the remarks and publishes
render-ready payloads to Redis; `app/subscription/share.py` appends them for
users whose owning admin the subscription is assigned to
(`external_subscription_admins`).

Everything in this router is sudo-only. Traffic through a foreign host is
metered on the remote panel and is invisible to this one, so who receives
these hosts is a billing decision, not a delegated one.

An assignment change drops the per-admin Redis mapping, so it takes effect on
the very next subscription fetch rather than after a TTL.
"""
from typing import List

from fastapi import APIRouter, Depends, HTTPException

from app.db import Session, crud, get_db
from app.models.admin import Admin
from app.models.external_subscription import (
    ExternalPollerIdentity,
    ExternalSubscriptionCreate,
    ExternalSubscriptionModify,
    ExternalSubscriptionResponse,
    ExternalSubscriptionStatus,
)
from app.utils import cache, responses

router = APIRouter(tags=["ExternalSubscription"], prefix="/api",
                   responses={401: responses._401})


def _with_status(rows) -> List[ExternalSubscriptionResponse]:
    """Attach each row's last poll outcome, read from Redis.

    Poll state is deliberately not in MySQL (that is what keeps the poller
    read-only against the panel DB), so an unreachable Redis simply means every
    row reports an empty status rather than failing the request.
    """
    statuses = cache.get_external_status([r.id for r in rows])
    out = []
    for row in rows:
        resp = ExternalSubscriptionResponse.model_validate(row)
        raw = statuses.get(row.id)
        if raw:
            try:
                resp.status = ExternalSubscriptionStatus.model_validate(raw)
            except Exception:
                # A malformed status blob must never break the list view; the
                # payloads it describes are unaffected either way.
                pass
        out.append(resp)
    return out


@router.get("/external-subscriptions",
            response_model=List[ExternalSubscriptionResponse],
            responses={403: responses._403})
def list_external_subscriptions(db: Session = Depends(get_db),
                                admin: Admin = Depends(Admin.check_sudo_admin)):
    """Every registered external subscription, with its last poll outcome."""
    return _with_status(crud.list_external_subscriptions(db))


@router.get("/external-subscriptions/poller",
            response_model=ExternalPollerIdentity,
            responses={403: responses._403})
def get_poller_identity(admin: Admin = Depends(Admin.check_sudo_admin)):
    """The poller's fallback Happ identity, used for rows with no `hwid` set.

    A registry row's own `hwid` always takes precedence. Where this fallback is
    used, pre-fill it as a device slot on the remote panel's integration user:
    a prefilled slot makes the remote device-limit check return 'matched', so
    the poller never auto-claims a slot and can never be denied once that
    user's slots fill up. Empty fields mean the poller has not reported in yet
    (not running, or Redis unreachable).
    """
    return ExternalPollerIdentity.model_validate(
        cache.get_external_poller_identity())


@router.post("/external-subscriptions",
             response_model=ExternalSubscriptionResponse,
             responses={403: responses._403, 409: responses._409})
def add_external_subscription(new_sub: ExternalSubscriptionCreate,
                              db: Session = Depends(get_db),
                              admin: Admin = Depends(Admin.check_sudo_admin)):
    """Register an external subscription.

    `admin_ids` selects who receives these hosts; an empty list means nobody.

    Set `hwid` to the HWID already registered as a device slot on the remote
    user, so the poller matches an existing slot instead of claiming a new one
    (on this panel family a claimed slot is freed only by an admin block, never
    by time). Omit it to use the poller's fallback identity.

    Nothing is fetched here — the poller picks the row up on its next cycle, so
    `status` stays empty until then.
    """
    if crud.get_external_subscription_by_url(db, new_sub.url):
        raise HTTPException(status_code=409,
                            detail="This subscription URL is already registered")
    dbsub = crud.create_external_subscription(db, new_sub)
    cache.invalidate_external_admin_ids()
    crud.touch_all_micky_copies(db)  # after the cache drop: copies render from it
    return _with_status([dbsub])[0]


@router.put("/external-subscriptions/{sub_id}",
            response_model=ExternalSubscriptionResponse,
            responses={403: responses._403, 404: responses._404,
                       409: responses._409})
def modify_external_subscription(sub_id: int,
                                 modified: ExternalSubscriptionModify,
                                 db: Session = Depends(get_db),
                                 admin: Admin = Depends(Admin.check_sudo_admin)):
    """Rename, retarget, reassign or enable/disable a registration.

    `admin_ids` is replace-set: send the full list. `[]` revokes it from
    everyone; omitting the field leaves the assignments alone.

    Disabling stops the panel from appending its hosts on the very next
    subscription fetch (the render path only asks for enabled rows); the
    poller also stops refreshing it. The stale payload is left in Redis so
    re-enabling is instant.
    """
    dbsub = crud.get_external_subscription(db, sub_id)
    if not dbsub:
        raise HTTPException(status_code=404,
                            detail="External subscription not found")
    if modified.url is not None and modified.url != dbsub.url:
        clash = crud.get_external_subscription_by_url(db, modified.url)
        if clash is not None and clash.id != dbsub.id:
            raise HTTPException(
                status_code=409,
                detail="This subscription URL is already registered")
    retargeted = modified.url is not None and modified.url != dbsub.url
    dbsub = crud.update_external_subscription(db, dbsub, modified)
    if retargeted:
        # The poller only overwrites payloads on a SUCCESSFUL fetch,
        # deliberately keeping the last good data otherwise. So without this
        # the row would keep serving the PREVIOUS remote account's hosts for
        # up to the 24h payload TTL if the new URL never polls cleanly.
        # Fail closed, exactly like the delete path.
        cache.drop_external_subscription_cache(dbsub.id)
    cache.invalidate_external_admin_ids()
    crud.touch_all_micky_copies(db)  # after the cache drop: copies render from it
    return _with_status([dbsub])[0]


@router.delete("/external-subscriptions/{sub_id}",
               responses={403: responses._403, 404: responses._404})
def remove_external_subscription(sub_id: int,
                                 db: Session = Depends(get_db),
                                 admin: Admin = Depends(Admin.check_sudo_admin)):
    """Unregister and drop the poller's Redis keys for this row.

    Payload keys carry only a 24h staleness TTL, so they have to be deleted
    explicitly or they would outlive the registration by up to a day.
    """
    dbsub = crud.get_external_subscription(db, sub_id)
    if not dbsub:
        raise HTTPException(status_code=404,
                            detail="External subscription not found")
    crud.remove_external_subscription(db, dbsub)
    cache.drop_external_subscription_cache(sub_id)
    cache.invalidate_external_admin_ids()
    crud.touch_all_micky_copies(db)  # after the cache drop: copies render from it
    return {"detail": "External subscription removed"}
