import os
from datetime import datetime

import yaml

from app.utils.system import readable_size


def to_yaml(obj):
    if not obj:
        return ""

    return yaml.dump(obj, allow_unicode=True, indent=2)


def exclude_keys(obj, *target_keys):
    return {key: val for key, val in obj.items() if key not in target_keys}


def only_keys(obj, *target_keys):
    return {key: val for key, val in obj.items() if key in target_keys}


def datetimeformat(dt):
    if isinstance(dt, int):
        dt = datetime.fromtimestamp(dt)
    formatted_datetime = dt.strftime('%Y-%m-%d %H:%M:%S')
    return formatted_datetime


def env_override(value, key):
    return os.getenv(key, value)


TK_MONTHS_SHORT = [
    "Ýan", "Few", "Mart", "Apr", "Maý", "Iýun",
    "Iýul", "Awg", "Sen", "Okt", "Noý", "Dek",
]


def _to_dt(value):
    if value is None or value == 0:
        return None
    if isinstance(value, (int, float)):
        return datetime.utcfromtimestamp(value)
    return value


def tkdate(value):
    dt = _to_dt(value)
    if not dt:
        return ""
    return f"{dt.day} {TK_MONTHS_SHORT[dt.month - 1]} {dt.year}"


def tkdatetime(value):
    dt = _to_dt(value)
    if not dt:
        return ""
    return f"{dt.day} {TK_MONTHS_SHORT[dt.month - 1]} {dt.year}, {dt.hour:02d}:{dt.minute:02d}"


def tkrelative(value):
    dt = _to_dt(value)
    if not dt:
        return ""
    diff_sec = (datetime.utcnow() - dt).total_seconds()
    if diff_sec < 0:
        diff_sec = 0
    mins = int(diff_sec // 60)
    if mins < 1:
        return "ýaňy ýaňyja"
    if mins < 60:
        return f"{mins} min öň"
    hrs = mins // 60
    if hrs < 24:
        return f"{hrs} sag öň"
    days = hrs // 24
    if days < 30:
        return f"{days} gün öň"
    return tkdate(value)


def days_until(value):
    """Whole days from now to a unix timestamp or datetime; floor, signed."""
    if not value:
        return None
    if isinstance(value, datetime):
        ts = value.timestamp()
    else:
        ts = float(value)
    return int((ts - datetime.utcnow().timestamp()) // 86400)


CUSTOM_FILTERS = {
    "yaml": to_yaml,
    "except": exclude_keys,
    "only": only_keys,
    "datetime": datetimeformat,
    "bytesformat": readable_size,
    "tkdate": tkdate,
    "tkdatetime": tkdatetime,
    "tkrelative": tkrelative,
    "days_until": days_until,
}
