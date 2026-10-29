from datetime import datetime
from typing import Dict, List, Optional

from pydantic import BaseModel, ConfigDict, Field, field_validator

# The client-type segments the remote panel's subscription route accepts
# (app/routers/subscription.py — the panels are the same fork, so this list is
# authoritative for them too). Used only to strip one off a pasted URL.
_CLIENT_TYPE_SEGMENTS = (
    "sing-box", "clash-meta", "clash", "outline", "v2ray-json", "v2ray",
)


def _normalize_url(v: str) -> str:
    """Require an absolute http(s) URL and strip a trailing client-type segment.

    The poller appends `/{client_type}` itself to force each format
    server-side, so a stored URL that already ends in e.g. `/v2ray` would
    produce `/sub/<token>/v2ray/v2ray` — a 404 on the remote panel. Operators
    paste whatever their browser showed them, so normalise instead of
    rejecting.
    """
    v = v.strip()
    if not (v.startswith("http://") or v.startswith("https://")):
        raise ValueError("url must start with http:// or https://")
    v = v.rstrip("/")
    for segment in _CLIENT_TYPE_SEGMENTS:
        if v.endswith("/" + segment):
            v = v[: -(len(segment) + 1)]
            break
    return v.rstrip("/")


def _normalize_hwid(v: Optional[str]) -> Optional[str]:
    """Trim, and treat an empty string as "not set".

    No format constraint beyond length: the HWID is an opaque token the remote
    panel matches verbatim (`hwid:<value>` in its device table), and different
    clients in this family use different shapes — a 16-hex-char id, an
    installation UUID, an Android id. Rejecting anything here would only stop
    the operator from pasting a value that the remote panel already accepts.
    """
    if v is None:
        return None
    v = v.strip()
    return v or None


class ExternalSubscriptionCreate(BaseModel):
    """Register a subscription URL that lives on another panel."""
    name: str = Field(min_length=1, max_length=64)
    url: str = Field(min_length=8, max_length=512)
    # X-Hwid for this subscription's fetches. Leave unset to use the poller's
    # own static identity — but prefer pasting the HWID already registered as a
    # device slot on the remote user, so the poller never claims a slot.
    hwid: Optional[str] = Field(default=None, max_length=128)
    enabled: bool = True
    # Admins whose users receive these hosts. EMPTY = nobody. This is a billing
    # boundary — foreign-host traffic is metered on the remote panel and is
    # invisible here — so an unassigned subscription must fail closed rather
    # than defaulting to everyone.
    admin_ids: List[int] = Field(default_factory=list)

    @field_validator("url")
    @classmethod
    def validate_url(cls, v: str) -> str:
        return _normalize_url(v)

    @field_validator("hwid")
    @classmethod
    def validate_hwid(cls, v: Optional[str]) -> Optional[str]:
        return _normalize_hwid(v)


class ExternalSubscriptionModify(BaseModel):
    """Partial update. Omitted fields are left untouched.

    `url` is intentionally modifiable: retargeting an existing row keeps the
    poller's Redis keys (which are keyed by row id) and the next cycle
    overwrites their payloads.

    `hwid` is the one field where "omitted" and "explicitly null" must differ —
    null means "clear it and fall back to the poller's static identity", which
    an `is not None` check could never express. crud therefore switches on
    `model_fields_set` for it.
    """
    name: Optional[str] = Field(default=None, min_length=1, max_length=64)
    url: Optional[str] = Field(default=None, min_length=8, max_length=512)
    hwid: Optional[str] = Field(default=None, max_length=128)
    enabled: Optional[bool] = None
    # Replace-set: send the full list. `[]` clears every assignment (nobody
    # receives it); omitting the field leaves the assignments untouched.
    admin_ids: Optional[List[int]] = None

    @field_validator("url")
    @classmethod
    def validate_url(cls, v: Optional[str]) -> Optional[str]:
        return None if v is None else _normalize_url(v)

    @field_validator("hwid")
    @classmethod
    def validate_hwid(cls, v: Optional[str]) -> Optional[str]:
        return _normalize_hwid(v)


class ExternalSubscriptionStatus(BaseModel):
    """Last poll outcome for one registered subscription.

    Published to Redis by the poller and read back here — never stored in
    MySQL, which is what keeps the poller read-only against the panel DB. All
    fields optional: `None` throughout means "the poller has not reported on
    this row yet", which is also what a fresh Redis looks like.
    """
    ok: Optional[bool] = None
    fetched_at: Optional[datetime] = None
    http_status: Optional[int] = None
    error: Optional[str] = None
    # Hosts in the last good payload, per format.
    host_counts: Dict[str, int] = Field(default_factory=dict)
    # Deduplicated ISO-3166 alpha-2 codes seen in the last good payload.
    # Empty when geo lookup produced nothing.
    countries: List[str] = Field(default_factory=list)
    # Verbatim `subscription-userinfo` header from the remote panel — the
    # remote user's own quota and expiry. This is the early warning: when that
    # user expires or caps out, every local user loses these hosts at once.
    remote_userinfo: Optional[str] = None


class ExternalSubscriptionResponse(BaseModel):
    id: int
    name: str
    url: str
    hwid: Optional[str] = None
    enabled: bool
    # Mapped from ExternalSubscription.admin_ids (a property over the
    # relationship), so from_attributes picks it up with no alias hop.
    admin_ids: List[int] = Field(default_factory=list)
    created_at: datetime
    status: ExternalSubscriptionStatus = Field(
        default_factory=ExternalSubscriptionStatus)

    model_config = ConfigDict(from_attributes=True)


class ExternalPollerIdentity(BaseModel):
    """The poller's own static Happ identity, as reported by the poller.

    This is the FALLBACK identity, used only for registry rows that carry no
    `hwid` of their own. A row with an explicit HWID always wins, which is the
    normal case: the operator pastes the HWID already registered as a device
    slot on the remote user.

    Read from Redis rather than duplicated as a constant here so the panel
    displays the value actually in use. Where this fallback is used, that
    string is what has to be pre-filled as a device slot on the remote panel: a
    prefilled slot makes the remote device-limit check return 'matched', so the
    poller never auto-claims and can never be denied once the remote user's
    slots fill up.
    """
    hwid: Optional[str] = None
    user_agent: Optional[str] = None
