from datetime import datetime
from typing import List, Optional

from pydantic import BaseModel, ConfigDict, Field, field_validator

# Share-link schemes a manual host may use. The link is appended verbatim to a
# v2ray (URI-list) subscription, so it must be a single proxy URI of a scheme
# clients in this family understand. This is the only validation — the link is
# otherwise opaque, exactly like an external-subscription payload (which the
# panel never parses either).
_SUPPORTED_SCHEMES = ("vless://", "vmess://", "trojan://", "ss://")


def _normalize_link(v: str) -> str:
    v = v.strip()
    if not v.startswith(_SUPPORTED_SCHEMES):
        raise ValueError(
            "link must be a single vless:// / vmess:// / trojan:// / ss:// URI")
    if "\n" in v or "\r" in v:
        # A newline would inject extra (unvalidated) lines into the user's
        # config on append — one row is one host.
        raise ValueError("link must be a single line")
    return v


class ManualHostCreate(BaseModel):
    """Register one operator-authored proxy host."""
    name: str = Field(min_length=1, max_length=64)
    link: str = Field(min_length=8, max_length=2048)
    enabled: bool = True
    # Admins whose users receive this host. EMPTY = nobody — "scope to specific
    # admins" must fail closed, matching external subscriptions.
    admin_ids: List[int] = Field(default_factory=list)

    @field_validator("link")
    @classmethod
    def validate_link(cls, v: str) -> str:
        return _normalize_link(v)


class ManualHostModify(BaseModel):
    """Partial update. Omitted fields are left untouched; `admin_ids` is a
    replace-set (`[]` clears every assignment, omitting it leaves them alone)."""
    name: Optional[str] = Field(default=None, min_length=1, max_length=64)
    link: Optional[str] = Field(default=None, min_length=8, max_length=2048)
    enabled: Optional[bool] = None
    admin_ids: Optional[List[int]] = None

    @field_validator("link")
    @classmethod
    def validate_link(cls, v: Optional[str]) -> Optional[str]:
        return None if v is None else _normalize_link(v)


class ManualHostResponse(BaseModel):
    id: int
    name: str
    link: str
    enabled: bool
    # Mapped from ManualHost.admin_ids (a property over the relationship).
    admin_ids: List[int] = Field(default_factory=list)
    created_at: datetime

    model_config = ConfigDict(from_attributes=True)
