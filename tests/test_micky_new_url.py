"""`new-url` (docs/micky-protocol.md): a Micky app still fetching on an old,
blocked domain learns its user's current link from a sealed OK answer."""
from urllib.parse import urlparse

import pytest

from app.utils import micky
from tests.micky_helpers import LINK_PRIVATE, Device

HAPP_UA = "Happ/4.4.1/Android/17891107313301967618"


@pytest.fixture(scope="module")
def moved(client, sudo, proto):
    """A Micky user whose admin moves from old.moved to new.moved."""
    client.post("/api/admin", headers=sudo, json={"username": "move_admin", "password": "pw12345",
                                                  "is_sudo": False, "subscription_url": "https://old.moved"})
    user = client.post("/api/user", headers=sudo, json={
        "username": "moving_user", "proxies": {proto: {}}, "inbounds": {},
        "client_type": "micky", "device_limit": 3}).json()
    client.put(f"/api/user/{user['username']}/set-owner", headers=sudo, params={"admin_username": "move_admin"})
    old_link = client.get(f"/api/user/{user['username']}", headers=sudo).json()["subscription_url"]
    old_url = micky.open_link(old_link, LINK_PRIVATE)
    assert urlparse(old_url).netloc == "old.moved"
    assert client.patch("/api/admin/move_admin", headers=sudo, json={"subscription_url": "https://new.moved"}).status_code == 200
    new_link = client.get(f"/api/user/{user['username']}", headers=sudo).json()["subscription_url"]
    return {"old_url": old_url, "new_url": micky.open_link(new_link, LINK_PRIVATE), "new_link": new_link}


def test_request_on_the_old_domain_carries_the_new_link(client, moved):
    assert urlparse(moved["new_url"]).netloc == "new.moved"
    r, payload = Device().get(client, urlparse(moved["old_url"]).path)  # old token, old domain
    assert payload["status"] == "ok"
    new_url = payload["headers"][micky.NEW_URL_HEADER]
    assert new_url.startswith("micky://s/")                           # host and token stay hidden
    assert micky.open_link(new_url, LINK_PRIVATE) == moved["new_url"]  # the app switches to this


def test_request_on_the_current_link_gets_the_same_url_back(client, moved):
    r, payload = Device().get(client, urlparse(moved["new_url"]).path)
    assert micky.open_link(payload["headers"][micky.NEW_URL_HEADER], LINK_PRIVATE) == moved["new_url"]  # no switch


def test_new_url_is_stable_between_refreshes(client, moved):
    """The app compares decrypted URLs; they must not change per request."""
    path, phone = urlparse(moved["old_url"]).path, Device()  # one phone refreshing twice
    urls = {micky.open_link(phone.get(client, path)[1]["headers"][micky.NEW_URL_HEADER], LINK_PRIVATE)
            for _ in range(2)}
    assert urls == {moved["new_url"]}


def test_never_a_plain_header_and_never_on_refusals(client, sudo, proto):
    user = client.post("/api/user", headers=sudo, json={
        "username": "nu_limit", "proxies": {proto: {}}, "inbounds": {},
        "client_type": "micky", "device_limit": 1}).json()
    path = urlparse(micky.open_link(user["subscription_url"], LINK_PRIVATE)).path
    r, ok = Device().get(client, path)
    assert micky.NEW_URL_HEADER not in {k.lower() for k in r.headers}  # only inside the sealed payload
    refusal = Device().get(client, path)[1]                              # second device: device_limit
    assert refusal["status"] == "refused" and "headers" not in refusal


def test_other_apps_never_get_new_url(client, sudo, proto):
    happ = client.post("/api/user", headers=sudo, json={
        "username": "nu_happ", "proxies": {proto: {}}, "inbounds": {},
        "client_type": "happ", "device_limit": 1}).json()
    from app.utils.jwt import create_subscription_token
    r = client.get(f"/sub/{create_subscription_token(happ['username'])}",
                   headers={"User-Agent": HAPP_UA, "X-Hwid": "nu-happ-1"})
    assert r.content and micky.NEW_URL_HEADER not in {k.lower() for k in r.headers}
