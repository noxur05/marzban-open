from datetime import datetime, timedelta, timezone
from typing import List, Optional, Union

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Query
from sqlalchemy.exc import IntegrityError

from app import logger, xray
from app.db import GetDB, Session, crud, get_db
from app.dependencies import get_expired_users_list, get_validated_user, validate_dates
from app.models.admin import Admin
from app.models.user import (
    ClientType,
    UserCreate,
    UserModify,
    UserResponse,
    UsersResponse,
    UserStatus,
    UsersUsagesResponse,
    UserUsagesResponse,
)
from app.utils import micky, report, responses
from config import SUDOERS

router = APIRouter(tags=["User"], prefix="/api", responses={401: responses._401})

# Upper bound on device_limit for a happ_only admin. UserCreate.device_limit is
# only `ge=1` server-side — the 20 in the dashboard's number input is a
# client-side hint that curl ignores — so without this a restricted admin could
# POST device_limit=999999 and get a nominally-compliant but effectively
# unlimited user, defeating the restriction entirely. Mirrors the dashboard's
# max={20} (app/dashboard/src/components/UserDialog.tsx). Deliberately NOT
# applied to sudo or unrestricted admins, so their behaviour is unchanged.
HAPP_ONLY_MAX_DEVICE_LIMIT = 20


def _check_happ_only_device_cap(device_limit: Optional[int]) -> None:
    """Refuse a device_limit above the happ_only ceiling. Shared by create and
    modify so the cap cannot be sidestepped by creating a compliant user and
    then raising its limit with a PUT."""
    if device_limit is not None and device_limit > HAPP_ONLY_MAX_DEVICE_LIMIT:
        raise HTTPException(
            status_code=400,
            detail=f"device_limit may not exceed {HAPP_ONLY_MAX_DEVICE_LIMIT}",
        )


@router.post("/user", response_model=UserResponse, responses={400: responses._400, 409: responses._409})
def add_user(
    new_user: UserCreate,
    bg: BackgroundTasks,
    db: Session = Depends(get_db),
    admin: Admin = Depends(Admin.get_current),
):
    """
    Add a new user

    - **username**: 3 to 32 characters, can include a-z, 0-9, and underscores.
    - **status**: User's status, defaults to `active`. Special rules if `on_hold`.
    - **expire**: UTC timestamp for account expiration. Use `0` for unlimited.
    - **data_limit**: Max data usage in bytes (e.g., `1073741824` for 1GB). `0` means unlimited.
    - **data_limit_reset_strategy**: Defines how/if data limit resets. `no_reset` means it never resets.
    - **proxies**: Dictionary of protocol settings (e.g., `vmess`, `vless`).
    - **inbounds**: Dictionary of protocol tags to specify inbound connections.
    - **note**: Optional text field for additional user information or notes.
    - **on_hold_timeout**: UTC timestamp when `on_hold` status should start or end.
    - **on_hold_expire_duration**: Duration (in seconds) for how long the user should stay in `on_hold` status.
    - **next_plan**: Next user plan (resets after use).
    """

    # TODO expire should be datetime instead of timestamp

    for proxy_type in new_user.proxies:
        if not xray.config.inbounds_by_protocol.get(proxy_type):
            raise HTTPException(
                status_code=400,
                detail=f"Protocol {proxy_type} is disabled on your server",
            )

    creating_admin = crud.get_admin(db, admin.username)

    # A Micky user's link and every answer to it need the panel's Micky keys;
    # without them the user could never be served (or shown a link).
    if new_user.client_type == ClientType.micky and not micky.is_configured():
        raise HTTPException(status_code=400, detail="Micky is not configured on this panel")

    # happ_only admins may create ONLY device-limited users — bound to Happ or
    # to Micky, the admin's choice. Sudo bypasses. Deliberately placed before the Outline grant check so a
    # restricted admin always gets the restriction's message, not a
    # permission message that doesn't explain the real constraint.
    if not admin.is_sudo and creating_admin and creating_admin.happ_only:
        if new_user.outline_enabled:
            # UserCreate.validate_outline already forbids outline_enabled
            # together with a device_limit, so this combination can never be
            # satisfied. Say so now instead of making them retry into a 422.
            raise HTTPException(
                status_code=400,
                detail="You may only create device-limited users, and Outline "
                       "access cannot be combined with a device limit",
            )
        if new_user.client_type not in (ClientType.happ, ClientType.micky) or new_user.device_limit is None:
            raise HTTPException(
                status_code=400,
                detail="You may only create device-limited users: client_type "
                       "must be 'happ' or 'micky' and device_limit must be set",
            )
        # Close the "comply nominally, then set device_limit=999999" hole. The
        # base field is only ge=1, so the ceiling has to be enforced here.
        _check_happ_only_device_cap(new_user.device_limit)

    # Outline access is privileged: only sudo or an admin granted
    # can_enable_outline may create an outline-enabled user.
    if new_user.outline_enabled and not (admin.is_sudo or admin.can_enable_outline):
        raise HTTPException(
            status_code=403,
            detail="You're not allowed to enable Outline access",
        )

    try:
        dbuser = crud.create_user(db, new_user, admin=creating_admin)
    except IntegrityError:
        db.rollback()
        raise HTTPException(status_code=409, detail="User already exists")

    bg.add_task(xray.operations.add_user, dbuser=dbuser)
    user = UserResponse.model_validate(dbuser)
    report.user_created(user=user, user_id=dbuser.id, by=admin, user_admin=dbuser.admin)
    logger.info(f'New user "{dbuser.username}" added')
    return user


@router.get("/user/{username}", response_model=UserResponse, responses={403: responses._403, 404: responses._404})
def get_user(dbuser: UserResponse = Depends(get_validated_user)):
    """Get user information"""
    return dbuser


@router.put("/user/{username}", response_model=UserResponse, responses={400: responses._400, 403: responses._403, 404: responses._404})
def modify_user(
    modified_user: UserModify,
    bg: BackgroundTasks,
    db: Session = Depends(get_db),
    dbuser: UsersResponse = Depends(get_validated_user),
    admin: Admin = Depends(Admin.get_current),
):
    """
    Modify an existing user

    - **username**: Cannot be changed. Used to identify the user.
    - **status**: User's new status. Can be 'active', 'disabled', 'on_hold', 'limited', or 'expired'.
    - **expire**: UTC timestamp for new account expiration. Set to `0` for unlimited, `null` for no change.
    - **data_limit**: New max data usage in bytes (e.g., `1073741824` for 1GB). Set to `0` for unlimited, `null` for no change.
    - **data_limit_reset_strategy**: New strategy for data limit reset. Options include 'daily', 'weekly', 'monthly', or 'no_reset'.
    - **proxies**: Dictionary of new protocol settings (e.g., `vmess`, `vless`). Empty dictionary means no change.
    - **inbounds**: Dictionary of new protocol tags to specify inbound connections. Empty dictionary means no change.
    - **note**: New optional text for additional user information or notes. `null` means no change.
    - **on_hold_timeout**: New UTC timestamp for when `on_hold` status should start or end. Only applicable if status is changed to 'on_hold'.
    - **on_hold_expire_duration**: New duration (in seconds) for how long the user should stay in `on_hold` status. Only applicable if status is changed to 'on_hold'.
    - **next_plan**: Next user plan (resets after use).

    Note: Fields set to `null` or omitted will not be modified.
    """

    for proxy_type in modified_user.proxies:
        if not xray.config.inbounds_by_protocol.get(proxy_type):
            raise HTTPException(
                status_code=400,
                detail=f"Protocol {proxy_type} is disabled on your server",
            )

    # Enabling Outline access is privileged. Only the off->on transition is
    # gated (crud.update_user latches on->off to a no-op).
    if modified_user.outline_enabled and not dbuser.outline_enabled:
        if not (admin.is_sudo or admin.can_enable_outline):
            raise HTTPException(
                status_code=403,
                detail="You're not allowed to enable Outline access",
            )
        # Mirror UserCreate's outline<->device_limit exclusion after create:
        # device-limited users are metered per HWID, can never use Outline
        # (the device-limit gate blocks them), and their encrypted Happ
        # subscription_url would yield a broken ssconf link.
        if dbuser.device_limit is not None:
            raise HTTPException(
                status_code=400,
                detail="a device-limited user cannot have Outline access",
            )

    # Same exclusion from the other direction: adopting the device-limit policy
    # on a user who already has Outline. Checked here so it surfaces as a 400
    # rather than the 500 a ValueError out of crud.update_user would produce.
    if modified_user.client_type is not None and dbuser.outline_enabled:
        raise HTTPException(
            status_code=400,
            detail="an Outline-enabled user cannot take a device limit",
        )

    # Each subscription belongs to one app for good: switching Happ <-> Micky
    # would orphan the link and device slots every client holds. A legacy user
    # may still adopt either app once.
    if (modified_user.client_type is not None and dbuser.client_type is not None
            and modified_user.client_type != dbuser.client_type):
        raise HTTPException(status_code=400, detail="A user's app (Happ or Micky) can't be changed")
    # Only adopting Micky needs the keys; the dashboard resends client_type on
    # every edit of an existing Micky user, which must keep working.
    if (modified_user.client_type == ClientType.micky and dbuser.client_type != ClientType.micky
            and not micky.is_configured()):
        raise HTTPException(status_code=400, detail="Micky is not configured on this panel")

    # The happ_only ceiling applies to edits too; enforcing it only on create
    # let a restricted admin PUT device_limit=999999 onto an existing user.
    # Checked against the acting admin, like add_user. Sudo bypasses.
    if not admin.is_sudo:
        acting_admin = crud.get_admin(db, admin.username)
        if acting_admin and acting_admin.happ_only:
            _check_happ_only_device_cap(modified_user.device_limit)

    old_status = dbuser.status
    dbuser = crud.update_user(db, dbuser, modified_user)
    user = UserResponse.model_validate(dbuser)

    if user.status in [UserStatus.active, UserStatus.on_hold]:
        bg.add_task(xray.operations.update_user, dbuser=dbuser)
    else:
        bg.add_task(xray.operations.remove_user, dbuser=dbuser)

    bg.add_task(report.user_updated, user=user, user_admin=dbuser.admin, by=admin)

    logger.info(f'User "{user.username}" modified')

    if user.status != old_status:
        bg.add_task(
            report.status_change,
            username=user.username,
            status=user.status,
            user=user,
            user_admin=dbuser.admin,
            by=admin,
        )
        logger.info(
            f'User "{dbuser.username}" status changed from {old_status} to {user.status}'
        )

    return user


@router.delete("/user/{username}", responses={403: responses._403, 404: responses._404})
def remove_user(
    bg: BackgroundTasks,
    db: Session = Depends(get_db),
    dbuser: UserResponse = Depends(get_validated_user),
    admin: Admin = Depends(Admin.get_current),
):
    """Remove a user"""
    crud.remove_user(db, dbuser)
    bg.add_task(xray.operations.remove_user, dbuser=dbuser)

    bg.add_task(
            report.user_deleted, username=dbuser.username,
            user_admin=Admin.model_validate(dbuser.admin) if dbuser.admin else None,
            by=admin
        )

    logger.info(f'User "{dbuser.username}" deleted')
    return {"detail": "User successfully deleted"}


@router.post("/user/{username}/reset", response_model=UserResponse, responses={403: responses._403, 404: responses._404})
def reset_user_data_usage(
    bg: BackgroundTasks,
    db: Session = Depends(get_db),
    dbuser: UserResponse = Depends(get_validated_user),
    admin: Admin = Depends(Admin.get_current),
):
    """Reset user data usage"""
    dbuser = crud.reset_user_data_usage(db=db, dbuser=dbuser)
    if dbuser.status in [UserStatus.active, UserStatus.on_hold]:
        bg.add_task(xray.operations.add_user, dbuser=dbuser)

    user = UserResponse.model_validate(dbuser)
    bg.add_task(
        report.user_data_usage_reset, user=user, user_admin=dbuser.admin, by=admin
    )

    logger.info(f'User "{dbuser.username}"\'s usage was reset')
    return dbuser


@router.post("/user/{username}/reset-devices", response_model=UserResponse,
             responses={403: responses._403, 404: responses._404})
def reset_user_devices(
    # Sudo FIRST: FastAPI resolves dependencies in signature order, so a
    # non-sudo caller is refused before get_validated_user can answer 404 for
    # a missing username (a username oracle otherwise).
    admin: Admin = Depends(Admin.check_sudo_admin),
    db: Session = Depends(get_db),
    dbuser: UserResponse = Depends(get_validated_user),
):
    """Delete the user's bound devices, freeing their device slots. SUDO ONLY.

    Distinct from `/user/{username}/reset`, which clears traffic usage only and
    leaves bindings intact. Blocked devices are preserved — a block is one-way
    by design and deleting the row would silently undo it (see
    crud.reset_user_devices).

    Not offered to owning admins: device rows are what the
    statistics cards and device billing count, so a reset would let an admin
    erase its own metered devices. Only a sudo may do it (operator request,
    2026-08-30).
    """
    removed = crud.reset_user_devices(db=db, dbuser=dbuser)
    logger.info(f'User "{dbuser.username}" had {removed} device(s) reset by {admin.username}')
    return dbuser


@router.post("/user/{username}/regenerate-sub", response_model=UserResponse,
             responses={403: responses._403, 404: responses._404})
def regenerate_user_subscription(
    db: Session = Depends(get_db),
    dbuser: UserResponse = Depends(get_validated_user),
):
    """Recompute this Happ or Micky user's app link from its admin's current
    domain (or the default). No-op for legacy users. Non-destructive: the
    token is refreshed but the old link stays valid too."""
    dbuser = crud.regenerate_happ_subscription(db, dbuser)
    logger.info(f'User "{dbuser.username}" subscription regenerated')
    return UserResponse.model_validate(dbuser)


def _bulk_regenerate_happ_subscriptions(admin_username: Optional[str]) -> None:
    """Background worker: opens its own session (never inherit the request's)
    and regenerates every Happ and Micky user's link (or one admin's)."""
    with GetDB() as db:
        try:
            n = crud.regenerate_happ_subscriptions(db, admin_username)
            logger.info("finished regenerating %d happ/micky subscriptions (admin=%s)",
                        n, admin_username or "ALL")
        except Exception:
            logger.exception("bulk happ/micky subscription regeneration failed")


@router.post("/users/regenerate-subs", responses={403: responses._403})
def regenerate_subscriptions(
    bg: BackgroundTasks,
    admin_username: Optional[str] = Query(default=None),
    db: Session = Depends(get_db),
    admin: Admin = Depends(Admin.check_sudo_admin),
):
    """Regenerate the app links of ALL Happ and Micky users, or one admin's
    (`admin_username`), from each admin's current domain. Changing an admin's
    domain already does this for that admin's users (routers/admin.py); this is
    the manual version.

    Runs in the background — there may be hundreds of users. Returns
    immediately with how many were queued.
    """
    count = crud.count_happ_users(db, admin_username)
    bg.add_task(_bulk_regenerate_happ_subscriptions, admin_username)
    logger.info("queued regeneration of %d happ/micky subscriptions (admin=%s) by %s",
                count, admin_username or "ALL", admin.username)
    return {"detail": "Regeneration started in the background", "queued": count}


@router.post("/user/{username}/revoke_sub", response_model=UserResponse, responses={403: responses._403, 404: responses._404})
def revoke_user_subscription(
    bg: BackgroundTasks,
    db: Session = Depends(get_db),
    dbuser: UserResponse = Depends(get_validated_user),
    admin: Admin = Depends(Admin.get_current),
):
    """Revoke users subscription (Subscription link and proxies)"""
    dbuser = crud.revoke_user_sub(db=db, dbuser=dbuser)

    if dbuser.status in [UserStatus.active, UserStatus.on_hold]:
        bg.add_task(xray.operations.update_user, dbuser=dbuser)
    user = UserResponse.model_validate(dbuser)
    bg.add_task(
        report.user_subscription_revoked, user=user, user_admin=dbuser.admin, by=admin
    )

    logger.info(f'User "{dbuser.username}" subscription revoked')

    return user


@router.get("/users", response_model=UsersResponse, responses={400: responses._400, 403: responses._403, 404: responses._404})
def get_users(
    offset: int = None,
    limit: int = None,
    username: List[str] = Query(None),
    search: Union[str, None] = None,
    owner: Union[List[str], None] = Query(None, alias="admin"),
    status: UserStatus = None,
    sort: str = None,
    db: Session = Depends(get_db),
    admin: Admin = Depends(Admin.get_current),
):
    """Get all users"""
    if sort is not None:
        opts = sort.strip(",").split(",")
        sort = []
        for opt in opts:
            try:
                sort.append(crud.UsersSortingOptions[opt])
            except KeyError:
                raise HTTPException(
                    status_code=400, detail=f'"{opt}" is not a valid sort option'
                )

    users, count = crud.get_users(
        db=db,
        offset=offset,
        limit=limit,
        search=search,
        usernames=username,
        status=status,
        sort=sort,
        admins=owner if admin.is_sudo else [admin.username],
        return_with_count=True,
    )

    return {"users": users, "total": count}


@router.post("/users/reset", responses={403: responses._403, 404: responses._404})
def reset_users_data_usage(
    db: Session = Depends(get_db), admin: Admin = Depends(Admin.check_sudo_admin)
):
    """Reset all users data usage"""
    dbadmin = crud.get_admin(db, admin.username)
    crud.reset_all_users_data_usage(db=db, admin=dbadmin)
    startup_config = xray.config.include_db_users()
    xray.core.restart(startup_config)
    for node_id, node in list(xray.nodes.items()):
        if node.connected:
            xray.operations.restart_node(node_id, startup_config)
    return {"detail": "Users successfully reset."}


@router.get("/user/{username}/usage", response_model=UserUsagesResponse, responses={403: responses._403, 404: responses._404})
def get_user_usage(
    dbuser: UserResponse = Depends(get_validated_user),
    start: str = "",
    end: str = "",
    db: Session = Depends(get_db),
):
    """Get users usage"""
    start, end = validate_dates(start, end)

    usages = crud.get_user_usages(db, dbuser, start, end)

    return {"usages": usages, "username": dbuser.username}


@router.post("/user/{username}/active-next", response_model=UserResponse, responses={403: responses._403, 404: responses._404})
def active_next_plan(
    bg: BackgroundTasks,
    db: Session = Depends(get_db),
    dbuser: UserResponse = Depends(get_validated_user),
):
    """Reset user by next plan"""

    if (dbuser is None or dbuser.next_plan is None):
        raise HTTPException(
            status_code=404,
            detail=f"User doesn't have next plan",
        )

    dbuser = crud.reset_user_by_next(db=db, dbuser=dbuser)

    if dbuser.status in [UserStatus.active, UserStatus.on_hold]:
        bg.add_task(xray.operations.add_user, dbuser=dbuser)

    user = UserResponse.model_validate(dbuser)
    bg.add_task(
        report.user_data_reset_by_next, user=user, user_admin=dbuser.admin,
    )

    logger.info(f'User "{dbuser.username}"\'s usage was reset by next plan')
    return dbuser


@router.get("/users/usage", response_model=UsersUsagesResponse)
def get_users_usage(
    start: str = "",
    end: str = "",
    db: Session = Depends(get_db),
    owner: Union[List[str], None] = Query(None, alias="admin"),
    admin: Admin = Depends(Admin.get_current),
):
    """Get all users usage"""
    start, end = validate_dates(start, end)

    usages = crud.get_all_users_usages(
        db=db, start=start, end=end, admin=owner if admin.is_sudo else [admin.username]
    )

    return {"usages": usages}


@router.put("/user/{username}/set-owner", response_model=UserResponse)
def set_owner(
    admin_username: str,
    dbuser: UserResponse = Depends(get_validated_user),
    db: Session = Depends(get_db),
    admin: Admin = Depends(Admin.check_sudo_admin),
):
    """Transfer a user to a different owning admin. SUDO ONLY.

    Also refreshes the user's Happ subscription link onto the new owner's
    domain — see `crud.set_owner` for why that is part of the transfer rather
    than a follow-up step.
    """
    new_admin = crud.get_admin(db, username=admin_username)
    if not new_admin:
        raise HTTPException(status_code=404, detail="Admin not found")

    # Captured before the crud call: it commits, which expires `dbuser`, and
    # reading the old owner afterwards would report the new one.
    old_owner = dbuser.admin.username if dbuser.admin else None

    dbuser = crud.set_owner(db, dbuser, new_admin)

    # Logged immediately, BEFORE UserResponse.model_validate below: the
    # transfer is already committed and not undoable through the API, while
    # model_validate runs validate_links -> generate_v2ray_links, which reads
    # the unsynchronised xray.hosts store and can raise during a core reload.
    # If it did, the client would get a 500 and there would be no record of who
    # moved the user or from whom.
    #
    # Names the NEW OWNER, not the acting sudo — the previous line logged
    # `admin.username` (whoever performed the transfer), which made the audit
    # trail say the user had been given to the operator doing the giving.
    logger.info(
        'User "%s" transferred from "%s" to "%s" by "%s"',
        dbuser.username, old_owner or "(none)", new_admin.username, admin.username,
    )

    return UserResponse.model_validate(dbuser)


@router.get("/users/expired", response_model=List[str])
def get_expired_users(
    expired_after: Optional[datetime] = Query(None, example="2024-01-01T00:00:00"),
    expired_before: Optional[datetime] = Query(None, example="2024-01-31T23:59:59"),
    db: Session = Depends(get_db),
    admin: Admin = Depends(Admin.get_current),
):
    """
    Get users who have expired within the specified date range.

    - **expired_after** UTC datetime (optional)
    - **expired_before** UTC datetime (optional)
    - At least one of expired_after or expired_before must be provided for filtering
    - If both are omitted, returns all expired users
    """

    expired_after, expired_before = validate_dates(expired_after, expired_before)

    expired_users = get_expired_users_list(db, admin, expired_after, expired_before)
    return [u.username for u in expired_users]


@router.delete("/users/expired", response_model=List[str])
def delete_expired_users(
    bg: BackgroundTasks,
    expired_after: Optional[datetime] = Query(None, example="2024-01-01T00:00:00"),
    expired_before: Optional[datetime] = Query(None, example="2024-01-31T23:59:59"),
    db: Session = Depends(get_db),
    admin: Admin = Depends(Admin.get_current),
):
    """
    Delete users who have expired within the specified date range.

    - **expired_after** UTC datetime (optional)
    - **expired_before** UTC datetime (optional)
    - At least one of expired_after or expired_before must be provided
    """
    expired_after, expired_before = validate_dates(expired_after, expired_before)

    expired_users = get_expired_users_list(db, admin, expired_after, expired_before)
    removed_users = [u.username for u in expired_users]

    if not removed_users:
        raise HTTPException(
            status_code=404, detail="No expired users found in the specified date range"
        )

    crud.remove_users(db, expired_users)

    for removed_user in removed_users:
        logger.info(f'User "{removed_user}" deleted')
        bg.add_task(
            report.user_deleted,
            username=removed_user,
            user_admin=next(
                (u.admin for u in expired_users if u.username == removed_user), None
            ),
            by=admin,
        )

    return removed_users
