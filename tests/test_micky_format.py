"""Micky gets the same subscription format as Happ — the v2ray URI list — so
hosts merged only into that format (external subscriptions, manual hosts)
reach Micky users too."""
import base64
import itertools
from urllib.parse import urlparse

from app.utils.jwt import create_subscription_token
from tests.micky_helpers import Device, is_uri_list

HAPP_UA = "Happ/4.4.1/Android/17891107313301967618"
_n = itertools.count()


def test_micky_gets_exactly_the_list_happ_gets(client, sudo, proto):
    legacy = client.post("/api/user", headers=sudo, json={
        "username": f"fmt{next(_n)}", "proxies": {proto: {}}, "inbounds": {}}).json()  # served to both apps
    path = urlparse(legacy["subscription_url"]).path
    happ = client.get(path, headers={"User-Agent": HAPP_UA, "X-Hwid": f"fmt-{next(_n)}"})
    happ_lines = base64.b64decode(happ.content).decode().splitlines()
    payload = Device().get(client, path)[1]
    assert payload["status"] == "ok" and is_uri_list(payload["body"])
    assert payload["body"].splitlines() == happ_lines


def test_manual_hosts_reach_micky_users(client, sudo, proto):
    from app.db import GetDB
    from app.db.models import Admin

    client.post("/api/admin", headers=sudo, json={"username": "fmt_admin", "password": "pw12345", "is_sudo": False})
    with GetDB() as db:
        admin_id = db.query(Admin.id).filter_by(username="fmt_admin").scalar()
    link = client.post("/api/manual-hosts", headers=sudo, json={
        "name": "fmt-manual", "admin_ids": [admin_id],
        "link": "vless://11111111-2222-3333-4444-555555555555@manual.example.com:443?type=ws&security=tls#Manual",
    }).json()["link"]
    user = client.post("/api/user", headers=sudo, json={
        "username": f"fmt{next(_n)}", "proxies": {proto: {}}, "inbounds": {},
        "client_type": "micky", "device_limit": 1}).json()
    client.put(f"/api/user/{user['username']}/set-owner", headers=sudo, params={"admin_username": "fmt_admin"})

    payload = Device().get(client, f"/sub/{create_subscription_token(user['username'])}")[1]
    assert payload["status"] == "ok"
    assert link in payload["body"].splitlines()  # was never in the sing-box body
