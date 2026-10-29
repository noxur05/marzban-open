"""`DELETE /api/admin/{username}` refuses an admin that still owns users: they
would be left ownerless. Deleting the users first ("Delete all users") is the
way through. marzban-cli keeps deleting such an admin (test_regenerate.py)."""
import time

from sqlalchemy.exc import IntegrityError

from app.db import crud


def _admin(client, sudo, proto, name, users):
    r = client.post("/api/admin", headers=sudo, json={"username": name, "password": "pw12345", "is_sudo": False})
    assert r.status_code == 200, r.text
    tok = client.post("/api/admin/token", data={"username": name, "password": "pw12345"}).json()["access_token"]
    for i in range(users):
        r = client.post("/api/user", headers={"Authorization": f"Bearer {tok}"},
                        json={"username": f"{name}_u{i}", "proxies": {proto: {}}, "inbounds": {}})
        assert r.status_code == 200, r.text
    return tok


def _admin_names(client, sudo):
    return {a["username"] for a in client.get("/api/admins", headers=sudo).json()}


def test_an_admin_with_users_is_not_deleted(client, sudo, proto):
    _admin(client, sudo, proto, "del_busy", users=2)
    r = client.delete("/api/admin/del_busy", headers=sudo)
    assert r.status_code == 409
    assert "2 user(s)" in r.json()["detail"]
    assert "del_busy" in _admin_names(client, sudo)
    users = client.get("/api/users", headers=sudo, params={"admin": "del_busy"}).json()
    assert users["total"] == 2


def test_an_admin_is_deleted_once_its_users_are(client, sudo, proto):
    _admin(client, sudo, proto, "del_cleared", users=1)
    assert client.delete("/api/admin/del_cleared/users", headers=sudo).status_code == 200
    assert client.delete("/api/admin/del_cleared", headers=sudo).status_code == 200
    assert "del_cleared" not in _admin_names(client, sudo)


def test_an_admin_without_users_is_deleted(client, sudo, proto):
    _admin(client, sudo, proto, "del_empty", users=0)
    assert client.delete("/api/admin/del_empty", headers=sudo).status_code == 200
    assert client.post("/api/admin/token", data={"username": "del_empty", "password": "pw12345"}).status_code == 401


def test_a_sudo_admin_is_still_refused_first(client, sudo, proto):
    r = client.post("/api/admin", headers=sudo, json={"username": "del_sudo", "password": "pw12345", "is_sudo": True})
    assert r.status_code == 200, r.text
    tok = client.post("/api/admin/token", data={"username": "del_sudo", "password": "pw12345"}).json()["access_token"]
    r = client.post("/api/user", headers={"Authorization": f"Bearer {tok}"},
                    json={"username": "del_sudo_u0", "proxies": {proto: {}}, "inbounds": {}})
    assert r.status_code == 200, r.text
    assert client.delete("/api/admin/del_sudo", headers=sudo).status_code == 403  # not 409


def test_a_user_added_after_the_count_still_stops_the_delete(client, sudo, proto, monkeypatch):
    # On MySQL users.admin_id refuses the DELETE; SQLite doesn't enforce it.
    _admin(client, sudo, proto, "del_race", users=0)

    def refused(db, dbadmin):
        raise IntegrityError("DELETE FROM admins", {}, Exception("foreign key constraint fails"))

    monkeypatch.setattr(crud, "remove_admin", refused)
    r = client.delete("/api/admin/del_race", headers=sudo)
    assert r.status_code == 409
    assert "still owns users" in r.json()["detail"]
    monkeypatch.undo()
    assert "del_race" in _admin_names(client, sudo)


def test_a_deleted_admins_token_does_not_work_for_a_new_admin_of_that_name(client, sudo, proto):
    old = {"Authorization": f"Bearer {_admin(client, sudo, proto, 'del_reused', users=0)}"}
    time.sleep(1.1)  # tokens carry whole seconds; the old one must be older
    assert client.delete("/api/admin/del_reused", headers=sudo).status_code == 200
    r = client.post("/api/admin", headers=sudo, json={"username": "del_reused", "password": "pw67890", "is_sudo": True})
    assert r.status_code == 200, r.text
    assert client.get("/api/admins", headers=old).status_code == 401
    tok = client.post("/api/admin/token", data={"username": "del_reused", "password": "pw67890"}).json()["access_token"]
    assert client.get("/api/admins", headers={"Authorization": f"Bearer {tok}"}).status_code == 200
