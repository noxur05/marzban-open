from datetime import datetime
from typing import Optional

from pydantic import BaseModel, ConfigDict, Field


class AnnouncementSet(BaseModel):
    """Request body to post an announcement.

    Append-only: this always creates a NEW row (never edits). Posting with
    `enabled=False` is how an announcement is turned off — the latest row
    being disabled makes the resolver fall back to the global one.
    """
    text: str = Field(min_length=1, max_length=200)
    enabled: bool = True


class AnnouncementResponse(BaseModel):
    """One announcement row (used for the history list)."""
    id: int
    admin_id: Optional[int] = None  # None = global fallback
    text: str
    enabled: bool
    created_at: datetime
    created_by_admin_id: Optional[int] = None

    model_config = ConfigDict(from_attributes=True)


class AnnouncementOverviewItem(BaseModel):
    """One row in the sudo overview: an admin (or the global fallback) plus
    its latest announcement row, if any."""
    admin_id: Optional[int] = None   # None = the global fallback
    username: Optional[str] = None   # None for the global row
    can_announce: Optional[bool] = None  # None for the global row
    latest: Optional[AnnouncementResponse] = None
