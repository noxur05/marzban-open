"""Who may create which kind of user, and what can't change afterwards."""
import itertools

import pytest

from app.utils import micky

_n = itertools.count()


def body(proto, **fields):
    return {"username": f"pol{next(_n)}", "proxies": {proto: {}}, "inbounds": {}, **fields}


@pytest.fixture(scope="module")
def happ_only(client, sudo):
    client.post("/api/admin", headers=sudo, json={"username": "ho_admin", "password": "pw12345",
                                                  "is_sudo": False, "happ_only": True})
    r = client.post("/api/admin/token", data={"username": "ho_admin", "password": "pw12345"})
    return {"Authorization": f"Bearer {r.json()['access_token']}"}


@pytest.mark.parametrize("app_type", ["happ", "micky"])
def test_happ_only_admin_may_create_either_app(client, happ_only, proto, app_type):
    r = client.post("/api/user", headers=happ_only, json=body(proto, client_type=app_type, device_limit=3))
    assert r.status_code == 200, r.text


def test_happ_only_admin_may_not_create_unlimited_users(client, happ_only, proto):
    assert client.post("/api/user", headers=happ_only, json=body(proto)).status_code == 400


@pytest.mark.parametrize("app_type", ["happ", "micky"])
def test_happ_only_device_cap_on_create_and_modify(client, happ_only, proto, app_type):
    assert client.post("/api/user", headers=happ_only,
                       json=body(proto, client_type=app_type, device_limit=21)).status_code == 400
    u = client.post("/api/user", headers=happ_only, json=body(proto, client_type=app_type, device_limit=2)).json()
    r = client.put(f"/api/user/{u['username']}", headers=happ_only, json={"client_type": app_type, "device_limit": 999})
    assert r.status_code == 400


def test_app_cannot_be_switched_but_legacy_may_adopt(client, sudo, proto):
    u = client.post("/api/user", headers=sudo, json=body(proto, client_type="happ", device_limit=2)).json()
    r = client.put(f"/api/user/{u['username']}", headers=sudo, json={"client_type": "micky", "device_limit": 2})
    assert r.status_code == 400
    legacy = client.post("/api/user", headers=sudo, json=body(proto)).json()
    r = client.put(f"/api/user/{legacy['username']}", headers=sudo, json={"client_type": "micky", "device_limit": 2})
    assert r.status_code == 200, r.text
    assert r.json()["subscription_url"].startswith("micky://s/")


def test_micky_users_take_no_prefilled_hwids(client, sudo, proto):
    r = client.post("/api/user", headers=sudo,
                    json=body(proto, client_type="micky", device_limit=2, allowed_hwids=["abc"]))
    assert r.status_code == 422


def test_micky_needs_the_panel_keys(client, sudo, proto, monkeypatch):
    monkeypatch.setattr(micky, "is_configured", lambda: False)
    r = client.post("/api/user", headers=sudo, json=body(proto, client_type="micky", device_limit=1))
    assert r.status_code == 400 and "not configured" in r.text


def test_editing_an_existing_micky_user_does_not_need_the_keys(client, sudo, proto, monkeypatch):
    u = client.post("/api/user", headers=sudo, json=body(proto, client_type="micky", device_limit=2)).json()
    monkeypatch.setattr(micky, "is_configured", lambda: False)
    # The dashboard resends client_type + device_limit on every edit.
    r = client.put(f"/api/user/{u['username']}", headers=sudo,
                   json={"client_type": "micky", "device_limit": 3, "note": "edited"})
    assert r.status_code == 200, r.text
