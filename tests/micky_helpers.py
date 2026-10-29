"""What the Micky app does, for tests: a device with its own P-256 key that
signs nothing, sends its public key + a fresh nonce, then verifies the panel's
signature and opens the sealed answer."""
import hashlib
import os

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec

from app.utils import micky

# docs/micky-protocol.md, layer 1 test vectors (NOT production keys).
LINK_PRIVATE = {
    1: micky.b64u_decode("v32AP2DJPyohbBIJuAzIkhFtLFCHwhTaRyBdwWbIu_4"),
    2: micky.b64u_decode("IirimmYibnyYQ38wwcpFepBsGhqRYoKBsO1ad3uR_n4"),
}
DOC_URL = "https://sub.example.net/sub/dGVzdCwxNzU4MjgwMDAw.a1b2c3d4e5"
DOC_LINKS = {
    1: "micky://s/AwEEBuJEDGYgvy1eDcJbUFKHYITVx-ILUZooV3XXmePphI9eCi4wBImXhopTGjF2bIB6wXILG7tekJCgL1m0odzJWPv1ZCCkVzajiBXfzKnLE6BKbH1hQCF_-KbMExzCIm0jIc_-3ZBAVFQrgMh4WwbxS0G9crZF4xhtzGU3HVjYNy6-lKXGJ4R2dyp-eA",
    2: "micky://s/AwIEJKySO3IR3dHaWNGhusYF3JBgtNpVQshDLxvULEWVMnFqgbHnHQcDjjb0EA-x17xaDl3yLLslzJjAeRwE08WshjIwiYxdApiC06V8v3wjn4f6y19yAvoK0U1RIhSFlmMnsIx_5bNqKvMJjLSJsg1yTBsCIOOCb4kkidjfWvwXC6J0WUWX726BxSjrEA",
}
SIGNING_PUBLIC = {1: micky._ed25519_pub(hashlib.sha256(b"micky test signing key").digest())}
UA = "Micky/4.1.2 (android) like ClashMeta v2ray sing-box"


class Device:
    def __init__(self):
        key = ec.generate_private_key(ec.SECP256R1())
        self.priv = key.private_numbers().private_value.to_bytes(32, "big")
        self.pub = key.public_key().public_bytes(
            serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint)
        self.device_key = micky.device_key_for(self.pub)

    def get(self, client, path, **extra_headers):
        """Fetch like the app; returns (response, opened payload or None)."""
        nonce = os.urandom(16)
        headers = {"User-Agent": UA, "M-Device-Pub": micky.b64u(self.pub), "M-Req-Nonce": micky.b64u(nonce),
                   "M-Device-Os": "Android", "M-Device-Model": "Test Phone", **extra_headers}
        r = client.get(path, headers=headers)
        if not r.content or "m-micky-sig" not in {k.lower() for k in r.headers}:
            return r, None
        req = micky.Request(device_pub=self.pub, req_nonce=nonce)
        return r, micky.open_response(r.content, r.headers, self.priv, req, SIGNING_PUBLIC)


def is_uri_list(body: str) -> bool:
    """A v2ray subscription body: one proxy URI per non-empty line."""
    lines = [line for line in body.splitlines() if line.strip()]
    return bool(lines) and all("://" in line for line in lines)
