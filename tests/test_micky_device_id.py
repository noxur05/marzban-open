"""M-Device-Id (docs/micky-protocol.md "Device identity"): Micky device slots
survive reinstalls. A reinstall = same M-Device-Id, new device key."""
import base64
import hashlib
import itertools
from datetime import datetime, timedelta
from urllib.parse import urlparse

import pytest

from app.utils import micky
from tests.micky_helpers import LINK_PRIVATE, Device

_n = itertools.count()


def device_id(os_name: str, platform_id: str) -> str:
    """What the app sends (spec): b64u(sha256("micky-device-id"|0x00|os|0x00|lower(trim(id))))."""
    raw = b"micky-device-id\x00" + os_name.encode() + b"\x00" + platform_id.strip().lower().encode()
    return base64.urlsafe_b64encode(hashlib.sha256(raw).digest()).rstrip(b"=").decode()


def test_device_id_matches_the_apps_vectors():
    # Cross-checked byte for byte with the Micky app (test/core/app_info/device_info_test.dart).
    assert device_id("Android", "a1b2c3d4e5f60718") == "AUktUEkPqndXoP6QZrPBVUhnYueDDkhPzalXrDAvaPg"
    assert device_id("Windows", "{8E3A1B2C-0000-4000-8000-ABCDEF012345}") == "1UBJ_uMAbpj59OGaHlQBd9yDtCn9-ucPUT387Y7noCI"


class Phone:
    """One physical phone: a stable M-Device-Id; each install has its own key."""

    def __init__(self, send_id=True):
        self.id = device_id("Android", f"phone-{next(_n):04x}") if send_id else None
        self.install = Device()

    def reinstall(self):
        self.install = Device()

    def get(self, client, path, **extra):
        headers = {"M-Device-Id": self.id, **extra} if self.id else extra
        return self.install.get(client, path, **headers)[1]


@pytest.fixture
def user(client, sudo, proto):
    u = client.post("/api/user", headers=sudo, json={
        "username": f"did{next(_n)}", "proxies": {proto: {}}, "inbounds": {},
        "client_type": "micky", "device_limit": 1}).json()
    u["path"] = urlparse(micky.open_link(u["subscription_url"], LINK_PRIVATE)).path
    return u


def devices(client, sudo, user):
    return client.get(f"/api/user/{user['username']}/devices", headers=sudo).json()


def test_reinstall_keeps_the_slot(client, sudo, user):
    phone = Phone()
    assert phone.get(client, user["path"])["status"] == "ok"
    phone.reinstall()
    assert phone.get(client, user["path"])["status"] == "ok"        # rebind, not device_limit
    rows = devices(client, sudo, user)
    assert len(rows) == 1 and rows[0]["device_id"] == phone.id       # admins see the id


def test_a_second_phone_still_hits_the_limit(client, user):
    assert Phone().get(client, user["path"])["status"] == "ok"
    assert Phone().get(client, user["path"])["code"] == "device_limit"


def test_rebind_budget_per_24h_then_window_resets(client, sudo, user):
    phone = Phone()
    assert phone.get(client, user["path"])["status"] == "ok"
    for _ in range(3):
        phone.reinstall()
        assert phone.get(client, user["path"])["status"] == "ok"
    bound = phone.install
    phone.reinstall()
    assert phone.get(client, user["path"])["code"] == "device_limit"  # 4th rebind in the window
    assert bound.get(client, user["path"], **{"M-Device-Id": phone.id})[1]["status"] == "ok"  # bound key still works

    from app.db import GetDB
    from app.db.models import UserSubscriptionDevice
    with GetDB() as db:
        row = db.query(UserSubscriptionDevice).filter_by(device_key="id:" + phone.id).one()
        row.rebind_window_start = datetime.utcnow() - timedelta(hours=25)
        db.commit()
    assert phone.get(client, user["path"])["status"] == "ok"          # new window


def test_older_build_slot_is_upgraded_in_place(client, sudo, user):
    phone = Phone(send_id=False)                                      # older build: key only
    assert phone.get(client, user["path"])["status"] == "ok"
    phone.id = device_id("Android", f"upgraded-{next(_n)}")           # app update starts sending the id
    assert phone.get(client, user["path"])["status"] == "ok"          # no second slot needed
    rows = devices(client, sudo, user)
    assert len(rows) == 1 and rows[0]["device_id"] == phone.id


def test_block_by_id_survives_reinstall(client, sudo, user):
    phone = Phone()
    assert phone.get(client, user["path"])["status"] == "ok"
    row = devices(client, sudo, user)[0]
    assert client.post(f"/api/user/{user['username']}/devices/{row['id']}/block", headers=sudo).status_code == 200
    phone.reinstall()
    assert phone.get(client, user["path"])["code"] == "device_blocked"


def test_block_placed_before_the_id_still_holds(client, sudo, proto):
    legacy = client.post("/api/user", headers=sudo, json={
        "username": f"did{next(_n)}", "proxies": {proto: {}}, "inbounds": {}}).json()  # no device limit
    path = urlparse(legacy["subscription_url"]).path  # legacy users get the plain URL
    phone = Phone(send_id=False)
    assert phone.get(client, path)["status"] == "ok"
    rows = devices(client, sudo, legacy)
    client.post(f"/api/user/{legacy['username']}/devices/{rows[0]['id']}/block", headers=sudo)
    phone.id = device_id("Android", f"late-{next(_n)}")
    assert phone.get(client, path)["code"] == "device_blocked"


@pytest.mark.parametrize("bad", ["", "short", "A" * 44, "AUktUEkPqndXoP6QZrPBVUhnYueDDkhPzalXrDAvaPh"])
def test_malformed_id_falls_back_to_the_key(client, sudo, user, bad):
    d = Device()
    assert d.get(client, user["path"], **{"M-Device-Id": bad})[1]["status"] == "ok"
    assert devices(client, sudo, user)[0]["device_id"] == d.device_key[3:]  # "pk:" identity
