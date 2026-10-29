"""A Happ or Micky user's app link changes only for a reason: taking an app
(or having no link yet), a domain change, a revoke, or an explicit regenerate.
Never on an ordinary edit: the dashboard resends client_type with every edit,
and a new link each time moved Micky apps ("address updated") after a renewal
and minted another never-expiring token."""
import itertools
import time
from urllib.parse import urlparse

import pytest

from app.db import GetDB, crud
from app.utils import micky
from tests.micky_helpers import LINK_PRIVATE, Device

_n = itertools.count()


def _create(client, sudo, proto, app_type, limit=2):
    r = client.post("/api/user", headers=sudo, json={
        "username": f"stable{next(_n)}_{app_type}", "proxies": {proto: {}}, "inbounds": {},
        "client_type": app_type, "device_limit": limit})
    assert r.status_code == 200, r.text
    return r.json()


def _link(client, sudo, username):
    return client.get(f"/api/user/{username}", headers=sudo).json()["subscription_url"]


@pytest.mark.parametrize("app_type", ["happ", "micky"])
def test_dashboard_edits_keep_the_link(client, sudo, proto, app_type):
    user = _create(client, sudo, proto, app_type)
    link = user["subscription_url"]
    # Tokens have 1-second resolution and Happ's encryption is cached: a link
    # regenerated within the same second would look unchanged.
    time.sleep(1.1)
    # What the dashboard sends: the app policy again, plus the actual edits.
    for edit in ({"data_limit": 10 * 2**30}, {"expire": 4_102_444_800}, {"note": "renewed"}):
        r = client.put(f"/api/user/{user['username']}", headers=sudo,
                       json={"client_type": app_type, "device_limit": 3, **edit})
        assert r.status_code == 200, r.text
        assert r.json()["subscription_url"] == link
    assert _link(client, sudo, user["username"]) == link


def test_a_micky_app_is_not_moved_by_an_edit(client, sudo, proto):
    user = _create(client, sudo, proto, "micky")
    path, phone = urlparse(micky.open_link(user["subscription_url"], LINK_PRIVATE)).path, Device()
    before = phone.get(client, path)[1]["headers"][micky.NEW_URL_HEADER]
    client.put(f"/api/user/{user['username']}", headers=sudo,
               json={"client_type": "micky", "device_limit": 2, "note": "edited"})
    after = phone.get(client, path)[1]["headers"][micky.NEW_URL_HEADER]
    assert after == before == user["subscription_url"]


def test_taking_an_app_creates_the_link(client, sudo, proto):
    name = f"stable{next(_n)}_legacy"
    client.post("/api/user", headers=sudo, json={"username": name, "proxies": {proto: {}}, "inbounds": {}})
    r = client.put(f"/api/user/{name}", headers=sudo, json={"client_type": "happ", "device_limit": 2})
    assert r.status_code == 200, r.text
    with GetDB() as db:
        assert crud.get_user(db, name).encrypted_subscription_url.startswith("happ://crypt5/")


def test_a_missing_link_is_filled_on_edit(client, sudo, proto):
    user = _create(client, sudo, proto, "happ")
    with GetDB() as db:
        crud.get_user(db, user["username"]).encrypted_subscription_url = None
        db.commit()
    client.put(f"/api/user/{user['username']}", headers=sudo, json={"client_type": "happ", "device_limit": 2})
    with GetDB() as db:
        assert crud.get_user(db, user["username"]).encrypted_subscription_url.startswith("happ://")


@pytest.mark.parametrize("app_type", ["happ", "micky"])
def test_revoke_and_regenerate_still_change_the_link(client, sudo, proto, app_type):
    user = _create(client, sudo, proto, app_type)
    link = user["subscription_url"]
    pause = 1.1 if app_type == "happ" else 0  # Micky links differ on every build anyway
    time.sleep(pause)
    revoked = client.post(f"/api/user/{user['username']}/revoke_sub", headers=sudo).json()["subscription_url"]
    assert revoked != link
    time.sleep(pause)
    regenerated = client.post(f"/api/user/{user['username']}/regenerate-sub", headers=sudo).json()["subscription_url"]
    assert regenerated != revoked
