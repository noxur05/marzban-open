"""Parse subscription-fetch headers into a device fingerprint.

The primary identity field (`device_key`) is:
  - "pk:<b64u(sha256(device key))>" for the Micky app (its own P-256 key)
  - "hwid:<X-Hwid>" when the client sends X-Hwid (Happ does)
  - "ua:<sha1(user_agent)>" otherwise (per-installation fallback)

Patterns derived from real production traffic analysis (~33k requests sampled):
  Happ format        : Happ/<version>/<platform>/<install-id>
  Generic format     : <client>/<version>...
  HiddifyNext        : HiddifyNext/<ver> (<platform>) ...
  Streisand iOS      : Streisand/<n> CFNetwork/... Darwin/...

Noise UAs (Mozilla browsers fetching for link previews, WhatsApp,
crawlers) are filtered out — they create rows we don't want.
"""

import hashlib
import re
from dataclasses import dataclass
from typing import Mapping, Optional

from app.utils import micky

# Happ: most-specific format, ~80% of traffic
HAPP_RE = re.compile(r"^Happ/([\d.]+)/(Android|ios|Windows)/", re.I)
# HiddifyNext: "HiddifyNext/4.1.1 (android) like ClashMeta v2ray sing-box"
HIDDIFY_RE = re.compile(r"^HiddifyNext/([\d.]+)\s*\((\w+)\)", re.I)
# Streisand iOS: contains "Darwin" — iOS marker
STREISAND_RE = re.compile(r"^Streisand/(\d+).*Darwin", re.I)
# Android default HTTP UA, sent by apps that don't set their own:
#   "Dalvik/2.1.0 (Linux; U; Android 14; SM-S918B Build/UP1A.231005.007)"
# Captures (android_version, device_model). The model is a *product*
# code (every phone of that model reports it identically): it never
# feeds device_key, but it IS the key for model-based blocking of these
# id-less clients (crud.is_model_blocked) — so all same-model devices
# share one block. A trailing build/ROM token (" Build/...", " MIUI/...",
# etc.) is stripped so the model stays stable across OS updates; multi-
# word models like "Infinix X6728" keep their space.
DALVIK_RE = re.compile(
    r"Dalvik/[\d.]+\s*\(.*?Android\s+([\d.]+)\s*;\s*"
    r"([^;)]+?)(?:\s+[A-Za-z][\w-]*/[^);]*)?\)",
    re.I,
)
# Generic fallback: "<name>/<version>" or "<name> <version>"
GENERIC_RE = re.compile(r"^([A-Za-z][\w-]+)[/\s]([\d.]+)")
# Last-resort name extraction when no version present (e.g. "ktor-client")
NAME_ONLY_RE = re.compile(r"^([A-Za-z][\w-]{1,30})")

# Client applications refused outright, before any per-user device policy.
#
# Dalvik is Android's default HttpURLConnection User-Agent — the *runtime*
# identifying itself because the app never set one:
#   "Dalvik/2.1.0 (Linux; U; Android 14; SM-A525F Build/UP1A.231005.007)"
# Every client actually in use here names itself (Happ, V2Box, Streisand,
# HiddifyNext, NekoBox, v2rayNG...), so a bare Dalvik fetch is an app that
# refuses to say what it is. Measured on production 2026-08-15: 217 device rows
# across 89 users, of which 55 had already been blocked by hand one at a time —
# which is the manual toil this replaces.
#
# The same reasoning covers other bare library / system User-Agents, added
# 2026-10-21 at the owner's request ("these should be blocked, fully"):
#   "okhttp/3.0.1"                    Square's Java/Kotlin HTTP library default
#   "Go-http-client/1.1" | "/2.0"     Go's net/http default
#   "AppleCoreMedia/1.0.0.23B85 (iPhone; U; CPU OS 26_1 …)"   iOS media stack
#   "GoogleMessages/20.2 facebookexternalhit/1.1 Facebot …"   SMS/RCS preview
# Measured on production that day: 18 device rows across 14 users, every one
# of whom also fetches with a named client (Happ, V2Box, Streisand, …), so
# nobody loses access. Each okhttp device fetched on one day only (1–7 times)
# and never again — a tool checking the link, not a client refreshing it.
# NOT here: "ktor-client", the same kind of bare library name, is a real VPN
# app on production — the only client of several active users.
#
# Distinct from NOISE_RE below: noise is *not recorded* but still served, since
# a link preview fetching a subscription is harmless. This list is *refused* —
# GoogleMessages included, though it is a link preview too.
#
# Matched from the START of the string, which is what keeps a well-behaved
# client that merely mentions the runtime in a longer UA ("MyVPN/2.0
# (Dalvik/2.1.0; Android 14)") out of the net. NOTE the anchoring comes from
# `is_blocked_client` using `re.match`, not from the leading `^` — switching
# that call to `re.search` would silently remove the guard.
#
# This is intentionally broader than DALVIK_RE above, which additionally
# requires the full "(Linux; U; Android <ver>; <model> …)" shape because it has
# to extract a model. Refusing does not need a model, so any bare `Dalvik/`
# is enough.
BLOCKED_CLIENT_RE = re.compile(
    r"^\s*(Dalvik|okhttp|Go-http-client|AppleCoreMedia|GoogleMessages)/", re.I)

# The one exception: the official Outline client fetches its dynamic key (the
# dashboard's ssconf://…/sub/<token>/outline link) with a plain Go http.Client,
# so it sends Go's default UA and refusing it there would cut off every
# outline_enabled user at their next connect. Exempt on the /outline route
# only, which serves nothing but `{}` unless an admin opted the user in.
# These fetches never write device rows (only the auto route records), which
# is why the production measurement above could not see them.
OUTLINE_KEY_CLIENT_RE = re.compile(r"^\s*Go-http-client/", re.I)

# UAs we don't want to track (link-preview crawlers, browsers, generic HTTP libs)
NOISE_RE = re.compile(
    r"^(WhatsApp|Mozilla/|curl/|Wget/|python-requests|"
    r"TwitterBot|FacebookExternalHit|Slackbot|TelegramBot|Discordbot|"
    r"LinkedInBot|Googlebot|bingbot|YandexBot)",
    re.I,
)

# Hard limits to protect DB columns from absurd values
MAX_UA_LEN = 512
MAX_HWID_LEN = 80
MAX_MODEL_LEN = 64
MAX_OSVER_LEN = 32
MAX_LOCALE_LEN = 16


@dataclass
class DeviceFingerprint:
    """Parsed identity for one subscription fetch."""
    device_key: str
    user_agent: str
    client_name: Optional[str]
    client_version: Optional[str]
    platform: Optional[str]
    os_version: Optional[str]
    device_model: Optional[str]
    locale: Optional[str]


def _trim(value: Optional[str], maxlen: int) -> Optional[str]:
    """Strip + truncate; treat empty strings as None."""
    if not value:
        return None
    s = value.strip()
    if not s:
        return None
    return s[:maxlen]


def is_blocked_client(user_agent: Optional[str], outline_key: bool = False) -> bool:
    """Whether this User-Agent belongs to a client application we refuse.

    A global rule: it does not depend on the user, their device_limit, or any
    per-device block row. Callers should check it before per-user gating and
    return an announcement, exactly like the other refusal paths.

    `outline_key` is True on the /outline route, where the Outline client's Go
    default UA is let through (see OUTLINE_KEY_CLIENT_RE).

    An empty or missing User-Agent is NOT blocked here — that is a different
    condition with different causes (a bare curl, a health check), and blocking
    it would be a policy nobody asked for.
    """
    if not (user_agent and BLOCKED_CLIENT_RE.match(user_agent)):
        return False
    return not (outline_key and OUTLINE_KEY_CLIENT_RE.match(user_agent))


def parse(headers: Mapping[str, str]) -> Optional[DeviceFingerprint]:
    """Build a device fingerprint from request headers.

    Returns None for noise UAs (browsers, crawlers, missing UA) — caller
    should skip recording in that case. Headers lookup is case-insensitive
    when called with FastAPI/Starlette's Headers; for plain dicts the
    caller must lowercase keys.
    """
    ua_raw = headers.get("user-agent") or headers.get("User-Agent") or ""
    ua = ua_raw.strip()[:MAX_UA_LEN]
    if not ua or NOISE_RE.match(ua):
        return None

    # True device identity: Micky by its public key (M-Device-Pub, checked by
    # the router before recording), Happ by X-Hwid. Micky never falls back to
    # X-Hwid, and M- headers from anything else are ignored.
    is_micky = micky.is_micky(ua)
    micky_req = micky.parse_request(headers) if is_micky else None
    hwid = "" if is_micky else (headers.get("x-hwid") or headers.get("X-Hwid") or "").strip()[:MAX_HWID_LEN]
    if micky_req:
        device_key = micky_req.device_key
    elif hwid:
        device_key = f"hwid:{hwid}"
    else:
        device_key = "ua:" + hashlib.sha1(ua.encode("utf-8")).hexdigest()

    client_name: Optional[str] = None
    client_version: Optional[str] = None
    platform: Optional[str] = None
    os_version: Optional[str] = None
    device_model: Optional[str] = None

    if (m := HAPP_RE.match(ua)):
        client_name = "Happ"
        client_version = m.group(1)
        # Normalize: "ios" → "iOS", others Title-cased as-is
        plat = m.group(2)
        platform = "iOS" if plat.lower() == "ios" else plat
    elif (m := HIDDIFY_RE.match(ua)):
        client_name = "HiddifyNext"
        client_version = m.group(1)
        platform = m.group(2).capitalize()
    elif STREISAND_RE.match(ua):
        client_name = "Streisand"
        platform = "iOS"
    elif (m := DALVIK_RE.match(ua)):
        # App didn't identify itself — the Android runtime did. Keep
        # "Dalvik" as the name so the admin can see the UA is generic,
        # but surface the model/OS the string does carry.
        client_name = "Dalvik"
        platform = "Android"
        os_version = m.group(1).strip()[:MAX_OSVER_LEN]
        device_model = m.group(2).strip()[:MAX_MODEL_LEN]
    elif (m := GENERIC_RE.match(ua)):
        client_name = m.group(1)
        client_version = m.group(2)
    elif (m := NAME_ONLY_RE.match(ua)):
        client_name = m.group(1)

    # Client-supplied device headers (Micky: M-*, Happ: X-*) — enrich /
    # override parsed values when present
    p = "m" if is_micky else "x"
    return DeviceFingerprint(
        device_key=device_key,
        user_agent=ua,
        client_name=client_name,
        client_version=client_version,
        platform=_trim(headers.get(f"{p}-device-os"), MAX_OSVER_LEN) or platform,
        os_version=_trim(headers.get(f"{p}-ver-os"), MAX_OSVER_LEN) or os_version,
        device_model=_trim(headers.get(f"{p}-device-model"), MAX_MODEL_LEN) or device_model,
        locale=_trim(headers.get(f"{p}-device-locale"), MAX_LOCALE_LEN),
    )
