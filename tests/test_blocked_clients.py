"""Client applications refused outright (device_fingerprint.BLOCKED_CLIENT_RE):
bare library / system User-Agents get the empty "Diňe Happ ulanyň" answer on
both subscription routes, before the info page and before a device row is
written, while every named client seen on production is still served."""
import base64

import pytest

from app.db import GetDB, crud
from app.db.models import UserSubscriptionDevice
from app.routers.subscription import CLIENT_BLOCKED_ANNOUNCEMENT_TK
from app.utils.device_fingerprint import is_blocked_client
from app.utils.jwt import create_subscription_token

# Real User-Agents from production's user_subscription_devices (2026-10-21).
REFUSED = [
    "Dalvik/2.1.0 (Linux; U; Android 16; SM-A546E Build/BP4A.251205.006)",
    "okhttp/3.0.1",
    "Go-http-client/1.1",
    "Go-http-client/2.0",
    "AppleCoreMedia/1.0.0.24A437 (iPhone; U; CPU OS 27_0 like Mac OS X; ru_ru)",
    "GoogleMessages/20.2 facebookexternalhit/1.1 Facebot Twitterbot/1.0",
]
SERVED = [
    "Happ/6.1.0/ios/2610091516618",
    "Happ/4.7.1/Android/17912108360521999608",
    "Micky/4.2.2802 (android) like ClashMeta v2ray sing-box",
    "MickyX/4.1.2 (android) like ClashMeta v2ray sing-box",
    # Our own subs-poller, fetching panels that run this same code.
    "Happ/5.2.0/ios/micky-subs-poller",
    "micky-subs-poller/1.0",
    "HiddifyNext/5.0.0 (macos) like ClashMeta v2ray sing-box",
    "HiddifyNextX/4.1.1 (windows) like ClashMeta v2ray sing-box",
    "INCY/2.6.3/ios CFNetwork/3860.500.112 Darwin/25.4.0",
    "NekoBox/PC/4.0.1 (Prefer ClashMeta Format)",
    "NekoRay/PC/3.26 (Prefer ClashMeta Format)",
    "SecureOrbit/1.0.3/ios/1",
    "Streisand/9 CFNetwork/3826.600.41 Darwin/24.6.0",
    "Throne/1.1.4",
    "v2box/6.1.4",
    "V2Box 10.1.9/iOS 26.5",
    "v2rayN/7.24.4",
    "v2rayNG/1.9.46",
    "v2raytun/android",
    "V2rayU/4.2.5 CFNetwork/1240.0.4.5 Darwin/20.6.0",
    # A real VPN app on production despite the bare library name.
    "ktor-client",
    "Mozilla/5.0 (Linux; Android 14; K) AppleWebKit/537.36 (KHTML, like Gecko) Chrome",
]


@pytest.mark.parametrize("ua", REFUSED + [
    "OKHTTP/4.12.0",            # any version, any case
    "  okhttp/3.0.1",           # leading whitespace
])
def test_refused(ua):
    assert is_blocked_client(ua)


@pytest.mark.parametrize("ua", SERVED + [
    "",
    None,
    "MyVPN/2.0 (Dalvik/2.1.0; Android 14) okhttp/4.12.0",  # named first: served
    "okhttp",                   # no slash: not the library's default shape
    "GoogleMessagesX/1.0",
])
def test_served(ua):
    assert not is_blocked_client(ua)


def _user(client, sudo, proto, name, **fields):
    r = client.post("/api/user", headers=sudo,
                    json={"username": name, "proxies": {proto: {}}, "inbounds": {}, **fields})
    assert r.status_code == 200, r.text
    return name


def _device_count(name):
    with GetDB() as db:
        user = crud.get_user(db, name)
        return db.query(UserSubscriptionDevice).filter_by(user_id=user.id).count()


def _title(r):
    return base64.b64decode(r.headers["profile-title"].removeprefix("base64:")).decode()


@pytest.mark.parametrize("path", ["", "/v2ray"])
@pytest.mark.parametrize("accept", ["*/*", "text/html"])
@pytest.mark.parametrize("ua", REFUSED)
def test_route_refuses_and_records_nothing(client, sudo, proto, ua, accept, path):
    name = _user(client, sudo, proto,
                 f"blk_{REFUSED.index(ua)}_{path.strip('/') or 'auto'}_{'h' if accept == 'text/html' else 'a'}")
    r = client.get(f"/sub/{create_subscription_token(name)}{path}",
                   headers={"User-Agent": ua, "Accept": accept})
    assert r.status_code == 200
    assert r.content == b""
    assert _title(r) == CLIENT_BLOCKED_ANNOUNCEMENT_TK
    if not path:  # only the auto route records devices at all
        assert _device_count(name) == 0


def test_named_client_still_served_and_recorded(client, sudo, proto):
    name = _user(client, sudo, proto, "blk_control")
    r = client.get(f"/sub/{create_subscription_token(name)}",
                   headers={"User-Agent": "v2box/6.1.4"})
    assert r.status_code == 200
    assert r.content
    assert _title(r) == name
    assert _device_count(name) == 1


GO_UAS = ["Go-http-client/1.1", "Go-http-client/2.0"]


@pytest.mark.parametrize("ua", GO_UAS)
def test_outline_client_still_gets_its_key(client, sudo, proto, ua):
    # The Outline app fetches ssconf://…/outline with Go's default UA.
    name = _user(client, sudo, proto, f"blk_outline_{ua[-3]}", outline_enabled=True)
    r = client.get(f"/sub/{create_subscription_token(name)}/outline", headers={"User-Agent": ua})
    assert r.status_code == 200
    key = r.json()
    assert key["server"] and key["server_port"] and key["password"] and key["method"]


def test_outline_exemption_is_go_only_and_outline_only(client, sudo, proto):
    name = _user(client, sudo, proto, "blk_outline_scope", outline_enabled=True)
    token = create_subscription_token(name)
    for path, ua in [("/outline", "okhttp/3.0.1"), ("/outline", "Dalvik/2.1.0"),
                     ("", GO_UAS[0]), ("/v2ray", GO_UAS[0]), ("/sing-box", GO_UAS[1])]:
        r = client.get(f"/sub/{token}{path}", headers={"User-Agent": ua})
        assert r.content == b"", (path, ua)
        assert _title(r) == CLIENT_BLOCKED_ANNOUNCEMENT_TK, (path, ua)


def test_outline_route_still_empty_for_opted_out_user(client, sudo, proto):
    name = _user(client, sudo, proto, "blk_outline_off")
    r = client.get(f"/sub/{create_subscription_token(name)}/outline", headers={"User-Agent": GO_UAS[0]})
    assert r.status_code == 200
    assert r.text == "{}"
