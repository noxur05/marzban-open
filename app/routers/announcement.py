from typing import List

from fastapi import APIRouter, Depends, HTTPException

from app.db import Session, crud, get_db
from app.dependencies import get_admin_by_username
from app.models.admin import Admin
from app.models.announcement import (
    AnnouncementOverviewItem,
    AnnouncementResponse,
    AnnouncementSet,
)
from app.utils import cache, responses

router = APIRouter(tags=["Announcement"], prefix="/api",
                   responses={401: responses._401})


def _to_resp(row):
    return AnnouncementResponse.model_validate(row) if row is not None else None


# --------------------------------------------------------------------------
# Self-service: any admin manages their OWN announcement (gated by can_announce)
# --------------------------------------------------------------------------

@router.get("/announcement", response_model=List[AnnouncementResponse])
def my_announcements(db: Session = Depends(get_db),
                     admin: Admin = Depends(Admin.get_current)):
    """The current admin's own announcement history (newest first)."""
    if admin.id is None:  # env-bootstrap sudo has no admins row
        return []
    return crud.list_announcements(db, admin.id)


@router.put("/announcement", response_model=AnnouncementResponse,
            responses={400: responses._400, 403: responses._403})
def set_my_announcement(body: AnnouncementSet,
                        db: Session = Depends(get_db),
                        admin: Admin = Depends(Admin.get_current)):
    """Append the current admin's own announcement (never edits in place)."""
    if admin.id is None:
        raise HTTPException(
            status_code=400,
            detail="This account has no admin row; set the global announcement instead.")
    if not admin.can_announce:
        raise HTTPException(
            status_code=403,
            detail="You don't have permission to post announcements.")
    row = crud.add_announcement(db, admin.id, body.text, body.enabled,
                                created_by_admin_id=admin.id)
    cache.invalidate_announcement(admin.id)
    return row


@router.delete("/announcement", responses={400: responses._400})
def disable_my_announcement(db: Session = Depends(get_db),
                            admin: Admin = Depends(Admin.get_current)):
    """Turn off the current admin's announcement (appends a disabled row).
    Always allowed — turning *off* doesn't need can_announce."""
    if admin.id is None:
        raise HTTPException(status_code=400, detail="This account has no admin row.")
    crud.disable_announcement(db, admin.id, created_by_admin_id=admin.id)
    cache.invalidate_announcement(admin.id)
    return {}


# --------------------------------------------------------------------------
# Sudo: overview + the global fallback + any admin's announcement
# --------------------------------------------------------------------------

@router.get("/announcements", response_model=List[AnnouncementOverviewItem],
            responses={403: responses._403})
def announcements_overview(db: Session = Depends(get_db),
                           admin: Admin = Depends(Admin.check_sudo_admin)):
    """Every admin (+ the global fallback) with their latest announcement row."""
    latest = crud.get_latest_announcements_map(db)
    items = [AnnouncementOverviewItem(
        admin_id=None, username=None, can_announce=None,
        latest=_to_resp(latest.get(None)))]
    for a in crud.get_admins(db):
        items.append(AnnouncementOverviewItem(
            admin_id=a.id, username=a.username, can_announce=a.can_announce,
            latest=_to_resp(latest.get(a.id))))
    return items


@router.get("/announcements/global", response_model=List[AnnouncementResponse],
            responses={403: responses._403})
def global_announcement_history(db: Session = Depends(get_db),
                                admin: Admin = Depends(Admin.check_sudo_admin)):
    return crud.list_announcements(db, None)


@router.put("/announcement/global", response_model=AnnouncementResponse,
            responses={403: responses._403})
def set_global_announcement(body: AnnouncementSet,
                            db: Session = Depends(get_db),
                            admin: Admin = Depends(Admin.check_sudo_admin)):
    row = crud.add_announcement(db, None, body.text, body.enabled,
                                created_by_admin_id=admin.id)
    cache.invalidate_announcement(None)
    return row


@router.delete("/announcement/global", responses={403: responses._403})
def disable_global_announcement(db: Session = Depends(get_db),
                                admin: Admin = Depends(Admin.check_sudo_admin)):
    crud.disable_announcement(db, None, created_by_admin_id=admin.id)
    cache.invalidate_announcement(None)
    return {}


@router.get("/admin/{username}/announcements",
            response_model=List[AnnouncementResponse],
            responses={403: responses._403, 404: responses._404})
def admin_announcement_history(dbadmin: Admin = Depends(get_admin_by_username),
                               db: Session = Depends(get_db),
                               admin: Admin = Depends(Admin.check_sudo_admin)):
    return crud.list_announcements(db, dbadmin.id)


@router.put("/admin/{username}/announcement", response_model=AnnouncementResponse,
            responses={403: responses._403, 404: responses._404})
def set_admin_announcement(body: AnnouncementSet,
                           dbadmin: Admin = Depends(get_admin_by_username),
                           db: Session = Depends(get_db),
                           admin: Admin = Depends(Admin.check_sudo_admin)):
    row = crud.add_announcement(db, dbadmin.id, body.text, body.enabled,
                                created_by_admin_id=admin.id)
    cache.invalidate_announcement(dbadmin.id)
    return row


@router.delete("/admin/{username}/announcement",
               responses={403: responses._403, 404: responses._404})
def disable_admin_announcement(dbadmin: Admin = Depends(get_admin_by_username),
                               db: Session = Depends(get_db),
                               admin: Admin = Depends(Admin.check_sudo_admin)):
    crud.disable_announcement(db, dbadmin.id, created_by_admin_id=admin.id)
    cache.invalidate_announcement(dbadmin.id)
    return {}
