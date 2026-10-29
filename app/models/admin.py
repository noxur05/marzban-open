import re
from datetime import date
from typing import List, Optional

from fastapi import Depends, HTTPException, status
from fastapi.security import OAuth2PasswordBearer
from passlib.context import CryptContext
from pydantic import BaseModel, ConfigDict, Field, field_validator
from pydantic_core import PydanticCustomError

from app.db import Session, crud, get_db
from app.utils.jwt import get_admin_payload
from config import SUDOERS

pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")
oauth2_scheme = OAuth2PasswordBearer(tokenUrl="/api/admin/token")  # Admin view url


class Token(BaseModel):
    access_token: str
    token_type: str = "bearer"


class Admin(BaseModel):
    # Surfacing the DB id so the dashboard can drive per-admin pickers
    # (Hosts owner selector, etc). None for the SUDOERS env-bootstrap
    # admin which has no row in the admins table.
    id: Optional[int] = Field(
        default=None,
        description=(
            "Database id. Null only for the SUDOERS env-bootstrap admin "
            "(no row in admins). Stable across modifications."
        ),
    )
    username: str
    is_sudo: bool
    telegram_id: Optional[int] = None
    discord_webhook: Optional[str] = None
    users_usage: Optional[int] = None
    subscription_url: Optional[str] = None
    # Sudo-toggled permission to post the admin's own subscription announcement.
    can_announce: bool = True
    # Sudo-granted permission to enable Outline access on a user. Sudo always may.
    can_enable_outline: bool = False
    # Sudo-toggled RESTRICTION: this admin may create ONLY Happ device-limited
    # users (client_type='happ' + device_limit required). Sudo bypasses.
    happ_only: bool = False
    model_config = ConfigDict(from_attributes=True)

    @field_validator("users_usage",  mode='before')
    def cast_to_int(cls, v):
        if v is None:  # Allow None values
            return v
        if isinstance(v, float):  # Allow float to int conversion
            return int(v)
        if isinstance(v, int):  # Allow integers directly
            return v
        raise ValueError("must be an integer or a float, not a string")  # Reject strings

    @classmethod
    def get_admin(cls, token: str, db: Session):
        payload = get_admin_payload(token)
        if not payload:
            return

        if payload['username'] in SUDOERS and payload['is_sudo'] is True:
            return cls(username=payload['username'], is_sudo=True)

        dbadmin = crud.get_admin(db, payload['username'])
        if not dbadmin:
            return

        if dbadmin.password_reset_at:
            if not payload.get("created_at"):
                return
            if dbadmin.password_reset_at > payload.get("created_at"):
                return

        return cls.model_validate(dbadmin)

    @classmethod
    def get_current(cls,
                    db: Session = Depends(get_db),
                    token: str = Depends(oauth2_scheme)):
        admin = cls.get_admin(token, db)
        if not admin:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Could not validate credentials",
                headers={"WWW-Authenticate": "Bearer"},
            )
        return admin

    @classmethod
    def check_sudo_admin(cls,
                         db: Session = Depends(get_db),
                         token: str = Depends(oauth2_scheme)):
        admin = cls.get_admin(token, db)
        if not admin:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Could not validate credentials",
                headers={"WWW-Authenticate": "Bearer"},
            )
        if not admin.is_sudo:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="You're not allowed"
            )
        return admin


class AdminListEntry(Admin):
    # `GET /api/admins` only: how many users this admin owns (any status), the
    # same count `DELETE /api/admin/{username}` refuses on.
    users_count: int = 0


# Admin names go into URL paths (/api/admin/{username}), so no '/', '?', '#',
# '%' or spaces; ASCII only, which also sidesteps MySQL's accent folding.
# Always fullmatch: '$' alone lets a trailing newline through.
ADMIN_USERNAME_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.@-]{2,31}")
# No '?', '#' or 'user@' either: the token path is appended after it.
_SUB_URL_RE = re.compile(r"https?://[^\s/?#@]+(/[^\s?#]*)?")
ADMIN_PASSWORD_MIN = 6


def _invalid(message):
    # PydanticCustomError, not ValueError: no "Value error, " prefix in the 422.
    return PydanticCustomError("value_error", message)


def normalize_subscription_url(value):
    """Prefix for build_subscription_url: f"{prefix}/{path}/{token}". A
    trailing '/' would give '//sub/..' (404); no scheme gives a relative link.
    None/'' pass through (PATCH: None = unchanged, '' = clear)."""
    if value is None:
        return value
    value = value.strip().rstrip("/")
    if not value:
        return value
    if not _SUB_URL_RE.fullmatch(value):
        raise _invalid("Subscription URL must look like https://sub.example.com")
    if len(value) > 512:
        raise _invalid("Subscription URL is too long")
    return value


class AdminCreate(Admin):
    password: str = Field(min_length=ADMIN_PASSWORD_MIN)
    telegram_id: Optional[int] = None
    discord_webhook: Optional[str] = None

    @property
    def hashed_password(self):
        return pwd_context.hash(self.password)

    @field_validator("username")
    @classmethod
    def validate_username(cls, value):
        if not ADMIN_USERNAME_RE.fullmatch(value):
            raise _invalid("3-32 characters: letters, digits and _ . @ -, starting with a letter or digit")
        return value

    @field_validator("subscription_url")
    @classmethod
    def validate_subscription_url(cls, value):
        return normalize_subscription_url(value)

    @field_validator("discord_webhook")
    @classmethod
    def validate_discord_webhook(cls, value):
        if value and not value.startswith("https://discord.com"):
            raise ValueError("Discord webhook must start with 'https://discord.com'")
        return value


class AdminModify(BaseModel):
    # Not empty either: an empty password used to hash to None and wipe the login.
    password: Optional[str] = Field(default=None, min_length=ADMIN_PASSWORD_MIN)
    is_sudo: bool
    telegram_id: Optional[int] = None
    discord_webhook: Optional[str] = None
    subscription_url: Optional[str] = None
    can_announce: bool = True
    can_enable_outline: bool = False
    happ_only: bool = False

    @property
    def hashed_password(self):
        if self.password:
            return pwd_context.hash(self.password)

    @field_validator("subscription_url")
    @classmethod
    def validate_subscription_url(cls, value):
        return normalize_subscription_url(value)

    @field_validator("discord_webhook")
    @classmethod
    def validate_discord_webhook(cls, value):
        if value and not value.startswith("https://discord.com"):
            raise ValueError("Discord webhook must start with 'https://discord.com'")
        return value


class AdminPartialModify(AdminModify):
    """Every field optional: omitted means "leave unchanged" (see
    crud.partial_update_admin). Used by `PATCH /api/admin/{username}` and by
    `marzban-cli admin update`.

    Each field is redeclared explicitly rather than derived from
    `AdminModify.__annotations__` with a comprehension, which is what this class
    used to do. That trick widened the *types* but re-declared the fields
    without defaults, and in Pydantic v2 a re-annotated field does not inherit
    its parent's default — so `is_sudo` (no default upstream) and every
    Optional-with-default field became REQUIRED here. The CLI never noticed
    because it passes all of them; a PATCH caller sending one field would have
    got a 422. Explicit and boring beats clever and load-bearing.
    """
    password: Optional[str] = Field(default=None, min_length=ADMIN_PASSWORD_MIN)
    is_sudo: Optional[bool] = None
    telegram_id: Optional[int] = None
    discord_webhook: Optional[str] = None
    subscription_url: Optional[str] = None
    can_announce: Optional[bool] = None
    can_enable_outline: Optional[bool] = None
    happ_only: Optional[bool] = None


class AdminUsageAdjustItem(BaseModel):
    # A single "add this much traffic to this local day" instruction.
    # `date` is an Asia/Ashgabat (UTC+5) local day; Pydantic parses/validates
    # "YYYY-MM-DD" at the boundary. `gb` is interpreted as GiB (1024**3 bytes)
    # to match the panel's display.
    date: date
    gb: float = Field(gt=0, le=100000)  # per-day cap ~100 TiB, sanity only


class AdminUsageAdjust(BaseModel):
    # Sudo-only. Add traffic to one admin across one or more chosen days.
    adjustments: List[AdminUsageAdjustItem] = Field(min_length=1)


class AdminUsageAdjustApplied(BaseModel):
    date: str
    added_bytes: int


class AdminUsageAdjustResponse(BaseModel):
    detail: str
    admin_id: int
    username: str
    total_added_bytes: int
    applied: List[AdminUsageAdjustApplied]


class AdminInDB(Admin):
    username: str
    hashed_password: str

    def verify_password(self, plain_password):
        return pwd_context.verify(plain_password, self.hashed_password)


class AdminValidationResult(BaseModel):
    username: str
    is_sudo: bool


class AdminStatsPoint(BaseModel):
    # One hourly bucket for one admin. `t` is the bucket's Asia/Ashgabat
    # (UTC+5) local timestamp "YYYY-MM-DDTHH:00:00". `traffic` is bytes
    # used during that hour (monotonic diff against the previous bucket,
    # clamped at 0). `devices` and `users` are stored point-in-time
    # gauges (active devices / active users at write time).
    t: str
    traffic: int
    devices: int
    users: int


class AdminStatsSeries(BaseModel):
    # Hourly series for one admin. `total_traffic` is the sum of every
    # bucket's `traffic` over the requested range.
    admin_id: int
    username: str
    total_traffic: int
    points: List[AdminStatsPoint]


class AdminsUsageResponse(BaseModel):
    start: str
    end: str
    # "hourly" or "daily" — the granularity the points were rolled up to,
    # chosen server-side by span. The dashboard hides the hourly toggle when
    # this is "daily" (hourly points aren't included in that response).
    granularity: str
    usages: List[AdminStatsSeries]


class TopUserEntry(BaseModel):
    # One row of the Statistics page's Users tab. `admin` is the owning
    # admin's username (None for an orphaned user) so the sudo, unfiltered
    # view can show whose user each row is. `value` is whatever the list ranks
    # by: active device count in `by_devices`, bytes inside the range in
    # `by_traffic`.
    username: str
    admin: Optional[str] = None
    value: int


class TopUsersResponse(BaseModel):
    # Same `start`/`end` echo as AdminsUsageResponse (Asia/Ashgabat local).
    # Each list holds at most TOP_USERS_LIMIT rows (routers/admin.py),
    # descending by `value`.
    start: str
    end: str
    by_devices: List[TopUserEntry]
    by_traffic: List[TopUserEntry]


class NamedCount(BaseModel):
    # A device count under a name: a client app or a platform. `name` is None
    # when the fetch's User-Agent did not carry one (device_fingerprint.parse
    # left the column unset).
    name: Optional[str] = None
    devices: int


class ClientAppStats(BaseModel):
    # One client application on the Statistics page's Apps tab.
    # `users` counts distinct users with at least one such device; `new_devices`
    # are the devices whose first fetch falls inside the range. `platforms`
    # splits `devices`, descending.
    name: Optional[str] = None
    devices: int
    users: int
    new_devices: int
    platforms: List[NamedCount]


class AdminClientApps(BaseModel):
    # One admin's app mix (the Admins tab's Micky ranking and admin details).
    # `username` is None for users with no owning admin. `clients` splits
    # `devices`, descending.
    username: Optional[str] = None
    devices: int
    clients: List[NamedCount]


class ClientAppsResponse(BaseModel):
    # Same `start`/`end` echo as AdminsUsageResponse (Asia/Ashgabat local).
    # `users` is distinct users over every app, so it can be less than the sum
    # of the per-app counts (one user, two apps). `clients` and `admins` are
    # descending by devices.
    start: str
    end: str
    devices: int
    users: int
    new_devices: int
    clients: List[ClientAppStats]
    admins: List[AdminClientApps]
