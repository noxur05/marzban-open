"""Changing an admin's domain moves its Happ and Micky links to the new domain
(no separate regenerate needed), and the old links keep working."""
import itertools
import time
from urllib.parse import urlparse

from app.utils import micky
from tests.micky_helpers import LINK_PRIVATE, Device

_n = itertools.count()


def test_domain_change_moves_happ_and_micky_links(client, sudo, proto):
    client.post("/api/admin", headers=sudo, json={"username": "dom_admin", "password": "pw12345", "is_sudo": False,
                                                  "subscription_url": "https://old.example"})
    tok = client.post("/api/admin/token", data={"username": "dom_admin", "password": "pw12345"}).json()["access_token"]
    admin = {"Authorization": f"Bearer {tok}"}

    def create(app_type):
        r = client.post("/api/user", headers=admin, json={"username": f"dom{next(_n)}_{app_type}", "proxies": {proto: {}},
                                                          "inbounds": {}, "client_type": app_type, "device_limit": 2})
        assert r.status_code == 200, r.text
        return r.json()

    m_user, h_user = create("micky"), create("happ")
    old_url = micky.open_link(m_user["subscription_url"], LINK_PRIVATE)
    assert urlparse(old_url).netloc == "old.example"
    old_happ_link = h_user["subscription_url"]

    # Changing the domain regenerates that admin's Happ and Micky links by
    # itself (background task; synchronous under TestClient).
    assert client.patch("/api/admin/dom_admin", headers=sudo, json={"subscription_url": "https://new.example"}).status_code == 200

    new_m = client.get(f"/api/user/{m_user['username']}", headers=sudo).json()["subscription_url"]
    new_url = micky.open_link(new_m, LINK_PRIVATE)
    assert urlparse(new_url).netloc == "new.example"
    new_h = client.get(f"/api/user/{h_user['username']}", headers=sudo).json()["subscription_url"]
    assert new_h.startswith("happ://") and new_h != old_happ_link

    # Old and new links both keep working (tokens are not revoked).
    for url in (old_url, new_url):
        payload = Device().get(client, urlparse(url).path)[1]
        assert payload and payload["status"] in ("ok", "refused")
        assert payload.get("code") != "refused"


def test_single_regenerate_works_for_micky(client, sudo, proto):
    u = client.post("/api/user", headers=sudo, json={"username": f"dom{next(_n)}_single", "proxies": {proto: {}},
                                                     "inbounds": {}, "client_type": "micky", "device_limit": 1}).json()
    r = client.post(f"/api/user/{u['username']}/regenerate-sub", headers=sudo)
    assert r.status_code == 200
    assert r.json()["subscription_url"].startswith("micky://s/") and r.json()["subscription_url"] != u["subscription_url"]


def test_cli_domain_change_moves_links(client, sudo, proto):
    from typer.testing import CliRunner

    from cli.admin import app as admin_cli

    client.post("/api/admin", headers=sudo, json={"username": "cli_admin", "password": "pw12345", "is_sudo": False,
                                                  "subscription_url": "https://old-cli.example"})
    tok = client.post("/api/admin/token", data={"username": "cli_admin", "password": "pw12345"}).json()["access_token"]
    u = client.post("/api/user", headers={"Authorization": f"Bearer {tok}"},
                    json={"username": f"dom{next(_n)}_cli", "proxies": {proto: {}}, "inbounds": {},
                          "client_type": "micky", "device_limit": 1}).json()

    # Enter keeps every field; only the subscription URL prefix changes.
    answers = ["", "", "", "", "", "https://new-cli.example", "", ""]
    r = CliRunner().invoke(admin_cli, ["update", "--username", "cli_admin"], input="\n".join(answers) + "\n")
    assert r.exit_code == 0, r.output
    assert "1 Happ/Micky link(s) moved" in r.output

    new_m = client.get(f"/api/user/{u['username']}", headers=sudo).json()["subscription_url"]
    assert urlparse(micky.open_link(new_m, LINK_PRIVATE)).netloc == "new-cli.example"


def _admin_with_users(client, sudo, proto, name, domain):
    client.post("/api/admin", headers=sudo, json={"username": name, "password": "pw12345", "is_sudo": False,
                                                  "subscription_url": domain})
    tok = client.post("/api/admin/token", data={"username": name, "password": "pw12345"}).json()["access_token"]
    return [client.post("/api/user", headers={"Authorization": f"Bearer {tok}"},
                        json={"username": f"dom{next(_n)}_{app_type}", "proxies": {proto: {}}, "inbounds": {},
                              "client_type": app_type, "device_limit": 1}).json()
            for app_type in ("micky", "happ")]


def _moved_to_default(client, sudo, m_user, h_user):
    new_m = client.get(f"/api/user/{m_user['username']}", headers=sudo).json()["subscription_url"]
    assert urlparse(micky.open_link(new_m, LINK_PRIVATE)).netloc == "panel.test"  # XRAY_SUBSCRIPTION_URL_PREFIX
    new_h = client.get(f"/api/user/{h_user['username']}", headers=sudo).json()["subscription_url"]
    assert new_h.startswith("happ://") and new_h != h_user["subscription_url"]


def test_cli_deleting_an_admin_moves_its_users_links(client, sudo, proto):
    from typer.testing import CliRunner

    from cli.admin import app as admin_cli

    m_user, h_user = _admin_with_users(client, sudo, proto, "gone_cli_admin", "https://gone-cli.example")
    time.sleep(1.1)
    r = CliRunner().invoke(admin_cli, ["delete", "--username", "gone_cli_admin", "--yes"])
    assert r.exit_code == 0, r.output
    assert "2 Happ/Micky link(s) moved" in r.output
    _moved_to_default(client, sudo, m_user, h_user)
