"""Micky subscription protocol — the panel side.

The byte-exact contract is docs/micky-protocol.md (mirrored from the Micky app
repo, where it is maintained). Two layers:

  * Link — `micky://s/…`: the subscription URL HPKE-sealed to a key pair whose
    PUBLIC half is here (MICKY_LINK_PUBLIC_KEYS) and whose private half ships in
    every app build. Obfuscation only: it keeps the host and token out of sight,
    nothing more. The panel can create links but never read them.
  * Response — every answer to a Micky request is HPKE-sealed to that device's
    own P-256 key (M-Device-Pub) and signed with the panel's Ed25519 key
    (MICKY_SIGNING_KEYS, which never leaves this server). This is the real
    security: a copied link, a replayed request or a captured answer is useless
    on any other device, and nothing can impersonate the panel.

The device is identified by its key: device_key = "pk:" + b64u(sha256(pub)), so
blocking, resetting and the device limit work exactly as for Happ's HWIDs.
"""

import base64
import hashlib
import json
import logging
import os
import re
import struct
from dataclasses import dataclass
from typing import Mapping, Optional

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey, Ed25519PublicKey
from pyhpke import AEADId, CipherSuite, KDFId, KEMId

from config import (
    MICKY_LINK_KEY_ID,
    MICKY_LINK_PUBLIC_KEYS,
    MICKY_SIGNING_KEY_ID,
    MICKY_SIGNING_KEYS,
)

LINK_PREFIX = "micky://s/"
LINK_VERSION = 0x03
LINK_INFO = b"micky-link"
SUB_INFO = b"micky-sub"      # HPKE info for responses
SIG_LABEL = b"micky-sub"     # first transcript field
REQ_NONCE_LEN = 16
PUB_LEN = 65                 # SEC1 uncompressed P-256 point: 0x04 | X | Y

DEVICE_PUB_HEADER = "m-device-pub"
REQ_NONCE_HEADER = "m-req-nonce"
DEVICE_ID_HEADER = "m-device-id"
DEVICE_ID_PREFIX = "id:"
PUB_KEY_PREFIX = "pk:"
# b64u(sha256(...)) without padding: exactly 43 chars, decoding to 32 bytes.
_DEVICE_ID_RE = re.compile(r"^[A-Za-z0-9_-]{42}[AEIMQUYcgkosw048]$")
KEY_ID_HEADER = "M-Micky-Key-Id"
SIG_HEADER = "M-Micky-Sig"

# Subscription headers the app uses; they travel INSIDE the sealed payload so
# the relay learns nothing about the account. profile-web-page-url is never
# sent to Micky (it would reveal the plain subscription URL).
NEW_URL_HEADER = "new-url"
PAYLOAD_HEADERS = (
    "profile-title", "subscription-userinfo", "profile-update-interval", "support-url", "announce",
    NEW_URL_HEADER,
)

# Refusal codes the app translates itself; `message` is the fallback text.
CODES = ("device_limit", "device_blocked", "expired", "limited", "disabled", "on_hold", "refused")

# DHKEM(P-256, HKDF-SHA256) / HKDF-SHA256 / AES-128-GCM — RFC 9180 Appendix A.3.
_SUITE = CipherSuite.new(KEMId.DHKEM_P256_HKDF_SHA256, KDFId.HKDF_SHA256, AEADId.AES128_GCM)

_MICKY_UA_RE = re.compile(r"^MickyX?/", re.IGNORECASE)


def b64u(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def b64u_decode(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def lp(data: bytes) -> bytes:
    """Length-prefixed field: 4-byte big-endian length, then the bytes."""
    return struct.pack(">I", len(data)) + data


def parse_keys(spec: str) -> dict[int, bytes]:
    """`"1:<b64u>,2:<b64u>"` → {1: bytes, 2: bytes}. Empty spec → {}."""
    keys: dict[int, bytes] = {}
    # Tolerate quotes: docker --env-file (scripts/rollback.sh) keeps them.
    spec = (spec or "").strip().strip('"').strip("'")
    for part in filter(None, (p.strip() for p in spec.split(","))):
        key_id, _, value = part.partition(":")
        keys[int(key_id)] = b64u_decode(value.strip())
    return keys


def _check_point(pub: bytes) -> None:
    """Raise ValueError unless `pub` is an uncompressed point on P-256. Never
    seal to an unchecked point."""
    if len(pub) != PUB_LEN:
        raise ValueError("not a 65-byte uncompressed P-256 public key")
    ec.EllipticCurvePublicKey.from_encoded_point(ec.SECP256R1(), pub)


def validate_keys(name: str, spec: str, check) -> dict[int, bytes]:
    """Parse and validate a key setting. Raises ValueError naming it."""
    try:
        keys = parse_keys(spec)
        for key_id, key in keys.items():
            if not 1 <= key_id <= 255:
                raise ValueError(f"key id {key_id} is not 1-255")
            check(key)
        return keys
    except Exception as exc:
        raise ValueError(f"{name} is malformed ({exc}); expected <id>:<base64url>,...") from exc


def _load_keys(name: str, spec: str, check) -> dict[int, bytes]:
    """A bad Micky key must never stop the panel for Happ and legacy users:
    log loudly and treat Micky as not configured (its users get the "not an
    official subscription" answer and can't be created until it's fixed)."""
    try:
        return validate_keys(name, spec, check)
    except ValueError as exc:
        logging.getLogger("uvicorn.error").error("Micky disabled: %s", exc)
        return {}


def _check_seed(seed: bytes) -> None:
    if len(seed) != 32:
        raise ValueError("an Ed25519 seed is 32 bytes")


_LINK_KEYS = _load_keys("MICKY_LINK_PUBLIC_KEYS", MICKY_LINK_PUBLIC_KEYS, _check_point)
_SIGNING_KEYS = _load_keys("MICKY_SIGNING_KEYS", MICKY_SIGNING_KEYS, _check_seed)


def is_micky(user_agent: str) -> bool:
    return bool(_MICKY_UA_RE.match(user_agent or ""))


def can_sign() -> bool:
    """Enough to ANSWER Micky requests (existing users keep working)."""
    return MICKY_SIGNING_KEY_ID in _SIGNING_KEYS


def can_make_links() -> bool:
    return MICKY_LINK_KEY_ID in _LINK_KEYS


def is_configured() -> bool:
    """Everything needed to CREATE Micky users: a link key and a signing key."""
    return can_make_links() and can_sign()


# ---------------------------------------------------------------------- link

def make_link(url: str, key_id: Optional[int] = None,
              link_public_keys: Optional[dict[int, bytes]] = None) -> str:
    """The `micky://s/…` link for a subscription URL. A fresh ephemeral key per
    call, so two links for the same URL differ."""
    keys = _LINK_KEYS if link_public_keys is None else link_public_keys
    kid = MICKY_LINK_KEY_ID if key_id is None else key_id
    if kid not in keys:
        raise ValueError(f"no Micky link public key with id {kid} (MICKY_LINK_PUBLIC_KEYS)")
    header = bytes([LINK_VERSION, kid])
    enc, ctx = _SUITE.create_sender_context(_SUITE.kem.deserialize_public_key(keys[kid]), info=LINK_INFO)
    return LINK_PREFIX + b64u(header + enc + ctx.seal(url.encode(), aad=header))


def open_link(link: str, link_private_keys: dict[int, bytes]) -> str:
    """What the app does with a link. Only tests and tooling have private keys."""
    if not link.startswith(LINK_PREFIX):
        raise ValueError("not a micky://s/ link")
    raw = b64u_decode(link[len(LINK_PREFIX):])
    if len(raw) < 2 + PUB_LEN + 16 or raw[0] != LINK_VERSION or raw[1] not in link_private_keys:
        raise ValueError("unsupported link")
    skr = _SUITE.kem.deserialize_private_key(link_private_keys[raw[1]])
    ctx = _SUITE.create_recipient_context(raw[2:2 + PUB_LEN], skr, info=LINK_INFO)
    try:
        return ctx.open(raw[2 + PUB_LEN:], aad=raw[:2]).decode()
    except Exception as exc:
        raise ValueError("invalid link") from exc


# ------------------------------------------------------------------- request

@dataclass(frozen=True)
class Request:
    device_pub: bytes
    req_nonce: bytes
    # M-Device-Id (docs/micky-protocol.md, "Device identity"): a stable
    # per-install-surviving id, or None (iOS, older builds, malformed).
    device_id: Optional[str] = None

    @property
    def pub_key(self) -> str:
        """Identity of this request's device KEY (changes on reinstall)."""
        return device_key_for(self.device_pub)

    @property
    def device_key(self) -> str:
        """The slot identity: "id:<M-Device-Id>" when present, else the key."""
        return DEVICE_ID_PREFIX + self.device_id if self.device_id else self.pub_key


def device_key_for(device_pub: bytes) -> str:
    """Key identity of a Micky device public key (46 chars)."""
    return PUB_KEY_PREFIX + b64u(hashlib.sha256(device_pub).digest())


def parse_device_id(value: str) -> Optional[str]:
    """A well-formed M-Device-Id (43 b64u chars = 32 bytes), else None. A bad
    value is ignored (the device falls back to its key), never refused."""
    value = (value or "").strip()
    return value if _DEVICE_ID_RE.match(value) else None


def _get(headers: Mapping[str, str], name: str) -> str:
    return (headers.get(name) or headers.get(name.title()) or "").strip()


def parse_request(headers: Mapping[str, str]) -> Optional[Request]:
    """The Micky request, or None when the device key or nonce is malformed."""
    try:
        pub = b64u_decode(_get(headers, DEVICE_PUB_HEADER))
        nonce = b64u_decode(_get(headers, REQ_NONCE_HEADER))
        if len(nonce) != REQ_NONCE_LEN:
            return None
        _check_point(pub)
    except (ValueError, TypeError):
        return None
    return Request(device_pub=pub, req_nonce=nonce, device_id=parse_device_id(_get(headers, DEVICE_ID_HEADER)))


# ------------------------------------------------------------------ response

def _aad(req: Request) -> bytes:
    return lp(req.device_pub) + lp(req.req_nonce)


def transcript(key_id: int, req: Request, body: bytes) -> bytes:
    return (lp(SIG_LABEL) + lp(str(key_id).encode()) + lp(req.device_pub) + lp(req.req_nonce)
            + lp(hashlib.sha256(body).digest()))


def seal(req: Request, payload: dict, key_id: Optional[int] = None,
         signing_keys: Optional[dict[int, bytes]] = None, eph_ikm: Optional[bytes] = None) -> tuple[bytes, dict]:
    """HPKE-seal `payload` to the device and sign it. Returns (body, headers).
    `eph_ikm` fixes the ephemeral key — test vectors only."""
    keys = _SIGNING_KEYS if signing_keys is None else signing_keys
    kid = MICKY_SIGNING_KEY_ID if key_id is None else key_id
    plaintext = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode()
    eks = _SUITE.kem.derive_key_pair(eph_ikm) if eph_ikm is not None else None
    enc, ctx = _SUITE.create_sender_context(_SUITE.kem.deserialize_public_key(req.device_pub),
                                            info=SUB_INFO, eks=eks)
    body = enc + ctx.seal(plaintext, aad=_aad(req))
    signature = Ed25519PrivateKey.from_private_bytes(keys[kid]).sign(transcript(kid, req, body))
    headers = {
        KEY_ID_HEADER: str(kid),
        SIG_HEADER: b64u(signature),
        "Cache-Control": "no-store, private",
    }
    return body, headers


def ok_payload(headers: Mapping[str, str], body: str) -> dict:
    lower = {k.lower(): v for k, v in headers.items()}
    return {"status": "ok", "headers": {k: lower[k] for k in PAYLOAD_HEADERS if lower.get(k)}, "body": body}


def refusal_payload(code: str, message: str) -> dict:
    return {"status": "refused", "code": code if code in CODES else "refused", "message": message}


def open_response(body: bytes, headers: Mapping[str, str], device_priv: bytes,
                  req: Request, signing_public_keys: dict[int, bytes]) -> dict:
    """What the app does (verify, then open). Tests and vectors only.
    Raises on a bad signature or a failed decryption."""
    lower = {k.lower(): v for k, v in headers.items()}  # header names are case-insensitive
    kid = int(lower[KEY_ID_HEADER.lower()])
    Ed25519PublicKey.from_public_bytes(signing_public_keys[kid]).verify(
        b64u_decode(lower[SIG_HEADER.lower()]), transcript(kid, req, body))
    ctx = _SUITE.create_recipient_context(body[:PUB_LEN], _SUITE.kem.deserialize_private_key(device_priv),
                                          info=SUB_INFO)
    return json.loads(ctx.open(body[PUB_LEN:], aad=_aad(req)))


# -------------------------------------------------------------- key material

def _p256_pub(private_scalar: bytes) -> bytes:
    priv = ec.derive_private_key(int.from_bytes(private_scalar, "big"), ec.SECP256R1())
    return priv.public_key().public_bytes(serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint)


def _ed25519_pub(seed: bytes) -> bytes:
    return Ed25519PrivateKey.from_private_bytes(seed).public_key().public_bytes(
        serialization.Encoding.Raw, serialization.PublicFormat.Raw)


def generate_keys() -> dict[str, str]:
    """Fresh production key material (scripts/micky_keys.py)."""
    link_priv = ec.generate_private_key(ec.SECP256R1()).private_numbers().private_value.to_bytes(32, "big")
    seed = os.urandom(32)
    return {
        "link_private": b64u(link_priv), "link_public": b64u(_p256_pub(link_priv)),
        "signing_seed": b64u(seed), "signing_public": b64u(_ed25519_pub(seed)),
    }


# -------------------------------------------------------------- test vectors

def _vector_inputs() -> dict:
    """Fixed inputs for the cross-repo layer 2 vectors — never use these keys for real."""
    device_priv = hashlib.sha256(b"micky test device").digest()
    return {
        "device_priv": device_priv,
        "req": Request(device_pub=_p256_pub(device_priv),
                       req_nonce=bytes.fromhex("b0b1b2b3b4b5b6b7b8b9babbbcbdbebf")),
        "signing_seed": hashlib.sha256(b"micky test signing key").digest(),
        "eph_ikm": hashlib.sha256(b"micky test ephemeral").digest(),
    }


def build_test_vectors() -> dict:
    """Deterministic layer 2 vectors: Ed25519 is deterministic and the HPKE
    ephemeral key is fixed via eph_ikm. Printed by `scripts/micky_keys.py
    --vectors`; the app's tests must reproduce `body` and `sig` exactly."""
    v = _vector_inputs()
    signing = {1: v["signing_seed"]}
    ok = ok_payload({"profile-title": "base64:dGVzdA==",
                     "subscription-userinfo": "upload=0; download=0; total=0; expire=0"},
                    '{"outbounds":[]}')
    refusal = refusal_payload("device_limit", "Enjam çägi doldy")
    out = {
        "device_priv": b64u(v["device_priv"]),
        "device_pub": b64u(v["req"].device_pub),
        "device_key": v["req"].device_key,
        "req_nonce": b64u(v["req"].req_nonce),
        "signing_key_id": 1,
        "signing_seed": b64u(v["signing_seed"]),
        "signing_public": b64u(_ed25519_pub(v["signing_seed"])),
        "eph_ikm": b64u(v["eph_ikm"]),
        # The fixed HPKE ephemeral key both answers use (DeriveKeyPair(eph_ikm)),
        # given directly so the app needn't implement DeriveKeyPair.
        "eph_priv": b64u(_SUITE.kem.derive_key_pair(v["eph_ikm"]).private_key.raw
                         .private_numbers().private_value.to_bytes(32, "big")),
        "eph_pub": b64u(_SUITE.kem.derive_key_pair(v["eph_ikm"]).public_key.to_public_bytes()),
    }
    for name, payload in (("ok", ok), ("refusal", refusal)):
        body, headers = seal(v["req"], payload, key_id=1, signing_keys=signing, eph_ikm=v["eph_ikm"])
        out[name] = {"payload": payload, "body": b64u(body), "sig": headers[SIG_HEADER]}
    return out
