import logging
from collections import Counter, defaultdict
from datetime import datetime, timedelta
from typing import List, Optional

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Request, status
from fastapi.security import OAuth2PasswordRequestForm
from sqlalchemy.exc import IntegrityError

from app import xray
from app.db import GetDB, Session, crud, get_db
from app.dependencies import get_admin_by_username, validate_admin
from app.models.admin import (
    Admin,
    AdminClientApps,
    AdminCreate,
    AdminListEntry,
    AdminModify,
    AdminPartialModify,
    AdminsUsageResponse,
    AdminStatsPoint,
    AdminStatsSeries,
    AdminUsageAdjust,
    AdminUsageAdjustApplied,
    AdminUsageAdjustResponse,
    ClientAppsResponse,
    ClientAppStats,
    NamedCount,
    Token,
    TopUserEntry,
    TopUsersResponse,
)
from app.utils import cache, report, responses
from app.utils.jwt import create_admin_token
from config import LOGIN_NOTIFY_WHITE_LIST, SUDOERS

router = APIRouter(tags=["Admin"], prefix="/api", responses={401: responses._401})

logger = logging.getLogger(__name__)

# Admin-usage snapshots are bucketed at the top of each UTC hour by the
# out-of-process Rust stats collector. The dashboard renders them in
# Asia/Ashgabat (UTC+5, no DST) local time so the breakdown lines up
# with the operator's day.
ASHGABAT_OFFSET = timedelta(hours=5)

# Default admin-usage window when the caller omits start/end.
ADMIN_USAGE_DEFAULT_DAYS = 30
# Hard cap on the requested span. A pathologically long range would make
# the single worker diff an unbounded number of hourly buckets, so we
# clamp (never 500) any request that asks for more than this.
ADMIN_USAGE_MAX_DAYS = 366
# Rows per list on the Statistics page's Users tab. Fixed rather than a query
# parameter: the tab shows a fixed-size Top 10, and an open-ended limit would
# let one request rank every user on the panel.
TOP_USERS_LIMIT = 10


def _resolve_usage_window(start: Optional[str], end: Optional[str]):
    """Parse the Statistics page's `start`/`end` into the window a query reads.

    Inputs are ISO datetimes in Asia/Ashgabat local time, naive (a tzinfo, if
    sent, is dropped). Defaults to the last ADMIN_USAGE_DEFAULT_DAYS; a
    reversed range is swapped rather than rejected; an over-long span is
    clamped to ADMIN_USAGE_MAX_DAYS anchored on `end` (never 500 — an abusive
    range must not tie up the single worker). Returns
    (start_local, end_local, start_utc, end_utc). Shared by /admins/usage,
    /admins/usage/top-users and /admins/usage/clients so every part of the
    page reads the same window.
    """
    now_local = datetime.utcnow() + ASHGABAT_OFFSET

    def parse_dt(value, default):
        if not value:
            return default
        try:
            return datetime.fromisoformat(value)
        except ValueError:
            raise HTTPException(
                status_code=422, detail="Invalid datetime, expected ISO 8601")

    start_local = parse_dt(
        start, now_local - timedelta(days=ADMIN_USAGE_DEFAULT_DAYS))
    end_local = parse_dt(end, now_local)
    if start_local.tzinfo is not None:
        start_local = start_local.replace(tzinfo=None)
    if end_local.tzinfo is not None:
        end_local = end_local.replace(tzinfo=None)

    if start_local > end_local:
        start_local, end_local = end_local, start_local

    max_span = timedelta(days=ADMIN_USAGE_MAX_DAYS)
    if end_local - start_local > max_span:
        start_local = end_local - max_span

    return (start_local, end_local,
            start_local - ASHGABAT_OFFSET, end_local - ASHGABAT_OFFSET)


def _resolve_usage_scope(db: Session, admin: Admin,
                         admin_username: Optional[str]) -> Optional[int]:
    """Whose users a Statistics query covers, as an admin id or None for all.

    Non-sudo callers are locked to their own id and `admin_username` is
    ignored — they can never view another admin. A sudo caller sees every
    admin by default (None), or one admin when `admin_username` is given;
    an unknown name is a 404. Shared by /admins/usage,
    /admins/usage/top-users and /admins/usage/clients.
    """
    if not admin.is_sudo:
        return admin.id
    if admin_username:
        target = crud.get_admin(db, admin_username)
        if not target:
            raise HTTPException(status_code=404, detail="Admin not found")
        return target.id
    return None


def get_client_ip(request: Request) -> str:
    """Extract the client's IP address from the request headers or client."""
    forwarded_for = request.headers.get("X-Forwarded-For")
    if forwarded_for:
        return forwarded_for.split(",")[0].strip()
    if request.client:
        return request.client.host
    return "Unknown"


@router.post("/admin/token", response_model=Token)
def admin_token(
    request: Request,
    form_data: OAuth2PasswordRequestForm = Depends(),
    db: Session = Depends(get_db),
):
    """Authenticate an admin and issue a token."""
    client_ip = get_client_ip(request)

    dbadmin = validate_admin(db, form_data.username, form_data.password)
    if not dbadmin:
        report.login(form_data.username, form_data.password, client_ip, False)
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Incorrect username or password",
            headers={"WWW-Authenticate": "Bearer"},
        )

    if client_ip not in LOGIN_NOTIFY_WHITE_LIST:
        report.login(form_data.username, "🔒", client_ip, True)

    return Token(access_token=create_admin_token(form_data.username, dbadmin.is_sudo))


@router.post(
    "/admin",
    response_model=Admin,
    responses={403: responses._403, 409: responses._409},
)
def create_admin(
    new_admin: AdminCreate,
    db: Session = Depends(get_db),
    admin: Admin = Depends(Admin.check_sudo_admin),
):
    """Create a new admin if the current admin has sudo privileges."""
    # Checked here, not left to the unique index: prod's collation ignores case
    # but SQLite's doesn't, and an env sudoer has no row at all.
    name = new_admin.username.lower()
    if name in {s.lower() for s in SUDOERS} or crud.get_admin_any_case(db, new_admin.username):
        raise HTTPException(status_code=409, detail="Admin already exists")
    try:
        dbadmin = crud.create_admin(db, new_admin)
    except IntegrityError:
        db.rollback()
        raise HTTPException(status_code=409, detail="Admin already exists")

    logger.warning("admin created: admin=%s sudo=%s by=%s",
                   dbadmin.username, dbadmin.is_sudo, admin.username)
    return dbadmin


def _regenerate_links_bg(admin_username: str) -> None:
    """Background worker (own session): rebuild the admin's Happ and Micky
    links on its current domain."""
    with GetDB() as db:
        try:
            n = crud.regenerate_happ_subscriptions(db, admin_username)
            logger.info("domain change: regenerated %d happ/micky links (admin=%s)", n, admin_username)
        except Exception:
            logger.exception("domain change: regenerating links failed (admin=%s)", admin_username)


def _regenerate_on_domain_change(bg: BackgroundTasks, admin, was_subscription_url) -> None:
    """A new subscription domain takes effect for the admin's Happ and Micky
    users at once: their stored links are rebuilt on it (old links keep
    working). Micky apps then pick the new link up from `new-url` in their
    next sealed answer, even when they only reach the old domain."""
    if admin.subscription_url != was_subscription_url:
        bg.add_task(_regenerate_links_bg, admin.username)


@router.put(
    "/admin/{username}",
    response_model=Admin,
    responses={403: responses._403, 404: responses._404},
)
def modify_admin(
    modified_admin: AdminModify,
    bg: BackgroundTasks,
    current_admin: Admin = Depends(Admin.check_sudo_admin),
    dbadmin: Admin = Depends(get_admin_by_username),
    db: Session = Depends(get_db),
):
    """Modify an existing admin's details."""
    # `current_admin` is declared FIRST on purpose: FastAPI solves dependencies in
    # signature order, so putting `get_admin_by_username` first would answer an
    # UNAUTHENTICATED request with 404 for a missing admin and 401 for an
    # existing one — an admin-username oracle. Auth first makes both 401.
    # A sudo may edit another sudo's account (all sudos are superusers) — the
    # old "another sudoer / use marzban-cli" block was removed by operator request.

    was_can_announce = dbadmin.can_announce
    was_subscription_url = dbadmin.subscription_url
    updated_admin = crud.update_admin(db, dbadmin, modified_admin)
    _regenerate_on_domain_change(bg, updated_admin, was_subscription_url)

    # Permission revoked -> append a disabled announcement row so this admin's
    # users fall back to the global one on their next fetch, and drop the cache.
    if was_can_announce and not updated_admin.can_announce:
        crud.disable_announcement(
            db, updated_admin.id, created_by_admin_id=current_admin.id)
        cache.invalidate_announcement(updated_admin.id)

    return updated_admin


@router.patch(
    "/admin/{username}",
    response_model=Admin,
    responses={400: responses._400, 403: responses._403, 404: responses._404},
)
def partial_modify_admin(
    modified_admin: AdminPartialModify,
    bg: BackgroundTasks,
    current_admin: Admin = Depends(Admin.check_sudo_admin),
    dbadmin: Admin = Depends(get_admin_by_username),
    db: Session = Depends(get_db),
):
    """Update only the fields present in the body; omit the rest.

    Exists because PUT takes a *full* `AdminModify` body, and `update_admin`
    writes every flag it carries — so a caller that wants to change one flag has
    to echo back every other one, and any it forgets silently resets to that
    field's default. The dashboard used to do exactly that, once per toggle.
    With PATCH a caller sends one field and nothing else can move.

    Guards mirror PUT exactly; the two differ only in which fields they write.
    `current_admin` is resolved before `get_admin_by_username` so an
    unauthenticated request cannot distinguish a missing admin (404) from an
    existing one (401).

    Note: for `telegram_id`, `discord_webhook` and `subscription_url`, null
    still means "leave unchanged" rather than "clear" — `crud.partial_update_admin`
    is shared with `marzban-cli admin update`, whose prompts submit an empty
    value for "keep what's there". Clearing one of those is a CLI operation.

    A sudo may edit another sudo's account (all sudos are superusers) — the old
    "another sudoer / use marzban-cli" block was removed by operator request.
    """

    was_can_announce = dbadmin.can_announce
    was_subscription_url = dbadmin.subscription_url
    updated_admin = crud.partial_update_admin(db, dbadmin, modified_admin)
    _regenerate_on_domain_change(bg, updated_admin, was_subscription_url)

    # Permission revoked -> append a disabled announcement row so this admin's
    # users fall back to the global one on their next fetch, and drop the cache.
    if was_can_announce and not updated_admin.can_announce:
        crud.disable_announcement(
            db, updated_admin.id, created_by_admin_id=current_admin.id)
        cache.invalidate_announcement(updated_admin.id)

    return updated_admin


@router.delete(
    "/admin/{username}",
    responses={403: responses._403, 409: responses._409},
)
def remove_admin(
    current_admin: Admin = Depends(Admin.check_sudo_admin),
    dbadmin: Admin = Depends(get_admin_by_username),
    db: Session = Depends(get_db),
):
    """Remove an admin from the database."""
    # `current_admin` is declared FIRST on purpose: FastAPI solves dependencies in
    # signature order, so putting `get_admin_by_username` first would answer an
    # UNAUTHENTICATED request with 404 for a missing admin and 401 for an
    # existing one — an admin-username oracle. Auth first makes both 401.
    if dbadmin.is_sudo:
        raise HTTPException(
            status_code=403,
            detail="You're not allowed to delete sudo accounts. Use marzban-cli instead.",
        )

    # An admin that still owns users is not deleted: they would be left
    # ownerless. Its users go first ("Delete all users", DELETE
    # /admin/{u}/users). marzban-cli still deletes it and keeps its users.
    owned = crud.get_users_count(db, admin=dbadmin)
    if owned:
        raise HTTPException(
            status_code=409,
            detail=f"{dbadmin.username} still owns {owned} user(s). Delete them first.",
        )

    username = dbadmin.username
    try:
        crud.remove_admin(db, dbadmin)
    except IntegrityError:
        # It gained a user after the count: users.admin_id refuses the delete
        # (MySQL; SQLite doesn't enforce foreign keys).
        db.rollback()
        raise HTTPException(status_code=409, detail=f"{username} still owns users. Delete them first.")
    # ON DELETE CASCADE clears this admin's external-subscription links. With
    # no users left nobody reads its cached mapping, so dropping the cache is a
    # cheap safeguard (wholesale, like every invalidation of it).
    cache.invalidate_external_admin_ids()
    crud.touch_all_micky_copies(db)  # after the cache drop: copies render from it
    # WARNING, like "admin users purged" below: an INFO line is discarded.
    logger.warning("admin deleted: admin=%s by=%s", username, current_admin.username)
    return {"detail": "Admin removed successfully"}


@router.get("/admin", response_model=Admin)
def get_current_admin(admin: Admin = Depends(Admin.get_current)):
    """Retrieve the current authenticated admin."""
    return admin


@router.get(
    "/admins",
    response_model=List[AdminListEntry],
    responses={403: responses._403},
)
def get_admins(
    offset: Optional[int] = None,
    limit: Optional[int] = None,
    username: Optional[str] = None,
    db: Session = Depends(get_db),
    admin: Admin = Depends(Admin.check_sudo_admin),
):
    """Fetch a list of admins with optional filters for pagination and username."""
    counts = crud.get_admins_users_counts(db)
    return [AdminListEntry.model_validate(a).model_copy(update={"users_count": counts.get(a.id, 0)})
            for a in crud.get_admins(db, offset, limit, username)]


@router.get(
    "/admins/usage",
    response_model=AdminsUsageResponse,
    responses={403: responses._403},
)
def get_admins_usage(
    start: Optional[str] = None,
    end: Optional[str] = None,
    admin_username: Optional[str] = None,
    db: Session = Depends(get_db),
    admin: Admin = Depends(Admin.get_current),
):
    """Per-admin hourly traffic + activity over a date range.

    `start`/`end` are ISO datetimes interpreted in Asia/Ashgabat (UTC+5)
    local time; they default to the last 30 days (now-30d .. now). Each
    point is a top-of-the-hour bucket: `traffic` is the monotonic diff
    of cumulative usage against the previous existing bucket (clamped at
    0; the first in-window bucket diffs against the pre-window baseline),
    while `devices` and `users` are stored point-in-time gauges. Sudo
    admins see every admin; a non-sudo admin sees only their own usage.

    An over-long requested span is clamped to `ADMIN_USAGE_MAX_DAYS`
    (anchored on `end`) rather than rejected, so an abusive range can
    never tie up the single worker; a `start` after `end` is swapped
    rather than rejected.
    """
    start_local, end_local, start_utc, end_utc = _resolve_usage_window(start, end)
    admin_id = _resolve_usage_scope(db, admin, admin_username)
    rows = crud.get_admin_hourly_stats(db, start_utc, end_utc, admin_id)
    usernames = {a.id: a.username for a in crud.get_admins(db)}

    per_admin = defaultdict(list)
    for row in rows:
        per_admin[row.admin_id].append(row)

    # Server-side adaptive granularity. A wide range (e.g. a month for every
    # admin) is rolled up to one point per local *day* instead of per hour,
    # shrinking the JSON payload + chart render ~24x. The rollup matches the
    # dashboard's former client-side aggregation exactly: traffic = SUM of the
    # hourly diffs, device/user gauges = ROUND(AVG) over the bucket. Narrow
    # ranges (<= HOURLY_MAX_SPAN_DAYS) stay hourly. The frontend reads the
    # returned `granularity` and drops the hourly toggle when it's "daily"
    # (the hourly points aren't sent, so it can't offer that view).
    HOURLY_MAX_SPAN_DAYS = 2
    span_days = (end_local - start_local).days
    granularity = "hourly" if span_days <= HOURLY_MAX_SPAN_DAYS else "daily"

    def _bucket_key(local_dt: datetime) -> str:
        if granularity == "daily":
            return local_dt.strftime("%Y-%m-%dT00:00:00")
        return local_dt.strftime("%Y-%m-%dT%H:00:00")

    usages = []
    # Include every admin, even those with no rows at all (LEFT JOIN -> 0).
    candidate_ids = (
        [admin_id] if admin_id is not None else list(usernames.keys())
    )
    for aid in candidate_ids:
        # rows arrive ordered by (admin_id, recorded_at) ascending.
        snaps = per_admin.get(aid, [])
        prev_value = None
        # bucket key -> [traffic_sum, device_sum, user_sum, count]. A plain
        # dict preserves chronological insertion order (snaps are sorted asc),
        # and a bucket only exists once it has >= 1 in-window snap, so the
        # averaging divisor is never zero.
        buckets: dict = {}
        for snap in snaps:
            in_window = start_utc <= snap.recorded_at <= end_utc
            if in_window:
                if prev_value is None:
                    traffic = 0
                else:
                    traffic = max(0, int(snap.users_usage) - prev_value)
                key = _bucket_key(snap.recorded_at + ASHGABAT_OFFSET)
                b = buckets.get(key)
                if b is None:
                    buckets[key] = [traffic, int(snap.device_count),
                                    int(snap.user_count), 1]
                else:
                    b[0] += traffic
                    b[1] += int(snap.device_count)
                    b[2] += int(snap.user_count)
                    b[3] += 1
            prev_value = int(snap.users_usage)

        points = [
            AdminStatsPoint(
                t=key,
                traffic=b[0],
                devices=round(b[1] / b[3]),
                users=round(b[2] / b[3]),
            )
            for key, b in buckets.items()
        ]
        usages.append(AdminStatsSeries(
            admin_id=aid,
            username=usernames.get(aid, f"#{aid}"),
            total_traffic=sum(b[0] for b in buckets.values()),
            points=points,
        ))

    usages.sort(key=lambda u: u.total_traffic, reverse=True)
    return AdminsUsageResponse(
        start=start_local.strftime("%Y-%m-%dT%H:00:00"),
        end=end_local.strftime("%Y-%m-%dT%H:00:00"),
        usages=usages,
        granularity=granularity,
    )


@router.get(
    "/admins/usage/top-users",
    response_model=TopUsersResponse,
    responses={403: responses._403, 404: responses._404},
)
def get_top_users(
    start: Optional[str] = None,
    end: Optional[str] = None,
    admin_username: Optional[str] = None,
    db: Session = Depends(get_db),
    admin: Admin = Depends(Admin.get_current),
):
    """The users with the most active devices and the most traffic in a
    range, for the Statistics page's Users tab.

    Lives next to /admins/usage because it is the same page, the same window
    (`start`/`end` are Asia/Ashgabat local, defaulted, swapped and clamped
    identically) and the same scope rules: a non-sudo admin gets its own
    users, a sudo admin gets every user on the panel, or one admin's users
    when `admin_username` is given. Each list holds at most TOP_USERS_LIMIT
    rows, descending.

    `by_devices` counts non-blocked devices — the same count as every other
    device figure in the UI. `by_traffic` sums real per-user metered traffic
    inside the range and does NOT include manual admin usage adjustments,
    which are admin-level (see crud.get_top_users_by_traffic).
    """
    start_local, end_local, start_utc, end_utc = _resolve_usage_window(start, end)
    admin_id = _resolve_usage_scope(db, admin, admin_username)

    by_devices = [
        TopUserEntry(username=u, admin=a, value=v)
        for u, a, v in crud.get_top_users_by_devices(db, admin_id, TOP_USERS_LIMIT)
    ]
    by_traffic = [
        TopUserEntry(username=u, admin=a, value=v)
        for u, a, v in crud.get_top_users_by_traffic(
            db, admin_id, start_utc, end_utc, TOP_USERS_LIMIT)
    ]
    return TopUsersResponse(
        start=start_local.strftime("%Y-%m-%dT%H:00:00"),
        end=end_local.strftime("%Y-%m-%dT%H:00:00"),
        by_devices=by_devices,
        by_traffic=by_traffic,
    )


def _merge_spellings(names) -> dict:
    """Stored name -> the name an app (or platform) is shown under.

    device_fingerprint.parse keeps a generic User-Agent's own casing, so one
    app can be stored as "v2box" and "V2Box", and Micky's two builds as
    "Micky" and "MickyX" (micky.is_micky treats both as Micky). MickyX counts
    as Micky; other names that differ only in case merge under their most
    common spelling. None (no name) stays None.
    """
    def key(name: str) -> str:
        k = name.lower()
        return "micky" if k in ("micky", "mickyx") else k

    spellings = defaultdict(Counter)
    for name in names:
        if name is not None:
            spellings[key(name)][name] += 1
    shown = {}
    for k, seen in spellings.items():
        # Most common spelling; on a tie the capitalised one ("Android", not "android").
        best = "Micky" if k == "micky" else max(
            seen.items(), key=lambda kv: (kv[1], kv[0] != kv[0].lower(), kv[0]))[0]
        for name in seen:
            shown[name] = best
    return shown


def _named_counts(counter: Counter) -> List[NamedCount]:
    """Descending by devices; ties by name, a missing name last."""
    ranked = sorted(counter.items(), key=lambda kv: (-kv[1], kv[0] is None, kv[0] or ""))
    return [NamedCount(name=name, devices=n) for name, n in ranked]


@router.get(
    "/admins/usage/clients",
    response_model=ClientAppsResponse,
    responses={403: responses._403, 404: responses._404},
)
def get_client_apps(
    start: Optional[str] = None,
    end: Optional[str] = None,
    admin_username: Optional[str] = None,
    db: Session = Depends(get_db),
    admin: Admin = Depends(Admin.get_current),
):
    """Which client applications (Happ, Micky, V2Box, ...) the devices in a
    range use, overall and per admin, for the Statistics page's Apps and
    Admins tabs.

    Same window and scope rules as /admins/usage/top-users. A device counts
    when it was in use during the range (crud.get_client_app_devices) and is
    not blocked; it is "new" when its first fetch falls inside the range. The
    app is the name device_fingerprint.parse read from the User-Agent, with
    spellings of one app merged (_merge_spellings; None when it read none).

    Only fetches that record a device are seen: the main subscription link
    does, the typed links (/{token}/{client_type}, e.g. Outline) do not. So
    "devices" here is not the page's Devices gauge (every non-blocked slot,
    averaged per hour, ownerless users left out), and past ranges are read
    with today's blocks, owners and deletions.
    """
    start_local, end_local, start_utc, end_utc = _resolve_usage_window(start, end)
    admin_id = _resolve_usage_scope(db, admin, admin_username)
    rows = crud.get_client_app_devices(db, admin_id, start_utc, end_utc)
    app_names = _merge_spellings(client for _, _, client, _, _ in rows)
    platform_names = _merge_spellings(platform for _, _, _, platform, _ in rows)

    devices = Counter()
    new_devices = Counter()
    users = defaultdict(set)
    platforms = defaultdict(Counter)
    per_admin = defaultdict(Counter)
    for owner, user_id, client, platform, first_seen in rows:
        client, platform = app_names.get(client), platform_names.get(platform)
        devices[client] += 1
        if first_seen >= start_utc:
            new_devices[client] += 1
        users[client].add(user_id)
        platforms[client][platform] += 1
        per_admin[owner][client] += 1

    clients = [
        ClientAppStats(
            name=c.name,
            devices=c.devices,
            users=len(users[c.name]),
            new_devices=new_devices[c.name],
            platforms=_named_counts(platforms[c.name]),
        )
        for c in _named_counts(devices)
    ]
    admins = sorted(
        (AdminClientApps(username=owner, devices=sum(counts.values()),
                         clients=_named_counts(counts))
         for owner, counts in per_admin.items()),
        key=lambda a: (-a.devices, a.username is None, a.username or ""),
    )
    return ClientAppsResponse(
        start=start_local.strftime("%Y-%m-%dT%H:00:00"),
        end=end_local.strftime("%Y-%m-%dT%H:00:00"),
        devices=len(rows),
        users=len({user_id for _, user_id, _, _, _ in rows}),
        new_devices=sum(new_devices.values()),
        clients=clients,
        admins=admins,
    )


def _restart_all_cores() -> None:
    """Rebuild the Xray config from the DB and restart the local core plus every
    connected node.

    Extracted because the bulk user endpoints below each need it verbatim, and
    three copies of a restart policy is two that will not get updated together.
    Note this drops EVERY user on the panel briefly, not only the admin's whose
    users changed — the calling UI has to say so.
    """
    startup_config = xray.config.include_db_users()
    xray.core.restart(startup_config)
    for node_id, node in list(xray.nodes.items()):
        if node.connected:
            xray.operations.restart_node(node_id, startup_config)


@router.post("/admin/{username}/users/disable", responses={403: responses._403, 404: responses._404})
def disable_all_active_users(
    dbadmin: Admin = Depends(get_admin_by_username),
    db: Session = Depends(get_db), admin: Admin = Depends(Admin.check_sudo_admin)
):
    """Disable all active users under a specific admin"""
    crud.disable_all_active_users(db=db, admin=dbadmin)
    _restart_all_cores()
    return {"detail": "Users successfully disabled"}


@router.post("/admin/{username}/users/activate", responses={403: responses._403, 404: responses._404})
def activate_all_disabled_users(
    dbadmin: Admin = Depends(get_admin_by_username),
    db: Session = Depends(get_db), admin: Admin = Depends(Admin.check_sudo_admin)
):
    """Activate all disabled users under a specific admin"""
    crud.activate_all_disabled_users(db=db, admin=dbadmin)
    _restart_all_cores()
    return {"detail": "Users successfully activated"}


@router.delete(
    "/admin/{username}/users",
    responses={403: responses._403, 404: responses._404},
)
def remove_admin_users(
    # Sudo FIRST: FastAPI resolves dependencies in signature order, so putting
    # get_admin_by_username first would answer an unauthenticated request with
    # 404 for a missing admin and 401 for an existing one — an admin-username
    # oracle. `remove_admin` above does the same; the three other
    # `/admin/{username}/...` routes in this module do NOT and still carry that
    # oracle (a fix for them is out of scope here, but it is real).
    current_admin: Admin = Depends(Admin.check_sudo_admin),
    dbadmin: Admin = Depends(get_admin_by_username),
    db: Session = Depends(get_db),
):
    """Permanently delete every user owned by a specific admin. SUDO ONLY.

    Deliberately separate from `DELETE /api/admin/{username}`, which removes
    only the admin row and refuses (409) while the admin still owns users.
    Removing an admin *and* its customers is therefore two explicit calls, so
    neither can surprise the operator into doing the other.

    A sudo admin is refused outright with 403, mirroring
    `DELETE /api/admin/{username}`, which refuses the same condition with the
    same status. The sudo accounts own the bulk of the panel's users and no
    confirmation dialog is worth that blast radius. Use `marzban-cli` if it is
    genuinely meant.
    """
    if dbadmin.is_sudo:
        raise HTTPException(
            status_code=403,
            detail="You're not allowed to delete a sudo admin's users. "
                   "Use marzban-cli instead.",
        )

    # Captured before the call: crud commits, which expires `dbadmin`, and
    # re-reading it afterwards would re-SELECT (and raise ObjectDeletedError if
    # another sudo removed the admin meanwhile).
    username = dbadmin.username
    deleted_ids = crud.remove_admin_users(db=db, dbadmin=dbadmin)

    # Logged immediately, BEFORE the restart below — the deletion is already
    # committed and irreversible at this point, so an exception out of
    # include_db_users()/core.restart() must not be able to lose the only
    # record that it happened.
    #
    # WARNING, not INFO, on purpose: this module's logger is
    # `logging.getLogger(__name__)`, and uvicorn configures only its own
    # `uvicorn*` loggers, leaving the root at WARNING with no handler — an INFO
    # line here is discarded outright (which is why the usage-adjust audit line
    # below is invisible).
    logger.warning(
        "admin users purged: admin=%s count=%d by=%s",
        username, len(deleted_ids), current_admin.username,
    )

    if deleted_ids:
        # Block verdicts are cached per user id for 30 minutes with no other
        # expiry path; the users they describe no longer exist.
        cache.invalidate_user_blocks(deleted_ids)
        # Rebuild and restart once, not per user: the users are gone from the
        # DB but still provisioned in every running core, and a per-user
        # xray.operations.remove_user would spawn one fire-and-forget thread
        # per inbound per node (unbounded, errors swallowed) and can exhaust
        # the DB pool.
        _restart_all_cores()

    removed = len(deleted_ids)
    return {"detail": f"{removed} user(s) successfully deleted", "removed": removed}


@router.post(
    "/admin/usage/reset/{username}",
    response_model=Admin,
    responses={403: responses._403},
)
def reset_admin_usage(
    dbadmin: Admin = Depends(get_admin_by_username),
    db: Session = Depends(get_db),
    current_admin: Admin = Depends(Admin.check_sudo_admin)
):
    """Resets usage of admin."""
    return crud.reset_admin_usage(db, dbadmin)


@router.post(
    "/admin/usage/adjust/{username}",
    response_model=AdminUsageAdjustResponse,
    responses={400: responses._400, 403: responses._403, 404: responses._404},
)
def adjust_admin_usage(
    body: AdminUsageAdjust,
    # current_admin FIRST (oracle-avoidance): FastAPI solves dependencies in
    # signature order, so putting get_admin_by_username first would answer an
    # unauthenticated request 404 for a missing admin and 401 for an existing
    # one — an admin-username oracle. Auth first makes both 401.
    current_admin: Admin = Depends(Admin.check_sudo_admin),
    dbadmin: Admin = Depends(get_admin_by_username),
    db: Session = Depends(get_db),
):
    """Add traffic to an admin on chosen local days (sudo only).

    Corrects usage this panel could not meter — external-subscription traffic
    is metered on the remote panel and is invisible here. Each `{date, gb}` is
    an Asia/Ashgabat local day and an amount in GiB; the amount is spread
    across that day's hourly snapshots and carried forward (see
    crud.adjust_admin_usage). Only fully-elapsed past days that already have
    snapshots are accepted.
    """
    now_local_day = (datetime.utcnow() + ASHGABAT_OFFSET).date()
    parsed: List = []
    for item in body.adjustments:
        if item.date >= now_local_day:
            raise HTTPException(
                status_code=400,
                detail=f"{item.date.isoformat()} is today or in the future; "
                       "only fully-elapsed past days can be adjusted")
        parsed.append((item.date, int(round(item.gb * 1024 ** 3))))

    try:
        result = crud.adjust_admin_usage(
            db, dbadmin, parsed, actor_id=current_admin.id)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))

    logger.info("admin usage adjusted: admin=%s by=%s total=%d bytes days=%s",
                dbadmin.username, current_admin.username,
                result["total_added_bytes"],
                ",".join(d.isoformat() for d, _ in result["applied"]))

    return AdminUsageAdjustResponse(
        detail="Usage adjusted",
        admin_id=dbadmin.id,
        username=dbadmin.username,
        total_added_bytes=result["total_added_bytes"],
        applied=[AdminUsageAdjustApplied(date=d.isoformat(), added_bytes=a)
                 for d, a in result["applied"]],
    )


@router.get(
    "/admin/usage/{username}",
    response_model=int,
    responses={403: responses._403},
)
def get_admin_usage(
    dbadmin: Admin = Depends(get_admin_by_username),
    current_admin: Admin = Depends(Admin.check_sudo_admin)
):
    """Retrieve the usage of given admin."""
    return dbadmin.users_usage
