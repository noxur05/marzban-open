"""Google copies of Micky subscriptions (docs/micky-protocol.md, "Google
copies"): the copy format, the endpoint the marzban-google-sync service reads,
the G headers in sealed answers, and the dirty marks that trigger uploads."""
import base64
import json
import os
from datetime import datetime, timedelta
from urllib.parse import urlparse

import pytest

from app.db import GetDB, crud
from app.utils import micky, micky_copy
from tests.micky_helpers import LINK_PRIVATE, Device

SECRET = "test-sync-secret"
AUTH = {"Authorization": f"Bearer {SECRET}"}
G_HEADERS = {micky_copy.G1_HEADER, micky_copy.G2_HEADER, micky_copy.KEY_HEADER, micky_copy.BASE_HEADER}


@pytest.fixture
def copies_on(monkeypatch):
    import app.routers.internal as internal
    import app.routers.subscription as subscription
    monkeypatch.setattr(internal, "MICKY_GOOGLE_COPIES", True)
    monkeypatch.setattr(internal, "GOOGLE_SYNC_SECRET", SECRET)
    monkeypatch.setattr(subscription, "MICKY_GOOGLE_COPIES", True)


def make_user(client, sudo, proto, name, client_type="micky", limit=3):
    r = client.post("/api/user", headers=sudo, json={
        "username": name, "proxies": {proto: {}}, "inbounds": {},
        "client_type": client_type, "device_limit": limit})
    assert r.status_code == 200, r.text
    return r.json()


def user_id(name):
    with GetDB() as db:
        return crud.get_user(db, name).id


def row(name):
    with GetDB() as db:
        r = crud.get_micky_copy(db, user_id(name))
        if r is None:
            return None
        return {c: getattr(r, c) for c in ("content_key", "key_changed_at", "firestore_doc_id",
                                            "drive_file_id", "synced_at", "dirty_at")}


def set_row(name, **values):
    with GetDB() as db:
        r = crud.get_micky_copy(db, user_id(name))
        for k, v in values.items():
            setattr(r, k, v)
        db.commit()


def fetch_copy(client, name, **headers):
    return client.get(f"/internal/micky-copy/{user_id(name)}", headers={**AUTH, **headers})


def open_copy(client, name):
    r = fetch_copy(client, name)
    assert r.status_code == 200, r.status_code
    return json.loads(micky_copy.decrypt(row(name)["content_key"], r.content)), r.headers["x-copy-sha"]


def sub_path(user):
    return urlparse(micky.open_link(user["subscription_url"], LINK_PRIVATE)).path


# --- format -----------------------------------------------------------------

def test_the_app_vectors_byte_for_byte():
    """The app decrypts these exact blobs; the panel must produce them."""
    data = json.load(open(os.path.join(os.path.dirname(__file__), "micky_copy_vectors.json")))
    assert data["aad"] == micky_copy.AAD.decode()
    key = data["key"]
    for v in data["vectors"]:
        plaintext = v["plaintext"].encode()
        blob = micky.b64u_decode(v["blob"])
        assert micky_copy.encrypt(key, plaintext, nonce=micky.b64u_decode(v["nonce"])) == blob, v["name"]
        assert micky_copy.decrypt(key, blob) == plaintext
        assert base64.b64encode(blob).decode() == v["firestore_bytesValue"]


def test_every_copy_gets_a_fresh_nonce():
    key = micky_copy.new_key()
    assert len(key) == 43
    assert micky_copy.encrypt(key, b"x") != micky_copy.encrypt(key, b"x")


# --- the endpoint -------------------------------------------------------------

def test_off_by_default(client, sudo, proto):
    make_user(client, sudo, proto, "gc_off")
    assert fetch_copy(client, "gc_off").status_code == 503


def test_only_the_sync_service_gets_it(client, sudo, proto, copies_on):
    make_user(client, sudo, proto, "gc_auth")
    path = f"/internal/micky-copy/{user_id('gc_auth')}"
    assert client.get(path).status_code == 401
    assert client.get(path, headers={"Authorization": "Bearer wrong"}).status_code == 401
    # Through a proxy = from outside: the same 404 as no endpoint at all.
    for h in ("X-Forwarded-For", "Via", "CF-Connecting-IP"):
        assert client.get(path, headers={**AUTH, h: "1.2.3.4"}).status_code == 404
    assert client.get(path, headers=AUTH).status_code == 200


def test_happ_and_legacy_users_have_no_copy(client, sudo, proto, copies_on):
    make_user(client, sudo, proto, "gc_happ", client_type="happ")
    assert row("gc_happ") is None
    assert fetch_copy(client, "gc_happ").status_code == 404


def test_a_micky_user_gets_a_key_and_a_copy(client, sudo, proto, copies_on):
    user = make_user(client, sudo, proto, "gc_basic")
    r = row("gc_basic")
    assert len(r["content_key"]) == 43 and r["synced_at"] is None
    copy, sha = open_copy(client, "gc_basic")
    assert copy["status"] == "ok" and copy["blocked"] == [] and isinstance(copy["issued_at"], int)
    assert "://" in copy["body"]
    assert copy["headers"][micky.NEW_URL_HEADER] == user["subscription_url"]
    assert not G_HEADERS & set(copy["headers"])
    assert len(sha) == 64


def test_the_copy_says_what_a_sealed_answer_says(client, sudo, proto, copies_on):
    user = make_user(client, sudo, proto, "gc_same")
    _, sealed = Device().get(client, sub_path(user))
    copy, _ = open_copy(client, "gc_same")
    assert sealed["status"] == copy["status"] == "ok"
    assert {k: v for k, v in sealed["headers"].items() if k not in G_HEADERS} == copy["headers"]
    assert sorted(sealed["body"].splitlines()) == sorted(copy["body"].splitlines())


def test_unchanged_copies_keep_their_sha_whatever_the_usage(client, sudo, proto, copies_on):
    make_user(client, sudo, proto, "gc_sha")
    _, sha1 = open_copy(client, "gc_sha")
    _, sha2 = open_copy(client, "gc_sha")
    with GetDB() as db:
        dbuser = crud.get_user(db, "gc_sha")
        dbuser.used_traffic = 12345
        db.commit()
    copy, sha3 = open_copy(client, "gc_sha")
    assert sha1 == sha2 == sha3 and "download=12345" in copy["headers"]["subscription-userinfo"]


def test_a_renewal_changes_the_sha_at_once(client, sudo, proto, copies_on):
    """total and expire share the usage line; only the download counter is ignored."""
    make_user(client, sudo, proto, "gc_renew")
    _, before = open_copy(client, "gc_renew")
    assert client.put("/api/user/gc_renew", headers=sudo, json={"data_limit": 10 * 2**30}).status_code == 200
    _, after = open_copy(client, "gc_renew")
    assert after != before


def test_random_picks_render_the_same_copy(client, sudo, proto, copies_on, monkeypatch):
    from app import xray
    tag = xray.config.inbounds_by_protocol[proto][0]["tag"]
    host = xray.hosts.get(tag)[0]
    monkeypatch.setitem(host, "address", ["*.a.test", "*.b.test", "c.test"])
    monkeypatch.setitem(host, "sni", ["*.s1.test", "s2.test"])
    make_user(client, sudo, proto, "gc_random")
    shas = {open_copy(client, "gc_random")[1] for _ in range(4)}
    assert len(shas) == 1


def test_a_lapsed_user_copy_is_the_refusal(client, sudo, proto, copies_on):
    make_user(client, sudo, proto, "gc_lapsed")
    _, sha_ok = open_copy(client, "gc_lapsed")
    before = row("gc_lapsed")["dirty_at"]
    client.put("/api/user/gc_lapsed", headers=sudo, json={"status": "disabled"})
    copy, sha = open_copy(client, "gc_lapsed")
    assert copy["status"] == "refused" and copy["code"] == "disabled" and "body" not in copy
    assert sha != sha_ok and row("gc_lapsed")["dirty_at"] > before


def test_blocked_devices_are_listed_and_mark_the_copy(client, sudo, proto, copies_on):
    user = make_user(client, sudo, proto, "gc_block")
    phone = Device()
    phone.get(client, sub_path(user))  # claims a slot
    device = client.get("/api/user/gc_block/devices", headers=sudo).json()[0]
    before = row("gc_block")["dirty_at"]
    assert client.post(f"/api/user/gc_block/devices/{device['id']}/block", headers=sudo).status_code == 200
    copy, _ = open_copy(client, "gc_block")
    assert copy["blocked"] == [phone.device_key]
    assert row("gc_block")["dirty_at"] > before


def test_a_deleted_user_keeps_its_row_so_the_copies_get_deleted(client, sudo, proto, copies_on):
    make_user(client, sudo, proto, "gc_gone")
    uid = user_id("gc_gone")
    assert client.delete("/api/user/gc_gone", headers=sudo).status_code == 200
    with GetDB() as db:
        assert crud.get_micky_copy(db, uid) is not None
    gone = client.get(f"/internal/micky-copy/{uid}", headers=AUTH)
    assert gone.status_code == 404 and gone.headers["x-copy-gone"] == "1"  # the only 404 that deletes


def test_other_404s_are_not_marked_gone(client, sudo, proto, copies_on):
    make_user(client, sudo, proto, "gc_unmarked")
    r = fetch_copy(client, "gc_unmarked", **{"X-Forwarded-For": "1.2.3.4"})
    assert r.status_code == 404 and "x-copy-gone" not in r.headers
    assert "x-copy-gone" not in client.get("/internal/no-such-route", headers=AUTH).headers


# --- G headers in sealed answers ---------------------------------------------

def test_g_headers_only_once_the_copies_are_written_with_the_current_key(client, sudo, proto, copies_on,
                                                                          monkeypatch):
    user = make_user(client, sudo, proto, "gc_gate")
    path, phone = sub_path(user), Device()

    def g_headers():
        _, payload = phone.get(client, path)
        assert payload["status"] == "ok"
        return {k: v for k, v in payload["headers"].items() if k in G_HEADERS}

    # Not ready yet: no G header at all, so the app keeps what it has.
    assert g_headers() == {}                                          # no ids yet
    set_row("gc_gate", firestore_doc_id="a" * 32, drive_file_id="drive-1")
    assert g_headers() == {}                                          # never synced
    r = row("gc_gate")
    set_row("gc_gate", synced_at=r["key_changed_at"] - timedelta(seconds=1))
    assert g_headers() == {}                                          # synced with an older key
    set_row("gc_gate", synced_at=r["key_changed_at"])
    assert g_headers() == {"micky-g1": "a" * 32, "micky-g2": "drive-1", "micky-gkey": r["content_key"]}

    monkeypatch.setattr(micky_copy, "BASE", '{"project":"micky-sub2"}')
    assert g_headers()["micky-gbase"] == '{"project":"micky-sub2"}'

    import app.routers.subscription as subscription
    monkeypatch.setattr(subscription, "MICKY_GOOGLE_COPIES", False)
    assert g_headers() == {"micky-g1": ""}                            # switched off: the app deletes


def test_g_headers_never_plain_and_never_on_refusals(client, sudo, proto, copies_on):
    user = make_user(client, sudo, proto, "gc_plain", limit=1)
    r = row("gc_plain")
    set_row("gc_plain", firestore_doc_id="b" * 32, drive_file_id="drive-2", synced_at=r["key_changed_at"])
    path = sub_path(user)
    resp, ok = Device().get(client, path)
    assert ok["headers"]["micky-g1"] == "b" * 32
    assert not G_HEADERS & {k.lower() for k in resp.headers}
    _, refusal = Device().get(client, path)  # second device: device_limit
    assert refusal["status"] == "refused" and "headers" not in refusal


def test_revoke_gives_a_new_key_and_withholds_it_until_resynced(client, sudo, proto, copies_on):
    make_user(client, sudo, proto, "gc_revoke")
    r = row("gc_revoke")
    set_row("gc_revoke", firestore_doc_id="c" * 32, drive_file_id="drive-3", synced_at=r["key_changed_at"])
    new = client.post("/api/user/gc_revoke/revoke_sub", headers=sudo).json()
    after = row("gc_revoke")
    assert after["content_key"] != r["content_key"] and after["key_changed_at"] > r["key_changed_at"]
    assert (after["firestore_doc_id"], after["drive_file_id"]) == ("c" * 32, "drive-3")  # same ids
    _, payload = Device().get(client, sub_path(new))
    assert not G_HEADERS & set(payload["headers"])                    # copies still have the old key
    copy, _ = open_copy(client, "gc_revoke")                          # the copy now uses the new key
    assert copy["headers"][micky.NEW_URL_HEADER] == new["subscription_url"]


# --- dirty marks ----------------------------------------------------------------

def test_status_changes_by_the_review_job_mark_the_copy(client, sudo, proto, copies_on):
    from app.models.user import UserStatus
    make_user(client, sudo, proto, "gc_review")
    before = row("gc_review")["dirty_at"]
    with GetDB() as db:
        crud.update_user_status(db, crud.get_user(db, "gc_review"), UserStatus.limited)
    assert row("gc_review")["dirty_at"] > before


def test_host_changes_mark_every_copy(client, sudo, proto, copies_on):
    make_user(client, sudo, proto, "gc_hosts")
    before = row("gc_hosts")["dirty_at"]
    hosts = client.get("/api/hosts", headers=sudo).json()
    assert client.put("/api/hosts", headers=sudo, json=hosts).status_code == 200
    assert row("gc_hosts")["dirty_at"] > before


def test_an_admins_users_are_marked_when_it_is_deleted(client, sudo, proto, copies_on):
    # The API refuses an admin that still owns users (409); marzban-cli still
    # deletes it, leaving them ownerless, which changes what their copies say.
    from typer.testing import CliRunner

    from cli.admin import app as admin_cli

    client.post("/api/admin", headers=sudo, json={"username": "gc_admin", "password": "pw12345", "is_sudo": False})
    make_user(client, sudo, proto, "gc_owned")
    client.put("/api/user/gc_owned/set-owner", headers=sudo, params={"admin_username": "gc_admin"})
    before = row("gc_owned")["dirty_at"]
    assert client.delete("/api/admin/gc_admin", headers=sudo).status_code == 409
    r = CliRunner().invoke(admin_cli, ["delete", "--username", "gc_admin", "--yes"])
    assert r.exit_code == 0, r.output
    assert row("gc_owned")["dirty_at"] > before


def test_a_legacy_user_taking_micky_gets_a_copy(client, sudo, proto, copies_on):
    client.post("/api/user", headers=sudo, json={"username": "gc_adopt", "proxies": {proto: {}}, "inbounds": {}})
    assert row("gc_adopt") is None
    r = client.put("/api/user/gc_adopt", headers=sudo, json={"client_type": "micky", "device_limit": 2})
    assert r.status_code == 200, r.text
    assert row("gc_adopt") is not None


def test_link_regeneration_marks_the_copy(client, sudo, proto, copies_on):
    make_user(client, sudo, proto, "gc_regen")
    before = row("gc_regen")["dirty_at"]
    with GetDB() as db:
        crud.regenerate_happ_subscription(db, crud.get_user(db, "gc_regen"))
    assert row("gc_regen")["dirty_at"] > before


def test_a_typo_in_micky_google_base_is_dropped():
    assert micky_copy._load_base("") is None
    assert micky_copy._load_base("not json") is None
    assert micky_copy._load_base('{"project": "micky-sub", "extra": "x"}') is None
    assert micky_copy._load_base('{"project": "p"}') is None          # the app's pattern: 6-30 chars
    assert micky_copy._load_base('{"key": "short"}') is None           # 20-128 chars
    key = "AIzaSyA-" + "x" * 31
    assert micky_copy._load_base(f'{{"project": "micky-sub", "key": "{key}"}}') == \
        f'{{"project":"micky-sub","key":"{key}"}}'


def test_dirty_marks_are_precise_enough_to_order_two_quick_changes(client, sudo, proto, copies_on):
    make_user(client, sudo, proto, "gc_quick")
    marks = []
    for _ in range(2):
        with GetDB() as db:
            crud.mark_copy_dirty(db, user_id("gc_quick"))
            db.commit()
        marks.append(row("gc_quick")["dirty_at"])
    assert marks[1] > marks[0] and isinstance(marks[0], datetime)


def test_the_migration_round_trips_and_repairs_the_table(client, sudo, proto):
    """Downgrade keeps micky_copies (its ids are the only record of the public
    copies); upgrading again marks every row dirty and adds rows for Micky
    users created while the previous image ran."""
    from alembic import command
    from alembic.config import Config
    from tests.conftest import ROOT
    make_user(client, sudo, proto, "gc_migr_kept")
    make_user(client, sudo, proto, "gc_migr_new")
    make_user(client, sudo, proto, "gc_migr_revoked")
    kept_before, revoked_before = row("gc_migr_kept"), row("gc_migr_revoked")
    with GetDB() as db:  # as if created under the previous image: no row
        db.delete(crud.get_micky_copy(db, user_id("gc_migr_new")))
        # ...and revoked under it: no key rotation
        crud.get_user(db, "gc_migr_revoked").sub_revoked_at = datetime.utcnow() + timedelta(seconds=1)
        db.commit()
    cfg = Config(os.path.join(ROOT, "alembic.ini"))
    command.downgrade(cfg, "-1")
    command.upgrade(cfg, "head")
    kept = row("gc_migr_kept")
    assert kept["content_key"] == kept_before["content_key"] and kept["dirty_at"] > kept_before["dirty_at"]
    assert row("gc_migr_new") is not None
    assert row("gc_migr_revoked")["content_key"] != revoked_before["content_key"]


# --- spec v2 -----------------------------------------------------------------

def test_no_copy_row_means_the_removal_signal(client, sudo, proto, copies_on):
    """A legacy user fetched by Micky has no copies: the app must drop any G profiles."""
    client.post("/api/user", headers=sudo, json={"username": "gc_legacy", "proxies": {proto: {}}, "inbounds": {}})
    link = client.get("/api/user/gc_legacy", headers=sudo).json()["subscription_url"]
    _, payload = Device().get(client, urlparse(link).path)
    assert payload["status"] == "ok" and payload["headers"]["micky-g1"] == ""
    assert not {"micky-g2", "micky-gkey"} & set(payload["headers"])


def test_copies_are_strictly_increasing_per_user(client, sudo, proto, copies_on):
    make_user(client, sudo, proto, "gc_issued")
    stamps = [open_copy(client, "gc_issued")[0]["issued_at"] for _ in range(3)]  # same second
    assert stamps[0] < stamps[1] < stamps[2] and 1_700_000_000 <= stamps[0] < 2**32


def test_blocked_lists_micky_identities_and_bound_keys_never_hardware_ids(client, sudo, proto, copies_on):
    from app.db.models import UserSubscriptionDevice
    make_user(client, sudo, proto, "gc_ids")
    uid = user_id("gc_ids")
    with GetDB() as db:
        for key, bound in (("hwid:RAWHWID", None), ("ua:abc", None), ("id:" + "i" * 43, "pk:" + "b" * 43),
                           ("pk:" + "p" * 43, None)):
            db.add(UserSubscriptionDevice(user_id=uid, device_key=key, bound_pub_key=bound, is_blocked=True))
        db.add(UserSubscriptionDevice(user_id=uid, device_key="pk:" + "n" * 43, is_blocked=False))
        db.commit()
    copy, _ = open_copy(client, "gc_ids")
    assert copy["blocked"] == sorted(["id:" + "i" * 43, "pk:" + "b" * 43, "pk:" + "p" * 43])


def test_a_blocked_slot_stays_blocked_when_the_id_goes_missing(client, sudo, proto):
    """The panel's own check: a request whose key is bound to a blocked id: slot."""
    from app.db.models import UserSubscriptionDevice
    user = make_user(client, sudo, proto, "gc_bound")
    phone = Device()
    with GetDB() as db:
        db.add(UserSubscriptionDevice(user_id=user_id("gc_bound"), device_key="id:" + "z" * 43,
                                      bound_pub_key=phone.device_key, is_blocked=True))
        db.commit()
    _, payload = phone.get(client, sub_path(user))  # no M-Device-Id sent
    assert payload["status"] == "refused" and payload["code"] == "device_blocked"


def test_deleting_an_admin_marks_every_copy(client, sudo, proto, copies_on):
    """Its host_admins rows cascade: hosts scoped only to it become everyone's."""
    client.post("/api/admin", headers=sudo, json={"username": "gc_admin2", "password": "pw12345", "is_sudo": False})
    make_user(client, sudo, proto, "gc_bystander")
    before = row("gc_bystander")["dirty_at"]
    assert client.delete("/api/admin/gc_admin2", headers=sudo).status_code == 200
    assert row("gc_bystander")["dirty_at"] > before


def test_the_endpoint_cannot_be_told_apart_from_nothing(client, sudo, proto, copies_on):
    make_user(client, sudo, proto, "gc_probe")
    path = f"/internal/micky-copy/{user_id('gc_probe')}"
    for method in ("post", "put", "delete", "patch", "options"):
        assert getattr(client, method)(path, headers=AUTH).status_code == 404
    assert client.head(path, headers=AUTH).status_code == 404
    assert client.get("/internal/micky-copy/abc", headers=AUTH).status_code == 404


def test_a_revoke_changes_the_sha_even_when_the_copy_says_the_same(client, sudo, proto, copies_on):
    make_user(client, sudo, proto, "gc_revsha")
    client.put("/api/user/gc_revsha", headers=sudo, json={"status": "disabled"})
    _, before = open_copy(client, "gc_revsha")
    client.post("/api/user/gc_revsha/revoke_sub", headers=sudo)
    copy, after = open_copy(client, "gc_revsha")
    assert copy["status"] == "refused" and after != before


def test_global_announcements_and_manual_hosts_mark_every_copy(client, sudo, proto, copies_on):
    make_user(client, sudo, proto, "gc_global")
    before = row("gc_global")["dirty_at"]
    assert client.put("/api/announcement/global", headers=sudo, json={"text": "hello", "enabled": True}).status_code == 200
    copy, _ = open_copy(client, "gc_global")
    assert row("gc_global")["dirty_at"] > before and "announce" in copy["headers"]
    before = row("gc_global")["dirty_at"]
    r = client.post("/api/manual-hosts", headers=sudo, json={"name": "gc-manual", "link": "trojan://pw@203.0.113.9:443#gc"})
    assert r.status_code in (200, 201), r.text
    assert row("gc_global")["dirty_at"] > before
    client.delete("/api/announcement/global", headers=sudo)


def test_a_revoke_key_always_lands_after_the_last_sync(client, sudo, proto, copies_on):
    """Even with the clock behind the last sync, the new key is gated."""
    make_user(client, sudo, proto, "gc_clock")
    r = row("gc_clock")
    future = datetime.utcnow() + timedelta(hours=1)  # as if synced by a clock ahead of ours
    set_row("gc_clock", firestore_doc_id="d" * 32, drive_file_id="drive-4", synced_at=future)
    client.post("/api/user/gc_clock/revoke_sub", headers=sudo)
    after = row("gc_clock")
    assert after["content_key"] != r["content_key"] and after["key_changed_at"] > future


def test_odd_spellings_of_the_route_look_like_nothing(client, sudo, proto, copies_on):
    make_user(client, sudo, proto, "gc_odd")
    base = f"/internal/micky-copy/{user_id('gc_odd')}"
    for path in (base + "/", "/internal/micky-copy/%C2%B3", "/internal/micky-copy/-1"):
        r = client.get(path, headers=AUTH, follow_redirects=False)
        assert r.status_code == 404 and "x-copy-gone" not in r.headers, path


def test_a_malformed_ref_is_never_sent(client, sudo, proto, copies_on):
    """The app would silently ignore all four headers; send none instead."""
    user = make_user(client, sudo, proto, "gc_badref")
    r = row("gc_badref")
    set_row("gc_badref", firestore_doc_id="has space", drive_file_id="drive-5", synced_at=r["key_changed_at"])
    _, payload = Device().get(client, sub_path(user))
    assert payload["status"] == "ok" and not G_HEADERS & set(payload["headers"])
