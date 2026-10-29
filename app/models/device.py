from datetime import datetime
from typing import Optional

from pydantic import BaseModel, ConfigDict


class DeviceResponse(BaseModel):
    """One device row in the API response.

    Excludes internal fields (device_key, user_id) — those are
    implementation details, not user-facing. The HWID itself is exposed
    so the admin UI can show which slot is bound to which hardware id.
    Admin-prefilled slots that have never connected have NULL timestamps.
    """
    id: int
    hwid: Optional[str] = None
    # Micky: the device's stable id (M-Device-Id), or its key hash for builds
    # that don't send one. Shown instead of an HWID; blocking blocks it.
    device_id: Optional[str] = None
    user_agent: Optional[str] = None
    client_name: Optional[str] = None
    client_version: Optional[str] = None
    platform: Optional[str] = None
    os_version: Optional[str] = None
    device_model: Optional[str] = None
    locale: Optional[str] = None
    last_ip: Optional[str] = None
    first_seen_at: Optional[datetime] = None
    last_seen_at: Optional[datetime] = None
    fetch_count: int
    # Always False on rows returned by /api/user/<u>/devices because the
    # endpoint filters blocked rows out. Surfacing the field anyway so a
    # future "show blocked too" view doesn't need a schema change.
    is_blocked: bool = False

    model_config = ConfigDict(from_attributes=True)
