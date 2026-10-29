"""Usage resets: only a traffic-limited user comes back, and a user the
periodic reset brings back is put back on Xray. Plus the XTLS flow rule that
add_user and update_user share."""
import itertools
from datetime import datetime, timedelta
from uuid import uuid4

import pytest

import app.xray.operations as _ops
from xray_api.types.account import VLESSAccount, XTLSFlows

_n = itertools.count()

# Taken at import, before conftest's `client` fixture stubs these out for the
# session (no live Xray in tests).
_REAL = {"add_user": _ops.add_user, "update_user": _ops.update_user}


def _user(client, sudo, proto, **extra):
    r = client.post("/api/user", headers=sudo, json={
        "username": f"reset{next(_n)}", "proxies": {proto: {}}, "inbounds": {}, **extra})
    assert r.status_code == 200, r.text
    return r.json()["username"]


def _set(username, **columns):
    from app.db import GetDB
    from app.db.models import User
    with GetDB() as db:
        user = db.query(User).filter_by(username=username).one()
        for name, value in columns.items():
            setattr(user, name, value)
        db.commit()


def _status(username):
    from app.db import GetDB
    from app.db.models import User
    with GetDB() as db:
        return db.query(User).filter_by(username=username).one().status


@pytest.fixture
def added(monkeypatch):
    """Usernames passed to xray.operations.add_user (creating a user adds it
    too, so tests clear this after their setup)."""
    import app.xray.operations as ops
    names = []
    monkeypatch.setattr(ops, "add_user", lambda dbuser: names.append(dbuser.username))
    return names


def test_periodic_reset_puts_a_limited_user_back_on_xray(client, sudo, proto, added):
    limited = _user(client, sudo, proto, data_limit=1024, data_limit_reset_strategy="day")
    active = _user(client, sudo, proto, data_limit=1024, data_limit_reset_strategy="day")
    two_days_ago = datetime.utcnow() - timedelta(days=2)
    _set(limited, used_traffic=4096, status="limited", created_at=two_days_ago)
    _set(active, used_traffic=512, created_at=two_days_ago)
    added.clear()

    from app.jobs.reset_user_data_usage import reset_user_data_usage
    reset_user_data_usage()

    assert _status(limited) == "active"
    assert limited in added          # it was off every node; now it's back
    assert active not in added       # already on the nodes


@pytest.mark.parametrize("before,after", [
    ("limited", "active"),
    ("active", "active"),
    ("disabled", "disabled"),
    ("on_hold", "on_hold"),
    ("expired", "expired"),
])
def test_usage_reset_lifts_only_the_traffic_limit(client, sudo, proto, before, after):
    name = _user(client, sudo, proto, data_limit=1024)
    _set(name, status=before, used_traffic=4096)
    r = client.post(f"/api/user/{name}/reset", headers=sudo)
    assert r.status_code == 200, r.text
    assert r.json()["status"] == after
    assert r.json()["used_traffic"] == 0


def test_resetting_a_disabled_user_does_not_put_it_back_on_xray(client, sudo, proto, added):
    name = _user(client, sudo, proto, data_limit=1024)
    _set(name, status="disabled")
    added.clear()
    assert client.post(f"/api/user/{name}/reset", headers=sudo).status_code == 200
    assert name not in added


@pytest.mark.parametrize("inbound,keeps_flow", [
    ({"network": "raw", "tls": "reality"}, True),     # dropped by update_user before the fix
    ({"network": "tcp", "tls": "tls"}, True),
    ({"network": "kcp", "tls": "reality"}, True),
    ({}, False),                                      # tcp by default, but no tls
    ({"network": "ws", "tls": "tls"}, False),
    ({"network": "raw", "tls": "none"}, False),
    ({"network": "tcp", "tls": "reality", "header_type": "http"}, False),
])
def test_flow_is_kept_only_where_xtls_works(inbound, keeps_flow):
    from app.xray.operations import _drop_unsupported_flow
    account = VLESSAccount(email="1.user", id=uuid4(), flow=XTLSFlows.VISION)
    _drop_unsupported_flow(account, inbound)
    assert account.flow == (XTLSFlows.VISION if keeps_flow else XTLSFlows.NONE)


@pytest.mark.parametrize("name", ["add_user", "update_user"])
def test_add_and_update_both_apply_the_shared_flow_rule(client, sudo, proto, monkeypatch, name):
    """The RAW bug was update_user carrying its own copy of the rule; both must
    go through _drop_unsupported_flow, once per inbound the user is on."""
    from app.db import GetDB
    from app.db.models import User

    seen = []
    monkeypatch.setattr(_ops, "_drop_unsupported_flow", lambda account, inbound: seen.append(inbound))
    for helper in ("_add_user_to_inbound", "_alter_inbound_user", "_remove_user_from_inbound"):
        monkeypatch.setattr(_ops, helper, lambda *a, **k: None)
    username = _user(client, sudo, proto)
    with GetDB() as db:
        _REAL[name](db.query(User).filter_by(username=username).one())
    assert seen, f"{name} bypassed _drop_unsupported_flow"
