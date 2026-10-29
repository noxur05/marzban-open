from typing import List

from fastapi import APIRouter, Depends, HTTPException

from app.db import Session, crud, get_db
from app.dependencies import get_validated_user
from app.models.device import DeviceResponse
from app.models.user import UserResponse
from app.utils import cache, responses

router = APIRouter(tags=["Device"], prefix="/api", responses={401: responses._401})


@router.get(
    "/user/{username}/devices",
    response_model=List[DeviceResponse],
    responses={403: responses._403, 404: responses._404},
)
def list_devices(
    db: Session = Depends(get_db),
    dbuser: UserResponse = Depends(get_validated_user),
):
    """List subscription-fetch devices seen for this user, newest first."""
    return crud.list_user_devices(db, dbuser.id)


@router.post(
    "/user/{username}/devices/{device_id}/block",
    response_model=DeviceResponse,
    responses={403: responses._403, 404: responses._404, 400: responses._400},
)
def block_device(
    device_id: int,
    db: Session = Depends(get_db),
    dbuser: UserResponse = Depends(get_validated_user),
):
    """Block a device row. One-way operation — the device disappears from
    every UI (modal, count, statistics, end-user subscription page) and
    on its next subscription fetch the panel responds with an empty body
    plus a Turkmen "Blok Edildiniz" profile-title.

    Two kinds of device can be blocked:
      - HWID-keyed (Happ / V2RayTun): blocked by exact hardware id.
      - Model-bearing (Dalvik / generic Android): no unique id, so the
        block is keyed on the device model — every device reporting that
        same model is cut off together (the only identifier such clients
        expose).

    UA-hashed rows with neither an HWID nor a device model are rejected
    with 400: there's no stable identifier, and a UA bump on app update
    would create a new row and bypass the block. Authorization rides on
    get_validated_user: sudo or the user's owning admin. There is no
    unblock endpoint — reactivation requires a direct DB write."""
    dbdevice = crud.get_user_device(db, dbuser.id, device_id)
    if not dbdevice:
        raise HTTPException(status_code=404, detail="Device not found")
    if not dbdevice.has_exact_identity and not dbdevice.device_model:
        raise HTTPException(
            status_code=400,
            detail="This device can't be blocked: it exposes neither an HWID "
                   "(Happ) nor a device model (Android/Dalvik), so there is no "
                   "stable identifier to enforce the block against. A UA change "
                   "on app update would create a new row and bypass it.",
        )
    blocked = crud.block_user_device(db, dbdevice)
    # Reflect the block in the read-through cache immediately so the device's
    # next subscription fetch rejects from cache (keys mirror the sub check).
    if blocked.hwid:
        cache.cache_block(blocked.user_id, hwid=blocked.hwid)
    elif blocked.has_exact_identity:
        # Micky key: blocked exactly — never by model, which would cut off
        # every device of that model the user owns.
        cache.cache_block(blocked.user_id, device_key=blocked.device_key)
    elif blocked.device_model:
        cache.cache_block(blocked.user_id, model=blocked.device_model)
    return blocked
