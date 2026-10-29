"""The subscription routes for Micky, Happ and legacy users, fetched the way
each client really fetches (docs/micky-protocol.md, marzban-micky-brief.md)."""
import base64
import itertools
import time

import pytest

from app.routers import subscription as sub
from app.utils import micky
from app.utils.jwt import create_subscription_token
from tests.micky_helpers import LINK_PRIVATE, Device, is_uri_list

HAPP_UA = "Happ/4.4.1/Android/17891107313301967618"
_n = itertools.count()


def title(r):
    return base64.b64decode(r.headers["profile-title"].removeprefix("base64:")).decode()


def make_user(client, headers, proto, **fields):
    name = f"u{next(_n)}_{fields.get('client_type') or 'legacy'}"
    r = client.post("/api/user", headers=headers,
                    json={"username": name, "proxies": {proto: {}}, "inbounds": {}, **fields})
    assert r.status_code == 200, r.text
    return r.json()


def paths(username):
    token = create_subscription_token(username)
    return [f"/sub/{token}", f"/sub/{token}/sing-box"]


@pytest.fixture(scope="module")
def users(client, sudo, proto):
    return {
        "micky": make_user(client, sudo, proto, client_type="micky", device_limit=2),
        "happ": make_user(client, sudo, proto, client_type="happ", device_limit=2),
        "legacy": make_user(client, sudo, proto),
    }


# ------------------------------------------------------------- the link

def test_micky_link_hides_host_and_token_and_opens_to_the_real_url(users):
    link = users["micky"]["subscription_url"]
    assert link.startswith("micky://s/")
    assert "panel.test" not in link and "/sub/" not in link
    url = micky.open_link(link, LINK_PRIVATE)
    assert url.startswith("https://panel.test/sub/")


def test_revoke_rotates_the_micky_link(client, sudo, users):
    name = users["micky"]["username"]
    before = micky.open_link(users["micky"]["subscription_url"], LINK_PRIVATE)
    time.sleep(1.1)  # tokens carry a whole-second timestamp
    r = client.post(f"/api/user/{name}/revoke_sub", headers=sudo)
    after = micky.open_link(r.json()["subscription_url"], LINK_PRIVATE)
    assert after != before
    users["micky"] = r.json()


def test_plain_url_never_leaves_the_api_for_micky(client, sudo, users):
    listed = client.get("/api/users", headers=sudo).json()["users"]
    row = next(u for u in listed if u["username"] == users["micky"]["username"])
    assert row["subscription_url"].startswith("micky://s/")


# ------------------------------------------- Micky user: only Micky is served

@pytest.mark.parametrize("route", [0, 1])
def test_micky_user_is_served_sealed_to_micky(client, users, route):
    r, payload = Device().get(client, paths(users["micky"]["username"])[route])
    assert r.status_code == 200
    assert r.headers["content-type"] == "application/octet-stream"
    assert "no-store" in r.headers["cache-control"]
    assert "profile-title" not in r.headers  # account headers travel inside the payload
    assert payload["status"] == "ok"
    assert is_uri_list(payload["body"])  # the v2ray list Happ gets
    # Google copies are off here: the only extra header is the removal signal.
    assert payload["headers"].pop("micky-g1") == ""
    assert set(payload["headers"]) <= set(micky.PAYLOAD_HEADERS)
    assert "profile-web-page-url" not in payload["headers"]
    assert isinstance(payload["issued_at"], int)  # orders new-url against Google copies


@pytest.mark.parametrize("route", [0, 1])
@pytest.mark.parametrize("ua,extra", [
    (HAPP_UA, {"X-Hwid": "happ-device-1"}),
    ("curl/8.5.0", {}),
    ("V2Box 10.1.7/iOS 26.0", {"X-Hwid": "E027F0D4"}),
])
def test_micky_user_refuses_every_other_app(client, users, route, ua, extra):
    r = client.get(paths(users["micky"]["username"])[route], headers={"User-Agent": ua, **extra})
    assert r.status_code == 200 and r.content == b""
    assert title(r) == sub.MICKY_ONLY_ANNOUNCEMENT_TK


def test_micky_user_gets_no_info_page_in_a_browser(client, users):
    r = client.get(paths(users["micky"]["username"])[0],
                   headers={"User-Agent": "Mozilla/5.0", "Accept": "text/html"})
    assert r.content == b"" and title(r) == sub.MICKY_ONLY_ANNOUNCEMENT_TK


def test_micky_with_a_malformed_key_gets_an_empty_unsigned_answer(client, users):
    r = client.get(paths(users["micky"]["username"])[0],
                   headers={"User-Agent": "Micky/4.1.2 (android)", "M-Device-Pub": "bogus", "M-Req-Nonce": "x"})
    assert r.status_code == 200 and r.content == b""
    assert "m-micky-sig" not in {k.lower() for k in r.headers}


# ------------------------------------------------ Happ user: never to Micky

def test_happ_user_refuses_micky_sealed(client, users):
    r, payload = Device().get(client, paths(users["happ"]["username"])[0])
    assert payload == {"status": "refused", "code": "refused", "message": sub.HAPP_ONLY_ANNOUNCEMENT_TK}


def test_happ_user_still_served_to_happ(client, users):
    r = client.get(paths(users["happ"]["username"])[0], headers={"User-Agent": HAPP_UA, "X-Hwid": "happ-A"})
    assert r.status_code == 200 and r.content  # plain config, unchanged
    assert users["happ"]["subscription_url"].startswith("happ://")


# ------------------------------------------------- legacy user: unchanged

def test_legacy_user_served_to_any_app_and_sealed_for_micky(client, users):
    path = paths(users["legacy"]["username"])[0]
    assert client.get(path, headers={"User-Agent": "curl/8.5.0"}).content
    r, payload = Device().get(client, path)
    assert payload["status"] == "ok"


# ------------------------------------------- devices: limit, block, reset

def test_device_limit_block_and_reset(client, sudo, proto):
    user = make_user(client, sudo, proto, client_type="micky", device_limit=1)
    path = paths(user["username"])[0]
    a, b = Device(), Device()

    assert a.get(client, path)[1]["status"] == "ok"            # claims the only slot
    assert a.get(client, path)[1]["status"] == "ok"            # same device again
    assert b.get(client, path)[1]["code"] == "device_limit"     # second device refused, sealed

    rows = client.get(f"/api/user/{user['username']}/devices", headers=sudo).json()
    assert len(rows) == 1  # b was refused, so never recorded
    row = rows[0]
    r = client.post(f"/api/user/{user['username']}/devices/{row['id']}/block", headers=sudo)
    assert r.status_code == 200, r.text
    assert a.get(client, path)[1]["code"] == "device_blocked"

    r = client.post(f"/api/user/{user['username']}/reset-devices", headers=sudo)
    assert r.status_code == 200, r.text
    assert b.get(client, path)[1]["status"] == "ok"             # the freed slot
    assert a.get(client, path)[1]["code"] == "device_blocked"   # blocks survive a reset


@pytest.mark.parametrize("status,code", [("disabled", "disabled")])
def test_status_refusal_is_sealed(client, sudo, proto, status, code):
    user = make_user(client, sudo, proto, client_type="micky", device_limit=1)
    client.put(f"/api/user/{user['username']}", headers=sudo, json={"status": status})
    assert Device().get(client, paths(user["username"])[0])[1]["code"] == code


# ------------------------------------------------ review: sealed everywhere

def test_micky_asking_for_html_still_gets_a_sealed_answer_and_the_device_checks(client, sudo, proto):
    user = make_user(client, sudo, proto, client_type="micky", device_limit=1)
    path = paths(user["username"])[0]
    assert Device().get(client, path)[1]["status"] == "ok"
    r, payload = Device().get(client, path, Accept="text/html")
    assert r.headers["content-type"] == "application/octet-stream"
    assert payload["code"] == "device_limit"


@pytest.mark.parametrize("fmt", ["outline", "v2ray", "clash", "sing-box"])
def test_every_client_type_path_is_the_sealed_v2ray_list_for_micky(client, sudo, proto, fmt):
    token = create_subscription_token(make_user(client, sudo, proto, client_type="micky", device_limit=1)["username"])
    r, payload = Device().get(client, f"/sub/{token}/{fmt}")
    assert r.headers["content-type"] == "application/octet-stream"
    assert "profile-web-page-url" not in r.headers and "profile-title" not in r.headers
    assert payload["status"] == "ok" and is_uri_list(payload["body"])


def test_serving_needs_only_the_signing_key(client, sudo, proto, monkeypatch):
    user = make_user(client, sudo, proto, client_type="micky", device_limit=1)
    monkeypatch.setattr(micky, "_LINK_KEYS", {})  # link key removed / rotated away
    assert Device().get(client, paths(user["username"])[0])[1]["status"] == "ok"


def test_revoke_without_a_link_key_never_exposes_the_plain_url(client, sudo, proto, monkeypatch):
    user = make_user(client, sudo, proto, client_type="micky", device_limit=1)
    monkeypatch.setattr(micky, "_LINK_KEYS", {})
    r = client.post(f"/api/user/{user['username']}/revoke_sub", headers=sudo)
    assert r.status_code == 200 and r.json()["subscription_url"] == ""


@pytest.mark.parametrize("name,value,needle", [
    ("MICKY_SIGNING_KEYS", "1:AAAA", "MICKY_SIGNING_KEYS"),
    ("MICKY_SIGNING_KEYS", "abc", "MICKY_SIGNING_KEYS"),
    ("MICKY_LINK_PUBLIC_KEYS", "1:AAAA", "MICKY_LINK_PUBLIC_KEYS"),
])
def test_malformed_key_settings_disable_micky_not_the_panel(name, value, needle, caplog):
    check = micky._check_point if name == "MICKY_LINK_PUBLIC_KEYS" else micky._check_seed
    with pytest.raises(ValueError, match=needle):
        micky.validate_keys(name, value, check)
    assert micky._load_keys(name, value, check) == {}  # logged, panel keeps running


def test_quoted_key_values_are_accepted():
    seed = micky.b64u(bytes(32))
    assert micky.validate_keys("MICKY_SIGNING_KEYS", f'"1:{seed}"', micky._check_seed) == {1: bytes(32)}
