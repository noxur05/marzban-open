"""`POST /api/admin` keeps every flag it is sent and refuses names and
domains that would break later (URL paths, MySQL's case-folding unique
index, links built as f"{domain}/sub/{token}"). `GET /api/admins` carries
each admin's user count for the dashboard list."""
import pytest
from pydantic import ValidationError

from app.db import GetDB, crud


def _create(client, sudo, name, **extra):
    return client.post("/api/admin", headers=sudo,
                       json={"username": name, "password": "pw12345", "is_sudo": False, **extra})


def _token(client, name):
    r = client.post("/api/admin/token", data={"username": name, "password": "pw12345"})
    return {"Authorization": f"Bearer {r.json()['access_token']}"}


def test_can_announce_false_is_kept_and_enforced(client, sudo):
    r = _create(client, sudo, "ac_quiet", can_announce=False)
    assert r.status_code == 200, r.text
    assert r.json()["can_announce"] is False
    r = client.put("/api/announcement", headers=_token(client, "ac_quiet"), json={"text": "hi", "enabled": True})
    assert r.status_code == 403


def test_a_quiet_admin_s_users_get_the_global_announcement(client, sudo, proto):
    # No announcement row is written on create: none is needed for the fallback.
    assert client.put("/api/announcement/global", headers=sudo, json={"text": "GLOBAL", "enabled": True}).status_code == 200
    _create(client, sudo, "ac_quiet2", can_announce=False)
    user = client.post("/api/user", headers=_token(client, "ac_quiet2"),
                       json={"username": "ac_quiet2_u", "proxies": {proto: {}}, "inbounds": {}}).json()
    r = client.get(user["subscription_url"].split("panel.test", 1)[1], headers={"User-Agent": "Happ/1.0"})
    assert r.headers.get("announce") == "base64:R0xPQkFM"  # "GLOBAL"
    with GetDB() as db:
        assert crud.list_announcements(db, crud.get_admin(db, "ac_quiet2").id) == []
    client.delete("/api/announcement/global", headers=sudo)


def test_every_flag_is_kept(client, sudo):
    r = _create(client, sudo, "ac_flags", can_enable_outline=True, happ_only=True, telegram_id=42,
                subscription_url="https://flags.example")
    assert r.status_code == 200, r.text
    body = r.json()
    assert (body["can_announce"], body["can_enable_outline"], body["happ_only"], body["telegram_id"],
            body["subscription_url"]) == (True, True, True, 42, "https://flags.example")
    assert any(a["username"] == "ac_flags" and a["id"] == body["id"]
               for a in client.get("/api/admins", headers=sudo).json())


def test_a_name_taken_in_another_case_is_a_conflict(client, sudo):
    assert _create(client, sudo, "Ac_Case").status_code == 200
    r = _create(client, sudo, "ac_case")
    assert r.status_code == 409 and r.json()["detail"] == "Admin already exists"


def test_the_env_sudo_name_is_a_conflict(client, sudo):
    assert _create(client, sudo, "root").status_code == 409  # SUDO_USERNAME in conftest


@pytest.mark.parametrize("name", ["", "ab", " ac_sp", "ac sp", "ac/slash", "ac?q", "ac#h", "ac%25", "-ac",
                                  "x" * 33, "Пользователь", "ac_nl\n", "root\n"])
def test_bad_names_are_refused(client, sudo, name):
    r = _create(client, sudo, name)
    assert r.status_code == 422 and "username" in r.json()["detail"]
    assert not r.json()["detail"]["username"].startswith("Value error")


@pytest.mark.parametrize("password", ["", "12345"])
def test_short_passwords_are_refused(client, sudo, password):
    r = client.post("/api/admin", headers=sudo, json={"username": "ac_pw", "password": password, "is_sudo": False})
    assert r.status_code == 422 and "password" in r.json()["detail"]


@pytest.mark.parametrize("i, url, stored", [
    (0, "https://trail.example/", "https://trail.example"),
    (1, "  https://*.wild.example  ", "https://*.wild.example"),
    (2, "", None),
    (3, "   ", None),
    (4, "https://sub.example/path/", "https://sub.example/path"),
])
def test_domains_are_normalised(client, sudo, i, url, stored):
    r = _create(client, sudo, f"ac_dom{i}", subscription_url=url)
    assert r.status_code == 200, r.text
    assert r.json()["subscription_url"] == stored


@pytest.mark.parametrize("url", ["panel.example", "ftp://x.example", "https://a b.example", "https://" + "a" * 600,
                                 "https://x.example?a=1", "https://x.example/#f", "https://u:p@x.example",
                                 "https://x.example/\nfoo"])
def test_bad_domains_are_refused(client, sudo, url):
    r = _create(client, sudo, "ac_baddom", subscription_url=url)
    assert r.status_code == 422 and "subscription_url" in r.json()["detail"]


def test_patch_normalises_the_domain_too(client, sudo):
    _create(client, sudo, "ac_patch")
    r = client.patch("/api/admin/ac_patch", headers=sudo, json={"subscription_url": "https://p.example/"})
    assert r.json()["subscription_url"] == "https://p.example"
    r = client.patch("/api/admin/ac_patch", headers=sudo, json={"subscription_url": ""})
    assert r.status_code == 200 and r.json()["subscription_url"] is None


def test_create_is_logged(client, sudo, monkeypatch):
    from app.routers import admin as admin_router
    lines = []
    monkeypatch.setattr(admin_router.logger, "warning", lambda msg, *a: lines.append(msg % a))
    _create(client, sudo, "ac_logged", is_sudo=True)
    assert "admin created: admin=ac_logged sudo=True by=root" in lines


@pytest.mark.parametrize("method", ["patch", "put"])
@pytest.mark.parametrize("password", ["", "12345"])
def test_a_short_new_password_is_refused_and_the_old_one_still_works(client, sudo, method, password):
    # An empty one used to hash to None and wipe the login.
    name = f"ac_reset_{method}{len(password)}"
    _create(client, sudo, name)
    r = getattr(client, method)(f"/api/admin/{name}", headers=sudo, json={"password": password, "is_sudo": False})
    assert r.status_code == 422 and "password" in r.json()["detail"]
    assert client.post("/api/admin/token", data={"username": name, "password": "pw12345"}).status_code == 200


def test_the_admins_list_counts_each_admin_s_users(client, sudo, proto):
    _create(client, sudo, "ac_two")
    _create(client, sudo, "ac_none")
    tok = _token(client, "ac_two")
    for i in range(2):
        r = client.post("/api/user", headers=tok,
                        json={"username": f"ac_two_u{i}", "proxies": {proto: {}}, "inbounds": {}})
        assert r.status_code == 200, r.text
    counts = {a["username"]: a["users_count"] for a in client.get("/api/admins", headers=sudo).json()}
    assert counts["ac_two"] == 2 and counts["ac_none"] == 0


def test_the_cli_refuses_a_bad_admin_without_printing_the_password():
    # Typer's default traceback prints local variables, the password among them.
    from typer.testing import CliRunner

    from cli.admin import app as admin_cli
    r = CliRunner(mix_stderr=False).invoke(admin_cli, [
        "create", "--username", "ac cli", "--no-sudo", "--password", "Secr1x",
        "--telegram-id", "", "--discord-webhook", "", "--subscription-url", ""])
    assert r.exit_code != 0 and not isinstance(r.exception, ValidationError)
    assert "username:" in r.stderr and "Secr1x" not in r.stdout + r.stderr
