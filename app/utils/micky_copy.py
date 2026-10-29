"""Google copies of Micky subscriptions: the copy format (docs/micky-protocol.md,
"Google copies"). Pure functions; the panel side lives in crud (keys, dirty
marks), app/routers/internal.py (the endpoint the sync service reads) and
app/routers/subscription.py (the G headers in sealed answers).

    blob = 0x01 | nonce (12 bytes) | AES-256-GCM(content key, nonce, plaintext, aad = "micky-copy")

Byte-exactness is pinned by the app's vectors (tests/micky_copy_vectors.json).
"""
import hashlib
import json
import logging
import re
import secrets
from typing import Optional

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from app.utils.micky import b64u, b64u_decode
from config import MICKY_GOOGLE_BASE

VERSION = b"\x01"
AAD = b"micky-copy"
NONCE_LEN = 12
KEY_LEN = 32

# Inside payload["headers"] of a sealed OK answer only, never inside a copy.
G1_HEADER = "micky-g1"
G2_HEADER = "micky-g2"
KEY_HEADER = "micky-gkey"
BASE_HEADER = "micky-gbase"

# Usage moves every few seconds of traffic; on its own it must not count as a
# change (the sync service re-uploads at least every 6 h anyway). Only the
# download counter: total and expire in the same line are real changes (a
# renewal must reach the copy within seconds).
_USAGE_HEADER = "subscription-userinfo"
_DOWNLOAD_RE = re.compile(r"(^|;\s*)download=\d+")


# The app's patterns for the header values (docs/micky-protocol.md): a value it
# rejects would make it silently ignore all four G headers.
_ID_RE = re.compile(r"[A-Za-z0-9_-]{1,128}")
_KEY_RE = re.compile(r"[A-Za-z0-9_-]{43}")


def valid_refs(g1: str, g2: str, key: str) -> bool:
    return bool(_ID_RE.fullmatch(g1 or "") and _ID_RE.fullmatch(g2 or "") and _KEY_RE.fullmatch(key or ""))


def new_key() -> str:
    """A fresh content key, b64u without padding (43 chars)."""
    return b64u(secrets.token_bytes(KEY_LEN))


def encrypt(key_b64u: str, plaintext: bytes, nonce: Optional[bytes] = None) -> bytes:
    """`nonce` is for the test vectors only; a copy always gets a fresh one."""
    key = b64u_decode(key_b64u)
    if len(key) != KEY_LEN:
        raise ValueError("content key must be 32 bytes")
    nonce = secrets.token_bytes(NONCE_LEN) if nonce is None else nonce
    if len(nonce) != NONCE_LEN:
        raise ValueError("nonce must be 12 bytes")
    return VERSION + nonce + AESGCM(key).encrypt(nonce, plaintext, AAD)


def decrypt(key_b64u: str, blob: bytes) -> bytes:
    """What the app does. Tests and vectors only."""
    if blob[:1] != VERSION or len(blob) < 1 + NONCE_LEN + 16:
        raise ValueError("not a version-1 copy")
    return AESGCM(b64u_decode(key_b64u)).decrypt(blob[1:1 + NONCE_LEN], blob[1 + NONCE_LEN:], AAD)


def serialize(payload: dict) -> bytes:
    """The plaintext: compact UTF-8 JSON, the same encoding as sealed answers."""
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode()


def content_sha(payload: dict) -> str:
    """Identifies what a copy says, minus what changes without anything
    happening (issued_at, the download counter). Equal sha → the service skips
    the upload."""
    stable = {k: v for k, v in payload.items() if k != "issued_at"}
    if isinstance(stable.get("headers"), dict) and _USAGE_HEADER in stable["headers"]:
        stable["headers"] = {**stable["headers"],
                             _USAGE_HEADER: _DOWNLOAD_RE.sub(r"\1download=*", stable["headers"][_USAGE_HEADER])}
    return hashlib.sha256(json.dumps(stable, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


# The app's patterns for micky-gbase (docs/micky-protocol.md); it ignores a
# base that doesn't match them, so the panel refuses to send one.
_BASE_FIELDS = {
    "project": re.compile(r"[a-z][a-z0-9-]{4,28}[a-z0-9]"),
    "key": re.compile(r"[A-Za-z0-9_-]{20,128}"),
}


def _load_base(raw: str) -> Optional[str]:
    """MICKY_GOOGLE_BASE, checked once: a JSON object with "project" and/or
    "key", each matching the app's pattern. Anything else is dropped with an
    error, so a typo never reaches the app."""
    if not raw.strip():
        return None
    try:
        value = json.loads(raw)
        if not (isinstance(value, dict) and value and set(value) <= set(_BASE_FIELDS)
                and all(isinstance(v, str) and _BASE_FIELDS[k].fullmatch(v) for k, v in value.items())):
            raise ValueError('expected {"project": "...", "key": "..."} matching the app\'s patterns')
    except ValueError as exc:
        logging.getLogger("uvicorn.error").error("MICKY_GOOGLE_BASE ignored: %s", exc)
        return None
    return json.dumps(value, separators=(",", ":"))


BASE = _load_base(MICKY_GOOGLE_BASE)
