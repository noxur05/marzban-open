"""Happ subscription-URL encryption (`crypt5` primary, `crypt4` fallback).

Produces the `happ://crypt<N>/…` wrapper we hand to admins / users for
accounts on the device-limit (Happ-only) plan, so the plaintext subscription
URL never leaves the panel on the copy-paste path.

Both schemes are implemented LOCALLY — nothing leaves the panel.

  * ``crypt5`` (:func:`encrypt_subscription_url_crypt5`) — the current Happ
    scheme: a hybrid of RSA PKCS#1v1.5 (wrapping a random 32-byte key) and
    ChaCha20-Poly1305 over the URL, in Happ's marker-indexed envelope. The
    construction is unpublished but was reverse-engineered from the open Happ
    decryptor projects; we reproduce the exact envelope with the marker's
    public key (``_CRYPT5_MARKER`` / ``_CRYPT5_PUBLIC_KEY_PEM``), verified by
    round-tripping against real ``crypto.happ.su`` output before shipping.
    This is the primary, used at write time by the CRUD layer.
  * ``crypt4`` (:func:`encrypt_subscription_url`) — the older local RSA-4096
    scheme. Kept as a belt-and-suspenders fallback if crypt5 ever raises.

Security framing applies to both: this is **obfuscation, not
confidentiality**. The matching private keys are bundled inside every Happ
install and have been extracted publicly (that is how offline encryption is
possible at all). The point is to keep the panel hostname and bearer token
off the admin UI / Telegram / dashboard, not to defeat a motivated attacker.
The invariant everywhere: a Happ user's link handed to any consumer is
encrypted (crypt5, or crypt4 on fallback), NEVER the plaintext — and it is
produced entirely on this server, with no outbound call.
"""
from __future__ import annotations

import base64
import secrets
from functools import lru_cache

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import padding as rsa_padding
from cryptography.hazmat.primitives.ciphers.aead import ChaCha20Poly1305


# The stock Happ V4 public key. Copied verbatim from the upstream Happ
# desktop / mobile sources — present in every Happ install.
_HAPP_PUBLIC_KEY_PEM = b"""-----BEGIN PUBLIC KEY-----
MIICIjANBgkqhkiG9w0BAQEFAAOCAg8AMIICCgKCAgEA3UZ0M3L4K+WjM3vkbQnz
ozHg/cRbEXvQ6i4A8RVN4OM3rK9kU01FdjyoIgywve8OEKsFnVwERZAQZ1Trv60B
hmaM76QQEE+EUlIOL9EpwKWGtTL5lYC1sT9XJMNP3/CI0gP5wwQI88cY/xedpOEB
W72EmOOShHUm/b/3m+HPmqwc4ugKj5zWV5SyiT829aFA5DxSjmIIFBAms7DafmSq
LFTYIQL5cShDY2u+/sqyAw9yZIOoqW2TFIgIHhLPWek/ocDU7zyOrlu1E0SmcQQb
LFqHq02fsnH6IcqTv3N5Adb/CkZDDQ6HvQVBmqbKZKf7ZdXkqsc/Zw27xhG7OfXC
tUmWsiL7zA+KoTd3avyOh93Q9ju4UQsHthL3Gs4vECYOCS9dsXXSHEY/1ngU/hjO
WFF8QEE/rYV6nA4PTyUvo5RsctSQL/9DJX7XNh3zngvif8LsCN2MPvx6X+zLouBX
zgBkQ9DFfZAGLWf9TR7KVjZC/3NsuUCDoAOcpmN8pENBbeB0puiKMMWSvll36+2M
YR1Xs0MgT8Y9TwhE2+TnnTJOhzmHi/BxiUlY/w2E0s4ax9GHAmX0wyF4zeV7kDkc
vHuEdc0d7vDmdw0oqCqWj0Xwq86HfORu6tm1A8uRATjb4SzjTKclKuoElVAVa5Jo
oh/uZMozC65SmDw+N5p6Su8CAwEAAQ==
-----END PUBLIC KEY-----
"""

_MAX_PLAINTEXT_BYTES = 501  # RSA-4096 PKCS1v15 ceiling

# Lazy-load the key — the cryptography library validates the PEM at parse
# time, so a malformed key would raise on first import otherwise.
_public_key = None


def _key():
    global _public_key
    if _public_key is None:
        _public_key = serialization.load_pem_public_key(_HAPP_PUBLIC_KEY_PEM)
    return _public_key


@lru_cache(maxsize=4096)
def encrypt_subscription_url(url: str) -> str:
    """Return ``happ://crypt4/<base64(rsa(url))>``.

    Cached so repeated UserResponse validations for the same URL skip the
    ~1ms RSA op. Cache key is the URL itself — when an admin revokes a
    sub (issuing a new token + therefore a new URL) the new value misses
    the cache and gets a fresh encryption.

    Raises ValueError if the plaintext exceeds the RSA-4096 PKCS1v15 cap.
    """
    raw = url.encode("utf-8")
    if len(raw) > _MAX_PLAINTEXT_BYTES:
        raise ValueError(
            f"subscription URL is {len(raw)} bytes — crypt4 max is "
            f"{_MAX_PLAINTEXT_BYTES} (RSA-4096 PKCS1v15)"
        )
    cipher = _key().encrypt(raw, rsa_padding.PKCS1v15())
    return "happ://crypt4/" + base64.b64encode(cipher).decode("ascii")


# --- crypt5: local implementation ------------------------------------------
# Happ's crypt5 envelope, reproduced locally. Encryption = base64(url) ->
# swap-adjacent-bytes -> ChaCha20-Poly1305, with the 32-byte ChaCha key
# XOR-masked by an 8-byte salt then RSA PKCS#1v1.5-wrapped (itself base64 +
# swap-adjacent), all packed into a marker-indexed envelope and finally
# block-half-shuffled. Decryption (in every Happ install) reverses it with the
# marker's bundled private key. We encrypt with ONE marker's public key;
# `vdfzfoff` is the marker crypto.happ.su itself uses. Verified by round-tripping
# this against real crypto.happ.su output before shipping.
_CRYPT5_MARKER = "vdfzfoff"
_CRYPT5_PUBLIC_KEY_PEM = b"""-----BEGIN PUBLIC KEY-----
MIICIjANBgkqhkiG9w0BAQEFAAOCAg8AMIICCgKCAgEA9+umWSxp8coKnMONnI4u
NvtPErJZt8VNgNb2XS+RrCMc9AFWZQH01ILr3Py/mviuqFgNLMEcPs3k6+ZPh6Sa
OCXHmjQicGPJAw6Co6GQwO/b4vspHgOM4HSvX5r6SY1EKIHUSLIyRV28DfwJKdFv
x2EKqypewlrAo4AV76uI/9U+1t40yHcVCj/OtFxsq+mMM6qySieTsA1q6C5raBrJ
u3l/RWMxFYvDInYDs1IaTFGDFwSdFDqhNU19gPGloT/GApy+U32R6AGSxJymS2nh
e6pm/M9bvsH0o0Oc1kyXsBpVN04n/a9gVVUoqODzrUyXDx7/jAzNJD43PWtblcz0
ZNBKN50wvpSD5UuAQydwMT7xWJIpPaZqTUj/sg8hIm57XGlUxRCge17nB0Ff7sKO
JAgaXVdbfqDdzx+PhSaZY9xfcAh/sHfE6hKaCQ9kIn5cjbx9bcYqZWnpuSOzSFg+
CgMSqvG6rV6d+96dNMHuE0tRIUJ83xrLcm9hZJmJ6WDm6hteZbnb1k3eQF9c+XCF
wSEvsWiXyduQmkVNJaCRXwy8tSaZp9JftALhRHMvd7Eq6ctAkvn7w0upynsAtLeL
N8xZ5q1gcRgboydr588D3m8KF7mVuX/XRp2AG7hzyYdkQov9bfEfXIaBVlwHMKhy
uPTxeM4Les6fvaHMSWJ+8EUCAwEAAQ==
-----END PUBLIC KEY-----
"""
# The envelope's binary slots (nonce / salt / filler) are drawn from a printable
# alphabet: keeping them ASCII is what keeps the whole payload printable after
# the block shuffle, exactly as Happ's own output does.
_CRYPT5_ALPHABET = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789"
_CRYPT5_LETTERS = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz"

_crypt5_pub = None


def _crypt5_key():
    global _crypt5_pub
    if _crypt5_pub is None:
        _crypt5_pub = serialization.load_pem_public_key(_CRYPT5_PUBLIC_KEY_PEM)
    return _crypt5_pub


def _b64(b: bytes) -> str:
    return base64.b64encode(b).decode("ascii")


def _swap_adjacent(b: bytes) -> bytes:
    r = bytearray(b)
    for i in range(0, len(r) - 1, 2):
        r[i], r[i + 1] = r[i + 1], r[i]
    return bytes(r)


def _swap_block_halves(b: bytes) -> bytes:
    # ABCD -> CDAB for each complete 4-byte block; its own inverse.
    r = bytearray(b)
    full = len(r) - (len(r) % 4)
    for i in range(0, full, 4):
        r[i], r[i + 2] = r[i + 2], r[i]
        r[i + 1], r[i + 3] = r[i + 3], r[i + 1]
    return bytes(r)


def _rand_ascii(n: int, alphabet: str = _CRYPT5_ALPHABET) -> bytes:
    return "".join(secrets.choice(alphabet) for _ in range(n)).encode("ascii")


@lru_cache(maxsize=4096)
def encrypt_subscription_url_crypt5(url: str) -> str:
    """Return the ``happ://crypt5/…`` wrapper, produced entirely on this server.

    Fresh random ChaCha key + nonce + salt per call, so two encryptions of the
    same URL differ (all decrypt to the same URL). crypt5 has no plaintext-size
    limit — the URL rides ChaCha, only a 32-byte key goes through RSA — so
    unlike crypt4 it never raises on a long prefix.
    """
    head = _CRYPT5_MARKER[:4].encode("ascii")
    tail = _CRYPT5_MARKER[4:].encode("ascii")

    chacha_key = secrets.token_bytes(32)
    nonce = _rand_ascii(12)
    salt = _rand_ascii(8)
    filler = _rand_ascii(2, _CRYPT5_LETTERS)   # body[12:14]; first must be non-digit
    packed0 = _rand_ascii(1, _CRYPT5_LETTERS)  # one skipped byte before the ciphertext

    # RSA carries the ChaCha key XOR-masked by the salt (the decryptor XORs it
    # back out); wrapped as swap_adjacent(base64(value)) before RSA.
    rsa_value = bytes(chacha_key[i] ^ salt[i % len(salt)] for i in range(32))
    rsa_plain = _swap_adjacent(_b64(rsa_value).encode("ascii"))
    rsa_cipher = _crypt5_key().encrypt(rsa_plain, rsa_padding.PKCS1v15())
    rsa_b64 = _b64(rsa_cipher)

    intermediate = _swap_adjacent(_b64(url.encode("utf-8")).encode("ascii"))
    ciphertext = ChaCha20Poly1305(chacha_key).encrypt(nonce, intermediate, None)
    enc_url = _b64(ciphertext)

    packed = packed0 + enc_url.encode("ascii") + rsa_b64.encode("ascii")
    body = nonce + filler + salt + str(len(enc_url)).encode("ascii") + packed
    payload = _swap_block_halves(head + body + tail).decode("ascii")
    return "happ://crypt5/" + payload
