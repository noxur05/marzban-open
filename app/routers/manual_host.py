"""Sudo-only registry for operator-authored ("manual") proxy hosts.

A row here is a full share link (vless://, vmess://, trojan://, ss://) for a
server that lives elsewhere — one this panel cannot express as a ProxyHost,
which only overrides a local inbound's address. `app/subscription/manual.py`
appends the link, verbatim, to the v2ray subscriptions of users whose owning
admin the host is assigned to (`manual_host_admins`).

Everything here is sudo-only. Who receives a host is a deliberate assignment:
an empty admin set means NOBODY (fail closed), matching external subscriptions.
An assignment change drops the per-admin Redis cache, so it takes effect on the
next subscription fetch rather than after a TTL.
"""
from typing import List

from fastapi import APIRouter, Depends, HTTPException

from app.db import Session, crud, get_db
from app.models.admin import Admin
from app.models.manual_host import (
    ManualHostCreate,
    ManualHostModify,
    ManualHostResponse,
)
from app.utils import cache, responses

router = APIRouter(tags=["ManualHost"], prefix="/api",
                   responses={401: responses._401})


@router.get("/manual-hosts",
            response_model=List[ManualHostResponse],
            responses={403: responses._403})
def list_manual_hosts(db: Session = Depends(get_db),
                      admin: Admin = Depends(Admin.check_sudo_admin)):
    """Every registered manual host."""
    return [ManualHostResponse.model_validate(h)
            for h in crud.list_manual_hosts(db)]


@router.post("/manual-hosts",
             response_model=ManualHostResponse,
             responses={403: responses._403})
def add_manual_host(new_host: ManualHostCreate,
                    db: Session = Depends(get_db),
                    admin: Admin = Depends(Admin.check_sudo_admin)):
    """Register a manual host.

    `admin_ids` selects who receives it; an empty list means nobody. The link
    is stored and appended verbatim, so it must already be a working share URI
    for the target clients.
    """
    dbhost = crud.create_manual_host(db, new_host)
    cache.invalidate_manual_admin_links()
    crud.touch_all_micky_copies(db)  # after the cache drop: copies render from it
    return ManualHostResponse.model_validate(dbhost)


@router.put("/manual-hosts/{host_id}",
            response_model=ManualHostResponse,
            responses={403: responses._403, 404: responses._404})
def modify_manual_host(host_id: int,
                       modified: ManualHostModify,
                       db: Session = Depends(get_db),
                       admin: Admin = Depends(Admin.check_sudo_admin)):
    """Rename, re-link, reassign or enable/disable a manual host.

    `admin_ids` is replace-set: send the full list. `[]` revokes it from
    everyone; omitting the field leaves the assignments alone.
    """
    dbhost = crud.get_manual_host(db, host_id)
    if not dbhost:
        raise HTTPException(status_code=404, detail="Manual host not found")
    dbhost = crud.update_manual_host(db, dbhost, modified)
    cache.invalidate_manual_admin_links()
    crud.touch_all_micky_copies(db)  # after the cache drop: copies render from it
    return ManualHostResponse.model_validate(dbhost)


@router.delete("/manual-hosts/{host_id}",
               responses={403: responses._403, 404: responses._404})
def remove_manual_host(host_id: int,
                       db: Session = Depends(get_db),
                       admin: Admin = Depends(Admin.check_sudo_admin)):
    """Unregister a manual host."""
    dbhost = crud.get_manual_host(db, host_id)
    if not dbhost:
        raise HTTPException(status_code=404, detail="Manual host not found")
    crud.remove_manual_host(db, dbhost)
    cache.invalidate_manual_admin_links()
    crud.touch_all_micky_copies(db)  # after the cache drop: copies render from it
    return {"detail": "Manual host removed"}
