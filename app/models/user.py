import re
import secrets
from datetime import datetime
from enum import Enum
from typing import Dict, List, Optional, Union

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app import xray
from app.models.admin import Admin
from app.models.proxy import ProxySettings, ProxyTypes
from app.subscription.share import generate_v2ray_links
from app.utils import micky
from app.utils.happ import encrypt_subscription_url
from app.utils.jwt import create_subscription_token
from config import XRAY_SUBSCRIPTION_PATH, XRAY_SUBSCRIPTION_URL_PREFIX

USERNAME_REGEXP = re.compile(r"^(?=\w{3,32}\b)[a-zA-Z0-9-_@.]+(?:_[a-zA-Z0-9-_@.]+)*$")


def build_subscription_url(username: str, admin=None) -> str:
    """Compute the plain subscription URL for a user.

    Extracted so create_user / revoke_user_sub can call it directly when
    they need a known-plain URL to feed into crypt4 — going through
    UserResponse would risk picking up the swapped (encrypted) value.
    Salt is regenerated each call; tokens carry their own iat so both
    are valid against the user's current sub_revoked_at.
    """
    salt = secrets.token_hex(8)
    admin_prefix = getattr(admin, "subscription_url", None) if admin else None
    base = admin_prefix if admin_prefix else XRAY_SUBSCRIPTION_URL_PREFIX
    url_prefix = base.replace('*', salt)
    token = create_subscription_token(username)
    return f"{url_prefix}/{XRAY_SUBSCRIPTION_PATH}/{token}"


class ReminderType(str, Enum):
    expiration_date = "expiration_date"
    data_usage = "data_usage"


class UserStatus(str, Enum):
    active = "active"
    disabled = "disabled"
    limited = "limited"
    expired = "expired"
    on_hold = "on_hold"


class ClientType(str, Enum):
    """The one app a user's subscription is bound to; drives the per-user
    device-limit policy. NULL = legacy, no enforcement, any app."""
    happ = "happ"
    micky = "micky"


class UserStatusModify(str, Enum):
    active = "active"
    disabled = "disabled"
    on_hold = "on_hold"


class UserStatusCreate(str, Enum):
    active = "active"
    on_hold = "on_hold"


class UserDataLimitResetStrategy(str, Enum):
    no_reset = "no_reset"
    day = "day"
    week = "week"
    month = "month"
    year = "year"


class NextPlanModel(BaseModel):
    data_limit: Optional[int] = None
    expire: Optional[int] = None
    add_remaining_traffic: bool = False
    fire_on_either: bool = True
    model_config = ConfigDict(from_attributes=True)


class User(BaseModel):
    proxies: Dict[ProxyTypes, ProxySettings] = {}
    expire: Optional[int] = Field(None, nullable=True)
    data_limit: Optional[int] = Field(
        ge=0, default=None, description="data_limit can be 0 or greater"
    )
    data_limit_reset_strategy: UserDataLimitResetStrategy = (
        UserDataLimitResetStrategy.no_reset
    )
    inbounds: Dict[ProxyTypes, List[str]] = {}
    note: Optional[str] = Field(None, nullable=True)
    sub_updated_at: Optional[datetime] = Field(None, nullable=True)
    sub_last_user_agent: Optional[str] = Field(None, nullable=True)
    online_at: Optional[datetime] = Field(None, nullable=True)
    on_hold_expire_duration: Optional[int] = Field(None, nullable=True)
    on_hold_timeout: Optional[Union[datetime, None]] = Field(None, nullable=True)

    auto_delete_in_days: Optional[int] = Field(None, nullable=True)

    next_plan: Optional[NextPlanModel] = Field(None, nullable=True)

    # Per-user Outline (Shadowsocks dynamic-key) access gate. Mutable:
    # settable on create and modify. None on input means "leave unchanged"
    # for UserModify and "default to False" at create; UserResponse always
    # reflects the stored bool. See models.User.outline_enabled.
    outline_enabled: Optional[bool] = Field(None, nullable=True)

    @field_validator('data_limit', mode='before')
    def cast_to_int(cls, v):
        if v is None:  # Allow None values
            return v
        if isinstance(v, float):  # Allow float to int conversion
            return int(v)
        if isinstance(v, int):  # Allow integers directly
            return v
        raise ValueError("data_limit must be an integer or a float, not a string")  # Reject strings

    @field_validator("proxies", mode="before")
    def validate_proxies(cls, v, values, **kwargs):
        if not v:
            raise ValueError("Each user needs at least one proxy")
        return {
            proxy_type: ProxySettings.from_dict(
                proxy_type, v.get(proxy_type, {}))
            for proxy_type in v
        }

    @field_validator("username", check_fields=False)
    @classmethod
    def validate_username(cls, v):
        if not USERNAME_REGEXP.match(v):
            raise ValueError(
                "Username only can be 3 to 32 characters and contain a-z, 0-9, and underscores in between."
            )
        return v

    @field_validator("note", check_fields=False)
    @classmethod
    def validate_note(cls, v):
        if v and len(v) > 500:
            raise ValueError("User's note can be a maximum of 500 character")
        return v

    @field_validator("on_hold_expire_duration", "on_hold_timeout", mode="before")
    def validate_timeout(cls, v, values):
        # Check if expire is 0 or None and timeout is not 0 or None
        if (v in (0, None)):
            return None
        return v


class UserCreate(User):
    username: str
    status: UserStatusCreate = None

    @model_validator(mode="before")
    @classmethod
    def reject_billed(cls, data):
        # Tombstone for the removed device-billed-user feature (migration
        # d404461618c5). Unknown fields are otherwise ignored, so a stale
        # client sending billed=true would silently get an ordinary user owned
        # by — and billed to — the caller. Refuse it loudly instead. Safe to
        # delete once no client sends the field.
        if isinstance(data, dict) and data.get("billed"):
            raise ValueError("device-billed users are no longer supported")
        return data

    # Device-limit policy. All three are accepted at create-time only;
    # the admin update endpoint silently ignores them (Pydantic extra=ignore).
    # See migration c5e2a8d3f117 and routers/subscription enforcement.
    client_type: Optional[ClientType] = None
    device_limit: Optional[int] = Field(None, ge=1)
    allowed_hwids: List[str] = Field(default_factory=list)

    model_config = ConfigDict(json_schema_extra={
        "example": {
            "username": "user1234",
            "proxies": {
                "vmess": {"id": "35e4e39c-7d5c-4f4b-8b71-558e4f37ff53"},
                "vless": {},
            },
            "inbounds": {
                "vmess": ["VMess TCP", "VMess Websocket"],
                "vless": ["VLESS TCP REALITY", "VLESS GRPC REALITY"],
            },
            "next_plan": {
                "data_limit": 0,
                "expire": 0,
                "add_remaining_traffic": False,
                "fire_on_either": True
            },
            "expire": 0,
            "data_limit": 0,
            "data_limit_reset_strategy": "no_reset",
            "status": "active",
            "note": "",
            "on_hold_timeout": "2023-11-03T20:30:00",
            "on_hold_expire_duration": 0,
            "client_type": "happ",
            "device_limit": 3,
            "allowed_hwids": ["device-uuid-1"]
        }
    })

    @field_validator("allowed_hwids", mode="before")
    @classmethod
    def normalize_hwids(cls, v):
        if v is None:
            return []
        if not isinstance(v, list):
            raise ValueError("allowed_hwids must be a list of strings")
        seen = set()
        out = []
        for h in v:
            if not isinstance(h, str):
                raise ValueError("HWID must be a string")
            h = h.strip()
            if not h:
                raise ValueError("HWID cannot be empty")
            if len(h) > 80:
                raise ValueError("HWID is too long (max 80 chars)")
            if h in seen:
                raise ValueError(f"Duplicate HWID: {h}")
            seen.add(h)
            out.append(h)
        return out

    @model_validator(mode="after")
    def validate_device_policy(self):
        # Both must be NULL or both must be set. No mixed state.
        if (self.client_type is None) != (self.device_limit is None):
            raise ValueError(
                "client_type and device_limit must both be set or both be null"
            )
        if self.allowed_hwids and self.device_limit is None:
            raise ValueError(
                "allowed_hwids cannot be set without a device_limit"
            )
        # Micky devices are identified by keys the app creates on first
        # launch, so there is nothing to pre-register.
        if self.allowed_hwids and self.client_type == ClientType.micky:
            raise ValueError("allowed_hwids apply to Happ users only")
        if self.device_limit is not None and len(self.allowed_hwids) > self.device_limit:
            raise ValueError(
                f"allowed_hwids length ({len(self.allowed_hwids)}) exceeds device_limit ({self.device_limit})"
            )
        return self

    @model_validator(mode="after")
    def validate_outline(self):
        # Outline (Shadowsocks dynamic-key) is an uncountable device, so it
        # can't coexist with the device-limit policy (metered per HWID).
        # Device-limited users also can't actually use Outline — the subscription gate
        # returns "Diňe Happ" before the Outline branch — and their
        # encrypted Happ subscription_url would yield a broken ssconf link.
        if self.outline_enabled and self.device_limit is not None:
            raise ValueError("outline_enabled cannot be combined with a device_limit")
        return self

    @property
    def excluded_inbounds(self):
        excluded = {}
        for proxy_type in self.proxies:
            excluded[proxy_type] = []
            for inbound in xray.config.inbounds_by_protocol.get(proxy_type, []):
                if not inbound["tag"] in self.inbounds.get(proxy_type, []):
                    excluded[proxy_type].append(inbound["tag"])

        return excluded

    @field_validator("inbounds", mode="before")
    def validate_inbounds(cls, inbounds, values, **kwargs):
        proxies = values.data.get("proxies", [])

        # delete inbounds that are for protocols not activated
        for proxy_type in inbounds.copy():
            if proxy_type not in proxies:
                del inbounds[proxy_type]

        # check by proxies to ensure that every protocol has inbounds set
        for proxy_type in proxies:
            tags = inbounds.get(proxy_type)

            if tags:
                for tag in tags:
                    if tag not in xray.config.inbounds_by_tag:
                        raise ValueError(f"Inbound {tag} doesn't exist")

            # elif isinstance(tags, list) and not tags:
            #     raise ValueError(f"{proxy_type} inbounds cannot be empty")

            else:
                inbounds[proxy_type] = [
                    i["tag"]
                    for i in xray.config.inbounds_by_protocol.get(proxy_type, [])
                ]

        return inbounds

    @field_validator("status", mode="before")
    def validate_status(cls, status, values):
        on_hold_expire = values.data.get("on_hold_expire_duration")
        expire = values.data.get("expire")
        if status == UserStatusCreate.on_hold:
            if (on_hold_expire == 0 or on_hold_expire is None):
                raise ValueError("User cannot be on hold without a valid on_hold_expire_duration.")
            if expire:
                raise ValueError("User cannot be on hold with specified expire.")
        return status


class UserModify(User):
    status: UserStatusModify = None
    data_limit_reset_strategy: UserDataLimitResetStrategy = None

    # Device-limit policy, settable after create so existing users can be
    # migrated onto Happ. Both fields must arrive together — see
    # validate_device_policy_modify. `allowed_hwids` is deliberately NOT here:
    # pinning HWIDs on an existing user is a separate concern from adopting
    # the policy, and reset-devices is the endpoint for clearing bindings.
    client_type: Optional[ClientType] = None
    device_limit: Optional[int] = Field(None, ge=1)
    model_config = ConfigDict(json_schema_extra={
        "example": {
            "proxies": {
                "vmess": {"id": "35e4e39c-7d5c-4f4b-8b71-558e4f37ff53"},
                "vless": {},
            },
            "inbounds": {
                "vmess": ["VMess TCP", "VMess Websocket"],
                "vless": ["VLESS TCP REALITY", "VLESS GRPC REALITY"],
            },
            "next_plan": {
                "data_limit": 0,
                "expire": 0,
                "add_remaining_traffic": False,
                "fire_on_either": True
            },
            "expire": 0,
            "data_limit": 0,
            "data_limit_reset_strategy": "no_reset",
            "status": "active",
            "note": "",
            "on_hold_timeout": "2023-11-03T20:30:00",
            "on_hold_expire_duration": 0,
            "client_type": "happ",
            "device_limit": 2,
        }
    })

    @model_validator(mode="after")
    def validate_device_policy_modify(self):
        """Adopting the device-limit policy is all-or-nothing, like create.

        On modify, `None` already means "leave unchanged", so the only shapes
        that need rejecting are the half-specified ones: a `client_type` with
        no `device_limit` would produce exactly the mixed state that
        `UserCreate.validate_device_policy` exists to prevent, and a
        `device_limit` alone would silently do nothing on a legacy user.

        Note this makes *clearing* the policy unexpressible by construction —
        `None` is "no change", never "set to NULL". That is deliberate:
        dropping `client_type` would turn a user's `happ://` link back into a
        plaintext URL, invalidating whatever every one of their clients holds.
        Reverting a user is an explicit, destructive operation and does not
        belong on the general-purpose edit endpoint.
        """
        if (self.client_type is None) != (self.device_limit is None):
            raise ValueError(
                "client_type and device_limit must be sent together"
            )
        return self

    @property
    def excluded_inbounds(self):
        excluded = {}
        for proxy_type in self.inbounds:
            excluded[proxy_type] = []
            for inbound in xray.config.inbounds_by_protocol.get(proxy_type, []):
                if not inbound["tag"] in self.inbounds.get(proxy_type, []):
                    excluded[proxy_type].append(inbound["tag"])

        return excluded

    @field_validator("inbounds", mode="before")
    def validate_inbounds(cls, inbounds, values, **kwargs):
        # check with inbounds, "proxies" is optional on modifying
        # so inbounds particularly can be modified
        if inbounds:
            for proxy_type, tags in inbounds.items():

                # if not tags:
                #     raise ValueError(f"{proxy_type} inbounds cannot be empty")

                for tag in tags:
                    if tag not in xray.config.inbounds_by_tag:
                        raise ValueError(f"Inbound {tag} doesn't exist")

        return inbounds

    @field_validator("proxies", mode="before")
    def validate_proxies(cls, v):
        return {
            proxy_type: ProxySettings.from_dict(
                proxy_type, v.get(proxy_type, {}))
            for proxy_type in v
        }

    @field_validator("status", mode="before")
    def validate_status(cls, status, values):
        on_hold_expire = values.data.get("on_hold_expire_duration")
        expire = values.data.get("expire")
        if status == UserStatusCreate.on_hold:
            if (on_hold_expire == 0 or on_hold_expire is None):
                raise ValueError("User cannot be on hold without a valid on_hold_expire_duration.")
            if expire:
                raise ValueError("User cannot be on hold with specified expire.")
        return status


class UserResponse(User):
    username: str
    status: UserStatus
    used_traffic: int
    lifetime_used_traffic: int = 0
    created_at: datetime
    links: List[str] = []
    subscription_url: str = ""
    proxies: dict
    excluded_inbounds: Dict[ProxyTypes, List[str]] = {}
    device_count: int = 0

    # Device-limit policy snapshot. NULL when feature is off for this user.
    client_type: Optional[ClientType] = None
    device_limit: Optional[int] = None

    # Pre-computed Happ crypt4 wrapper, read from the ORM column. Hidden
    # from serialized responses via exclude=True — its value is surfaced
    # through the `subscription_url` field by validate_subscription_url
    # so consumers never see the plaintext URL for Happ-limited users.
    encrypted_subscription_url: Optional[str] = Field(default=None, exclude=True)

    admin: Optional[Admin] = None
    model_config = ConfigDict(from_attributes=True)

    @model_validator(mode="after")
    def validate_links(self):
        if not self.links:
            self.links = generate_v2ray_links(
                self.proxies, self.inbounds, extra_data=self.model_dump(), reverse=False,
                user_admin_id=self.admin.id if self.admin else None,
            )
        return self

    @model_validator(mode="after")
    def validate_subscription_url(self):
        if not self.subscription_url:
            self.subscription_url = build_subscription_url(self.username, self.admin)
        # Hand consumers the crypt4 wrapper instead of the plaintext URL
        # for Happ-limited users. The encrypted column is populated at
        # create-time (and refreshed by revoke_user_sub) by the CRUD layer.
        # If somehow missing (legacy Happ user that pre-dates the column,
        # or a write race), encrypt on the fly — the plaintext URL must
        # NEVER leave this method for a Happ user.
        if self.client_type == ClientType.happ:
            self.subscription_url = (
                self.encrypted_subscription_url
                or encrypt_subscription_url(self.subscription_url)
            )
        # Same invariant for Micky: its micky://s/ link, never the plain URL
        # (which would reveal the host and token). Empty if the panel has no
        # link key — creating Micky users is refused in that state anyway.
        elif self.client_type == ClientType.micky:
            self.subscription_url = self.encrypted_subscription_url or (
                micky.make_link(self.subscription_url) if micky.can_make_links() else ""
            )
        return self

    @field_validator("proxies", mode="before")
    def validate_proxies(cls, v, values, **kwargs):
        if isinstance(v, list):
            v = {p.type: p.settings for p in v}
        return super().validate_proxies(v, values, **kwargs)

    @field_validator("used_traffic", "lifetime_used_traffic", mode='before')
    def cast_to_int(cls, v):
        if v is None:  # Allow None values
            return v
        if isinstance(v, float):  # Allow float to int conversion
            return int(v)
        if isinstance(v, int):  # Allow integers directly
            return v
        raise ValueError("must be an integer or a float, not a string")  # Reject strings

    @field_validator("device_count", mode="before")
    @classmethod
    def default_device_count(cls, v):
        """Coerce a missing device_count to 0 instead of failing validation.

        `User.device_count` is a `query_expression` (app/db/models.py): it holds
        a real value ONLY while the ORM object still carries the
        `with_expression()` that crud.get_user_queryset attached. A `db.commit()`
        expires the attribute and it then reads as **None** — verified on prod:
        after get_user -> 1, after commit -> None, after refresh -> 1.

        Every mutating crud.* function commits, so any response built from a
        post-commit ORM object hands None to a non-optional `int` field and
        Pydantic raises. Because the write has already been committed at that
        point, the caller sees a 500 for an operation that actually SUCCEEDED —
        which is exactly what broke PUT /api/user/{u}, /reset and /revoke_sub in
        production (101 + 20 + 8 failures, zero successes).

        `default_expr=literal(0)` on the column does not cover the expired-
        attribute case, so the documented intent ("reads as 0 so
        UserResponse.device_count stays a clean int") is enforced here instead.

        A response built this way reports 0 rather than the true count. That is
        cosmetic and self-correcting: every list/read path goes through
        get_user_queryset, which applies with_expression and returns the real
        number.
        """
        return 0 if v is None else v


class SubscriptionUser(BaseModel):
    """What a subscription answer needs from a user, built per /sub fetch from
    crud.get_user_for_sub.

    Not UserResponse: building one renders every link, mints a token and
    encrypts the subscription link (all thrown away on this path, and paid
    even by refused requests), and it needs the usage-reset history for
    lifetime usage. Field names and values match UserResponse:
    generate_subscription and its format variables read them by name
    (`user.__dict__`: username, status, used_traffic, data_limit, expire,
    on_hold_expire_duration), and `_with_new_url` reads client_type and
    encrypted_subscription_url.
    """
    id: int
    username: str
    status: UserStatus
    used_traffic: int
    data_limit: Optional[int] = None
    expire: Optional[int] = None
    on_hold_expire_duration: Optional[int] = None
    proxies: Dict[ProxyTypes, ProxySettings] = {}
    inbounds: Dict[ProxyTypes, List[str]] = {}
    admin: Optional[Admin] = None
    client_type: Optional[ClientType] = None
    device_limit: Optional[int] = None
    outline_enabled: Optional[bool] = None
    encrypted_subscription_url: Optional[str] = None
    model_config = ConfigDict(from_attributes=True)

    # Same conversions as UserResponse / User, so the values are identical.
    @field_validator("proxies", mode="before")
    def validate_proxies(cls, v):
        if isinstance(v, list):
            v = {p.type: p.settings for p in v}
        return {pt: ProxySettings.from_dict(pt, v.get(pt, {})) for pt in v}

    @field_validator("on_hold_expire_duration", mode="before")
    def validate_timeout(cls, v):
        return None if v in (0, None) else v


class SubscriptionUserResponse(UserResponse):
    admin: Admin | None = Field(default=None, exclude=True)
    excluded_inbounds: Dict[ProxyTypes, List[str]] | None = Field(None, exclude=True)
    note: str | None = Field(None, exclude=True)
    inbounds: Dict[ProxyTypes, List[str]] | None = Field(None, exclude=True)
    auto_delete_in_days: int | None = Field(None, exclude=True)
    device_count: int = Field(0, exclude=True)
    model_config = ConfigDict(from_attributes=True)


class UsersResponse(BaseModel):
    users: List[UserResponse]
    total: int


class UserUsageResponse(BaseModel):
    node_id: Union[int, None] = None
    node_name: str
    used_traffic: int

    @field_validator("used_traffic",  mode='before')
    def cast_to_int(cls, v):
        if v is None:  # Allow None values
            return v
        if isinstance(v, float):  # Allow float to int conversion
            return int(v)
        if isinstance(v, int):  # Allow integers directly
            return v
        raise ValueError("must be an integer or a float, not a string")  # Reject strings


class UserUsagesResponse(BaseModel):
    username: str
    usages: List[UserUsageResponse]


class UsersUsagesResponse(BaseModel):
    usages: List[UserUsageResponse]
