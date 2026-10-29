"""Layer 1 (links) and layer 2 (sealed, signed answers) against the shared
vectors in docs/micky-protocol.md — byte compatibility with the Micky app."""
import pytest
from cryptography.exceptions import InvalidSignature

from app.utils import micky
from tests.micky_helpers import DOC_LINKS, DOC_URL, LINK_PRIVATE, SIGNING_PUBLIC, Device

LINK_PUBLIC = {k: micky._p256_pub(v) for k, v in LINK_PRIVATE.items()}


@pytest.mark.parametrize("key_id", [1, 2])
def test_opens_the_apps_published_links(key_id):
    assert micky.open_link(DOC_LINKS[key_id], LINK_PRIVATE) == DOC_URL


@pytest.mark.parametrize("key_id", [1, 2])
def test_panel_links_open_with_the_apps_key(key_id):
    link = micky.make_link(DOC_URL, key_id=key_id, link_public_keys=LINK_PUBLIC)
    assert link.startswith("micky://s/")
    assert micky.open_link(link, LINK_PRIVATE) == DOC_URL
    assert DOC_URL not in link and "example" not in link


def test_links_differ_every_time():
    assert micky.make_link(DOC_URL, 1, LINK_PUBLIC) != micky.make_link(DOC_URL, 1, LINK_PUBLIC)


def test_link_with_wrong_key_or_version_is_rejected():
    link = micky.make_link(DOC_URL, 1, LINK_PUBLIC)
    with pytest.raises(ValueError):
        micky.open_link(link, {1: LINK_PRIVATE[2]})
    raw = bytearray(micky.b64u_decode(link[len("micky://s/"):]))
    raw[0] = 0x02
    with pytest.raises(ValueError):
        micky.open_link("micky://s/" + micky.b64u(bytes(raw)), LINK_PRIVATE)


def test_production_link_public_key_is_valid():
    micky._check_point(micky.b64u_decode(
        "BCx9umaDGyYhZb9OBIM4B0s2pdFCOw8GPgtv912GFAwSfP9mATTreH6DcogMEm40247UrFrKXEdY1hzL0fvPDjo"))


def test_layer2_vectors_verify_open_and_are_stable():
    v = micky.build_test_vectors()
    inputs = micky._vector_inputs()
    for name in ("ok", "refusal"):
        payload = micky.open_response(micky.b64u_decode(v[name]["body"]),
                                      {"M-Micky-Key-Id": "1", "M-Micky-Sig": v[name]["sig"]},
                                      inputs["device_priv"], inputs["req"], SIGNING_PUBLIC)
        assert payload == v[name]["payload"]
    assert micky.build_test_vectors() == v  # deterministic: the app must reproduce these bytes


def test_tampered_body_fails_the_signature():
    v = micky.build_test_vectors()
    inputs = micky._vector_inputs()
    body = bytearray(micky.b64u_decode(v["ok"]["body"]))
    body[-1] ^= 1
    with pytest.raises(InvalidSignature):
        micky.open_response(bytes(body), {"M-Micky-Key-Id": "1", "M-Micky-Sig": v["ok"]["sig"]},
                            inputs["device_priv"], inputs["req"], SIGNING_PUBLIC)


def test_answer_for_one_device_cannot_be_opened_by_another():
    a, b = Device(), Device()
    req = micky.Request(device_pub=a.pub, req_nonce=b"\x00" * 16)
    body, headers = micky.seal(req, micky.refusal_payload("device_limit", "x"))
    with pytest.raises(Exception):
        micky.open_response(body, headers, b.priv, micky.Request(device_pub=b.pub, req_nonce=b"\x00" * 16),
                            SIGNING_PUBLIC)


@pytest.mark.parametrize("pub,nonce", [
    ("", "AAAAAAAAAAAAAAAAAAAAAA"),                           # no key
    (micky.b64u(b"\x04" + b"\x01" * 64), "AAAAAAAAAAAAAAAAAAAAAA"),  # not on the curve
    (None, "AAAA"),                                           # nonce not 16 bytes
])
def test_malformed_requests_are_not_parsed(pub, nonce):
    if pub is None:
        pub = micky.b64u(Device().pub)
    assert micky.parse_request({"m-device-pub": pub, "m-req-nonce": nonce}) is None


def test_ok_payload_keeps_only_the_headers_the_app_uses():
    p = micky.ok_payload({"profile-title": "t", "profile-web-page-url": "https://leak", "content-disposition": "x",
                          "subscription-userinfo": "u", "announce": ""}, "{}")
    assert p["headers"] == {"profile-title": "t", "subscription-userinfo": "u"}


def test_micky_user_agent_detection():
    assert micky.is_micky("Micky/4.1.2 (android) like ClashMeta")
    assert micky.is_micky("MickyX/4.1.2 (windows)")
    assert not micky.is_micky("Happ/4.4.1/Android/123")
    assert not micky.is_micky("HiddifyNext/4.1.1 (android) like ClashMeta v2ray sing-box Micky")


def test_reproduces_the_apps_reference_vectors_byte_for_byte():
    """Vectors generated independently by the Micky app side (2026-09-29): the
    panel's seal + sign over the app's exact plaintext and ephemeral key must
    give the app's exact body and signature."""
    import json
    import os

    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    from pyhpke import KEMKeyPair

    v = json.load(open(os.path.join(os.path.dirname(__file__), "app_vectors.json")))
    d, suite = micky.b64u_decode, micky._SUITE
    req = micky.Request(device_pub=d(v["device_pub"]), req_nonce=d(v["req_nonce"]))
    for name, eph in (("ok", "eph_priv_ok"), ("refused", "eph_priv_refused")):
        eks = KEMKeyPair(suite.kem.deserialize_private_key(d(v[eph])),
                         suite.kem.deserialize_public_key(micky._p256_pub(d(v[eph]))))
        enc, ctx = suite.create_sender_context(suite.kem.deserialize_public_key(req.device_pub),
                                               info=micky.SUB_INFO, eks=eks)
        body = enc + ctx.seal(d(v[name]["plaintext_b64u"]), aad=micky._aad(req))
        assert micky.b64u(body) == v[name]["body"]
        sig = Ed25519PrivateKey.from_private_bytes(d(v["signing_seed"])).sign(micky.transcript(1, req, body))
        assert micky.b64u(sig) == v[name]["sig"]
