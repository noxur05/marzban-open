"""Test harness: a throwaway SQLite panel with the real alembic migrations and
Micky test keys, driven through FastAPI's TestClient.

Environment is set BEFORE anything imports `config` (decouple reads os.environ
first, and load_dotenv never overrides it), so a developer's .env can't leak in.
"""
import os
import stat
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

_tmp = tempfile.mkdtemp(prefix="marzban-tests-")
_xray = os.path.join(_tmp, "xray")
with open(_xray, "w") as f:
    f.write('#!/bin/sh\necho "Xray 25.1.1 (test stub)"\n')
os.chmod(_xray, os.stat(_xray).st_mode | stat.S_IEXEC)

# Layer 1 test key 1 from docs/micky-protocol.md (its private half is in
# tests/micky_helpers.py); signing seed = the layer 2 vector seed
# (sha256(b"micky test signing key"), app/utils/micky.py:_vector_inputs).
LINK_PUBLIC_1 = "BB6o4psXm28Jd9DP0Zbiypo4eS1HYzhD7_wn4fUpVdUrCk-qyc66zbR0q-UvXUgol_aO3p31nOTgae35w02iLQU"
SIGNING_SEED_1 = "X8CohMk6_akOFbgQzUfDTq83JJU-QEwHSiMNs2dYxsE"

os.environ.update({
    "SQLALCHEMY_DATABASE_URL": f"sqlite:///{_tmp}/test.db",
    "XRAY_EXECUTABLE_PATH": _xray,
    "XRAY_JSON": os.path.join(ROOT, "xray_config.json"),
    "XRAY_SUBSCRIPTION_URL_PREFIX": "https://panel.test",
    "SUDO_USERNAME": "root",
    "SUDO_PASSWORD": "rootpw",
    "REDIS_URL": "",
    "TELEGRAM_API_TOKEN": "",
    "MICKY_LINK_PUBLIC_KEYS": f"1:{LINK_PUBLIC_1}",
    "MICKY_LINK_KEY_ID": "1",
    "MICKY_SIGNING_KEYS": f"1:{SIGNING_SEED_1}",
    "MICKY_SIGNING_KEY_ID": "1",
})

import pytest  # noqa: E402


@pytest.fixture(scope="session")
def client():
    from alembic.config import Config
    from alembic import command
    command.upgrade(Config(os.path.join(ROOT, "alembic.ini")), "head")

    import app.xray.operations as ops
    for name in ("add_user", "update_user", "remove_user"):
        setattr(ops, name, lambda *a, **k: None)  # no live Xray in tests

    from fastapi.testclient import TestClient
    from app import app
    # No `with`: startup would start the scheduler, Xray and the dashboard.
    return TestClient(app, raise_server_exceptions=True)


@pytest.fixture(scope="session")
def sudo(client):
    r = client.post("/api/admin/token", data={"username": "root", "password": "rootpw"})
    return {"Authorization": f"Bearer {r.json()['access_token']}"}


@pytest.fixture(scope="session")
def proto():
    from app import xray
    return next(iter(xray.config.inbounds_by_protocol))
