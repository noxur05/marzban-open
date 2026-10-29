import base64
import contextlib
import contextvars
import math
import random
import secrets
from collections import defaultdict
from datetime import datetime as dt
from datetime import timedelta
from typing import TYPE_CHECKING, List, Literal, Union

from jdatetime import date as jd

from app import xray
from app.utils.system import get_public_ip, get_public_ipv6, readable_size

from . import *
from . import external, manual

if TYPE_CHECKING:
    from app.models.user import UserResponse

from config import (
    ACTIVE_STATUS_TEXT,
    DISABLED_STATUS_TEXT,
    EXPIRED_STATUS_TEXT,
    LIMITED_STATUS_TEXT,
    ONHOLD_STATUS_TEXT,
)

SERVER_IP = get_public_ip()
SERVER_IPV6 = get_public_ipv6()

STATUS_EMOJIS = {
    "active": "✅",
    "expired": "⌛️",
    "limited": "🪫",
    "disabled": "❌",
    "on_hold": "🔌",
}

STATUS_TEXTS = {
    "active": ACTIVE_STATUS_TEXT,
    "expired": EXPIRED_STATUS_TEXT,
    "limited": LIMITED_STATUS_TEXT,
    "disabled": DISABLED_STATUS_TEXT,
    "on_hold": ONHOLD_STATUS_TEXT,
}


# Per-render randomness: the wildcard salts and the pick among several SNIs,
# hosts, addresses and Reality short ids. Subscription requests use the module
# RNG. A caller that needs the same output for the same inputs (the Google
# copies, uploaded only when they change) installs a seeded one.
_rng: contextvars.ContextVar = contextvars.ContextVar("subscription_rng", default=None)


@contextlib.contextmanager
def seeded_randomness(seed):
    token = _rng.set(random.Random(seed))
    try:
        yield
    finally:
        _rng.reset(token)


def _choice(seq):
    return (_rng.get() or random).choice(seq)


def _salt() -> str:
    rng = _rng.get()
    return f"{rng.getrandbits(64):016x}" if rng else secrets.token_hex(8)


def generate_v2ray_links(proxies: dict, inbounds: dict, extra_data: dict, reverse: bool,
                         user_admin_id: int | None = None) -> list:
    format_variables = setup_format_variables(extra_data)
    conf = V2rayShareLink()
    return process_inbounds_and_tags(inbounds, proxies, format_variables, conf=conf,
                                     reverse=reverse, user_admin_id=user_admin_id)


def generate_clash_subscription(
        proxies: dict, inbounds: dict, extra_data: dict, reverse: bool, is_meta: bool = False,
        user_admin_id: int | None = None,
) -> str:
    if is_meta is True:
        conf = ClashMetaConfiguration()
    else:
        conf = ClashConfiguration()

    format_variables = setup_format_variables(extra_data)
    return process_inbounds_and_tags(
        inbounds, proxies, format_variables, conf=conf, reverse=reverse,
        user_admin_id=user_admin_id,
    )


def generate_singbox_subscription(
        proxies: dict, inbounds: dict, extra_data: dict, reverse: bool,
        user_admin_id: int | None = None,
) -> str:
    conf = SingBoxConfiguration()

    format_variables = setup_format_variables(extra_data)
    return process_inbounds_and_tags(
        inbounds, proxies, format_variables, conf=conf, reverse=reverse,
        user_admin_id=user_admin_id,
    )


def generate_outline_subscription(
        proxies: dict, inbounds: dict, extra_data: dict, reverse: bool,
        user_admin_id: int | None = None,
) -> str:
    conf = OutlineConfiguration()

    format_variables = setup_format_variables(extra_data)
    return process_inbounds_and_tags(
        inbounds, proxies, format_variables, conf=conf, reverse=reverse,
        user_admin_id=user_admin_id,
    )


def generate_v2ray_json_subscription(
        proxies: dict, inbounds: dict, extra_data: dict, reverse: bool,
        user_admin_id: int | None = None,
) -> str:
    conf = V2rayJsonConfig()

    format_variables = setup_format_variables(extra_data)
    return process_inbounds_and_tags(
        inbounds, proxies, format_variables, conf=conf, reverse=reverse,
        user_admin_id=user_admin_id,
    )


def generate_subscription(
        user: "UserResponse | SubscriptionUser",
        config_format: Literal["v2ray", "clash-meta", "clash", "sing-box", "outline", "v2ray-json"],
        as_base64: bool,
        reverse: bool,
) -> str:
    # Per-admin host scoping: an unowned host is global; an owned host
    # is only visible to users whose admin matches. user.admin is the
    # joinedload'd Admin Pydantic; None means the user has no owner
    # (sees global hosts only).
    user_admin_id = user.admin.id if user.admin else None
    kwargs = {
        "proxies": user.proxies,
        "inbounds": user.inbounds,
        "extra_data": user.__dict__,
        "reverse": reverse,
        "user_admin_id": user_admin_id,
    }

    # Hosts mirrored from other panels, for the subscriptions assigned to this
    # user's owning admin. Resolved here — the single funnel every format and
    # both subscription endpoints pass through — rather than in
    # UserResponse.validate_links, which also runs for every row of
    # GET /api/users and must not pay for this.
    external_payloads = external.payloads_for(user, config_format)
    # Operator-authored hosts assigned to this user's owning admin. Same seam,
    # same v2ray-only line-append; resolved here (the single funnel) not in
    # UserResponse.validate_links.
    manual_payloads = manual.payloads_for(user, config_format)

    if config_format == "v2ray":
        config = "\n".join(generate_v2ray_links(**kwargs))
    elif config_format == "clash-meta":
        config = generate_clash_subscription(**kwargs, is_meta=True)
    elif config_format == "clash":
        config = generate_clash_subscription(**kwargs)
    elif config_format == "sing-box":
        config = generate_singbox_subscription(**kwargs)
    elif config_format == "outline":
        config = generate_outline_subscription(**kwargs)
    elif config_format == "v2ray-json":
        config = generate_v2ray_json_subscription(**kwargs)
    else:
        raise ValueError(f'Unsupported format "{config_format}"')

    # Before the base64 step: the payloads are stored decoded so the merge is a
    # plain line append and only the final result gets encoded. Manual hosts go
    # through the same merge (idempotent line concatenation), after the external
    # ones for a stable order.
    config = external.merge_into(config, config_format, external_payloads)
    config = external.merge_into(config, config_format, manual_payloads)

    if as_base64:
        config = base64.b64encode(config.encode()).decode()

    return config


def format_time_left(seconds_left: int) -> str:
    if not seconds_left or seconds_left <= 0:
        return "∞"

    minutes, seconds = divmod(seconds_left, 60)
    hours, minutes = divmod(minutes, 60)
    days, hours = divmod(hours, 24)
    months, days = divmod(days, 30)

    result = []
    if months:
        result.append(f"{months}m")
    if days:
        result.append(f"{days}d")
    if hours and (days < 7):
        result.append(f"{hours}h")
    if minutes and not (months or days):
        result.append(f"{minutes}m")
    if seconds and not (months or days):
        result.append(f"{seconds}s")
    return " ".join(result)


def setup_format_variables(extra_data: dict) -> dict:
    from app.models.user import UserStatus

    user_status = extra_data.get("status")
    expire_timestamp = extra_data.get("expire")
    on_hold_expire_duration = extra_data.get("on_hold_expire_duration")
    now = dt.utcnow()
    now_ts = now.timestamp()

    if user_status != UserStatus.on_hold:
        if expire_timestamp is not None and expire_timestamp >= 0:
            seconds_left = expire_timestamp - int(dt.utcnow().timestamp())
            expire_datetime = dt.fromtimestamp(expire_timestamp)
            expire_date = expire_datetime.date()
            jalali_expire_date = jd.fromgregorian(
                year=expire_date.year, month=expire_date.month, day=expire_date.day
            ).strftime("%Y-%m-%d")
            if now_ts < expire_timestamp:
                days_left = (expire_datetime - dt.utcnow()).days + 1
                time_left = format_time_left(seconds_left)
            else:
                days_left = "0"
                time_left = "0"

        else:
            days_left = "∞"
            time_left = "∞"
            expire_date = "∞"
            jalali_expire_date = "∞"
    else:
        if on_hold_expire_duration is not None and on_hold_expire_duration >= 0:
            days_left = timedelta(seconds=on_hold_expire_duration).days
            time_left = format_time_left(on_hold_expire_duration)
            expire_date = "-"
            jalali_expire_date = "-"
        else:
            days_left = "∞"
            time_left = "∞"
            expire_date = "∞"
            jalali_expire_date = "∞"

    if extra_data.get("data_limit"):
        data_limit = readable_size(extra_data["data_limit"])
        data_left = extra_data["data_limit"] - extra_data["used_traffic"]
        usage_Percentage = round((extra_data["used_traffic"] / extra_data["data_limit"]) * 100.0, 2)

        if data_left < 0:
            data_left = 0
        data_left = readable_size(data_left)
    else:
        data_limit = "∞"
        data_left = "∞"
        usage_Percentage = "∞"

    status_emoji = STATUS_EMOJIS.get(extra_data.get("status")) or ""
    status_template = STATUS_TEXTS.get(extra_data.get("status")) or ""

    # Create a temporary dictionary with variables excluding STATUS_TEXT
    temp_vars = {
        "SERVER_IP": SERVER_IP,
        "SERVER_IPV6": SERVER_IPV6,
        "USERNAME": extra_data.get("username", "{USERNAME}"),
        "DATA_USAGE": readable_size(extra_data.get("used_traffic")),
        "DATA_LIMIT": data_limit,
        "DATA_LEFT": data_left,
        "DAYS_LEFT": days_left,
        "EXPIRE_DATE": expire_date,
        "JALALI_EXPIRE_DATE": jalali_expire_date,
        "TIME_LEFT": time_left,
        "STATUS_EMOJI": status_emoji,
        "USAGE_PERCENTAGE": usage_Percentage,
    }

    # Format the status text using the temporary variables
    status_text = status_template.format_map(defaultdict(lambda: "<missing>", temp_vars))

    # Create the final format_variables including the formatted STATUS_TEXT
    format_variables = defaultdict(
        lambda: "<missing>",
        {
            **temp_vars,
            "STATUS_TEXT": status_text,
        },
    )

    return format_variables


def process_inbounds_and_tags(
        inbounds: dict,
        proxies: dict,
        format_variables: dict,
        conf: Union[
            V2rayShareLink,
            V2rayJsonConfig,
            SingBoxConfiguration,
            ClashConfiguration,
            ClashMetaConfiguration,
            OutlineConfiguration
        ],
        reverse=False,
        user_admin_id: int | None = None,
) -> Union[List, str]:
    _inbounds = []
    for protocol, tags in inbounds.items():
        for tag in tags:
            _inbounds.append((protocol, [tag]))
    index_dict = {proxy: index for index, proxy in enumerate(
        xray.config.inbounds_by_tag.keys())}
    inbounds = sorted(
        _inbounds, key=lambda x: index_dict.get(x[1][0], float('inf')))

    for protocol, tags in inbounds:
        settings = proxies.get(protocol)
        if not settings:
            continue

        format_variables.update({"PROTOCOL": protocol.name})
        for tag in tags:
            inbound = xray.config.inbounds_by_tag.get(tag)
            if not inbound:
                continue

            format_variables.update({"TRANSPORT": inbound["network"]})
            host_inbound = inbound.copy()
            for host in xray.hosts.get(tag, []):
                # Per-admin host scoping. Empty admin_ids = global (visible
                # to all). Populated = visible only to users whose admin is
                # in the set. Orphans (user_admin_id is None) see global
                # hosts only — None will not be in any populated set.
                allowed_admins = host.get("admin_ids") or ()
                if allowed_admins and user_admin_id not in allowed_admins:
                    continue
                sni = ""
                sni_list = host["sni"] or inbound["sni"]
                if sni_list:
                    salt = _salt()
                    sni = _choice(sni_list).replace("*", salt)

                # Into this host's copy, not the shared inbound: the rendered
                # link must carry this pick (it used to carry the previous
                # render's, from any thread), and config is shared state.
                if sids := inbound.get("sids"):
                    host_inbound["sid"] = _choice(sids)

                req_host = ""
                req_host_list = host["host"] or inbound["host"]
                if req_host_list:
                    salt = _salt()
                    req_host = _choice(req_host_list).replace("*", salt)

                address = ""
                address_list = host['address']
                if host['address']:
                    salt = _salt()
                    address = _choice(address_list).replace('*', salt)

                if host["path"] is not None:
                    path = host["path"].format_map(format_variables)
                else:
                    path = inbound.get("path", "").format_map(format_variables)

                if host.get("use_sni_as_host", False) and sni:
                    req_host = sni

                host_inbound.update(
                    {
                        "port": host["port"] or inbound["port"],
                        "sni": sni,
                        "host": req_host,
                        "tls": inbound["tls"] if host["tls"] is None else host["tls"],
                        "alpn": host["alpn"] if host["alpn"] else None,
                        "path": path,
                        "fp": host["fingerprint"] or inbound.get("fp", ""),
                        "ais": host["allowinsecure"]
                        or inbound.get("allowinsecure", ""),
                        "mux_enable": host["mux_enable"],
                        "fragment_setting": host["fragment_setting"],
                        "noise_setting": host["noise_setting"],
                        "random_user_agent": host["random_user_agent"],
                    }
                )

                conf.add(
                    remark=host["remark"].format_map(format_variables),
                    address=address.format_map(format_variables),
                    inbound=host_inbound,
                    settings=settings.model_dump()
                )

    return conf.render(reverse=reverse)


def encode_title(text: str) -> str:
    return f"base64:{base64.b64encode(text.encode()).decode()}"
