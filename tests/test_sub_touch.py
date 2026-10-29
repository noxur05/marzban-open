"""update_user_sub on a served /sub fetch: the write lands (at most once a
minute per user agent), and nothing after its commit re-reads the (now
expired) user row."""
from datetime import datetime, timedelta
from urllib.parse import urlparse

import pytest
from sqlalchemy import event

from app.db import GetDB, crud
from app.db.base import engine
from app.utils import micky
from app.utils.jwt import create_subscription_token
from tests.micky_helpers import LINK_PRIVATE, Device

HAPP_UA = "Happ/4.4.1/Android/17891107313301967618"
HAPP_UA_NEW = "Happ/4.5.0/Android/17891107313301967618"


class _SQL:
    def __init__(self):
        self.stmts = []

    def __call__(self, conn, cursor, statement, params, context, executemany):
        self.stmts.append(" ".join(statement.split()))

    def touches(self):
        return [s for s in self.stmts if s.startswith("UPDATE users ")]

    def after_user_update(self):
        i = next(i for i, s in enumerate(self.stmts) if s.startswith("UPDATE users "))
        return self.stmts[i + 1:]


@pytest.fixture
def sql():
    rec = _SQL()
    event.listen(engine, "before_cursor_execute", rec)
    yield rec
    event.remove(engine, "before_cursor_execute", rec)


def _user(client, sudo, proto, name, **fields):
    r = client.post("/api/user", headers=sudo,
                    json={"username": name, "proxies": {proto: {}}, "inbounds": {}, **fields})
    assert r.status_code == 200, r.text
    return r.json()


def _age_touch(name, seconds):
    with GetDB() as db:
        dbuser = crud.get_user(db, name)
        dbuser.sub_updated_at = datetime.utcnow() - timedelta(seconds=seconds)
        db.commit()


@pytest.mark.parametrize("fields", [{}, {"client_type": "happ", "device_limit": 2}], ids=["legacy", "happ"])
def test_served_fetch_records_ua_and_does_not_reload_the_user(client, sudo, proto, sql, fields):
    name = f"touch_{fields.get('client_type', 'legacy')}"
    _user(client, sudo, proto, name, **fields)
    path = f"/sub/{create_subscription_token(name)}"
    assert client.get(path, headers={"User-Agent": HAPP_UA, "X-Hwid": "touch-1"}).status_code == 200
    sql.stmts.clear()
    # A new user agent is written at once, throttle or not.
    r = client.get(path, headers={"User-Agent": HAPP_UA_NEW, "X-Hwid": "touch-1"})
    assert r.status_code == 200 and r.content
    assert len(sql.touches()) == 1
    assert not [s for s in sql.after_user_update() if "FROM users" in s]
    u = client.get(f"/api/user/{name}", headers=sudo).json()
    assert u["sub_last_user_agent"] == HAPP_UA_NEW and u["sub_updated_at"]


def test_a_repeat_fetch_within_a_minute_writes_nothing(client, sudo, proto, sql):
    _user(client, sudo, proto, "touch_repeat")
    path = f"/sub/{create_subscription_token('touch_repeat')}"
    hdrs = {"User-Agent": HAPP_UA, "X-Hwid": "touch-2"}
    client.get(path, headers=hdrs)
    sql.stmts.clear()
    assert client.get(path, headers=hdrs).status_code == 200
    assert sql.touches() == []

    _age_touch("touch_repeat", 61)  # older than the throttle: written again
    sql.stmts.clear()
    assert client.get(path, headers=hdrs).status_code == 200
    assert len(sql.touches()) == 1

    _age_touch("touch_repeat", -3600)  # in the future (clock jump): written, not frozen
    sql.stmts.clear()
    assert client.get(path, headers=hdrs).status_code == 200
    assert len(sql.touches()) == 1


def test_micky_fetch_keeps_new_url_and_does_not_reload_the_user(client, sudo, proto, sql):
    user = _user(client, sudo, proto, "touch_micky", client_type="micky", device_limit=2)
    path, phone = urlparse(micky.open_link(user["subscription_url"], LINK_PRIVATE)).path, Device()
    phone.get(client, path)                                      # claims the slot
    _age_touch("touch_micky", 61)                                # so this fetch writes
    sql.stmts.clear()
    _, payload = phone.get(client, path)
    assert payload["status"] == "ok"
    assert payload["headers"][micky.NEW_URL_HEADER] == user["subscription_url"]
    assert len(sql.touches()) == 1
    assert not [s for s in sql.after_user_update() if "FROM users" in s]
