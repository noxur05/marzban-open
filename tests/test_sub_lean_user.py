"""The lean subscription load (crud.get_user_for_sub + SubscriptionUser): the
answers are exactly what the full UserResponse produced, the info page still
shows lifetime usage and the next plan, and a fetch never reads the usage
history."""
import base64

import pytest
from sqlalchemy import event
from sqlalchemy.exc import InvalidRequestError

from app.db import GetDB, crud
from app.db.base import engine
from app.models.user import UserResponse
from app.routers.subscription import client_config
from app.subscription.share import generate_subscription
from app.utils.system import readable_size
from app.utils.jwt import create_subscription_token

HAPP_UA = "Happ/4.4.1/Android/17891107313301967618"
GB = 1024 ** 3


def _user(client, sudo, proto, name, **fields):
    r = client.post("/api/user", headers=sudo,
                    json={"username": name, "proxies": {proto: {}}, "inbounds": {}, **fields})
    assert r.status_code == 200, r.text
    return name


def _full_render(name, fmt):
    """What the previous code served: generate_subscription over the full UserResponse."""
    cfg = client_config[fmt]
    with GetDB() as db:
        user = UserResponse.model_validate(crud.get_user(db, name))
        return generate_subscription(user=user, config_format=cfg["config_format"],
                                     as_base64=cfg["as_base64"], reverse=cfg["reverse"])


@pytest.fixture
def sql():
    stmts = []

    def rec(conn, cursor, statement, params, context, executemany):
        stmts.append(" ".join(statement.split()))

    event.listen(engine, "before_cursor_execute", rec)
    yield stmts
    event.remove(engine, "before_cursor_execute", rec)


@pytest.mark.parametrize("fmt", ["v2ray", "v2ray-json", "clash", "clash-meta", "sing-box", "outline"])
@pytest.mark.parametrize("kind", ["legacy", "happ"])
def test_every_format_is_unchanged(client, sudo, proto, fmt, kind):
    fields = {"client_type": "happ", "device_limit": 2} if kind == "happ" else {"outline_enabled": True}
    name = _user(client, sudo, proto, f"lean_{kind}_{fmt.replace('-', '_')}", **fields)
    r = client.get(f"/sub/{create_subscription_token(name)}/{fmt}",
                   headers={"User-Agent": HAPP_UA, "X-Hwid": "lean-1"})
    assert r.status_code == 200
    if fmt == "outline" and kind == "happ":
        assert r.text == "{}"  # Outline stays off for device-limited users
    else:
        assert r.text == _full_render(name, fmt)


@pytest.mark.parametrize("ua, fmt", [
    (HAPP_UA, "v2ray"),
    ("v2rayNG/1.8.5", "v2ray"),
    ("clash-verge/v1.3.8", "clash-meta"),
    ("ClashForAndroid/2.5.12", "clash"),
    ("SFA/1.8.0", "sing-box"),
])
def test_the_auto_route_is_unchanged(client, sudo, proto, ua, fmt):
    name = _user(client, sudo, proto, f"lean_auto_{fmt.replace('-', '_')}_{len(ua)}")
    r = client.get(f"/sub/{create_subscription_token(name)}", headers={"User-Agent": ua})
    assert r.status_code == 200
    assert r.text == _full_render(name, fmt)
    title = base64.b64decode(r.headers["profile-title"].removeprefix("base64:")).decode()
    assert title == name
    assert r.headers["subscription-userinfo"] == "upload=0; download=0; total=0; expire=0"


def test_a_fetch_never_reads_the_usage_history(client, sudo, proto, sql):
    name = _user(client, sudo, proto, "lean_sql", client_type="happ", device_limit=2)
    path = f"/sub/{create_subscription_token(name)}"
    hdrs = {"User-Agent": HAPP_UA, "X-Hwid": "lean-sql"}
    client.get(path, headers=hdrs)  # claims the slot, writes the touch
    sql.clear()
    assert client.get(path, headers=hdrs).status_code == 200
    assert not [s for s in sql if "user_usage_logs" in s or "next_plans" in s]
    assert len([s for s in sql if " FROM users" in s]) == 1


def test_the_lean_user_refuses_to_lazy_load_what_it_left_out(client, sudo, proto):
    name = _user(client, sudo, proto, "lean_guard")
    with GetDB() as db:
        dbuser = crud.get_user_for_sub(db, name)
        for attr in ("usage_logs", "next_plan"):
            with pytest.raises(InvalidRequestError):
                getattr(dbuser, attr)


def test_the_info_page_still_shows_lifetime_usage_and_the_next_plan(client, sudo, proto):
    name = _user(client, sudo, proto, "lean_page")
    with GetDB() as db:  # 5 GB used, reset (logged), then 1 GB more
        dbuser = crud.get_user(db, name)
        dbuser.used_traffic = 5 * GB
        db.commit()
        crud.reset_user_data_usage(db, dbuser)
        dbuser = crud.get_user(db, name)
        dbuser.used_traffic = 1 * GB
        db.commit()
    # After the reset: a reset deletes the next plan.
    r = client.put(f"/api/user/{name}", headers=sudo,
                   json={"next_plan": {"data_limit": 7 * GB, "expire": 0, "add_remaining_traffic": True}})
    assert r.status_code == 200, r.text
    r = client.get(f"/sub/{create_subscription_token(name)}", headers={"Accept": "text/html"})
    assert r.status_code == 200 and "text/html" in r.headers["content-type"]
    assert readable_size(6 * GB) in r.text          # lifetime = 5 (history) + 1 (current)
    assert "Indiki meýilnama nobatda" in r.text     # next plan block
    assert readable_size(7 * GB) in r.text
