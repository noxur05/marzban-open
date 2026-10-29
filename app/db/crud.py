"""
Functions for managing proxy hosts, users, user templates, nodes, and administrative tasks.
"""

import logging
import time
from datetime import date, datetime, timedelta
from enum import Enum
from typing import Dict, List, Optional, Tuple, Union

from sqlalchemy import and_, delete, func, or_, select, update
from sqlalchemy.orm import Query, Session, joinedload, raiseload, selectinload, with_expression
from sqlalchemy.sql.functions import coalesce

from app.db.models import (
    JWT,
    TLS,
    Admin,
    AdminHourlyUsage,
    AdminUsageAdjustment,
    AdminUsageLogs,
    Announcement,
    ExternalSubscription,
    ManualHost,
    MickyCopy,
    NextPlan,
    Node,
    NodeUsage,
    NodeUserUsage,
    NotificationReminder,
    Proxy,
    ProxyHost,
    ProxyInbound,
    ProxyTypes,
    System,
    User,
    UserSubscriptionDevice,
    UserTemplate,
    UserUsageResetLogs,
    excluded_inbounds_association,
    external_subscription_admins_association,
    manual_host_admins_association,
)
from app.models.admin import AdminCreate, AdminModify, AdminPartialModify
from app.models.external_subscription import (
    ExternalSubscriptionCreate,
    ExternalSubscriptionModify,
)
from app.models.manual_host import ManualHostCreate, ManualHostModify
from app.models.node import NodeCreate, NodeModify, NodeStatus, NodeUsageResponse
from app.models.proxy import ProxyHost as ProxyHostModify
from app.models.user import (
    ClientType,
    ReminderType,
    UserCreate,
    UserDataLimitResetStrategy,
    UserModify,
    UserResponse,
    UserStatus,
    UserUsageResponse,
    build_subscription_url,
)
from app.models.user_template import UserTemplateCreate, UserTemplateModify
from app.utils import micky, micky_copy
from app.utils.happ import (
    encrypt_subscription_url,
    encrypt_subscription_url_crypt5,
)
from app.utils.helpers import calculate_expiration_days, calculate_usage_percent
from config import NOTIFY_DAYS_LEFT, NOTIFY_REACHED_USAGE_PERCENT, USERS_AUTODELETE_DAYS

logger = logging.getLogger(__name__)


def _refresh_encrypted_subscription_url(dbuser: User) -> None:
    """Recompute the app-specific link on a User row: `happ://crypt5/…` for
    Happ, `micky://s/…` for Micky. Both are computed locally (no outbound
    call) and the stored value is never the plaintext URL.

    Happ: crypt5, falling back to crypt4 if crypt5 ever raises (Happ decrypts
    both). Micky: sealed to MICKY_LINK_PUBLIC_KEYS; a fresh ephemeral key each
    time, so the link changes on every refresh even for the same URL.

    No-op for legacy users. Callers must persist the changed
    attribute via db.commit() — this helper only mutates the in-memory
    SQLAlchemy object so it composes cleanly with other ORM writes in
    the same transaction.
    """
    if dbuser.client_type == ClientType.micky:
        if not micky.can_make_links():
            # No link key (removed or rotated ahead of its key): never fall
            # back to the plain URL. The API shows no link until keys return.
            logger.warning("no Micky link key; cleared the link of user %s", dbuser.username)
            dbuser.encrypted_subscription_url = None
            return
        dbuser.encrypted_subscription_url = micky.make_link(
            build_subscription_url(dbuser.username, dbuser.admin))
        return
    if dbuser.client_type != ClientType.happ:
        return
    plain = build_subscription_url(dbuser.username, dbuser.admin)
    try:
        dbuser.encrypted_subscription_url = encrypt_subscription_url_crypt5(plain)
    except Exception:
        logger.warning(
            "crypt5 encryption failed for user %s; storing a crypt4 link instead",
            dbuser.username, exc_info=True,
        )
        dbuser.encrypted_subscription_url = encrypt_subscription_url(plain)



# --- Google copies of Micky subscriptions ------------------------------------
# docs/micky-protocol.md "Google copies"; analysis/google-copies-plan.md.
# A Micky user's copy is marked dirty in the SAME transaction as any change to
# what it says (status, servers, links, the blocked-device list...); the
# marzban-google-sync service then re-renders it through
# /internal/micky-copy/<id> and uploads it. None of these commit: they ride
# the caller's transaction. A user without a row (anyone not on Micky) is a
# no-op everywhere.

def get_micky_copy(db: Session, user_id: int) -> Optional[MickyCopy]:
    return db.query(MickyCopy).filter(MickyCopy.user_id == user_id).first()


def ensure_micky_copy(db: Session, dbuser: User) -> None:
    """A Micky user gets a copy row (and its content key) the first time, and
    a dirty mark after that. Anyone else's row (left Micky) is only marked, so
    the service deletes its copies; no row = no-op."""
    if dbuser.id is None:
        return
    if dbuser.client_type != ClientType.micky:
        mark_copy_dirty(db, dbuser.id)  # left Micky: the panel now answers 404, its copies go
        return
    now = datetime.utcnow()
    row = get_micky_copy(db, dbuser.id)
    if row is None:
        db.add(MickyCopy(user_id=dbuser.id, content_key=micky_copy.new_key(),
                         key_changed_at=now, dirty_at=now, created_at=now))
    else:
        row.dirty_at = max(now, row.dirty_at + timedelta(microseconds=1)) if row.dirty_at else now


def mark_copy_dirty(db: Session, user_id: int) -> None:
    mark_copies_dirty(db, [user_id])


def mark_copies_dirty(db: Session, user_ids) -> None:
    ids = list(user_ids)
    if ids:
        db.query(MickyCopy).filter(MickyCopy.user_id.in_(ids)).update(
            {MickyCopy.dirty_at: datetime.utcnow()}, synchronize_session=False)


def mark_admin_copies_dirty(db: Session, admin_id: Optional[int]) -> None:
    """Every copy of one admin's users; admin_id None = every copy (global
    changes: hosts, the global announcement, an action across all users)."""
    q = db.query(MickyCopy)
    if admin_id is not None:
        q = q.filter(MickyCopy.user_id.in_(select(User.id).where(User.admin_id == admin_id)))
    q.update({MickyCopy.dirty_at: datetime.utcnow()}, synchronize_session=False)


def mark_all_copies_dirty(db: Session) -> None:
    mark_admin_copies_dirty(db, None)


def touch_all_micky_copies(db: Session) -> None:
    """For changes made outside the database (PUT /api/core/config)."""
    mark_all_copies_dirty(db)
    db.commit()


def stamp_copy_issued_at(db: Session, row: MickyCopy) -> int:
    """issued_at for the copy about to be rendered: max(now, last + 1), so a
    user's copies are strictly increasing even within one second or across a
    clock step back (docs/micky-protocol.md). Commits."""
    row = db.query(MickyCopy).filter(MickyCopy.id == row.id).with_for_update().populate_existing().one()
    issued_at = max(int(time.time()), (row.last_issued_at or 0) + 1)
    row.last_issued_at = issued_at
    db.commit()
    return issued_at


def rotate_copy_key(db: Session, user_id: int) -> None:
    """Revoke: a new content key, so a phone holding the old one can no longer
    read the copy. The Google ids stay; the sealed answer withholds the G
    headers until the copies are re-encrypted with this key.

    The key's timestamp is the gate (synced_at >= key_changed_at), so it must
    land after the last sync even if the clock stepped back: past synced_at
    and dirty_at, with the row locked while it is computed."""
    row = db.query(MickyCopy).filter(MickyCopy.user_id == user_id).with_for_update().first()
    if row is None:
        return
    tick = timedelta(microseconds=1)
    now = max(datetime.utcnow(), *(t + tick for t in (row.synced_at, row.dirty_at, row.key_changed_at) if t))
    row.content_key, row.key_changed_at, row.dirty_at = micky_copy.new_key(), now, now


def regenerate_happ_subscription(db: Session, dbuser: User) -> User:
    """Recompute one Happ user's encrypted subscription URL from its admin's
    CURRENT domain (admin.subscription_url, or the default if the admin has
    none — build_subscription_url decides). No-op for non-Happ users.

    Non-destructive: a fresh token is minted but `sub_revoked_at` is untouched,
    so the new link is valid and any old link stays valid too.

    Cheap and safe to call inline: crypt5 (and the crypt4 fallback) are
    computed LOCALLY in `app/utils/happ.py` — nothing leaves the panel. An
    earlier version of this line called it "one crypt5 API call", which is
    wrong and had already propagated into another docstring; there is no
    outbound request on this path.
    """
    _refresh_encrypted_subscription_url(dbuser)
    mark_copy_dirty(db, dbuser.id)
    db.commit()
    db.refresh(dbuser)
    return dbuser


def count_happ_users(db: Session, admin_username: Optional[str] = None) -> int:
    """How many Happ and Micky users a regeneration would touch (optionally
    one admin's). Both links embed the owner's domain."""
    q = db.query(func.count(User.id)).filter(User.client_type.in_((ClientType.happ, ClientType.micky)))
    if admin_username:
        q = q.filter(User.admin.has(Admin.username == admin_username))
    return int(q.scalar() or 0)


def app_user_ids(db: Session, admin_id: Optional[int]) -> List[int]:
    """Ids of the Happ and Micky users owned by `admin_id` (None: ownerless)."""
    return [r[0] for r in db.query(User.id).filter(
        User.admin_id == admin_id,
        User.client_type.in_((ClientType.happ, ClientType.micky))).all()]


def regenerate_happ_subscriptions(db: Session,
                                  admin_username: Optional[str] = None,
                                  user_ids: Optional[List[int]] = None) -> int:
    """Bulk-recompute the app links of every Happ and Micky user (or one
    admin's, or only `user_ids`) onto the owner's current domain. Returns the
    count regenerated.

    Links are computed locally, but a whole-panel run still touches every
    row — call this only from a background task, never inline.
    Commits per user so partial progress survives a restart and one crypt
    failure never rolls back the batch. Iterates by id (re-fetching each user)
    to avoid the expire-on-commit reload churn of holding all rows at once.
    """
    id_q = db.query(User.id).filter(User.client_type.in_((ClientType.happ, ClientType.micky)))
    if admin_username:
        id_q = id_q.filter(User.admin.has(Admin.username == admin_username))
    if user_ids is not None:
        id_q = id_q.filter(User.id.in_(user_ids))
    ids = [r[0] for r in id_q.all()]

    n = 0
    for uid in ids:
        dbuser = db.query(User).filter(User.id == uid).first()
        if dbuser is None:
            continue
        try:
            _refresh_encrypted_subscription_url(dbuser)
            mark_copy_dirty(db, uid)
            db.commit()
            n += 1
        except Exception:
            db.rollback()
            logger.exception("failed to regenerate subscription for user id %s", uid)
        if n % 100 == 0 and n:
            logger.info("regenerated %d happ subscriptions so far (admin=%s)",
                        n, admin_username or "ALL")
    return n


def add_default_host(db: Session, inbound: ProxyInbound):
    """
    Adds a default host to a proxy inbound.

    Args:
        db (Session): Database session.
        inbound (ProxyInbound): Proxy inbound to add the default host to.
    """
    host = ProxyHost(remark="🚀 Marz ({USERNAME}) [{PROTOCOL} - {TRANSPORT}]", address="{SERVER_IP}", inbound=inbound)
    db.add(host)
    db.commit()


def get_or_create_inbound(db: Session, inbound_tag: str) -> ProxyInbound:
    """
    Retrieves or creates a proxy inbound based on the given tag.

    Args:
        db (Session): Database session.
        inbound_tag (str): The tag of the inbound.

    Returns:
        ProxyInbound: The retrieved or newly created proxy inbound.
    """
    inbound = db.query(ProxyInbound).filter(ProxyInbound.tag == inbound_tag).first()
    if not inbound:
        inbound = ProxyInbound(tag=inbound_tag)
        db.add(inbound)
        db.commit()
        add_default_host(db, inbound)
        db.refresh(inbound)
    return inbound


def get_hosts(db: Session, inbound_tag: str) -> List[ProxyHost]:
    """
    Retrieves hosts for a given inbound tag.

    Args:
        db (Session): Database session.
        inbound_tag (str): The tag of the inbound.

    Returns:
        List[ProxyHost]: List of hosts for the inbound.
    """
    inbound = get_or_create_inbound(db, inbound_tag)
    # Eager-load the per-host admin owners to avoid N+1 when callers iterate
    # host.admins (cache builder in app/xray/__init__.py, GET /api/hosts
    # for the dashboard). selectinload issues one IN-batched query for the
    # whole set of hosts.
    return (
        db.query(ProxyHost)
        .options(selectinload(ProxyHost.admins))
        .filter(ProxyHost.inbound_tag == inbound.tag)
        .all()
    )


def _resolve_admin_owners(db: Session, modified_hosts) -> dict:
    """Batched lookup of Admin rows referenced by host.admin_ids.

    Returns ``{admin_id: Admin}`` over the union of every modified host's
    admin_ids list. Raises ValueError if any referenced id doesn't exist —
    callers (the host routers) catch this and turn it into a 400.
    """
    needed = {
        aid
        for h in modified_hosts
        for aid in (getattr(h, "admin_ids", None) or [])
    }
    if not needed:
        return {}
    admins_by_id = {
        a.id: a
        for a in db.query(Admin).filter(Admin.id.in_(needed)).all()
    }
    missing = needed - admins_by_id.keys()
    if missing:
        raise ValueError(
            f"unknown admin_id(s) in host.admin_ids: {sorted(missing)}"
        )
    return admins_by_id


def add_host(db: Session, inbound_tag: str, host: ProxyHostModify) -> List[ProxyHost]:
    """
    Adds a new host to a proxy inbound.

    Args:
        db (Session): Database session.
        inbound_tag (str): The tag of the inbound.
        host (ProxyHostModify): Host details to be added.

    Returns:
        List[ProxyHost]: Updated list of hosts for the inbound.
    """
    inbound = get_or_create_inbound(db, inbound_tag)
    admins_by_id = _resolve_admin_owners(db, [host])
    inbound.hosts.append(
        ProxyHost(
            remark=host.remark,
            address=host.address,
            port=host.port,
            path=host.path,
            sni=host.sni,
            host=host.host,
            inbound=inbound,
            security=host.security,
            alpn=host.alpn,
            fingerprint=host.fingerprint,
            admins=[admins_by_id[aid] for aid in (host.admin_ids or [])],
        )
    )
    mark_all_copies_dirty(db)
    db.commit()
    db.refresh(inbound)
    return inbound.hosts


def update_hosts(db: Session, inbound_tag: str, modified_hosts: List[ProxyHostModify]) -> List[ProxyHost]:
    """
    Updates hosts for a given inbound tag.

    Args:
        db (Session): Database session.
        inbound_tag (str): The tag of the inbound.
        modified_hosts (List[ProxyHostModify]): List of modified hosts.

    Returns:
        List[ProxyHost]: Updated list of hosts for the inbound.
    """
    inbound = get_or_create_inbound(db, inbound_tag)
    admins_by_id = _resolve_admin_owners(db, modified_hosts)
    inbound.hosts = [
        ProxyHost(
            remark=host.remark,
            address=host.address,
            port=host.port,
            path=host.path,
            sni=host.sni,
            host=host.host,
            inbound=inbound,
            security=host.security,
            alpn=host.alpn,
            fingerprint=host.fingerprint,
            allowinsecure=host.allowinsecure,
            is_disabled=host.is_disabled,
            mux_enable=host.mux_enable,
            fragment_setting=host.fragment_setting,
            noise_setting=host.noise_setting,
            random_user_agent=host.random_user_agent,
            use_sni_as_host=host.use_sni_as_host,
            admins=[admins_by_id[aid] for aid in (host.admin_ids or [])],
        ) for host in modified_hosts
    ]
    mark_all_copies_dirty(db)
    db.commit()
    db.refresh(inbound)
    return inbound.hosts


def get_user_queryset(db: Session) -> Query:
    """
    Base user query with eager-loaded relationships.

    Loading strategy:
      - admin, next_plan: joinedload (one-to-one) — single JOIN
      - proxies + excluded_inbounds: selectinload (one-to-many) — bulk IN query
      - usage_logs: selectinload (one-to-many) — bulk IN query
    Together these eliminate N+1 fetches when serializing UserResponse.
    """
    return (
        db.query(User)
        .options(joinedload(User.admin))
        .options(joinedload(User.next_plan))
        .options(selectinload(User.proxies).selectinload(Proxy.excluded_inbounds))
        .options(selectinload(User.usage_logs))
        .options(with_expression(
            User.device_count,
            _active_device_count_subquery(),
        ))
    )


def get_user_for_sub(db: Session, username: str) -> Optional[User]:
    """The user as a subscription fetch needs it: the row, its admin (host
    scoping) and its proxies with their excluded inbounds.

    Not get_user_queryset: that also loads the user's whole usage-reset
    history (`usage_logs`, for lifetime usage), the next plan and a device
    count. Only the HTML info page shows those, and it loads the full user
    itself. They are `raiseload` here, so code on the subscription path that
    starts reading them fails in tests instead of silently costing a query
    per fetch (or reading a wrong value).
    """
    return (
        db.query(User)
        .options(joinedload(User.admin))
        .options(selectinload(User.proxies).selectinload(Proxy.excluded_inbounds))
        .options(raiseload(User.usage_logs), raiseload(User.next_plan))
        .filter(User.username == username)
        .first()
    )


def get_user_for_sub_by_id(db: Session, user_id: int) -> Optional[User]:
    """get_user_for_sub by id (the Google-copy endpoint knows only the id)."""
    return (
        db.query(User)
        .options(joinedload(User.admin))
        .options(selectinload(User.proxies).selectinload(Proxy.excluded_inbounds))
        .options(raiseload(User.usage_logs), raiseload(User.next_plan))
        .filter(User.id == user_id)
        .first()
    )


def get_blocked_copy_identities(db: Session, user_id: int) -> List[str]:
    """The `blocked` list of a Google copy (docs/micky-protocol.md): for every
    blocked device row of the user, its key if it is a Micky identity (id: /
    pk:) plus its bound key identity (pk:…, stored as is) when it has one.
    Never hwid:/ua: values: raw hardware ids don't belong in Google storage."""
    found = set()
    for key, bound in (db.query(UserSubscriptionDevice.device_key, UserSubscriptionDevice.bound_pub_key)
                       .filter(UserSubscriptionDevice.user_id == user_id,
                               UserSubscriptionDevice.is_blocked.is_(True))):
        for identity in (key, bound):
            if identity and identity.startswith(("id:", "pk:")):
                found.add(identity)
    return sorted(found)


def is_bound_key_blocked(db: Session, user_id: int, pub_key: str) -> bool:
    """Whether a blocked row of the user has this device key bound (an id:
    slot). A phone whose M-Device-Id later goes missing must stay blocked."""
    return db.query(UserSubscriptionDevice.id).filter(
        UserSubscriptionDevice.user_id == user_id,
        UserSubscriptionDevice.bound_pub_key == pub_key,
        UserSubscriptionDevice.is_blocked.is_(True),
    ).first() is not None


def get_effective_announcement_text(db: Session, admin_id: Optional[int]) -> Optional[str]:
    """cache.get_effective_announcement without the cache: a Google copy is
    rendered right after a change, before the cache is invalidated."""
    for owner in ((admin_id, None) if admin_id is not None else (None,)):
        latest = get_latest_announcement(db, owner)
        if latest is not None and latest.enabled:
            return latest.text
    return None


def get_user(db: Session, username: str) -> Optional[User]:
    """
    Retrieves a user by username.

    Args:
        db (Session): Database session.
        username (str): The username of the user.

    Returns:
        Optional[User]: The user object if found, else None.
    """
    return get_user_queryset(db).filter(User.username == username).first()


def get_user_by_id(db: Session, user_id: int) -> Optional[User]:
    """
    Retrieves a user by user ID.

    Args:
        db (Session): Database session.
        user_id (int): The ID of the user.

    Returns:
        Optional[User]: The user object if found, else None.
    """
    return get_user_queryset(db).filter(User.id == user_id).first()


UsersSortingOptions = Enum('UsersSortingOptions', {
    'username': User.username.asc(),
    'used_traffic': User.used_traffic.asc(),
    'data_limit': User.data_limit.asc(),
    'expire': User.expire.asc(),
    'created_at': User.created_at.asc(),
    '-username': User.username.desc(),
    '-used_traffic': User.used_traffic.desc(),
    '-data_limit': User.data_limit.desc(),
    '-expire': User.expire.desc(),
    '-created_at': User.created_at.desc(),
})


def get_users(db: Session,
              offset: Optional[int] = None,
              limit: Optional[int] = None,
              usernames: Optional[List[str]] = None,
              search: Optional[str] = None,
              status: Optional[Union[UserStatus, list]] = None,
              sort: Optional[List[UsersSortingOptions]] = None,
              admin: Optional[Admin] = None,
              admins: Optional[List[str]] = None,
              reset_strategy: Optional[Union[UserDataLimitResetStrategy, list]] = None,
              return_with_count: bool = False) -> Union[List[User], Tuple[List[User], int]]:
    """
    Retrieves users based on various filters and options.

    Args:
        db (Session): Database session.
        offset (Optional[int]): Number of records to skip.
        limit (Optional[int]): Number of records to retrieve.
        usernames (Optional[List[str]]): List of usernames to filter by.
        search (Optional[str]): Search term to filter by username or note.
        status (Optional[Union[UserStatus, list]]): User status or list of statuses to filter by.
        sort (Optional[List[UsersSortingOptions]]): Sorting options.
        admin (Optional[Admin]): Admin to filter users by.
        admins (Optional[List[str]]): List of admin usernames to filter users by.
        reset_strategy (Optional[Union[UserDataLimitResetStrategy, list]]): Data limit reset strategy to filter by.
        return_with_count (bool): Whether to return the total count of users.

    Returns:
        Union[List[User], Tuple[List[User], int]]: List of users or tuple of users and total count.
    """
    query = get_user_queryset(db)

    if search:
        query = query.filter(or_(User.username.ilike(f"%{search}%"), User.note.ilike(f"%{search}%")))

    if usernames:
        query = query.filter(User.username.in_(usernames))

    if status:
        if isinstance(status, list):
            query = query.filter(User.status.in_(status))
        else:
            query = query.filter(User.status == status)

    if reset_strategy:
        if isinstance(reset_strategy, list):
            query = query.filter(User.data_limit_reset_strategy.in_(reset_strategy))
        else:
            query = query.filter(User.data_limit_reset_strategy == reset_strategy)

    if admin:
        query = query.filter(User.admin == admin)

    if admins:
        query = query.filter(User.admin.has(Admin.username.in_(admins)))

    if return_with_count:
        count = query.count()

    if sort:
        query = query.order_by(*(opt.value for opt in sort))

    if offset:
        query = query.offset(offset)
    if limit:
        query = query.limit(limit)

    if return_with_count:
        return query.all(), count

    return query.all()


def get_user_usages(db: Session, dbuser: User, start: datetime, end: datetime) -> List[UserUsageResponse]:
    """
    Retrieves user usages within a specified date range.

    Args:
        db (Session): Database session.
        dbuser (User): The user object.
        start (datetime): Start date for usage retrieval.
        end (datetime): End date for usage retrieval.

    Returns:
        List[UserUsageResponse]: List of user usage responses.
    """

    usages = {0: UserUsageResponse(  # Main Core
        node_id=None,
        node_name="Master",
        used_traffic=0
    )}

    for node in db.query(Node).all():
        usages[node.id] = UserUsageResponse(
            node_id=node.id,
            node_name=node.name,
            used_traffic=0
        )

    cond = and_(NodeUserUsage.user_id == dbuser.id,
                NodeUserUsage.created_at >= start,
                NodeUserUsage.created_at <= end)

    for v in db.query(NodeUserUsage).filter(cond):
        try:
            usages[v.node_id or 0].used_traffic += v.used_traffic
        except KeyError:
            pass

    return list(usages.values())


def get_users_count(db: Session, status: UserStatus = None, admin: Admin = None) -> int:
    """
    Retrieves the count of users based on status and admin filters.

    Args:
        db (Session): Database session.
        status (UserStatus, optional): Status to filter users by.
        admin (Admin, optional): Admin to filter users by.

    Returns:
        int: Count of users matching the criteria.
    """
    query = db.query(User.id)
    if admin:
        query = query.filter(User.admin == admin)
    if status:
        query = query.filter(User.status == status)
    return query.count()


def create_user(db: Session, user: UserCreate, admin: Admin = None) -> User:
    """
    Creates a new user with provided details.

    Args:
        db (Session): Database session.
        user (UserCreate): User creation details.
        admin (Admin, optional): Admin associated with the user.

    Returns:
        User: The created user object.
    """
    excluded_inbounds_tags = user.excluded_inbounds
    proxies = []
    for proxy_type, settings in user.proxies.items():
        excluded_inbounds = [
            get_or_create_inbound(db, tag) for tag in excluded_inbounds_tags[proxy_type]
        ]
        proxies.append(
            Proxy(type=proxy_type.value,
                  settings=settings.dict(no_obj=True),
                  excluded_inbounds=excluded_inbounds)
        )

    # Admin-prefilled HWID slots. Stored in user_subscription_devices with
    # NULL telemetry until the device actually connects. validate_device_policy
    # on UserCreate enforces len(allowed_hwids) <= device_limit and the
    # pair-or-neither rule, so we just persist what we got.
    prefilled_devices = [
        UserSubscriptionDevice(
            device_key=DEVICE_KEY_HWID_PREFIX + hwid,
            first_seen_at=None,
            last_seen_at=None,
            fetch_count=0,
        )
        for hwid in (user.allowed_hwids or [])
    ]

    dbuser = User(
        username=user.username,
        proxies=proxies,
        status=user.status,
        data_limit=(user.data_limit or None),
        expire=(user.expire or None),
        admin=admin,
        data_limit_reset_strategy=user.data_limit_reset_strategy,
        note=user.note,
        on_hold_expire_duration=(user.on_hold_expire_duration or None),
        on_hold_timeout=(user.on_hold_timeout or None),
        auto_delete_in_days=user.auto_delete_in_days,
        client_type=user.client_type,
        device_limit=user.device_limit,
        outline_enabled=bool(user.outline_enabled),
        devices=prefilled_devices,
        next_plan=NextPlan(
            data_limit=user.next_plan.data_limit,
            expire=user.next_plan.expire,
            add_remaining_traffic=user.next_plan.add_remaining_traffic,
            fire_on_either=user.next_plan.fire_on_either,
        ) if user.next_plan else None
    )
    db.add(dbuser)
    db.commit()
    db.refresh(dbuser)

    # Pre-compute the app link (Happ crypt5 / Micky micky://s/) now that the
    # user and its admin relationship are fully persisted. Legacy users are a
    # no-op. A second commit avoids re-running the full create path on this
    # single column.
    if dbuser.client_type in (ClientType.happ, ClientType.micky):
        _refresh_encrypted_subscription_url(dbuser)
        ensure_micky_copy(db, dbuser)
        db.commit()
        db.refresh(dbuser)
    return dbuser


def remove_user(db: Session, dbuser: User) -> User:
    """
    Removes a user from the database.

    Args:
        db (Session): Database session.
        dbuser (User): The user object to be removed.

    Returns:
        User: The removed user object.
    """
    db.delete(dbuser)
    mark_copy_dirty(db, dbuser.id)  # the row outlives the user: its copies get deleted
    db.commit()
    return dbuser


def remove_users(db: Session, dbusers: List[User]):
    """
    Removes multiple users from the database.

    Args:
        db (Session): Database session.
        dbusers (List[User]): List of user objects to be removed.
    """
    for dbuser in dbusers:
        db.delete(dbuser)
    mark_copies_dirty(db, [u.id for u in dbusers])
    db.commit()
    return


def update_user(db: Session, dbuser: User, modify: UserModify) -> User:
    """
    Updates a user with new details.

    Args:
        db (Session): Database session.
        dbuser (User): The user object to be updated.
        modify (UserModify): New details for the user.

    Returns:
        User: The updated user object.
    """
    added_proxies: Dict[ProxyTypes, Proxy] = {}
    if modify.proxies:
        for proxy_type, settings in modify.proxies.items():
            dbproxy = db.query(Proxy) \
                .where(Proxy.user == dbuser, Proxy.type == proxy_type) \
                .first()
            if dbproxy:
                dbproxy.settings = settings.dict(no_obj=True)
            else:
                new_proxy = Proxy(type=proxy_type, settings=settings.dict(no_obj=True))
                dbuser.proxies.append(new_proxy)
                added_proxies.update({proxy_type: new_proxy})
        for proxy in dbuser.proxies:
            if proxy.type not in modify.proxies:
                db.delete(proxy)
    if modify.inbounds:
        for proxy_type, tags in modify.excluded_inbounds.items():
            dbproxy = db.query(Proxy) \
                .where(Proxy.user == dbuser, Proxy.type == proxy_type) \
                .first() or added_proxies.get(proxy_type)
            if dbproxy:
                dbproxy.excluded_inbounds = [get_or_create_inbound(db, tag) for tag in tags]

    if modify.status is not None:
        dbuser.status = modify.status

    if modify.data_limit is not None:
        dbuser.data_limit = (modify.data_limit or None)
        if dbuser.status not in (UserStatus.expired, UserStatus.disabled):
            if not dbuser.data_limit or dbuser.used_traffic < dbuser.data_limit:
                if dbuser.status != UserStatus.on_hold:
                    dbuser.status = UserStatus.active

                for percent in sorted(NOTIFY_REACHED_USAGE_PERCENT, reverse=True):
                    if not dbuser.data_limit or (calculate_usage_percent(
                            dbuser.used_traffic, dbuser.data_limit) < percent):
                        reminder = get_notification_reminder(db, dbuser.id, ReminderType.data_usage, threshold=percent)
                        if reminder:
                            delete_notification_reminder(db, reminder)

            else:
                dbuser.status = UserStatus.limited

    if modify.expire is not None:
        dbuser.expire = (modify.expire or None)
        if dbuser.status in (UserStatus.active, UserStatus.expired):
            if not dbuser.expire or dbuser.expire > datetime.utcnow().timestamp():
                dbuser.status = UserStatus.active
                for days_left in sorted(NOTIFY_DAYS_LEFT):
                    if not dbuser.expire or (calculate_expiration_days(
                            dbuser.expire) > days_left):
                        reminder = get_notification_reminder(
                            db, dbuser.id, ReminderType.expiration_date, threshold=days_left)
                        if reminder:
                            delete_notification_reminder(db, reminder)
            else:
                dbuser.status = UserStatus.expired

    if modify.note is not None:
        dbuser.note = modify.note or None

    # Outline gate is a one-way grant: it can be turned on but never off.
    # Only a False->True transition is honored; True->False (and None) are
    # ignored so an edit can't silently revoke access. Router enforces the
    # can_enable_outline permission before we get here.
    if modify.outline_enabled and not dbuser.outline_enabled:
        dbuser.outline_enabled = True

    if modify.data_limit_reset_strategy is not None:
        dbuser.data_limit_reset_strategy = modify.data_limit_reset_strategy.value

    if modify.on_hold_timeout is not None:
        dbuser.on_hold_timeout = modify.on_hold_timeout

    if modify.on_hold_expire_duration is not None:
        dbuser.on_hold_expire_duration = modify.on_hold_expire_duration

    if modify.next_plan is not None:
        dbuser.next_plan = NextPlan(
            data_limit=modify.next_plan.data_limit,
            expire=modify.next_plan.expire,
            add_remaining_traffic=modify.next_plan.add_remaining_traffic,
            fire_on_either=modify.next_plan.fire_on_either,
        )
    elif dbuser.next_plan is not None:
        db.delete(dbuser.next_plan)

    # Device-limit policy. UserModify.validate_device_policy_modify guarantees
    # the two arrive together, so testing one is enough.
    if modify.client_type is not None:
        if dbuser.outline_enabled:
            # Mirrors UserCreate.validate_outline: Outline is an uncountable
            # device and cannot coexist with per-HWID metering.
            raise ValueError(
                "an Outline-enabled user cannot take a device limit"
            )
        adopting = dbuser.client_type != modify.client_type
        dbuser.client_type = modify.client_type
        dbuser.device_limit = modify.device_limit
        # The stored app link changes only when the user takes an app (or has
        # none yet). The dashboard resends client_type on EVERY edit: a new link
        # each time minted another never-expiring token, and moved Micky apps
        # ("address updated", new-url) after a mere renewal. Links otherwise
        # change only on a domain change, a revoke, or an explicit regenerate.
        if adopting or not dbuser.encrypted_subscription_url:
            _refresh_encrypted_subscription_url(dbuser)

    # Any edit can change the copy; a legacy user taking Micky gets one.
    ensure_micky_copy(db, dbuser)
    dbuser.edit_at = datetime.utcnow()

    db.commit()
    db.refresh(dbuser)
    return dbuser


def reset_user_devices(db: Session, dbuser: User, include_blocked: bool = False) -> int:
    """Delete a user's bound devices, freeing their slots. Returns the count.

    Blocked rows are PRESERVED by default. A block is a one-way admin action
    with no unblock endpoint by design (see migration a8c4e7b21f53); deleting
    the row would let that device re-register and silently undo the block, so
    this would become the backdoor unblock the design rules out. `include_blocked`
    exists for the deliberate "wipe everything" case and should stay off for
    routine resets and for the Happ migration.
    """
    q = db.query(UserSubscriptionDevice).filter(
        UserSubscriptionDevice.user_id == dbuser.id
    )
    if not include_blocked:
        q = q.filter(UserSubscriptionDevice.is_blocked.is_(False))

    removed = q.delete(synchronize_session=False)
    mark_copy_dirty(db, dbuser.id)
    db.commit()
    db.refresh(dbuser)
    return removed


def reset_user_data_usage(db: Session, dbuser: User) -> User:
    """
    Resets the data usage of a user and logs the reset.

    Args:
        db (Session): Database session.
        dbuser (User): The user object whose data usage is to be reset.

    Returns:
        User: The updated user object.
    """
    usage_log = UserUsageResetLogs(
        user=dbuser,
        used_traffic_at_reset=dbuser.used_traffic,
    )
    db.add(usage_log)

    dbuser.used_traffic = 0
    dbuser.node_usages.clear()
    # Fresh traffic lifts only the traffic limit: a limited user comes back,
    # disabled / expired / on_hold stay as they are (same rule as
    # reset_all_users_data_usage). The old `(expired or disabled)` evaluated
    # to `expired` alone, so a reset re-enabled disabled and on_hold users.
    if dbuser.status not in (UserStatus.on_hold, UserStatus.expired, UserStatus.disabled):
        dbuser.status = UserStatus.active.value

    if dbuser.next_plan:
        db.delete(dbuser.next_plan)
        dbuser.next_plan = None
    db.add(dbuser)

    mark_copy_dirty(db, dbuser.id)
    db.commit()
    db.refresh(dbuser)
    return dbuser


def reset_user_by_next(db: Session, dbuser: User) -> User:
    """
    Resets the data usage of a user based on next user.

    Args:
        db (Session): Database session.
        dbuser (User): The user object whose data usage is to be reset.

    Returns:
        User: The updated user object.
    """

    if (dbuser.next_plan is None):
        return

    usage_log = UserUsageResetLogs(
        user=dbuser,
        used_traffic_at_reset=dbuser.used_traffic,
    )
    db.add(usage_log)

    dbuser.node_usages.clear()
    dbuser.status = UserStatus.active.value

    dbuser.data_limit = dbuser.next_plan.data_limit + \
        (0 if dbuser.next_plan.add_remaining_traffic else dbuser.data_limit - dbuser.used_traffic)
    dbuser.expire = dbuser.next_plan.expire

    dbuser.used_traffic = 0
    db.delete(dbuser.next_plan)
    dbuser.next_plan = None
    db.add(dbuser)

    mark_copy_dirty(db, dbuser.id)
    db.commit()
    db.refresh(dbuser)
    return dbuser


def revoke_user_sub(db: Session, dbuser: User) -> User:
    """
    Revokes the subscription of a user and updates proxies settings.

    Args:
        db (Session): Database session.
        dbuser (User): The user object whose subscription is to be revoked.

    Returns:
        User: The updated user object.
    """
    dbuser.sub_revoked_at = datetime.utcnow()

    # Re-encrypt before building the UserResponse so the swap inside
    # validate_subscription_url already picks up the fresh wrapper, and
    # before the update_user round-trip persists everything else.
    _refresh_encrypted_subscription_url(dbuser)

    rotate_copy_key(db, dbuser.id)

    user = UserResponse.model_validate(dbuser)
    for proxy_type, settings in user.proxies.copy().items():
        settings.revoke()
        user.proxies[proxy_type] = settings
    dbuser = update_user(db, dbuser, user)

    db.commit()
    db.refresh(dbuser)
    return dbuser


SUB_TOUCH_INTERVAL = timedelta(seconds=60)


def update_user_sub(db: Session, dbuser: User, user_agent: str) -> User:
    """
    Records the subscription fetch time and user agent (at most once a
    minute per user agent; see below).

    Args:
        db (Session): Database session.
        dbuser (User): The user object whose subscription is to be updated.
        user_agent (str): The user agent string to update.

    Returns:
        User: the same object; after a write its attributes are expired.
    """
    # Throttled: an UPDATE + COMMIT on every subscription fetch is most of a
    # fetch's database cost, and the readers (the info page, the Telegram user
    # card, the API) show this as "last updated", where a minute doesn't
    # matter. A new user agent is always written.
    now = datetime.utcnow()
    # A stored time in the future (clock jump, imported data) doesn't count as
    # recent, or the field would freeze until real time caught up.
    if (dbuser.sub_last_user_agent == user_agent and dbuser.sub_updated_at
            and timedelta(0) <= now - dbuser.sub_updated_at < SUB_TOUCH_INTERVAL):
        return dbuser
    dbuser.sub_updated_at = now
    dbuser.sub_last_user_agent = user_agent

    # No refresh: nothing here comes from the database, and reloading the user
    # (row + eager loads, 4 queries) on every subscription fetch is the cost
    # the caller avoids by reading what it needs before calling this.
    db.commit()
    return dbuser


def reset_all_users_data_usage(db: Session, admin: Optional[Admin] = None):
    """
    Resets the data usage for all users or users under a specific admin.

    Args:
        db (Session): Database session.
        admin (Optional[Admin]): Admin to filter users by, if any.
    """
    query = get_user_queryset(db)

    if admin:
        query = query.filter(User.admin == admin)

    for dbuser in query.all():
        dbuser.used_traffic = 0
        if dbuser.status not in [UserStatus.on_hold, UserStatus.expired, UserStatus.disabled]:
            dbuser.status = UserStatus.active
        dbuser.usage_logs.clear()
        dbuser.node_usages.clear()
        if dbuser.next_plan:
            db.delete(dbuser.next_plan)
            dbuser.next_plan = None
        db.add(dbuser)

    mark_admin_copies_dirty(db, admin.id if admin else None)
    db.commit()


def disable_all_active_users(db: Session, admin: Optional[Admin] = None):
    """
    Disable all active users or users under a specific admin.

    Args:
        db (Session): Database session.
        admin (Optional[Admin]): Admin to filter users by, if any.
    """
    query = db.query(User).filter(User.status.in_((UserStatus.active, UserStatus.on_hold)))
    if admin:
        query = query.filter(User.admin == admin)

    query.update({User.status: UserStatus.disabled, User.last_status_change: datetime.utcnow()}, synchronize_session=False)

    mark_admin_copies_dirty(db, admin.id if admin else None)
    db.commit()


def activate_all_disabled_users(db: Session, admin: Optional[Admin] = None):
    """
    Activate all disabled users or users under a specific admin.

    Args:
        db (Session): Database session.
        admin (Optional[Admin]): Admin to filter users by, if any.
    """
    query_for_active_users = db.query(User).filter(User.status == UserStatus.disabled)
    query_for_on_hold_users = db.query(User).filter(
        and_(
            User.status == UserStatus.disabled, User.expire.is_(
                None), User.on_hold_expire_duration.isnot(None), User.online_at.is_(None)
        ))
    if admin:
        query_for_active_users = query_for_active_users.filter(User.admin == admin)
        query_for_on_hold_users = query_for_on_hold_users.filter(User.admin == admin)

    query_for_on_hold_users.update(
        {User.status: UserStatus.on_hold, User.last_status_change: datetime.utcnow()}, synchronize_session=False)
    query_for_active_users.update(
        {User.status: UserStatus.active, User.last_status_change: datetime.utcnow()}, synchronize_session=False)

    mark_admin_copies_dirty(db, admin.id if admin else None)
    db.commit()


#: Users purged per transaction by `remove_admin_users`. Small on purpose —
#: see that function's "Why batched" note. 200 users is roughly 350k
#: `node_user_usages` rows at the worst realistic age, which is a scan short
#: enough to stay well inside MySQL's 50s `innodb_lock_wait_timeout`.
ADMIN_PURGE_BATCH = 200


def remove_admin_users(db: Session, dbadmin: Admin) -> List[int]:
    """Delete every user OWNED by this admin, plus their dependent rows.

    Scope is `users.admin_id` only — the same ownership axis every other
    admin-scoped query uses (`get_users(admin=...)`,
    `disable_all_active_users`).

    Children are removed with bulk DELETEs in FK-safe order rather than by
    iterating `db.delete(user)`, which would load every child collection into
    Python to cascade it. Only `user_subscription_devices` has ON DELETE
    CASCADE in the schema; every other child FK was created without one, so the
    order below is load-bearing.

    Why batched, and why the ids are materialised first:

      * `node_user_usages` holds one row per user per node per hour and has NO
        index with `user_id` leading — its only non-PK index is
        `UniqueConstraint('created_at', 'user_id', 'node_id')`, and
        `ix_node_user_usages_id` was dropped in migration 852d951c9c08. So
        every DELETE here is a full table scan, and under InnoDB's REPEATABLE
        READ that scan locks the rows it walks. One unbatched statement over a
        large admin would hold those locks for minutes while
        `jobs/record_usages.py` tries to UPDATE the same table every 10s;
        that job's `safe_execute` retries only deadlocks (1213), not lock-wait
        timeouts (1205), so it would raise and drop a recording cycle for
        EVERY admin. Batching bounds each transaction instead. The real fix is
        an index on `node_user_usages.user_id`; until then, keep the batches
        small.
      * Each batch deletes its users' children AND the users together, so a
        failure part-way leaves earlier batches cleanly deleted and later ones
        untouched — never a user stripped of its proxies but still present.
      * The id list is snapshotted once rather than re-derived per statement.
        A subquery would be re-evaluated by each DELETE, so a user created
        between the child deletes and the user delete would reach the final
        statement with its `proxies` row intact and trip FK 1451 (or, on
        SQLite, be silently orphaned).

    Every statement sets `synchronize_session=False` (the convention already
    used at `reset_user_devices`, `disable_all_active_users` and
    `activate_all_disabled_users`). Without it SQLAlchemy 2.0 defaults to
    `"auto"`, cannot evaluate an `in_()` criteria in Python, and falls back to
    `"fetch"` — which collects every deleted primary key, via a second
    full-scan SELECT on MySQL, defeating the point of a bulk DELETE.

    Two tables are handled differently from the single-user path
    (`remove_user`), deliberately:

      * `user_usage_logs` — `User.usage_logs` declares no cascade, so a
        single-user delete NULLs its `user_id` and strands the row forever.
        Nothing reads a reset log without its user, so this deletes it.
      * `user_subscription_devices` — the DB-level cascade only fires where
        foreign keys are enforced, and SQLite does not enforce them unless
        `PRAGMA foreign_keys=ON`, which this app never sets (`app/db/base.py`).
        Deleting explicitly keeps both backends identical.

    Returns the ids of the deleted users, so the caller can invalidate any
    per-user cache keyed on them. Deliberately does NOT touch
    `Admin.users_usage` — that is a denormalised *lifetime* counter, and the
    admin's historical traffic did happen — and sends no per-user
    notifications, matching `disable_all_active_users`.
    """
    user_ids = [
        row[0] for row in
        db.query(User.id).filter(User.admin_id == dbadmin.id).all()
    ]
    if not user_ids:
        return []

    opts = {"synchronize_session": False}
    deleted: List[int] = []

    for start in range(0, len(user_ids), ADMIN_PURGE_BATCH):
        batch = user_ids[start:start + ADMIN_PURGE_BATCH]
        mark_copies_dirty(db, batch)

        # Association rows first: they reference proxies, which reference users.
        db.execute(
            delete(excluded_inbounds_association)
            .where(
                excluded_inbounds_association.c.proxy_id.in_(
                    select(Proxy.id).where(Proxy.user_id.in_(batch)).scalar_subquery()
                )
            )
            .execution_options(**opts)
        )
        for child in (
            Proxy,
            NodeUserUsage,
            NotificationReminder,
            NextPlan,
            UserUsageResetLogs,
            UserSubscriptionDevice,
        ):
            db.execute(
                delete(child).where(child.user_id.in_(batch)).execution_options(**opts)
            )

        db.execute(delete(User).where(User.id.in_(batch)).execution_options(**opts))
        db.commit()
        deleted.extend(batch)

    return deleted


def autodelete_expired_users(db: Session,
                             include_limited_users: bool = False) -> List[User]:
    """
    Deletes expired (optionally also limited) users whose auto-delete time has passed.

    Args:
        db (Session): Database session
        include_limited_users (bool, optional): Whether to delete limited users as well.
            Defaults to False.

    Returns:
        list[User]: List of deleted users.
    """
    target_status = (
        [UserStatus.expired] if not include_limited_users
        else [UserStatus.expired, UserStatus.limited]
    )

    auto_delete = coalesce(User.auto_delete_in_days, USERS_AUTODELETE_DAYS)

    query = db.query(
        User, auto_delete,  # Use global auto-delete days as fallback
    ).filter(
        auto_delete >= 0,  # Negative values prevent auto-deletion
        User.status.in_(target_status),
    ).options(joinedload(User.admin))

    # TODO: Handle time filter in query itself (NOTE: Be careful with sqlite's strange datetime handling)
    expired_users = [
        user
        for (user, auto_delete) in query
        if user.last_status_change + timedelta(days=auto_delete) <= datetime.utcnow()
    ]

    if expired_users:
        remove_users(db, expired_users)

    return expired_users


def get_all_users_usages(
        db: Session, admin: Admin, start: datetime, end: datetime
) -> List[UserUsageResponse]:
    """
    Retrieves usage data for all users associated with an admin within a specified time range.

    This function calculates the total traffic used by users across different nodes,
    including a "Master" node that represents the main core.

    Args:
        db (Session): Database session for querying.
        admin (Admin): The admin user for which to retrieve user usage data.
        start (datetime): The start date and time of the period to consider.
        end (datetime): The end date and time of the period to consider.

    Returns:
        List[UserUsageResponse]: A list of UserUsageResponse objects, each representing
        the usage data for a specific node or the main core.
    """
    usages = {0: UserUsageResponse(  # Main Core
        node_id=None,
        node_name="Master",
        used_traffic=0
    )}

    for node in db.query(Node).all():
        usages[node.id] = UserUsageResponse(
            node_id=node.id,
            node_name=node.name,
            used_traffic=0
        )

    admin_users = set(user.id for user in get_users(db=db, admins=admin))

    cond = and_(
        NodeUserUsage.created_at >= start,
        NodeUserUsage.created_at <= end,
        NodeUserUsage.user_id.in_(admin_users)
    )

    for v in db.query(NodeUserUsage).filter(cond):
        try:
            usages[v.node_id or 0].used_traffic += v.used_traffic
        except KeyError:
            pass

    return list(usages.values())


def update_user_status(db: Session, dbuser: User, status: UserStatus) -> User:
    """
    Updates a user's status and records the time of change.

    Args:
        db (Session): Database session.
        dbuser (User): The user to update.
        status (UserStatus): The new status.

    Returns:
        User: The updated user object.
    """
    dbuser.status = status
    dbuser.last_status_change = datetime.utcnow()
    mark_copy_dirty(db, dbuser.id)
    db.commit()
    db.refresh(dbuser)
    return dbuser


def set_owner(db: Session, dbuser: User, admin: Admin) -> User:
    """
    Transfers a user to a new owning admin.

    One thing moves with the user beyond `admin_id`, because leaving it behind
    is silently wrong rather than loudly broken:

      * `encrypted_subscription_url` is recomputed **when the subscription
        prefix actually changes**. A Happ link encodes the OWNER's domain —
        `build_subscription_url` reads `admin.subscription_url`, falling back
        to `XRAY_SUBSCRIPTION_URL_PREFIX` — so a transfer that skips this
        leaves the customer holding a `happ://` link pointing at the PREVIOUS
        admin's domain, which keeps working right up until that domain is
        retired. Non-destructive: `sub_revoked_at` is untouched, so the old
        link stays valid alongside the new one. No-op for non-Happ users.

        Guarded on the prefix rather than run unconditionally because
        `build_subscription_url` mints a fresh salt and token on every call:
        regenerating when both admins resolve to the same prefix would rotate
        a customer's stored link to a different string pointing at the
        identical host, for nothing. This also makes a transfer to the user's
        existing owner a genuine no-op.

        Note this is a **local** computation — `app/utils/happ.py` implements
        crypt5 and crypt4 entirely on this server, with no outbound call — so
        it is cheap and cannot block on a third party.

    Everything else that is admin-scoped — per-admin hosts, external
    subscriptions, manual hosts, announcements — is resolved from the user's
    *current* owner at render time, so it follows the transfer on the user's
    next subscription fetch with no bookkeeping here.

    Usage attribution splits three ways, and the difference is not intuitive:

      * `Admin.users_usage` (the lifetime counter) does NOT move — the old
        admin's traffic did happen under them, and the new owner accrues from
        the transfer forward.
      * `admin_hourly_usages` does not move either; past snapshots are already
        written and the Rust collector only ever upserts the current hour.
      * `GET /api/users/usage` **is retroactive**. `get_all_users_usages`
        resolves the admin's users by CURRENT ownership and then sums
        `NodeUserUsage` over the requested window, so after a transfer that
        report attributes the user's *past* traffic to the new owner and drops
        it from the old owner's report for the same past window.

    Args:
        db (Session): Database session.
        dbuser (User): The user object whose owner is to be set.
        admin (Admin): The admin to set as owner.

    Returns:
        User: The updated user object.
    """
    # Read the OLD prefix before reassigning; `build_subscription_url` resolves
    # the same fallback, so comparing the raw columns is enough.
    old_prefix = getattr(dbuser.admin, "subscription_url", None) if dbuser.admin else None
    new_prefix = getattr(admin, "subscription_url", None)

    dbuser.admin = admin
    if old_prefix != new_prefix:
        _refresh_encrypted_subscription_url(dbuser)
    mark_copy_dirty(db, dbuser.id)
    db.commit()
    db.refresh(dbuser)
    return dbuser


def start_user_expire(db: Session, dbuser: User) -> User:
    """
    Starts the expiration timer for a user.

    Args:
        db (Session): Database session.
        dbuser (User): The user object whose expiration timer is to be started.

    Returns:
        User: The updated user object.
    """
    expire = int(datetime.utcnow().timestamp()) + dbuser.on_hold_expire_duration
    dbuser.expire = expire
    dbuser.on_hold_expire_duration = None
    dbuser.on_hold_timeout = None
    mark_copy_dirty(db, dbuser.id)
    db.commit()
    db.refresh(dbuser)
    return dbuser


def get_system_usage(db: Session) -> System:
    """
    Retrieves system usage information.

    Args:
        db (Session): Database session.

    Returns:
        System: System usage information.
    """
    return db.query(System).first()


def get_jwt_secret_key(db: Session) -> str:
    """
    Retrieves the JWT secret key.

    Args:
        db (Session): Database session.

    Returns:
        str: JWT secret key.
    """
    return db.query(JWT).first().secret_key


def get_tls_certificate(db: Session) -> TLS:
    """
    Retrieves the TLS certificate.

    Args:
        db (Session): Database session.

    Returns:
        TLS: TLS certificate information.
    """
    return db.query(TLS).first()


def get_admin(db: Session, username: str) -> Admin:
    """
    Retrieves an admin by username.

    Args:
        db (Session): Database session.
        username (str): The username of the admin.

    Returns:
        Admin: The admin object.
    """
    return db.query(Admin).filter(Admin.username == username).first()


def get_admin_any_case(db: Session, username: str) -> Optional[Admin]:
    """Same name in any letter case: MySQL's admins.username collation folds
    case, SQLite's doesn't, so the API decides instead of the database."""
    return db.query(Admin).filter(func.lower(Admin.username) == username.lower()).first()


def create_admin(db: Session, admin: AdminCreate) -> Admin:
    """
    Creates a new admin in the database.

    Args:
        db (Session): Database session.
        admin (AdminCreate): The admin creation data.

    Returns:
        Admin: The created admin object.
    """
    dbadmin = Admin(
        username=admin.username,
        hashed_password=admin.hashed_password,
        is_sudo=admin.is_sudo,
        telegram_id=admin.telegram_id if admin.telegram_id else None,
        discord_webhook=admin.discord_webhook if admin.discord_webhook else None,
        subscription_url=admin.subscription_url if admin.subscription_url else None,
        can_announce=admin.can_announce,
        can_enable_outline=admin.can_enable_outline,
        happ_only=admin.happ_only,
        # Tokens are looked up by username, so a deleted admin's unexpired
        # token would otherwise work for a new admin given the same name.
        # Whole seconds, like the token's iat, so a login in this same second
        # still counts (Admin.get_admin rejects only iat < this).
        password_reset_at=datetime.utcnow().replace(microsecond=0),
    )
    db.add(dbadmin)
    db.commit()
    db.refresh(dbadmin)
    return dbadmin


def update_admin(db: Session, dbadmin: Admin, modified_admin: AdminModify) -> Admin:
    """
    Updates an admin's details.

    Args:
        db (Session): Database session.
        dbadmin (Admin): The admin object to be updated.
        modified_admin (AdminModify): The modified admin data.

    Returns:
        Admin: The updated admin object.
    """
    if modified_admin.is_sudo:
        dbadmin.is_sudo = modified_admin.is_sudo
    if modified_admin.password is not None and dbadmin.hashed_password != modified_admin.hashed_password:
        dbadmin.hashed_password = modified_admin.hashed_password
        dbadmin.password_reset_at = datetime.utcnow()
    if modified_admin.telegram_id:
        dbadmin.telegram_id = modified_admin.telegram_id
    if modified_admin.discord_webhook:
        dbadmin.discord_webhook = modified_admin.discord_webhook
    if modified_admin.subscription_url is not None:
        dbadmin.subscription_url = modified_admin.subscription_url or None
    dbadmin.can_announce = modified_admin.can_announce
    dbadmin.can_enable_outline = modified_admin.can_enable_outline
    dbadmin.happ_only = modified_admin.happ_only

    db.commit()
    db.refresh(dbadmin)
    return dbadmin


def partial_update_admin(db: Session, dbadmin: Admin, modified_admin: AdminPartialModify) -> Admin:
    """
    Partially updates an admin's details.

    Args:
        db (Session): Database session.
        dbadmin (Admin): The admin object to be updated.
        modified_admin (AdminPartialModify): The modified admin data.

    Returns:
        Admin: The updated admin object.
    """
    if modified_admin.is_sudo is not None:
        dbadmin.is_sudo = modified_admin.is_sudo
    if modified_admin.password is not None and dbadmin.hashed_password != modified_admin.hashed_password:
        dbadmin.hashed_password = modified_admin.hashed_password
        dbadmin.password_reset_at = datetime.utcnow()
    if modified_admin.telegram_id is not None:
        dbadmin.telegram_id = modified_admin.telegram_id
    if modified_admin.discord_webhook is not None:
        dbadmin.discord_webhook = modified_admin.discord_webhook
    if modified_admin.subscription_url is not None:
        dbadmin.subscription_url = modified_admin.subscription_url or None
    if modified_admin.can_announce is not None:
        dbadmin.can_announce = modified_admin.can_announce
    if modified_admin.can_enable_outline is not None:
        dbadmin.can_enable_outline = modified_admin.can_enable_outline
    if modified_admin.happ_only is not None:
        dbadmin.happ_only = modified_admin.happ_only

    db.commit()
    db.refresh(dbadmin)
    return dbadmin


def remove_admin(db: Session, dbadmin: Admin) -> Admin:
    """
    Removes an admin from the database.

    Args:
        db (Session): Database session.
        dbadmin (Admin): The admin object to be removed.

    Returns:
        Admin: The removed admin object.
    """
    db.delete(dbadmin)
    # Only marzban-cli gets here while it owns users (the API answers 409):
    # they stay, with no admin (other hosts, other announcement). Its
    # host_admins rows cascade: a host scoped only to it is now everyone's.
    mark_all_copies_dirty(db)
    db.commit()
    return dbadmin


def get_admin_by_id(db: Session, id: int) -> Admin:
    """
    Retrieves an admin by their ID.

    Args:
        db (Session): Database session.
        id (int): The ID of the admin.

    Returns:
        Admin: The admin object.
    """
    return db.query(Admin).filter(Admin.id == id).first()


def get_admin_by_telegram_id(db: Session, telegram_id: int) -> Admin:
    """
    Retrieves an admin by their Telegram ID.

    Args:
        db (Session): Database session.
        telegram_id (int): The Telegram ID of the admin.

    Returns:
        Admin: The admin object.
    """
    return db.query(Admin).filter(Admin.telegram_id == telegram_id).first()


def get_admins(db: Session,
               offset: Optional[int] = None,
               limit: Optional[int] = None,
               username: Optional[str] = None) -> List[Admin]:
    """
    Retrieves a list of admins with optional filters and pagination.

    Args:
        db (Session): Database session.
        offset (Optional[int]): The number of records to skip (for pagination).
        limit (Optional[int]): The maximum number of records to return.
        username (Optional[str]): The username to filter by.

    Returns:
        List[Admin]: A list of admin objects.
    """
    query = db.query(Admin)
    if username:
        query = query.filter(Admin.username.ilike(f'%{username}%'))
    if offset:
        query = query.offset(offset)
    if limit:
        query = query.limit(limit)
    return query.all()


def get_admins_users_counts(db: Session) -> Dict[int, int]:
    """admin id -> users it owns, any status (same basis as get_users_count)."""
    rows = db.query(User.admin_id, func.count(User.id)).group_by(User.admin_id).all()
    return {admin_id: n for admin_id, n in rows if admin_id is not None}


def reset_admin_usage(db: Session, dbadmin: Admin) -> int:
    """
    Retrieves an admin's usage by their username.
    Args:
        db (Session): Database session.
        dbadmin (Admin): The admin object to be updated.
    Returns:
        Admin: The updated admin.
    """
    if (dbadmin.users_usage == 0):
        return dbadmin

    usage_log = AdminUsageLogs(
        admin=dbadmin,
        used_traffic_at_reset=dbadmin.users_usage
    )
    db.add(usage_log)
    dbadmin.users_usage = 0

    db.commit()
    db.refresh(dbadmin)
    return dbadmin


# Asia/Ashgabat (UTC+5, no DST) — the local day the usage dashboard buckets by
# (mirrors ASHGABAT_OFFSET in app/routers/admin.py). A "day" the operator picks
# is this local day; snapshots are stored at UTC top-of-hour.
_ADMIN_USAGE_TZ_OFFSET = timedelta(hours=5)


def adjust_admin_usage(db: Session,
                       dbadmin: Admin,
                       adjustments: List[Tuple[date, int]],
                       actor_id: Optional[int] = None) -> dict:
    """Add traffic to an admin on chosen local days. THE FIRST/ONLY Python
    writer of `admin_hourly_usages` — everywhere else that table is read-only
    and owned by the Rust collector.

    `adjustments` is a list of `(for_day, added_bytes)`. The dashboard derives
    per-day usage by diffing a MONOTONIC cumulative counter across hourly
    snapshots, so for each day D with amount A this:

      * ramps D's own snapshots: the k-th of n gets `A*k//n` (integer, so the
        day ends exactly +A and the per-hour diffs sum to A);
      * adds a flat +A to every LATER snapshot (carry-forward — without it the
        day after D would diff negative and lose the bump at the boundary);
      * bumps `admins.users_usage` by ΣA, so the collector's next hourly
        snapshot (recomputed from that counter) continues from the raised
        level and tomorrow's chart does not dip.

    Editing past rows is safe: the collector only ever upserts the current UTC
    hour. Raises ValueError if a chosen day has no snapshots to attribute to,
    or if a day appears twice. Everything commits in one transaction. Returns
    a summary dict.

    Every write is an ATOMIC in-SQL increment (never a Python read-modify-write):
    the Rust collector concurrently upserts the current-hour row and the
    record_usages job increments `admins.users_usage` every 10s, so a value read
    into Python and written back could clobber a concurrent write. Only the
    in-day snapshot *timestamps* are read into Python (≤24 per day) to size the
    ramp; the carry-forward is a single ranged UPDATE per day, not a full-history
    scan.
    """
    if not adjustments:
        raise ValueError("no adjustments given")
    days = [d for d, _ in adjustments]
    if len(set(days)) != len(days):
        raise ValueError("the same day appears more than once")

    # Ascending by day so each later day's ramp stacks on the earlier days'
    # flat carry-forward rather than fighting it.
    items = sorted(((d, int(a)) for d, a in adjustments), key=lambda x: x[0])

    def day_bounds_utc(d: date) -> Tuple[datetime, datetime]:
        start_utc = datetime(d.year, d.month, d.day) - _ADMIN_USAGE_TZ_OFFSET
        return start_utc, start_utc + timedelta(days=1)

    total_added = 0
    for d, amount in items:
        start_utc, end_utc = day_bounds_utc(d)
        in_day = [
            row[0] for row in
            db.query(AdminHourlyUsage.recorded_at)
            .filter(AdminHourlyUsage.admin_id == dbadmin.id,
                    AdminHourlyUsage.recorded_at >= start_utc,
                    AdminHourlyUsage.recorded_at < end_utc)
            .order_by(AdminHourlyUsage.recorded_at)
            .all()
        ]
        if not in_day:
            raise ValueError(f"no usage snapshots exist for {d.isoformat()}")
        n = len(in_day)
        # Ramp: the k-th of n in-day snapshots gets cumulative +A*k//n (integer,
        # so the day ends exactly +A and per-hour diffs sum to A). One atomic
        # UPDATE per in-day row.
        for k, ts in enumerate(in_day, 1):
            db.execute(
                update(AdminHourlyUsage)
                .where(AdminHourlyUsage.admin_id == dbadmin.id,
                       AdminHourlyUsage.recorded_at == ts)
                .values(users_usage=AdminHourlyUsage.users_usage + (amount * k // n))
            )
        # Flat carry-forward: every snapshot after this day gets +A, in one
        # ranged UPDATE (without it the next day would diff negative and lose
        # the bump at the boundary).
        db.execute(
            update(AdminHourlyUsage)
            .where(AdminHourlyUsage.admin_id == dbadmin.id,
                   AdminHourlyUsage.recorded_at >= end_utc)
            .values(users_usage=AdminHourlyUsage.users_usage + amount)
        )
        total_added += amount

    # Mirror record_usages.py:174-176 — atomic increment of the live counter.
    db.execute(
        update(Admin)
        .where(Admin.id == dbadmin.id)
        .values(users_usage=Admin.users_usage + total_added)
    )

    for d, amount in items:
        db.add(AdminUsageAdjustment(
            admin_id=dbadmin.id,
            for_day=d,
            added_bytes=amount,
            created_by_admin_id=actor_id,
        ))

    db.commit()
    db.refresh(dbadmin)
    return {"total_added_bytes": total_added, "applied": list(items)}


def get_admin_hourly_stats(db: Session,
                           start_utc: datetime,
                           end_utc: datetime,
                           admin_id: Optional[int] = None
                           ) -> List[AdminHourlyUsage]:
    """Fetch hourly admin snapshots for a bounded range plus a baseline.

    Returns the bounded baseline (the single latest row per admin with
    `recorded_at < start_utc`) together with every row in the closed
    window [start_utc, end_utc]. The baseline lets the caller diff the
    first in-window bucket against the last value before the window,
    instead of having to read the whole table from the beginning.

    Rows are written by the out-of-process Rust stats collector. The
    stored `users_usage` is monotonic (Admin.users_usage + Σ reset
    logs), so diffing consecutive buckets yields correct per-bucket
    usage even across a `reset_admin_usage` call.

    Args:
        db (Session): Database session.
        start_utc (datetime): Inclusive lower bound (UTC) on the window.
        end_utc (datetime): Inclusive upper bound (UTC) on the window.
        admin_id (Optional[int]): Restrict to a single admin.

    Returns:
        List[AdminHourlyUsage]: Baseline + in-window rows, ordered by
        (admin_id, recorded_at) ascending.
    """
    # Defend against a reversed window: an inverted [start, end] would
    # otherwise silently return no in-window rows. Callers already swap,
    # this just keeps the query honest in isolation.
    if start_utc > end_utc:
        start_utc, end_utc = end_utc, start_utc

    # Per-admin baseline: latest recorded_at strictly before the window.
    # The (admin_id, recorded_at) index backs this grouped MAX, so it is
    # a bounded per-group lookup rather than a full-table scan.
    baseline_q = (
        db.query(
            AdminHourlyUsage.admin_id,
            func.max(AdminHourlyUsage.recorded_at).label("recorded_at"),
        )
        .filter(AdminHourlyUsage.recorded_at < start_utc)
    )
    if admin_id is not None:
        baseline_q = baseline_q.filter(AdminHourlyUsage.admin_id == admin_id)
    baseline_keys = baseline_q.group_by(AdminHourlyUsage.admin_id).all()

    rows: List[AdminHourlyUsage] = []
    if baseline_keys:
        baseline_filter = or_(*[
            and_(
                AdminHourlyUsage.admin_id == aid,
                AdminHourlyUsage.recorded_at == recorded_at,
            )
            for aid, recorded_at in baseline_keys
        ])
        rows.extend(db.query(AdminHourlyUsage).filter(baseline_filter).all())

    window_q = db.query(AdminHourlyUsage).filter(
        AdminHourlyUsage.recorded_at >= start_utc,
        AdminHourlyUsage.recorded_at <= end_utc,
    )
    if admin_id is not None:
        window_q = window_q.filter(AdminHourlyUsage.admin_id == admin_id)
    rows.extend(window_q.all())

    rows.sort(key=lambda r: (r.admin_id, r.recorded_at))
    return rows


def get_top_users_by_devices(db: Session,
                             admin_id: Optional[int],
                             limit: int) -> List[Tuple[str, Optional[str], int]]:
    """Users with the most active devices, for the Statistics page's Users
    tab.

    "Active device" is `_active_device_filter` (NOT is_blocked, no time
    window) — the same definition behind User.device_count, count_devices and
    the collector's device_count gauge, so the number next to a user here
    matches the device modal and the Statistics cards byte for byte (the
    "Client apps" view, get_client_app_devices, adds a time window on top).
    Any user status counts: a limited or expired user's devices still hold
    slots and still show in every other gauge.

    `admin_id=None` ranks every user on the panel (sudo, unfiltered);
    otherwise only that admin's users. Returns (username, owner_username,
    device_count) descending, at most `limit` rows. Selects and groups plain
    columns rather than loading ORM entities so the GROUP BY is valid under
    MySQL's ONLY_FULL_GROUP_BY and on SQLite alike.
    """
    cnt = func.count(UserSubscriptionDevice.id).label("cnt")
    owner = Admin.username.label("owner")
    q = (
        db.query(User.username, owner, cnt)
        .select_from(User)
        .join(UserSubscriptionDevice, UserSubscriptionDevice.user_id == User.id)
        .outerjoin(Admin, Admin.id == User.admin_id)
        .filter(_active_device_filter())
    )
    if admin_id is not None:
        q = q.filter(User.admin_id == admin_id)
    q = (
        q.group_by(User.id, User.username, Admin.username)
        .order_by(cnt.desc(), User.username.asc())
        .limit(limit)
    )
    return [(u, a, int(c)) for u, a, c in q.all()]


def get_top_users_by_traffic(db: Session,
                             admin_id: Optional[int],
                             start: datetime,
                             end: datetime,
                             limit: int) -> List[Tuple[str, Optional[str], int]]:
    """Users with the most traffic in the Statistics chart's window, for the
    page's Users tab.

    `start`/`end` are the SAME snapshot window the chart reads (UTC,
    inclusive). They are shifted back one hour here because the two tables
    label time differently: `admin_hourly_usages.recorded_at = T` is a
    cumulative snapshot AT T, so the chart's row T holds the traffic of
    (T-1h, T]; `node_user_usages.created_at = T` is a forward bucket holding
    [T, T+1h) (record_usages truncates utcnow to the hour). Without the shift
    the two halves of the page disagree by an hour — a burst at 23:30 local
    would appear in one day's "Top users" and the neighbouring day's traffic
    bar.

    Sums `node_user_usages` — the per-user, per-node hourly rows written by
    record_usages — so this is REAL metered traffic only, and it can total
    less than the admin's "Total Usage" for the same range for two reasons:
    manual admin usage adjustments (adjust_admin_usage) are admin-level and
    never appear here; and a per-user usage reset (reset_user_data_usage,
    reset_user_by_next, reset_all_users_data_usage) deletes the user's
    node_user_usages rows outright, so a user reset inside the range loses
    its pre-reset traffic from this list while the admin counter keeps it.

    The `created_at` range scan is index-backed: it is the leading column of
    the table's (created_at, user_id, node_id) unique constraint. Scoping and
    return shape match get_top_users_by_devices.
    """
    bucket_shift = timedelta(hours=1)
    total = func.sum(NodeUserUsage.used_traffic).label("total")
    owner = Admin.username.label("owner")
    q = (
        db.query(User.username, owner, total)
        .select_from(NodeUserUsage)
        .join(User, User.id == NodeUserUsage.user_id)
        .outerjoin(Admin, Admin.id == User.admin_id)
        .filter(NodeUserUsage.created_at >= start - bucket_shift,
                NodeUserUsage.created_at <= end - bucket_shift)
    )
    if admin_id is not None:
        q = q.filter(User.admin_id == admin_id)
    q = (
        q.group_by(User.id, User.username, Admin.username)
        .order_by(total.desc(), User.username.asc())
        .limit(limit)
    )
    return [(u, a, int(c or 0)) for u, a, c in q.all()]


def get_client_app_devices(db: Session,
                           admin_id: Optional[int],
                           start: datetime,
                           end: datetime,
                           ) -> List[Tuple[Optional[str], int, Optional[str], Optional[str], datetime]]:
    """Every active device in use during [start, end] (UTC), for the
    Statistics page's Apps tab.

    A device only stores its first and last fetch, so "in use" means its
    first fetch is at or before `end` and its last fetch at or after `start`.
    For a window that ends now this is exact (it fetched since `start`); for
    a past window it also counts a device that skipped the whole window
    between two fetches. Admin-prefilled slots that never fetched have no
    client and are left out, and so are blocked devices
    (`_active_device_filter`), like every other device figure in the UI.

    `admin_id=None` covers every user on the panel (sudo, unfiltered);
    otherwise only that admin's users. Returns plain rows of (owner_username,
    user_id, client_name, platform, first_seen_at) for the router to count:
    distinct users per app cannot be summed from a grouped query, and the
    table holds one row per device, so the scan stays small.
    """
    owner = Admin.username.label("owner")
    q = (
        db.query(owner,
                 UserSubscriptionDevice.user_id,
                 UserSubscriptionDevice.client_name,
                 UserSubscriptionDevice.platform,
                 UserSubscriptionDevice.first_seen_at)
        .select_from(UserSubscriptionDevice)
        .join(User, User.id == UserSubscriptionDevice.user_id)
        .outerjoin(Admin, Admin.id == User.admin_id)
        .filter(_active_device_filter(),
                UserSubscriptionDevice.first_seen_at <= end,
                UserSubscriptionDevice.last_seen_at >= start)
    )
    if admin_id is not None:
        q = q.filter(User.admin_id == admin_id)
    return q.all()


def create_user_template(db: Session, user_template: UserTemplateCreate) -> UserTemplate:
    """
    Creates a new user template in the database.

    Args:
        db (Session): Database session.
        user_template (UserTemplateCreate): The user template creation data.

    Returns:
        UserTemplate: The created user template object.
    """
    inbound_tags: List[str] = []
    for _, i in user_template.inbounds.items():
        inbound_tags.extend(i)
    dbuser_template = UserTemplate(
        name=user_template.name,
        data_limit=user_template.data_limit,
        expire_duration=user_template.expire_duration,
        username_prefix=user_template.username_prefix,
        username_suffix=user_template.username_suffix,
        inbounds=db.query(ProxyInbound).filter(ProxyInbound.tag.in_(inbound_tags)).all()
    )
    db.add(dbuser_template)
    db.commit()
    db.refresh(dbuser_template)
    return dbuser_template


def update_user_template(
        db: Session, dbuser_template: UserTemplate, modified_user_template: UserTemplateModify) -> UserTemplate:
    """
    Updates a user template's details.

    Args:
        db (Session): Database session.
        dbuser_template (UserTemplate): The user template object to be updated.
        modified_user_template (UserTemplateModify): The modified user template data.

    Returns:
        UserTemplate: The updated user template object.
    """
    if modified_user_template.name is not None:
        dbuser_template.name = modified_user_template.name
    if modified_user_template.data_limit is not None:
        dbuser_template.data_limit = modified_user_template.data_limit
    if modified_user_template.expire_duration is not None:
        dbuser_template.expire_duration = modified_user_template.expire_duration
    if modified_user_template.username_prefix is not None:
        dbuser_template.username_prefix = modified_user_template.username_prefix
    if modified_user_template.username_suffix is not None:
        dbuser_template.username_suffix = modified_user_template.username_suffix

    if modified_user_template.inbounds:
        inbound_tags: List[str] = []
        for _, i in modified_user_template.inbounds.items():
            inbound_tags.extend(i)
        dbuser_template.inbounds = db.query(ProxyInbound).filter(ProxyInbound.tag.in_(inbound_tags)).all()

    db.commit()
    db.refresh(dbuser_template)
    return dbuser_template


def remove_user_template(db: Session, dbuser_template: UserTemplate):
    """
    Removes a user template from the database.

    Args:
        db (Session): Database session.
        dbuser_template (UserTemplate): The user template object to be removed.
    """
    db.delete(dbuser_template)
    db.commit()


def get_user_template(db: Session, user_template_id: int) -> UserTemplate:
    """
    Retrieves a user template by its ID.

    Args:
        db (Session): Database session.
        user_template_id (int): The ID of the user template.

    Returns:
        UserTemplate: The user template object.
    """
    return db.query(UserTemplate).filter(UserTemplate.id == user_template_id).first()


def get_user_templates(
        db: Session, offset: Union[int, None] = None, limit: Union[int, None] = None) -> List[UserTemplate]:
    """
    Retrieves a list of user templates with optional pagination.

    Args:
        db (Session): Database session.
        offset (Union[int, None]): The number of records to skip (for pagination).
        limit (Union[int, None]): The maximum number of records to return.

    Returns:
        List[UserTemplate]: A list of user template objects.
    """
    dbuser_templates = db.query(UserTemplate)
    if offset:
        dbuser_templates = dbuser_templates.offset(offset)
    if limit:
        dbuser_templates = dbuser_templates.limit(limit)

    return dbuser_templates.all()


def get_node(db: Session, name: str) -> Optional[Node]:
    """
    Retrieves a node by its name.

    Args:
        db (Session): The database session.
        name (str): The name of the node to retrieve.

    Returns:
        Optional[Node]: The Node object if found, None otherwise.
    """
    return db.query(Node).filter(Node.name == name).first()


def get_node_by_id(db: Session, node_id: int) -> Optional[Node]:
    """
    Retrieves a node by its ID.

    Args:
        db (Session): The database session.
        node_id (int): The ID of the node to retrieve.

    Returns:
        Optional[Node]: The Node object if found, None otherwise.
    """
    return db.query(Node).filter(Node.id == node_id).first()


def get_nodes(db: Session,
              status: Optional[Union[NodeStatus, list]] = None,
              enabled: bool = None) -> List[Node]:
    """
    Retrieves nodes based on optional status and enabled filters.

    Args:
        db (Session): The database session.
        status (Optional[Union[NodeStatus, list]]): The status or list of statuses to filter by.
        enabled (bool): If True, excludes disabled nodes.

    Returns:
        List[Node]: A list of Node objects matching the criteria.
    """
    query = db.query(Node)

    if status:
        if isinstance(status, list):
            query = query.filter(Node.status.in_(status))
        else:
            query = query.filter(Node.status == status)

    if enabled:
        query = query.filter(Node.status != NodeStatus.disabled)

    return query.all()


def get_nodes_usage(db: Session, start: datetime, end: datetime) -> List[NodeUsageResponse]:
    """
    Retrieves usage data for all nodes within a specified time range.

    Args:
        db (Session): The database session.
        start (datetime): The start time of the usage period.
        end (datetime): The end time of the usage period.

    Returns:
        List[NodeUsageResponse]: A list of NodeUsageResponse objects containing usage data.
    """
    usages = {0: NodeUsageResponse(  # Main Core
        node_id=None,
        node_name="Master",
        uplink=0,
        downlink=0
    )}

    for node in db.query(Node).all():
        usages[node.id] = NodeUsageResponse(
            node_id=node.id,
            node_name=node.name,
            uplink=0,
            downlink=0
        )

    cond = and_(NodeUsage.created_at >= start, NodeUsage.created_at <= end)

    for v in db.query(NodeUsage).filter(cond):
        try:
            usages[v.node_id or 0].uplink += v.uplink
            usages[v.node_id or 0].downlink += v.downlink
        except KeyError:
            pass

    return list(usages.values())


def create_node(db: Session, node: NodeCreate) -> Node:
    """
    Creates a new node in the database.

    Args:
        db (Session): The database session.
        node (NodeCreate): The node creation model containing node details.

    Returns:
        Node: The newly created Node object.
    """
    dbnode = Node(name=node.name,
                  address=node.address,
                  port=node.port,
                  api_port=node.api_port)

    db.add(dbnode)
    db.commit()
    db.refresh(dbnode)
    return dbnode


def remove_node(db: Session, dbnode: Node) -> Node:
    """
    Removes a node from the database.

    Args:
        db (Session): The database session.
        dbnode (Node): The Node object to be removed.

    Returns:
        Node: The removed Node object.
    """
    db.delete(dbnode)
    db.commit()
    return dbnode


def update_node(db: Session, dbnode: Node, modify: NodeModify) -> Node:
    """
    Updates an existing node with new information.

    Args:
        db (Session): The database session.
        dbnode (Node): The Node object to be updated.
        modify (NodeModify): The modification model containing updated node details.

    Returns:
        Node: The updated Node object.
    """
    if modify.name is not None:
        dbnode.name = modify.name

    if modify.address is not None:
        dbnode.address = modify.address

    if modify.port is not None:
        dbnode.port = modify.port

    if modify.api_port is not None:
        dbnode.api_port = modify.api_port

    if modify.status is NodeStatus.disabled:
        dbnode.status = modify.status
        dbnode.xray_version = None
        dbnode.message = None
    else:
        dbnode.status = NodeStatus.connecting

    if modify.usage_coefficient:
        dbnode.usage_coefficient = modify.usage_coefficient

    db.commit()
    db.refresh(dbnode)
    return dbnode


def update_node_status(db: Session, dbnode: Node, status: NodeStatus, message: str = None, version: str = None) -> Node:
    """
    Updates the status of a node.

    Args:
        db (Session): The database session.
        dbnode (Node): The Node object to be updated.
        status (NodeStatus): The new status of the node.
        message (str, optional): A message associated with the status update.
        version (str, optional): The version of the node software.

    Returns:
        Node: The updated Node object.
    """
    dbnode.status = status
    dbnode.message = message
    dbnode.xray_version = version
    dbnode.last_status_change = datetime.utcnow()
    db.commit()
    db.refresh(dbnode)
    return dbnode


def create_notification_reminder(
        db: Session, reminder_type: ReminderType, expires_at: datetime, user_id: int, threshold: Optional[int] = None) -> NotificationReminder:
    """
    Creates a new notification reminder.

    Args:
        db (Session): The database session.
        reminder_type (ReminderType): The type of reminder.
        expires_at (datetime): The expiration time of the reminder.
        user_id (int): The ID of the user associated with the reminder.
        threshold (Optional[int]): The threshold value to check for (e.g., days left or usage percent).

    Returns:
        NotificationReminder: The newly created NotificationReminder object.
    """
    reminder = NotificationReminder(type=reminder_type, expires_at=expires_at, user_id=user_id)
    if threshold is not None:
        reminder.threshold = threshold
    db.add(reminder)
    db.commit()
    db.refresh(reminder)
    return reminder


def get_notification_reminder(
        db: Session, user_id: int, reminder_type: ReminderType, threshold: Optional[int] = None
) -> Union[NotificationReminder, None]:
    """
    Retrieves a notification reminder for a user.

    Args:
        db (Session): The database session.
        user_id (int): The ID of the user.
        reminder_type (ReminderType): The type of reminder to retrieve.
        threshold (Optional[int]): The threshold value to check for (e.g., days left or usage percent).

    Returns:
        Union[NotificationReminder, None]: The NotificationReminder object if found and not expired, None otherwise.
    """
    query = db.query(NotificationReminder).filter(
        NotificationReminder.user_id == user_id,
        NotificationReminder.type == reminder_type
    )

    # If a threshold is provided, filter for reminders with this threshold
    if threshold is not None:
        query = query.filter(NotificationReminder.threshold == threshold)

    reminder = query.first()

    if reminder is None:
        return None

    # Check if the reminder has expired
    if reminder.expires_at and reminder.expires_at < datetime.utcnow():
        db.delete(reminder)
        db.commit()
        return None

    return reminder


def delete_notification_reminder_by_type(
        db: Session, user_id: int, reminder_type: ReminderType, threshold: Optional[int] = None
) -> None:
    """
    Deletes a notification reminder for a user based on the reminder type and optional threshold.

    Args:
        db (Session): The database session.
        user_id (int): The ID of the user.
        reminder_type (ReminderType): The type of reminder to delete.
        threshold (Optional[int]): The threshold to delete (e.g., days left or usage percent). If not provided, deletes all reminders of that type.
    """
    stmt = delete(NotificationReminder).where(
        NotificationReminder.user_id == user_id,
        NotificationReminder.type == reminder_type
    )

    # If a threshold is provided, include it in the filter
    if threshold is not None:
        stmt = stmt.where(NotificationReminder.threshold == threshold)

    db.execute(stmt)
    db.commit()


def delete_notification_reminder(db: Session, dbreminder: NotificationReminder) -> None:
    """
    Deletes a specific notification reminder.

    Args:
        db (Session): The database session.
        dbreminder (NotificationReminder): The NotificationReminder object to delete.
    """
    db.delete(dbreminder)
    db.commit()
    return


def count_online_users(db: Session, hours: int = 24):
    twenty_four_hours_ago = datetime.utcnow() - timedelta(hours=hours)
    query = db.query(func.count(User.id)).filter(User.online_at.isnot(
        None), User.online_at >= twenty_four_hours_ago)
    return query.scalar()


# ----------------------------------------------------------------------
# Subscription device tracking
# ----------------------------------------------------------------------

DEVICE_THROTTLE_SECONDS = 60
"""Skip the UPDATE when the same device was just seen — caps write rate."""

DEVICE_KEY_HWID_PREFIX = "hwid:"
"""Prefix for HWID-based device keys. UA-hash devices use 'ua:'. The
device-limit policy only matches against rows with the 'hwid:' prefix."""

DEVICE_KEY_PUBKEY_PREFIX = "pk:"
"""Prefix for Micky device keys: "pk:" + base64url(sha256(device public
key)) (app/utils/micky.py). Like 'hwid:' rows these are exact identities —
blocking and the device limit apply to them the same way."""

DEVICE_KEY_UA_PREFIX = "ua:"
"""Prefix for UA-hash device keys (clients that send no X-Hwid, e.g.
Dalvik/generic Android). These can't be blocked by exact identity, only
by device model — see is_model_blocked."""


def _active_device_filter():
    """SQL filter clause for 'active' devices, applied everywhere a UI
    or limit check touches the user_subscription_devices table.

    Active = NOT is_blocked. Time is intentionally not part of this
    filter — a device holds its slot until the admin explicitly blocks
    it. Letting time alone free a slot caused over-shoot when the
    original device later returned with the same HWID (it matched its
    old row and reactivated while a new device sat in its freed slot).

    Admin-prefilled rows (NULL last_seen_at) and recently-used rows are
    both active. Stale rows are also active until blocked — admins can
    see them in the UserDevicesModal and remove them with the Block
    button when they want to free the slot."""
    return UserSubscriptionDevice.is_blocked.is_(False)


def _active_device_count_subquery():
    """Correlated COUNT used in get_user_queryset to populate
    User.device_count without an N+1. Mirrors count_user_devices'
    filter — both reduce to NOT is_blocked, so the displayed count
    and the limit-enforcement count are byte-identical."""
    return (
        select(func.count(UserSubscriptionDevice.id))
        .where(
            UserSubscriptionDevice.user_id == User.id,
            _active_device_filter(),
        )
        .correlate_except(UserSubscriptionDevice)
        .scalar_subquery()
    )


def record_subscription_device(
    db: Session,
    user_id: int,
    fp,  # device_fingerprint.DeviceFingerprint
    ip: Optional[str] = None,
) -> None:
    """Upsert a device row for one subscription fetch.

    Throttled: if the same device was last_seen_at within
    DEVICE_THROTTLE_SECONDS, this is a no-op. Designed for use from a
    BackgroundTask — wraps no exception handling itself so the caller
    decides how to log failures.
    """
    now = datetime.utcnow()
    existing = (
        db.query(UserSubscriptionDevice)
        .filter_by(user_id=user_id, device_key=fp.device_key)
        .first()
    )
    if existing:
        # Throttle is keyed on a real prior contact. Admin-prefilled and
        # newly-claimed rows have last_seen_at=NULL — they MUST go through
        # the enrichment path on first sight, otherwise the row stays
        # without client_name/platform forever.
        is_first_contact = existing.last_seen_at is None
        if not is_first_contact and \
           (now - existing.last_seen_at).total_seconds() < DEVICE_THROTTLE_SECONDS:
            return  # throttled
        existing.last_seen_at = now
        if existing.first_seen_at is None:
            existing.first_seen_at = now
        existing.fetch_count = (existing.fetch_count or 0) + 1
        if ip:
            existing.last_ip = ip
        # Refresh enriched fields. client_name / platform were missing
        # from the original list, which caused claimed slots to render as
        # "unknown" indefinitely. user_agent already updates each call;
        # the rest follow the same "if new value supplied, write it" rule.
        if fp.client_name:
            existing.client_name = fp.client_name
        if fp.platform:
            existing.platform = fp.platform
        if fp.client_version:
            existing.client_version = fp.client_version
        if fp.os_version:
            existing.os_version = fp.os_version
        if fp.device_model:
            existing.device_model = fp.device_model
        if fp.locale:
            existing.locale = fp.locale
        if fp.user_agent:
            existing.user_agent = fp.user_agent
    else:
        db.add(UserSubscriptionDevice(
            user_id=user_id,
            device_key=fp.device_key,
            user_agent=fp.user_agent,
            client_name=fp.client_name,
            client_version=fp.client_version,
            platform=fp.platform,
            os_version=fp.os_version,
            device_model=fp.device_model,
            locale=fp.locale,
            last_ip=ip,
            first_seen_at=now,
            last_seen_at=now,
            fetch_count=1,
        ))
    db.commit()


def list_user_devices(db: Session, user_id: int) -> List[UserSubscriptionDevice]:
    """Return the user's non-blocked devices, ordered most-recently-seen
    first. Stale devices are still shown — admins clean them up with
    the Block button when they want to free the slot."""
    return (
        db.query(UserSubscriptionDevice)
        .filter(
            UserSubscriptionDevice.user_id == user_id,
            _active_device_filter(),
        )
        .order_by(UserSubscriptionDevice.last_seen_at.desc())
        .all()
    )


def is_hwid_blocked(db: Session, user_id: int, hwid: str) -> bool:
    """Cheap existence check for the global block enforcement: returns
    True iff this user has a blocked row keyed on this exact HWID.

    Called on every subscription fetch (when X-Hwid is present) so it
    short-circuits to a single index-backed equality lookup. Only checks
    HWID-prefixed rows because UA-hashed rows can't be reliably blocked
    (a UA bump on app update produces a fresh device_key)."""
    return is_device_key_blocked(db, user_id, DEVICE_KEY_HWID_PREFIX + hwid)


def is_device_key_blocked(db: Session, user_id: int, device_key: str) -> bool:
    """is_hwid_blocked for any exact identity ('hwid:' or 'pk:' key)."""
    return (
        db.query(UserSubscriptionDevice.id)
        .filter_by(user_id=user_id, device_key=device_key, is_blocked=True)
        .first()
        is not None
    )


def is_model_blocked(db: Session, user_id: int, device_model: str) -> bool:
    """Global block enforcement for clients with no unique device id —
    Dalvik / generic Android, which send no X-Hwid. Returns True iff this
    user has a blocked UA-keyed row carrying this device model.

    A Dalvik UA only exposes a *product* model code (every unit of that
    model reports it identically), so this deliberately blocks all
    same-model devices together — it is the only identifier such clients
    give us. Scoped to 'ua:' rows so a precise HWID block of a same-model
    Happ device never leaks into a model-wide block. Cheap: filtered by
    user_id (indexed prefix) over a user's handful of device rows."""
    if not device_model:
        return False
    return (
        db.query(UserSubscriptionDevice.id)
        .filter(
            UserSubscriptionDevice.user_id == user_id,
            UserSubscriptionDevice.device_model == device_model,
            UserSubscriptionDevice.device_key.like(DEVICE_KEY_UA_PREFIX + "%"),
            UserSubscriptionDevice.is_blocked.is_(True),
        )
        .first()
        is not None
    )


# ----------------------------------------------------------------------
# Announcements (append-only — see app/db/models.py:Announcement)
# ----------------------------------------------------------------------

def get_latest_announcement(db: Session, admin_id: Optional[int]) -> Optional[Announcement]:
    """Most recent announcement row for an admin (admin_id) or for the
    global fallback (admin_id=None). 'Most recent' = MAX(id). Returns the
    raw row, which MAY be disabled — the caller applies the resolution
    rule (use only if enabled)."""
    q = db.query(Announcement)
    if admin_id is None:
        q = q.filter(Announcement.admin_id.is_(None))
    else:
        q = q.filter(Announcement.admin_id == admin_id)
    return q.order_by(Announcement.id.desc()).first()


def resolve_announcement_text(db: Session, admin_id: Optional[int]) -> Optional[str]:
    """Effective announcement text for an admin's users (DB-backed; the
    cache layer applies the same rule and falls back here on a miss).

    The admin's LATEST row if it is enabled; else the latest global row if
    it is enabled; else None. NB: latest row *if* enabled — NOT the latest
    *enabled* row (a disable overrides an older enabled row)."""
    if admin_id is not None:
        own = get_latest_announcement(db, admin_id)
        if own is not None and own.enabled:
            return own.text
    glob = get_latest_announcement(db, None)
    if glob is not None and glob.enabled:
        return glob.text
    return None


def add_announcement(db: Session,
                     admin_id: Optional[int],
                     text: str,
                     enabled: bool,
                     created_by_admin_id: Optional[int]) -> Announcement:
    """Append a new announcement row (never edits an existing one).
    admin_id=None writes the global fallback."""
    row = Announcement(
        admin_id=admin_id,
        text=text,
        enabled=enabled,
        created_by_admin_id=created_by_admin_id,
    )
    db.add(row)
    mark_admin_copies_dirty(db, admin_id)
    db.commit()
    db.refresh(row)
    return row


def disable_announcement(db: Session,
                         admin_id: Optional[int],
                         created_by_admin_id: Optional[int]) -> Optional[Announcement]:
    """Turn an announcement off by APPENDING a disabled row that carries the
    current text (keeps append-only history intact). No-op (returns None) if
    nothing is currently showing (no row, or the latest is already disabled)."""
    latest = get_latest_announcement(db, admin_id)
    if latest is None or not latest.enabled:
        return None
    return add_announcement(db, admin_id, latest.text, False, created_by_admin_id)


def list_announcements(db: Session, admin_id: Optional[int],
                       limit: int = 50) -> List[Announcement]:
    """History for one admin (or global, admin_id=None), newest first."""
    q = db.query(Announcement)
    if admin_id is None:
        q = q.filter(Announcement.admin_id.is_(None))
    else:
        q = q.filter(Announcement.admin_id == admin_id)
    return q.order_by(Announcement.id.desc()).limit(limit).all()


def get_latest_announcements_map(db: Session) -> dict:
    """{admin_id -> latest Announcement row} for the sudo overview; the
    global fallback is under key None. One row per group (MAX id)."""
    sub = (
        db.query(
            Announcement.admin_id.label("admin_id"),
            func.max(Announcement.id).label("max_id"),
        )
        .group_by(Announcement.admin_id)
        .subquery()
    )
    rows = db.query(Announcement).join(sub, Announcement.id == sub.c.max_id).all()
    return {r.admin_id: r for r in rows}


# ---------------------------------------------------------------------------
# External subscriptions (other-panel mirroring)
#
# Sudo-authored registry. The Rust poller reads these rows read-only and
# publishes payloads to Redis; nothing here writes poll state, which is what
# keeps MySQL out of the poller's write path.
# ---------------------------------------------------------------------------

def get_external_subscription_ids_for_admin(db: Session, admin_id: int) -> List[int]:
    """Enabled subscription ids assigned to one admin, oldest first.

    The only query the subscription render path can make, and only on a Redis
    miss. Index-backed by ix_external_subscription_admins_admin_id.
    """
    rows = (
        db.query(ExternalSubscription.id)
        .join(
            external_subscription_admins_association,
            external_subscription_admins_association.c.external_subscription_id
            == ExternalSubscription.id,
        )
        .filter(
            external_subscription_admins_association.c.admin_id == admin_id,
            ExternalSubscription.enabled.is_(True),
        )
        .order_by(ExternalSubscription.id)
        .all()
    )
    return [r[0] for r in rows]


def _resolve_admins(db: Session, admin_ids: List[int]) -> List[Admin]:
    """Admin rows for the given ids, ignoring ids that do not exist.

    Silently dropping unknown ids rather than erroring: the picker sends what
    it last saw, and an admin deleted in between should not fail the save.
    """
    if not admin_ids:
        return []
    return db.query(Admin).filter(Admin.id.in_(set(admin_ids))).all()


def list_external_subscriptions(db: Session,
                                enabled_only: bool = False
                                ) -> List[ExternalSubscription]:
    """All registered external subscriptions, oldest first (stable order, so
    the appended hosts keep a stable position in every user's config)."""
    query = db.query(ExternalSubscription)
    if enabled_only:
        query = query.filter(ExternalSubscription.enabled.is_(True))
    return query.order_by(ExternalSubscription.id).all()


def get_external_subscription(db: Session, sub_id: int) -> Optional[ExternalSubscription]:
    return db.query(ExternalSubscription).filter_by(id=sub_id).first()


def get_external_subscription_by_url(db: Session, url: str) -> Optional[ExternalSubscription]:
    """Duplicate guard for the router. The table carries no unique index on
    `url` on purpose (see migration c2f7a91d4e08), so uniqueness is enforced
    here."""
    return db.query(ExternalSubscription).filter_by(url=url).first()


def create_external_subscription(db: Session,
                                 new_sub: ExternalSubscriptionCreate
                                 ) -> ExternalSubscription:
    dbsub = ExternalSubscription(
        name=new_sub.name,
        url=new_sub.url,
        hwid=new_sub.hwid,
        enabled=new_sub.enabled,
        admins=_resolve_admins(db, new_sub.admin_ids),
    )
    db.add(dbsub)
    mark_all_copies_dirty(db)
    db.commit()
    db.refresh(dbsub)
    return dbsub


def update_external_subscription(db: Session,
                                 dbsub: ExternalSubscription,
                                 modified: ExternalSubscriptionModify
                                 ) -> ExternalSubscription:
    """Partial update — an omitted field means "leave unchanged".

    `hwid` switches on `model_fields_set` rather than `is not None`, because
    explicitly sending null has to mean "clear it, fall back to the poller's
    static identity" — a meaning an `is not None` guard cannot express.
    """
    if modified.name is not None:
        dbsub.name = modified.name
    if modified.url is not None:
        dbsub.url = modified.url
    if "hwid" in modified.model_fields_set:
        dbsub.hwid = modified.hwid
    if modified.enabled is not None:
        dbsub.enabled = modified.enabled
    # Replace-set semantics: the picker always sends the full list, and an
    # empty list means "nobody" — which is a legitimate state here, unlike the
    # host_admins junction where empty means "everybody".
    if modified.admin_ids is not None:
        dbsub.admins = _resolve_admins(db, modified.admin_ids)

    mark_all_copies_dirty(db)
    db.commit()
    db.refresh(dbsub)
    return dbsub


def remove_external_subscription(db: Session, dbsub: ExternalSubscription) -> None:
    """Delete a registry row.

    The poller's Redis payload keys are written with no TTL, so a deleted row
    would otherwise leave its payload behind forever. The router drops those
    keys explicitly after this returns; the render path also only ever asks
    for the ids it read from this table, so a leftover key is inert either
    way.
    """
    db.delete(dbsub)
    mark_all_copies_dirty(db)
    db.commit()


# ---------------------------------------------------------------------------
# Manual hosts (operator-authored proxy links, the poller-free twin of the
# external-subscription registry). Sudo-authored; served straight from here.
# ---------------------------------------------------------------------------

def get_manual_host_links_for_admin(db: Session, admin_id: int) -> List[str]:
    """Enabled manual-host links assigned to one admin, oldest first.

    The only query the subscription render path can make for manual hosts, and
    only on a Redis miss. Index-backed by ix_manual_host_admins_admin_id.
    """
    rows = (
        db.query(ManualHost.link)
        .join(
            manual_host_admins_association,
            manual_host_admins_association.c.manual_host_id == ManualHost.id,
        )
        .filter(
            manual_host_admins_association.c.admin_id == admin_id,
            ManualHost.enabled.is_(True),
        )
        .order_by(ManualHost.id)
        .all()
    )
    return [r[0] for r in rows]


def list_manual_hosts(db: Session) -> List[ManualHost]:
    """All registered manual hosts, oldest first (stable order, so the appended
    hosts keep a stable position in every user's config).

    Eager-loads admins so the sudo list view serializes each row's admin_ids
    without an N+1 lazy load per host.
    """
    return (
        db.query(ManualHost)
        .options(selectinload(ManualHost.admins))
        .order_by(ManualHost.id)
        .all()
    )


def get_manual_host(db: Session, host_id: int) -> Optional[ManualHost]:
    return db.query(ManualHost).filter_by(id=host_id).first()


def create_manual_host(db: Session, new_host: ManualHostCreate) -> ManualHost:
    dbhost = ManualHost(
        name=new_host.name,
        link=new_host.link,
        enabled=new_host.enabled,
        admins=_resolve_admins(db, new_host.admin_ids),
    )
    db.add(dbhost)
    mark_all_copies_dirty(db)
    db.commit()
    db.refresh(dbhost)
    return dbhost


def update_manual_host(db: Session,
                       dbhost: ManualHost,
                       modified: ManualHostModify) -> ManualHost:
    """Partial update — an omitted field means "leave unchanged"."""
    if modified.name is not None:
        dbhost.name = modified.name
    if modified.link is not None:
        dbhost.link = modified.link
    if modified.enabled is not None:
        dbhost.enabled = modified.enabled
    # Replace-set: the picker always sends the full list, and `[]` means
    # "nobody" (a legitimate state here, unlike the host_admins junction).
    if modified.admin_ids is not None:
        dbhost.admins = _resolve_admins(db, modified.admin_ids)

    mark_all_copies_dirty(db)
    db.commit()
    db.refresh(dbhost)
    return dbhost


def remove_manual_host(db: Session, dbhost: ManualHost) -> None:
    db.delete(dbhost)
    mark_all_copies_dirty(db)
    db.commit()


def get_user_device(db: Session, user_id: int, device_id: int) -> Optional[UserSubscriptionDevice]:
    """Fetch one device row scoped to a user, regardless of blocked/stale
    state — the router uses this for the block action, where we need to
    operate on the raw row even if it's already been filtered out of
    list_user_devices."""
    return (
        db.query(UserSubscriptionDevice)
        .filter_by(id=device_id, user_id=user_id)
        .first()
    )


def block_user_device(db: Session, dbdevice: UserSubscriptionDevice) -> UserSubscriptionDevice:
    """Flip a device row to blocked. Idempotent: already-blocked rows
    just commit a no-op so the API endpoint stays simple. There is no
    unblock counterpart — blocks are intentionally one-way at the API
    layer (per HANDOFF design decision)."""
    if not dbdevice.is_blocked:
        dbdevice.is_blocked = True
        mark_copy_dirty(db, dbdevice.user_id)  # the copy's blocked list
        db.commit()
        db.refresh(dbdevice)
    return dbdevice


def count_user_devices(db: Session, user_id: int) -> int:
    """Count slots held against the user's device_limit. Every non-blocked
    row holds a slot — including stale ones. The only way to free a slot
    is for the admin to block the device explicitly."""
    return (
        db.query(func.count(UserSubscriptionDevice.id))
        .filter(
            UserSubscriptionDevice.user_id == user_id,
            _active_device_filter(),
        )
        .scalar()
    ) or 0


def count_devices(
    db: Session,
    *,
    admin: Optional[Admin] = None,
    only_active_users: bool = False,
) -> int:
    """Count non-blocked devices across many users for the Statistics card.

    Scope behaviour (matches `get_users_count`):
      - admin=None        -> every user (sudo / unscoped view)
      - admin=<row>       -> only that admin's users (non-sudo, or sudo
                             filtering the panel by a specific admin)

    `only_active_users=True` narrows to users with status=active. Used
    for the "Active Users' Devices" half of the card; the "Total"
    half passes False so every status counts.

    Blocked devices are always excluded via _active_device_filter."""
    query = (
        db.query(func.count(UserSubscriptionDevice.id))
        .join(User, User.id == UserSubscriptionDevice.user_id)
        .filter(_active_device_filter())
    )
    if admin is not None:
        query = query.filter(User.admin == admin)
    if only_active_users:
        query = query.filter(User.status == UserStatus.active)
    return query.scalar() or 0


def claim_or_check_hwid(
    db: Session, user_id: int, hwid: str, device_limit: int
) -> str:
    """Device-limit policy check for one subscription fetch.

    Returns one of:
      'blocked' — HWID has an existing row with is_blocked=TRUE. Caller
                  should serve the 'Blok Edildiniz' announcement and
                  must NOT fall through to update_user_sub/telemetry.
      'matched' — HWID already has an unblocked slot (admin-prefilled
                  or previously auto-claimed). Caller should serve the
                  config and let record_subscription_device bump telemetry.
      'claimed' — HWID was unknown but a free slot existed. A new row was
                  INSERTed; caller should serve.
      'denied'  — HWID unknown and no free slot. Caller should serve the
                  device-limit announcement.

    Slots are released only by explicit admin block. Time is not a
    factor — a stale device keeps its slot indefinitely so a returning
    HWID always finds its row intact and never has to race a new device
    for the slot.

    Concurrency: two new HWIDs arriving in the same instant can both
    pass the count check before either INSERT lands, momentarily
    exceeding device_limit by 1. The unique constraint on
    (user_id, device_key) prevents duplicate slots for the same HWID.
    Bounded over-claim by 1; not amplified by stale-row reactivation
    anymore.
    """
    return claim_or_check_device_key(db, user_id, DEVICE_KEY_HWID_PREFIX + hwid, device_limit)


MICKY_MAX_REBINDS = 3
"""Reinstalls (same M-Device-Id, new device key) a Micky slot may absorb per
24 h window. An id copied onto several devices would otherwise flip the slot's
key on every refresh; past the budget the NEW key is refused (device_limit)
and the currently bound key keeps working."""
MICKY_REBIND_WINDOW = timedelta(hours=24)


def claim_or_check_micky_device(
    db: Session, user_id: int, device_id_key: Optional[str], pub_key: str, device_limit: int
) -> str:
    """Slot check for a Micky request (docs/micky-protocol.md "Device identity").

    `device_id_key` is "id:<M-Device-Id>" or None; `pub_key` is the request's
    "pk:<hash>". Outcomes: "matched", "claimed", "blocked", "denied" — the same
    contract as claim_or_check_device_key, so the router is unchanged:

      * no id (iOS, older builds): exactly the "pk:" behaviour of before;
      * known id, same key: matched;
      * known id, NEW key (reinstall): rebind — the slot keeps its place and
        its key is replaced, within MICKY_MAX_REBINDS per window, else denied;
      * unknown id whose key already holds a "pk:" slot (an older build that
        updated): that slot is upgraded to the id in place, no new slot;
      * otherwise a new slot, if one is free.
    Same concurrency caveat as claim_or_check_hwid.
    """
    if device_id_key is None:
        return claim_or_check_device_key(db, user_id, pub_key, device_limit)

    row = db.query(UserSubscriptionDevice).filter_by(user_id=user_id, device_key=device_id_key).first()
    if row:
        if row.is_blocked:
            return "blocked"
        if row.bound_pub_key in (None, pub_key):
            if row.bound_pub_key is None:
                row.bound_pub_key = pub_key
                db.commit()
            return "matched"
        now = datetime.utcnow()
        if row.rebind_window_start is None or now - row.rebind_window_start >= MICKY_REBIND_WINDOW:
            row.rebind_window_start, row.rebind_count = now, 0
        if row.rebind_count >= MICKY_MAX_REBINDS:
            db.commit()
            return "denied"
        row.rebind_count += 1
        row.bound_pub_key = pub_key
        db.commit()
        return "matched"

    legacy = db.query(UserSubscriptionDevice).filter_by(user_id=user_id, device_key=pub_key).first()
    if legacy:
        if legacy.is_blocked:
            return "blocked"
        legacy.device_key = device_id_key
        legacy.bound_pub_key = pub_key
        db.commit()
        return "matched"

    if count_user_devices(db, user_id) >= device_limit:
        return "denied"
    db.add(UserSubscriptionDevice(
        user_id=user_id,
        device_key=device_id_key,
        bound_pub_key=pub_key,
        first_seen_at=None,
        last_seen_at=None,
        fetch_count=0,
    ))
    db.commit()
    return "claimed"


def claim_or_check_device_key(
    db: Session, user_id: int, device_key: str, device_limit: int
) -> str:
    """claim_or_check_hwid for any exact identity ('hwid:' or 'pk:' key);
    same outcomes and the same concurrency caveat."""
    existing = (
        db.query(UserSubscriptionDevice)
        .filter_by(user_id=user_id, device_key=device_key)
        .first()
    )
    if existing:
        if existing.is_blocked:
            return "blocked"
        return "matched"

    current = count_user_devices(db, user_id)
    if current >= device_limit:
        return "denied"

    db.add(UserSubscriptionDevice(
        user_id=user_id,
        device_key=device_key,
        first_seen_at=None,
        last_seen_at=None,
        fetch_count=0,
    ))
    db.commit()
    return "claimed"
