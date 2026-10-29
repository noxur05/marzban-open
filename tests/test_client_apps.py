"""`GET /api/admins/usage/clients`: which client apps the devices in a range
use, overall and per admin. A device counts when its first..last fetch span
overlaps the range and it is not blocked; it is new when its first fetch is
inside the range."""
from datetime import datetime, timedelta

import pytest

from app.db import GetDB, crud
from app.db.models import UserSubscriptionDevice

NOW = datetime.utcnow()
# Whole Ashgabat days, well clear of the UTC+5 shift: 10..5 days ago.
RANGE = {
    "start": (NOW - timedelta(days=10)).strftime("%Y-%m-%dT00:00:00"),
    "end": (NOW - timedelta(days=5)).strftime("%Y-%m-%dT23:59:59"),
}


def _days_ago(n):
    return NOW - timedelta(days=n)


def _admin(client, sudo, proto, name, users):
    r = client.post("/api/admin", headers=sudo, json={"username": name, "password": "pw12345", "is_sudo": False})
    assert r.status_code == 200, r.text
    tok = client.post("/api/admin/token", data={"username": name, "password": "pw12345"}).json()["access_token"]
    auth = {"Authorization": f"Bearer {tok}"}
    for i in range(users):
        r = client.post("/api/user", headers=auth,
                        json={"username": f"{name}_u{i}", "proxies": {proto: {}}, "inbounds": {}})
        assert r.status_code == 200, r.text
    return auth


def _device(username, key, client_name, platform, first, last, blocked=False):
    with GetDB() as db:
        db.add(UserSubscriptionDevice(
            user_id=crud.get_user(db, username).id, device_key=key,
            client_name=client_name, platform=platform,
            first_seen_at=first, last_seen_at=last, is_blocked=blocked))
        db.commit()


@pytest.fixture(scope="module")
def main(client, sudo, proto):
    auth = _admin(client, sudo, proto, "apps_main", users=3)
    u0, u1, u2 = "apps_main_u0", "apps_main_u1", "apps_main_u2"
    _device(u0, "hwid:a", "Happ", "Android", _days_ago(20), _days_ago(1))      # spans the range
    _device(u0, "hwid:b", "Happ", "Android", _days_ago(7), _days_ago(6))       # new in range
    _device(u1, "hwid:c", "Happ", "iOS", _days_ago(30), _days_ago(8))          # ends inside
    _device(u1, "id:d", "Micky", "Android", _days_ago(9), NOW)                 # new in range
    _device(u2, "ua:e", None, None, _days_ago(15), _days_ago(4))               # no client name
    _device(u2, "hwid:f", "V2Box", "iOS", _days_ago(20), NOW, blocked=True)    # blocked
    _device(u2, "ua:g", "Streisand", "iOS", _days_ago(30), _days_ago(12))      # before the range
    _device(u2, "hwid:h", "Streisand", "iOS", _days_ago(4), NOW)               # after the range
    _device(u2, "hwid:i", None, None, None, None)                              # prefilled, never seen
    return auth


def test_counts_devices_in_use_during_the_range(client, main):
    r = client.get("/api/admins/usage/clients", headers=main, params=RANGE)
    assert r.status_code == 200, r.text
    body = r.json()
    assert (body["devices"], body["users"], body["new_devices"]) == (5, 3, 2)
    assert body["clients"] == [
        {"name": "Happ", "devices": 3, "users": 2, "new_devices": 1,
         "platforms": [{"name": "Android", "devices": 2}, {"name": "iOS", "devices": 1}]},
        {"name": "Micky", "devices": 1, "users": 1, "new_devices": 1,
         "platforms": [{"name": "Android", "devices": 1}]},
        {"name": None, "devices": 1, "users": 1, "new_devices": 0,
         "platforms": [{"name": None, "devices": 1}]},
    ]
    assert body["admins"] == [{"username": "apps_main", "devices": 5, "clients": [
        {"name": "Happ", "devices": 3}, {"name": "Micky", "devices": 1}, {"name": None, "devices": 1},
    ]}]


def test_a_range_that_ends_now_counts_what_fetched_since_its_start(client, sudo, main):
    r = client.get("/api/admins/usage/clients", headers=sudo, params={
        "admin_username": "apps_main",
        "start": (NOW - timedelta(days=2)).strftime("%Y-%m-%dT00:00:00"),
    })
    assert r.status_code == 200, r.text
    body = r.json()
    # hwid:a (last 1 day ago), Micky and the late Streisand; the blocked V2Box stays out.
    assert [(c["name"], c["devices"], c["new_devices"]) for c in body["clients"]] == [
        ("Happ", 1, 0), ("Micky", 1, 0), ("Streisand", 1, 0)]


def test_sudo_sees_every_admin_and_can_filter_one(client, sudo, proto, main):
    _admin(client, sudo, proto, "apps_other", users=1)
    _device("apps_other_u0", "hwid:x", "Micky", "iOS", _days_ago(8), NOW)
    _device("apps_other_u0", "hwid:y", "Micky", "Android", _days_ago(8), NOW)

    every = client.get("/api/admins/usage/clients", headers=sudo, params=RANGE).json()
    rows = {a["username"]: a for a in every["admins"]}
    assert rows["apps_main"]["devices"] == 5
    assert rows["apps_other"] == {"username": "apps_other", "devices": 2,
                                  "clients": [{"name": "Micky", "devices": 2}]}
    assert every["devices"] == sum(a["devices"] for a in every["admins"])
    assert every["devices"] == sum(c["devices"] for c in every["clients"])
    assert [a["devices"] for a in every["admins"]] == sorted((a["devices"] for a in every["admins"]), reverse=True)

    one = client.get("/api/admins/usage/clients", headers=sudo,
                     params={**RANGE, "admin_username": "apps_other"}).json()
    assert [a["username"] for a in one["admins"]] == ["apps_other"]
    assert one["clients"] == [{"name": "Micky", "devices": 2, "users": 1, "new_devices": 2,
                               "platforms": [{"name": "Android", "devices": 1}, {"name": "iOS", "devices": 1}]}]


def test_an_ordinary_admin_only_sees_its_own_devices(client, sudo, proto, main):
    auth = _admin(client, sudo, proto, "apps_nosy", users=0)
    r = client.get("/api/admins/usage/clients", headers=auth, params={**RANGE, "admin_username": "apps_main"})
    assert r.status_code == 200, r.text
    assert r.json()["devices"] == 0 and r.json()["admins"] == [] and r.json()["clients"] == []


def test_an_unknown_admin_is_404(client, sudo):
    r = client.get("/api/admins/usage/clients", headers=sudo, params={**RANGE, "admin_username": "apps_nobody"})
    assert r.status_code == 404


def test_range_edges_are_ashgabat_days_and_inclusive(client, sudo, proto):
    # start=D 00:00 and end=E 23:59:59 in Ashgabat (UTC+5) are these UTC instants.
    d, e = (NOW - timedelta(days=20)).date(), (NOW - timedelta(days=15)).date()
    start_utc = datetime(d.year, d.month, d.day) - timedelta(hours=5)
    end_utc = datetime(e.year, e.month, e.day, 23, 59, 59) - timedelta(hours=5)
    second = timedelta(seconds=1)
    _admin(client, sudo, proto, "apps_edge", users=1)
    u = "apps_edge_u0"
    _device(u, "hwid:last-at-start", "Happ", "iOS", start_utc - timedelta(days=3), start_utc)
    _device(u, "hwid:last-before-start", "V2Box", "iOS", start_utc - timedelta(days=3), start_utc - second)
    _device(u, "hwid:first-at-start", "Micky", "Android", start_utc, NOW)
    _device(u, "hwid:first-before-start", "Micky", "Android", start_utc - second, NOW)
    _device(u, "hwid:first-at-end", "Streisand", "iOS", end_utc, NOW)
    _device(u, "hwid:first-after-end", "NekoBox", "Android", end_utc + second, NOW)

    r = client.get("/api/admins/usage/clients", headers=sudo, params={
        "admin_username": "apps_edge", "start": f"{d}T00:00:00", "end": f"{e}T23:59:59"})
    assert r.status_code == 200, r.text
    got = {c["name"]: (c["devices"], c["new_devices"]) for c in r.json()["clients"]}
    assert got == {"Happ": (1, 0), "Micky": (2, 1), "Streisand": (1, 1)}


def test_users_without_an_owner_are_a_row_of_their_own(client, sudo, proto):
    # The env sudo admin has no admins row, so its users have no owner.
    r = client.post("/api/user", headers=sudo,
                    json={"username": "apps_orphan", "proxies": {proto: {}}, "inbounds": {}})
    assert r.status_code == 200, r.text
    _device("apps_orphan", "hwid:orphan", "Happ", "Android", _days_ago(8), NOW)
    rows = {a["username"]: a for a in client.get("/api/admins/usage/clients", headers=sudo, params=RANGE).json()["admins"]}
    assert any(c["name"] == "Happ" and c["devices"] >= 1 for c in rows[None]["clients"])


def test_spellings_of_one_app_merge(client, sudo, proto):
    # MickyX is Micky's other build; generic UAs keep their own casing.
    _admin(client, sudo, proto, "apps_spell", users=1)
    u = "apps_spell_u0"
    _device(u, "hwid:s1", "Micky", "Android", _days_ago(8), NOW)
    _device(u, "hwid:s2", "MickyX", "android", _days_ago(8), NOW)
    _device(u, "hwid:s3", "V2Box", "iOS", _days_ago(8), NOW)
    _device(u, "hwid:s4", "V2Box", "iOS", _days_ago(8), NOW)
    _device(u, "hwid:s5", "v2box", "iOS", _days_ago(8), NOW)
    body = client.get("/api/admins/usage/clients", headers=sudo,
                      params={**RANGE, "admin_username": "apps_spell"}).json()
    assert [(c["name"], c["devices"], c["users"]) for c in body["clients"]] == [("V2Box", 3, 1), ("Micky", 2, 1)]
    micky = next(c for c in body["clients"] if c["name"] == "Micky")
    assert micky["platforms"] == [{"name": "Android", "devices": 2}]
    assert body["admins"][0]["clients"] == [{"name": "V2Box", "devices": 3}, {"name": "Micky", "devices": 2}]


def test_a_merged_app_counts_users_and_new_devices_across_spellings(client, sudo, proto):
    _admin(client, sudo, proto, "apps_merge", users=2)
    _device("apps_merge_u0", "hwid:m1", "Micky", "Android", _days_ago(8), NOW)
    _device("apps_merge_u1", "hwid:m2", "MickyX", "Android", _days_ago(7), NOW)
    body = client.get("/api/admins/usage/clients", headers=sudo,
                      params={**RANGE, "admin_username": "apps_merge"}).json()
    assert [(c["name"], c["devices"], c["users"], c["new_devices"]) for c in body["clients"]] == [("Micky", 2, 2, 2)]


def test_admins_are_ordered_by_devices(client, sudo, proto):
    _admin(client, sudo, proto, "apps_aa_small", users=1)
    _admin(client, sudo, proto, "apps_zz_big", users=1)
    _device("apps_aa_small_u0", "hwid:o1", "Happ", "iOS", _days_ago(8), NOW)
    for i in range(3):
        _device("apps_zz_big_u0", f"hwid:o2-{i}", "Happ", "iOS", _days_ago(8), NOW)
    names = [a["username"] for a in client.get("/api/admins/usage/clients", headers=sudo, params=RANGE).json()["admins"]]
    assert names.index("apps_zz_big") < names.index("apps_aa_small")
