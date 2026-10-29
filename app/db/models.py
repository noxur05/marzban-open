import os
from datetime import datetime

from sqlalchemy import (
    JSON,
    BigInteger,
    Boolean,
    Column,
    Date,
    DateTime,
    Enum,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Table,
    UniqueConstraint,
    func,
    literal,
)
from sqlalchemy.dialects import mysql
from sqlalchemy.ext.hybrid import hybrid_property
from sqlalchemy.orm import query_expression, relationship
from sqlalchemy.sql.expression import select, text

from app import xray
from app.db.base import Base
from app.models.node import NodeStatus
from app.models.proxy import (
    ProxyHostALPN,
    ProxyHostFingerprint,
    ProxyHostSecurity,
    ProxyTypes,
)
from app.models.user import ClientType, ReminderType, UserDataLimitResetStrategy, UserStatus


class Admin(Base):
    __tablename__ = "admins"

    id = Column(Integer, primary_key=True)
    username = Column(String(34), unique=True, index=True)
    hashed_password = Column(String(128))
    users = relationship("User", back_populates="admin", foreign_keys="User.admin_id")
    created_at = Column(DateTime, default=datetime.utcnow)
    is_sudo = Column(Boolean, default=False)
    password_reset_at = Column(DateTime, nullable=True)
    telegram_id = Column(BigInteger, nullable=True, default=None)
    discord_webhook = Column(String(1024), nullable=True, default=None)
    users_usage = Column(BigInteger, nullable=False, default=0)
    subscription_url = Column(String(512), nullable=True, default=None)

    # Sudo-toggled permission: may this admin post their own subscription
    # announcement? Revoking (False) appends a disabled announcement row so
    # the admin's users fall back to the global one. See Announcement below.
    can_announce = Column(Boolean, nullable=False, default=True, server_default='1')
    # Sudo-granted permission to enable Outline access on a user
    # (User.outline_enabled). Default FALSE; sudo admins always may. See
    # migration b7e2f4a91c83.
    can_enable_outline = Column(Boolean, nullable=False, default=False, server_default='0')
    # Sudo-toggled RESTRICTION (not a grant): when TRUE this admin may create
    # ONLY Happ device-limited users — every user they create must carry
    # client_type='happ' AND a device_limit. Enforced at create time in
    # routers/user.add_user; sudo admins bypass it. Because client_type and
    # device_limit are immutable after create, the restriction needs no
    # follow-up enforcement on update. Default FALSE = unrestricted, so every
    # existing admin is unaffected. See migration a4c9e1b73f80.
    #
    # Deliberate hole: this constrains what an admin CREATES, not what it can
    # end up OWNING. A sudo admin may still hand a legacy (non-Happ) user to a
    # happ_only admin via PUT /api/user/{u}/set-owner, and users the admin
    # owned before the flag was set keep their existing shape. Both are
    # sudo-only actions and sudo bypasses the restriction by design.
    happ_only = Column(Boolean, nullable=False, default=False, server_default='0')

    usage_logs = relationship("AdminUsageLogs", back_populates="admin")


class AdminUsageLogs(Base):
    __tablename__ = "admin_usage_logs"

    id = Column(Integer, primary_key=True)
    admin_id = Column(Integer, ForeignKey("admins.id"))
    admin = relationship("Admin", back_populates="usage_logs")
    used_traffic_at_reset = Column(BigInteger, nullable=False)
    reset_at = Column(DateTime, default=datetime.utcnow)


class AdminUsageAdjustment(Base):
    """Audit trail of manual traffic corrections (see migration e1a7c3f9b204).

    A sudo operator adds traffic to an admin on a chosen local day to account
    for usage this panel cannot meter (external-subscription traffic, metered
    on the remote panel). Applying an adjustment edits the admin's historical
    `admin_hourly_usages` snapshots with a carry-forward offset and bumps
    `admins.users_usage`; this row records that it happened so it is visible
    and reversible.

    `admin_id` and `created_by_admin_id` are plain ints with NO FK — audit
    rows must survive deletion of either admin (same convention as
    `Announcement.created_by_admin_id`).
    """
    __tablename__ = "admin_usage_adjustments"

    id = Column(Integer, primary_key=True)
    admin_id = Column(Integer, nullable=False)
    # Asia/Ashgabat local day the traffic was attributed to.
    for_day = Column(Date, nullable=False)
    # Bytes added to that day (already ramped across its hourly snapshots).
    added_bytes = Column(BigInteger, nullable=False)
    # The sudo admin who performed it (audit only; survives their deletion).
    created_by_admin_id = Column(Integer, nullable=True)
    created_at = Column(DateTime, nullable=False, default=datetime.utcnow)


class AdminHourlyUsage(Base):
    """Top-of-the-hour snapshot of an admin's traffic + activity gauges.

    Written by the out-of-process Rust stats collector (the panel never
    writes this table; Alembic owns the DDL). One row per admin per UTC
    hour, keyed on (admin_id, recorded_at) where recorded_at is the top
    of the hour.

    `users_usage` is monotonic — it stores
    `Admin.users_usage + Σ AdminUsageLogs.used_traffic_at_reset`, so it
    survives a `reset_admin_usage` call. The admin-usage dashboard
    derives per-bucket usage by diffing consecutive hourly snapshots.

    `device_count` and `user_count` are point-in-time gauges (active
    devices / active users at write time), not diffed.

    No FK on `admin_id` on purpose (matches AdminUsageLogs): a RESTRICT FK
    would block admin deletion and we want snapshot history retained even
    after an admin is removed.
    """
    __tablename__ = "admin_hourly_usages"
    __table_args__ = (
        UniqueConstraint("admin_id", "recorded_at", name="uq_admin_hour"),
        Index(
            "ix_admin_hourly_usages_admin_recorded",
            "admin_id", "recorded_at",
        ),
        # Standalone recorded_at index so the sudo "all admins" window query
        # (filters only on recorded_at) is index-backed, not a full scan.
        Index("ix_admin_hourly_usages_recorded", "recorded_at"),
    )

    id = Column(Integer, primary_key=True)
    admin_id = Column(Integer, nullable=False)
    recorded_at = Column(DateTime, nullable=False)
    users_usage = Column(BigInteger, nullable=False, default=0, server_default='0')
    device_count = Column(Integer, nullable=False, default=0, server_default='0')
    user_count = Column(Integer, nullable=False, default=0, server_default='0')
    created_at = Column(DateTime, nullable=False, default=datetime.utcnow)


class Announcement(Base):
    """Append-only subscription announcement (delivered via Happ `announce`).

    Every change — including a *disable* — INSERTs a new row; rows are never
    UPDATEd or DELETEd (except the ON DELETE CASCADE when the owning admin is
    removed). ``admin_id`` NULL = the single global fallback.

    Effective value (resolved in crud): the LATEST row per admin (MAX id) if
    it is enabled; else the latest global row if enabled; else none. NB: the
    latest row *if* enabled — NOT the latest *enabled* row (a disable must
    override an older enabled row).
    """
    __tablename__ = "announcements"
    __table_args__ = (
        # "latest per admin" = MAX(id) WHERE admin_id=? and the global
        # (admin_id IS NULL) lookup are index-backed by this composite.
        Index("ix_announcements_admin_id", "admin_id", "id"),
    )

    id = Column(Integer, primary_key=True)
    admin_id = Column(
        Integer,
        ForeignKey("admins.id", ondelete="CASCADE"),
        nullable=True,  # NULL = global fallback announcement
    )
    text = Column(String(200), nullable=False)
    enabled = Column(Boolean, nullable=False, default=True, server_default='1')
    created_at = Column(DateTime, nullable=False, default=datetime.utcnow)
    # Who wrote this row (the admin themselves, or a sudo acting for them).
    # Audit only — plain int, no FK, so it survives the author's deletion.
    created_by_admin_id = Column(Integer, nullable=True)


# Which admins receive a given external subscription's mirrored hosts.
#
# NOTE the empty set means the OPPOSITE of host_admins above: a subscription
# with no rows here is served to NOBODY, not to everybody. Those hosts are
# metered on someone else's panel, so "unassigned" must fail closed. See
# migration b8d4f2c7a913.
external_subscription_admins_association = Table(
    "external_subscription_admins",
    Base.metadata,
    Column(
        "external_subscription_id",
        ForeignKey("external_subscriptions.id", ondelete="CASCADE"),
        primary_key=True,
    ),
    Column("admin_id", ForeignKey("admins.id", ondelete="CASCADE"), primary_key=True),
)

class ExternalSubscription(Base):
    """A subscription URL belonging to an ordinary user on ANOTHER panel.

    Sudo registers these; the out-of-process Rust poller
    (`stats-collector/src/poller/`) fetches each one on an interval, verifies
    every host's country from its IP, relabels the remarks, and publishes
    render-ready payloads to Redis. At render time the panel appends those
    hosts for users whose owning admin the subscription is assigned to
    (app/subscription/share.py).

    This table is CONFIGURATION, not cache. The poller only ever reads it and
    writes exclusively to Redis, so a Redis flush or reboot self-heals on the
    next cycle — but losing these rows loses the feature. Poll state (last
    fetch, error, remote quota) deliberately lives in Redis, not here, which
    is what keeps the poller read-only against MySQL.
    """
    __tablename__ = "external_subscriptions"

    id = Column(Integer, primary_key=True)
    # Operator-facing label ("Panel 2 — NL"). Shown in the panel UI and in
    # the poller's log lines; never sent to the remote panel.
    name = Column(String(64), nullable=False)
    # Base subscription URL, WITHOUT a trailing client-type segment. The
    # poller appends /{client_type} itself so the remote panel does the
    # format conversion — that is what lets this feature support every
    # output format without ever parsing a proxy URI.
    url = Column(String(512), nullable=False)
    # HWID sent as `X-Hwid` when fetching this subscription. Remote panels in
    # this family reject a fetch without it (403 "HWID header required") and
    # advertise `subscription-always-hwid-enable: true`. Per-row so the
    # operator can paste the HWID that is already registered as a device slot
    # on the remote user: a prefilled slot means the poller never has to claim
    # one, and on this panel family a claimed slot is released only by an admin
    # block — never by time — so a HWID that drifts burns slots permanently.
    # NULL falls back to the poller's own static identity.
    hwid = Column(String(128), nullable=True)
    enabled = Column(Boolean, nullable=False, default=True, server_default='1')
    created_at = Column(DateTime, nullable=False, default=datetime.utcnow)

    # Admins whose users receive these hosts. EMPTY = nobody (not everybody —
    # see the association table's note). Read at subscription-render time from
    # the user's *current* owner, so a set-owner transfer takes effect on that
    # user's next fetch with no extra bookkeeping.
    admins = relationship("Admin", secondary=external_subscription_admins_association)

    @property
    def admin_ids(self):
        """Flat int list of linked admin ids, under the Pydantic field name so
        from_attributes mapping works without an alias hop (same trick as
        ProxyHost.admin_ids)."""
        return [a.id for a in self.admins]


# Which admins receive a given manual host. EMPTY = nobody, exactly like
# external_subscription_admins (and the OPPOSITE of host_admins) — "scope to
# specific admins" must fail closed. See migration f2b9d7c04a16.
manual_host_admins_association = Table(
    "manual_host_admins",
    Base.metadata,
    Column(
        "manual_host_id",
        ForeignKey("manual_hosts.id", ondelete="CASCADE"),
        primary_key=True,
    ),
    Column("admin_id", ForeignKey("admins.id", ondelete="CASCADE"), primary_key=True),
)


class ManualHost(Base):
    """An operator-authored proxy host — the manual twin of ExternalSubscription.

    Sudo pastes a full share link (vless://, vmess://, trojan://, ss://) for a
    server this panel cannot express as a ProxyHost (a ProxyHost only overrides
    a local inbound's address and takes protocol/uuid/keys from that inbound, so
    it cannot point at a foreign server with its own credentials). At
    subscription-render time the panel appends this link, verbatim, to the
    v2ray configs of users whose owning admin the host is assigned to
    (app/subscription/manual.py, reusing external.merge_into).

    Configuration, not cache: unlike ExternalSubscription there is no poller —
    the link is served straight from here, with a Redis cache in front of the
    per-admin lookup for the subscription hot path.
    """
    __tablename__ = "manual_hosts"

    id = Column(Integer, primary_key=True)
    # Operator-facing label. Shown in the panel UI; not sent anywhere.
    name = Column(String(64), nullable=False)
    # The full share link, stored opaquely and appended verbatim.
    link = Column(String(2048), nullable=False)
    enabled = Column(Boolean, nullable=False, default=True, server_default='1')
    created_at = Column(DateTime, nullable=False, default=datetime.utcnow)

    # Admins whose users receive this host. EMPTY = nobody (see the association
    # table's note). Read at render time from the user's *current* owner.
    admins = relationship("Admin", secondary=manual_host_admins_association)

    @property
    def admin_ids(self):
        """Flat int list of linked admin ids, under the Pydantic field name so
        from_attributes mapping works without an alias hop."""
        return [a.id for a in self.admins]


class User(Base):
    __tablename__ = "users"

    id = Column(Integer, primary_key=True)
    username = Column(String(34, collation='NOCASE'), unique=True, index=True)
    proxies = relationship("Proxy", back_populates="user", cascade="all, delete-orphan")
    status = Column(Enum(UserStatus), nullable=False, default=UserStatus.active)
    used_traffic = Column(BigInteger, default=0)
    node_usages = relationship("NodeUserUsage", back_populates="user", cascade="all, delete-orphan")
    notification_reminders = relationship("NotificationReminder", back_populates="user", cascade="all, delete-orphan")
    data_limit = Column(BigInteger, nullable=True)
    data_limit_reset_strategy = Column(
        Enum(UserDataLimitResetStrategy),
        nullable=False,
        default=UserDataLimitResetStrategy.no_reset,
    )
    usage_logs = relationship("UserUsageResetLogs", back_populates="user")  # maybe rename it to reset_usage_logs?
    expire = Column(Integer, nullable=True)
    admin_id = Column(Integer, ForeignKey("admins.id"))
    admin = relationship("Admin", back_populates="users", foreign_keys=[admin_id])

    sub_revoked_at = Column(DateTime, nullable=True, default=None)
    sub_updated_at = Column(DateTime, nullable=True, default=None)
    sub_last_user_agent = Column(String(512), nullable=True, default=None)
    created_at = Column(DateTime, default=datetime.utcnow)
    note = Column(String(500), nullable=True, default=None)
    online_at = Column(DateTime, nullable=True, default=None)
    on_hold_expire_duration = Column(BigInteger, nullable=True, default=None)
    on_hold_timeout = Column(DateTime, nullable=True, default=None)

    # Device-limit policy. Both NULL = feature off (legacy users unchanged).
    # Both must be set together; immutable after create. See migration
    # c5e2a8d3f117.
    client_type = Column(Enum(ClientType, name="clienttype"), nullable=True, default=None)
    device_limit = Column(Integer, nullable=True, default=None)

    # Per-user gate for the Outline (Shadowsocks dynamic-key) subscription.
    # Outline sends no X-Hwid, so an Outline device can't be counted by the
    # device-limit policy — access is therefore opt-in per user. Default
    # FALSE (incl. all existing users); the /outline endpoint returns an
    # empty {} config unless enabled. Mutable, unlike client_type /
    # device_limit. See migration b7e2f4a91c83.
    outline_enabled = Column(Boolean, nullable=False, default=False, server_default='0')

    # Pre-computed Happ crypt4 wrapper for this user's subscription URL.
    # Populated at create-time when client_type='happ' and refreshed by
    # revoke_user_sub. NULL for all legacy / non-Happ users — in which case
    # UserResponse generates the plain URL on the fly. See migration
    # d7f6b1a204e9 and app/utils/happ.py.
    encrypted_subscription_url = Column(String(1024), nullable=True, default=None)

    # * Positive values: User will be deleted after the value of this field in days automatically.
    # * Negative values: User won't be deleted automatically at all.
    # * NULL: Uses global settings.
    auto_delete_in_days = Column(Integer, nullable=True, default=None)

    edit_at = Column(DateTime, nullable=True, default=None)
    last_status_change = Column(DateTime, default=datetime.utcnow, nullable=True)

    next_plan = relationship(
        "NextPlan",
        uselist=False,
        back_populates="user",
        cascade="all, delete-orphan"
    )

    devices = relationship(
        "UserSubscriptionDevice",
        back_populates="user",
        cascade="all, delete-orphan",
        passive_deletes=True,
    )

    @hybrid_property
    def reseted_usage(self) -> int:
        return int(sum([log.used_traffic_at_reset for log in self.usage_logs]))

    @reseted_usage.expression
    def reseted_usage(cls):
        return (
            select(func.sum(UserUsageResetLogs.used_traffic_at_reset)).
            where(UserUsageResetLogs.user_id == cls.id).
            label('reseted_usage')
        )

    @property
    def lifetime_used_traffic(self) -> int:
        return int(
            sum([log.used_traffic_at_reset for log in self.usage_logs])
            + self.used_traffic
        )

    @property
    def last_traffic_reset_time(self):
        return self.usage_logs[-1].reset_at if self.usage_logs else self.created_at

    @property
    def excluded_inbounds(self):
        _ = {}
        for proxy in self.proxies:
            _[proxy.type] = [i.tag for i in proxy.excluded_inbounds]
        return _

    @property
    def inbounds(self):
        _ = {}
        for proxy in self.proxies:
            _[proxy.type] = []
            excluded_tags = [i.tag for i in proxy.excluded_inbounds]
            for inbound in xray.config.inbounds_by_protocol.get(proxy.type, []):
                if inbound["tag"] not in excluded_tags:
                    _[proxy.type].append(inbound["tag"])

        return _


excluded_inbounds_association = Table(
    "exclude_inbounds_association",
    Base.metadata,
    Column("proxy_id", ForeignKey("proxies.id")),
    Column("inbound_tag", ForeignKey("inbounds.tag")),
)

# Junction for per-admin host scoping. Empty link set = global host (visible
# to every user); one or more rows = visible only to users whose admin_id is
# in this set. See migration f3c1d8e92a17 and the filter in
# app/subscription/share.py:process_inbounds_and_tags.
host_admins_association = Table(
    "host_admins",
    Base.metadata,
    Column("host_id", ForeignKey("hosts.id", ondelete="CASCADE"), primary_key=True),
    Column("admin_id", ForeignKey("admins.id", ondelete="CASCADE"), primary_key=True),
)

template_inbounds_association = Table(
    "template_inbounds_association",
    Base.metadata,
    Column("user_template_id", ForeignKey("user_templates.id")),
    Column("inbound_tag", ForeignKey("inbounds.tag")),
)


class NextPlan(Base):
    __tablename__ = 'next_plans'

    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, ForeignKey('users.id'), nullable=False)
    data_limit = Column(BigInteger, nullable=False)
    expire = Column(Integer, nullable=True)
    add_remaining_traffic = Column(Boolean, nullable=False, default=False, server_default='0')
    fire_on_either = Column(Boolean, nullable=False, default=True, server_default='0')

    user = relationship("User", back_populates="next_plan")


class UserTemplate(Base):
    __tablename__ = "user_templates"

    id = Column(Integer, primary_key=True)
    name = Column(String(64), nullable=False, unique=True)
    data_limit = Column(BigInteger, default=0)
    expire_duration = Column(BigInteger, default=0)  # in seconds
    username_prefix = Column(String(20), nullable=True)
    username_suffix = Column(String(20), nullable=True)

    inbounds = relationship(
        "ProxyInbound", secondary=template_inbounds_association
    )


class UserUsageResetLogs(Base):
    __tablename__ = "user_usage_logs"

    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, ForeignKey("users.id"))
    user = relationship("User", back_populates="usage_logs")
    used_traffic_at_reset = Column(BigInteger, nullable=False)
    reset_at = Column(DateTime, default=datetime.utcnow)


class Proxy(Base):
    __tablename__ = "proxies"

    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, ForeignKey("users.id"))
    user = relationship("User", back_populates="proxies")
    type = Column(Enum(ProxyTypes), nullable=False)
    settings = Column(JSON, nullable=False)
    excluded_inbounds = relationship(
        "ProxyInbound", secondary=excluded_inbounds_association
    )


class ProxyInbound(Base):
    __tablename__ = "inbounds"

    id = Column(Integer, primary_key=True)
    tag = Column(String(256), unique=True, nullable=False, index=True)
    hosts = relationship(
        "ProxyHost", back_populates="inbound", cascade="all, delete-orphan"
    )


class ProxyHost(Base):
    __tablename__ = "hosts"
    # __table_args__ = (
    #     UniqueConstraint('inbound_tag', 'remark'),
    # )

    id = Column(Integer, primary_key=True)
    remark = Column(String(256), unique=False, nullable=False)
    address = Column(String(256), unique=False, nullable=False)
    port = Column(Integer, nullable=True)
    path = Column(String(256), unique=False, nullable=True)
    sni = Column(String(1000), unique=False, nullable=True)
    host = Column(String(1000), unique=False, nullable=True)
    security = Column(
        Enum(ProxyHostSecurity),
        unique=False,
        nullable=False,
        default=ProxyHostSecurity.inbound_default,
    )
    alpn = Column(
        Enum(ProxyHostALPN),
        unique=False,
        nullable=False,
        default=ProxyHostSecurity.none,
        server_default=ProxyHostSecurity.none.name
    )
    fingerprint = Column(
        Enum(ProxyHostFingerprint),
        unique=False,
        nullable=False,
        default=ProxyHostSecurity.none,
        server_default=ProxyHostSecurity.none.name
    )

    inbound_tag = Column(String(256), ForeignKey("inbounds.tag"), nullable=False)
    inbound = relationship("ProxyInbound", back_populates="hosts")
    allowinsecure = Column(Boolean, nullable=True)
    is_disabled = Column(Boolean, nullable=True, default=False)
    mux_enable = Column(Boolean, nullable=False, default=False, server_default='0')
    fragment_setting = Column(String(100), nullable=True)
    noise_setting = Column(String(2000), nullable=True)
    random_user_agent = Column(Boolean, nullable=False, default=False, server_default='0')
    use_sni_as_host = Column(Boolean, nullable=False, default=False, server_default="0")

    # Empty = global (visible to every user). Populated = visible only to
    # users whose admin_id matches one of these admins. See migration
    # f3c1d8e92a17 and the request-time filter in app/subscription/share.py.
    admins = relationship("Admin", secondary=host_admins_association)

    @property
    def admin_ids(self):
        """Flat int list of linked admin ids, surfaced under the Pydantic
        field name so UserResponse-style from_attributes mapping works
        without an alias hop."""
        return [a.id for a in self.admins]


class System(Base):
    __tablename__ = "system"

    id = Column(Integer, primary_key=True)
    uplink = Column(BigInteger, default=0)
    downlink = Column(BigInteger, default=0)


class JWT(Base):
    __tablename__ = "jwt"

    id = Column(Integer, primary_key=True)
    secret_key = Column(
        String(64), nullable=False, default=lambda: os.urandom(32).hex()
    )


class TLS(Base):
    __tablename__ = "tls"

    id = Column(Integer, primary_key=True)
    key = Column(String(4096), nullable=False)
    certificate = Column(String(2048), nullable=False)


class Node(Base):
    __tablename__ = "nodes"

    id = Column(Integer, primary_key=True)
    name = Column(String(256, collation='NOCASE'), unique=True)
    address = Column(String(256), unique=False, nullable=False)
    port = Column(Integer, unique=False, nullable=False)
    api_port = Column(Integer, unique=False, nullable=False)
    xray_version = Column(String(32), nullable=True)
    status = Column(Enum(NodeStatus), nullable=False, default=NodeStatus.connecting)
    last_status_change = Column(DateTime, default=datetime.utcnow)
    message = Column(String(1024), nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)
    uplink = Column(BigInteger, default=0)
    downlink = Column(BigInteger, default=0)
    user_usages = relationship("NodeUserUsage", back_populates="node", cascade="all, delete-orphan")
    usages = relationship("NodeUsage", back_populates="node", cascade="all, delete-orphan")
    usage_coefficient = Column(Float, nullable=False, server_default=text("1.0"), default=1)


class NodeUserUsage(Base):
    __tablename__ = "node_user_usages"
    __table_args__ = (
        UniqueConstraint('created_at', 'user_id', 'node_id'),
    )

    id = Column(Integer, primary_key=True)
    created_at = Column(DateTime, unique=False, nullable=False)  # one hour per record
    user_id = Column(Integer, ForeignKey("users.id"))
    user = relationship("User", back_populates="node_usages")
    node_id = Column(Integer, ForeignKey("nodes.id"))
    node = relationship("Node", back_populates="user_usages")
    used_traffic = Column(BigInteger, default=0)


class NodeUsage(Base):
    __tablename__ = "node_usages"
    __table_args__ = (
        UniqueConstraint('created_at', 'node_id'),
    )

    id = Column(Integer, primary_key=True)
    created_at = Column(DateTime, unique=False, nullable=False)  # one hour per record
    node_id = Column(Integer, ForeignKey("nodes.id"))
    node = relationship("Node", back_populates="usages")
    uplink = Column(BigInteger, default=0)
    downlink = Column(BigInteger, default=0)


class NotificationReminder(Base):
    __tablename__ = "notification_reminders"

    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, ForeignKey("users.id"))
    user = relationship("User", back_populates="notification_reminders")
    type = Column(Enum(ReminderType), nullable=False)
    threshold = Column(Integer, nullable=True)
    expires_at = Column(DateTime, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)


class UserSubscriptionDevice(Base):
    __tablename__ = "user_subscription_devices"
    __table_args__ = (
        UniqueConstraint("user_id", "device_key", name="uq_user_device"),
        Index("ix_user_recent_devices", "user_id", "last_seen_at"),
    )

    id = Column(Integer, primary_key=True)
    user_id = Column(
        Integer,
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
    )
    device_key = Column(String(128), nullable=False)
    user_agent = Column(String(512), nullable=True)
    client_name = Column(String(64), nullable=True)
    client_version = Column(String(32), nullable=True)
    platform = Column(String(32), nullable=True)
    os_version = Column(String(32), nullable=True)
    device_model = Column(String(64), nullable=True)
    locale = Column(String(16), nullable=True)
    last_ip = Column(String(45), nullable=True)
    # NULL = admin-prefilled slot that has never been contacted by a real
    # fetch. Filled in by record_subscription_device on first contact.
    #
    # IMPORTANT: no Python-side default=datetime.utcnow here — SQLAlchemy
    # treats an explicit None constructor arg as "use the default", which
    # would override the explicit NULL that crud.claim_or_check_hwid /
    # crud.create_user(allowed_hwids=...) need to write. record_subscription_device
    # passes an explicit datetime when it actually wants a value.
    first_seen_at = Column(DateTime, nullable=True)
    last_seen_at = Column(DateTime, nullable=True)
    fetch_count = Column(Integer, default=1, nullable=False)
    # One-way block flag. A blocked device fails the global active filter
    # in app/db/crud.py — invisible to every UI, doesn't count toward
    # device_limit, and the subscription router returns an empty body
    # with a Turkmen "Blok Edildiniz" profile-title when this HWID
    # fetches. Blocking is the only way for an admin to free a slot;
    # time alone never expires a device. No unblock endpoint by design
    # (see migration a8c4e7b21f53).
    is_blocked = Column(Boolean, nullable=False, default=False, server_default='0')
    # Micky "id:" slots (M-Device-Id, survives reinstalls): the "pk:" identity
    # of the device key currently bound to the slot, and the rebind budget —
    # a reinstall (new key, same id) rebinds the slot at most
    # MICKY_MAX_REBINDS times per 24 h window. NULL / 0 for every other row.
    bound_pub_key = Column(String(46), nullable=True)
    rebind_window_start = Column(DateTime, nullable=True)
    rebind_count = Column(Integer, nullable=False, default=0, server_default='0')

    user = relationship("User", back_populates="devices")

    @property
    def hwid(self):
        """The actual HWID value when device_key uses the 'hwid:' prefix.
        None for UA-hash-based entries (where the device is identified by
        a hash of the user-agent rather than a real hardware id)."""
        if self.device_key and self.device_key.startswith("hwid:"):
            return self.device_key[5:]
        return None

    @property
    def device_id(self):
        """The Micky identity shown to admins: the M-Device-Id for "id:" slots,
        the key hash for "pk:" slots (older builds, iOS). None otherwise."""
        if self.device_key and self.device_key.startswith(("id:", "pk:")):
            return self.device_key[3:]
        return None

    @property
    def has_exact_identity(self) -> bool:
        """True for devices identified by a real id — an HWID (Happ) or a
        Micky device id / key — which can be blocked one by one."""
        return bool(self.device_key) and self.device_key.startswith(("hwid:", "pk:", "id:"))


# Microseconds on MySQL (plain DATETIME keeps whole seconds); see MickyCopy.
PRECISE_DATETIME = DateTime().with_variant(mysql.DATETIME(fsp=6), "mysql")


class MickyCopy(Base):
    """A Micky user's encrypted Google copies: G1 (a Firestore document) and G2
    (a Drive file), docs/micky-protocol.md "Google copies".

    The panel owns the key and decides when a copy changed (`dirty_at`, set in
    the same transaction as the change); the marzban-google-sync service owns
    the Google ids and the sync columns. It sets `synced_at` to the `dirty_at`
    it read, so a change that lands during an upload keeps the row dirty.

    No foreign key to users, on purpose: the row has to outlive a deleted user,
    because its ids are the only way to delete the public copies. The service
    deletes the copies and then the row once the panel answers 404 for it."""
    __tablename__ = "micky_copies"

    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, nullable=False, unique=True)
    # b64u (no padding) of 32 random bytes. New when the user starts using
    # Micky and on every revoke; `key_changed_at` records when.
    content_key = Column(String(43), nullable=False)
    key_changed_at = Column(PRECISE_DATETIME, nullable=False, default=datetime.utcnow)
    firestore_doc_id = Column(String(64), nullable=True)
    drive_file_id = Column(String(128), nullable=True)
    content_sha = Column(String(64), nullable=True)
    dirty_at = Column(PRECISE_DATETIME, nullable=False, default=datetime.utcnow, index=True)
    synced_at = Column(PRECISE_DATETIME, nullable=True)
    checked_at = Column(DateTime, nullable=True)
    last_upload_at = Column(DateTime, nullable=True)
    # issued_at of the last copy rendered: the next is max(now, this + 1), so
    # a user's copies are strictly increasing (docs/micky-protocol.md).
    last_issued_at = Column(BigInteger, nullable=True)
    attempts = Column(Integer, nullable=False, default=0, server_default='0')
    last_error = Column(String(255), nullable=True)
    next_attempt_at = Column(DateTime, nullable=True)
    created_at = Column(DateTime, nullable=False, default=datetime.utcnow)


# Lazy by default — only populated by callers that wire it up via
# `with_expression(User.device_count, <correlated subquery>)`. Lets
# crud.get_user_queryset inject a runtime-evaluated cutoff without
# freezing it at module load.
#
# WARNING — measured on prod, do not trust `default_expr` to make this
# safe. For an object loaded through get_user_queryset:
#     after crud.get_user()  -> 1     (with_expression applied)
#     after db.commit()      -> None  (attribute EXPIRED, not 0)
#     after db.refresh()     -> 1     (restored)
# `default_expr` applies when a *query* omits with_expression; it does
# NOT cover an expired attribute. So any response serialized from a
# post-commit ORM object gets None here, and a non-optional
# `device_count: int` on the Pydantic model then raises — turning a
# committed write into a 500. That broke PUT /api/user/{u}, /reset and
# /revoke_sub in production. UserResponse.default_device_count coerces
# None -> 0; keep that validator if you touch this.
User.device_count = query_expression(default_expr=literal(0))
