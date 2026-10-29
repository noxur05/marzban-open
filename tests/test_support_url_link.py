"""SUB_SUPPORT_URL_LINK_USERS: listed Happ users fetching with Happ get their
own stored happ:// link as support-url on OK answers; everyone else, and every
refusal, keeps the usual SUB_SUPPORT_URL."""
import pytest

from app.db import GetDB, crud
from app.routers import subscription as sub
from app.utils.jwt import create_subscription_token
from config import SUB_SUPPORT_URL

HAPP_UA = "Happ/4.4.1/Android/17891107313301967618"


def _user(client, sudo, proto, name, **fields):
    r = client.post("/api/user", headers=sudo,
                    json={"username": name, "proxies": {proto: {}}, "inbounds": {}, **fields})
    assert r.status_code == 200, r.text
    return name


def _stored_link(name):
    with GetDB() as db:
        return crud.get_user(db, name).encrypted_subscription_url


@pytest.fixture
def listed(monkeypatch):
    names = set()
    monkeypatch.setattr(sub, "SUB_SUPPORT_URL_LINK_USERS", names)
    return names


@pytest.mark.parametrize("path", ["", "/v2ray"])
def test_a_listed_happ_user_gets_the_stored_link(client, sudo, proto, listed, path):
    name = _user(client, sudo, proto, f"sulink_happ{path.strip('/')}", client_type="happ", device_limit=1)
    listed.add(name)
    r = client.get(f"/sub/{create_subscription_token(name)}{path}",
                   headers={"User-Agent": HAPP_UA, "X-Hwid": "sulink-1"})
    assert r.status_code == 200
    link = _stored_link(name)
    assert link.startswith("happ://crypt")
    assert r.headers["support-url"] == link


def test_an_unlisted_happ_user_keeps_the_usual_url(client, sudo, proto, listed):
    name = _user(client, sudo, proto, "sulink_unlisted", client_type="happ", device_limit=1)
    listed.add("someone_else")
    r = client.get(f"/sub/{create_subscription_token(name)}",
                   headers={"User-Agent": HAPP_UA, "X-Hwid": "sulink-2"})
    assert r.status_code == 200
    assert r.headers["support-url"] == SUB_SUPPORT_URL


def test_a_listed_legacy_user_never_gets_a_link(client, sudo, proto, listed):
    name = _user(client, sudo, proto, "sulink_legacy")
    listed.add(name)
    r = client.get(f"/sub/{create_subscription_token(name)}", headers={"User-Agent": "v2rayNG/1.8.5"})
    assert r.status_code == 200
    assert r.headers["support-url"] == SUB_SUPPORT_URL


def test_a_phone_over_the_limit_never_gets_the_link(client, sudo, proto, listed):
    name = _user(client, sudo, proto, "sulink_limit", client_type="happ", device_limit=1)
    listed.add(name)
    token = create_subscription_token(name)
    assert client.get(f"/sub/{token}", headers={"User-Agent": HAPP_UA, "X-Hwid": "sulink-a"}).status_code == 200
    r = client.get(f"/sub/{token}", headers={"User-Agent": HAPP_UA, "X-Hwid": "sulink-b"})
    assert "support-url" not in r.headers
    assert _stored_link(name) not in r.text


def test_the_name_matches_in_any_case(client, sudo, proto, listed):
    name = _user(client, sudo, proto, "SuLink_Case", client_type="happ", device_limit=1)
    listed.add("sulink_case")  # config.py lowercases the list
    r = client.get(f"/sub/{create_subscription_token(name)}",
                   headers={"User-Agent": HAPP_UA, "X-Hwid": "sulink-3"})
    assert r.headers["support-url"] == _stored_link(name)


def test_another_app_keeps_the_usual_url(client, sudo, proto, listed):
    name = _user(client, sudo, proto, "sulink_other_app", client_type="happ", device_limit=1)
    listed.add(name)
    r = client.get(f"/sub/{create_subscription_token(name)}",
                   headers={"User-Agent": "v2rayNG/1.8.5", "X-Hwid": "sulink-4"})
    assert r.status_code == 200
    assert r.headers["support-url"] == SUB_SUPPORT_URL


def test_a_stored_link_that_is_not_happ_is_never_served(client, sudo, proto, listed):
    name = _user(client, sudo, proto, "sulink_plain", client_type="happ", device_limit=1)
    listed.add(name)
    with GetDB() as db:
        crud.get_user(db, name).encrypted_subscription_url = "https://panel.test/sub/xyz"
        db.commit()
    r = client.get(f"/sub/{create_subscription_token(name)}",
                   headers={"User-Agent": HAPP_UA, "X-Hwid": "sulink-5"})
    assert r.headers["support-url"] == SUB_SUPPORT_URL


def test_an_inactive_user_never_gets_the_link(client, sudo, proto, listed):
    name = _user(client, sudo, proto, "sulink_disabled", client_type="happ", device_limit=1)
    listed.add(name)
    link = _stored_link(name)
    assert client.put(f"/api/user/{name}", headers=sudo, json={"status": "disabled"}).status_code == 200
    r = client.get(f"/sub/{create_subscription_token(name)}",
                   headers={"User-Agent": HAPP_UA, "X-Hwid": "sulink-6"})
    assert "support-url" not in r.headers
    assert link not in r.text


def test_a_blocked_phone_never_gets_the_link(client, sudo, proto, listed):
    name = _user(client, sudo, proto, "sulink_blocked", client_type="happ", device_limit=1)
    listed.add(name)
    token = create_subscription_token(name)
    headers = {"User-Agent": HAPP_UA, "X-Hwid": "sulink-7"}
    assert client.get(f"/sub/{token}", headers=headers).status_code == 200
    row = client.get(f"/api/user/{name}/devices", headers=sudo).json()[0]
    assert client.post(f"/api/user/{name}/devices/{row['id']}/block", headers=sudo).status_code == 200
    r = client.get(f"/sub/{token}", headers=headers)
    assert "support-url" not in r.headers
    assert _stored_link(name) not in r.text
