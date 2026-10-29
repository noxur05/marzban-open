<!-- Mirror of docs/micky-protocol.md in the Micky app repo (mickymusty/hiddify-app-custom), as of
     2026-10-09 on the app's branch feat/google-copies (final, not merged yet; base eb67756e).
     That copy is authoritative; change both together. The "Google copies" vectors are pinned in
     tests/micky_copy_vectors.json. -->

# Micky subscription protocol

_Status: agreed and cross-verified 2026-09-29 (app + panel); Google copies added 2026-10-09 — the one protocol for the Micky app and the panel. It replaces the shared-HMAC
scheme (`M-Micky-Ts` / `M-Micky-Sign` / `M-Micky-Nonce`, `MICKY_HMAC_SECRET`) completely; there is no
"v1/v2" and no fallback. Both sides MUST follow this document byte for byte and check the shared test
vectors below._

Two layers:

| | Layer 1 — link | Layer 2 — response |
|---|---|---|
| Goal | nobody *sees* the subscription URL (address + token) | only the requesting device can read its config; nobody can fake the panel; device limit counts real devices |
| Keys | one key pair for everyone: panel has the **public** key, the app build has the **private** key | a key pair **per device** (private key never leaves it) + the panel's **signing** key pair (app has only the public key) |
| Strength | obfuscation — the private key ships in every app | real security, even if the app is reverse-engineered |

Common notation: `b64u` = base64url **without padding**. `LP(x)` = 4-byte big-endian length of `x`, then `x`.
HPKE = RFC 9180 **base mode, single-shot**, suite `DHKEM(P-256, HKDF-SHA256)` (0x0010) / `HKDF-SHA256`
(0x0001) / `AES-128-GCM` (0x0001). P-256 public keys = SEC1 uncompressed, 65 bytes (`0x04|X|Y`); private
keys = 32-byte big-endian scalars. Receivers MUST reject public keys that are not on the curve.

---

## Layer 1 — encrypted link (implemented in the app: `lib/core/app_info/micky_link.dart`)

```
micky://s/<b64u(payload)>
payload = 0x03 | key_id (1 byte) | enc (65 bytes) | ciphertext (incl. 16-byte GCM tag)
(enc, ciphertext) = HPKE.seal(link_public_key[key_id], plaintext = URL (UTF-8),
                              info = "micky-link", aad = 0x03 | key_id)
URL = the subscription URL on the relay, e.g. https://sub.example.net/sub/<token>
```

- Panel: `MICKY_LINK_PUBLIC_KEYS = "1:<b64u 65 bytes>,…"`, `MICKY_LINK_KEY_ID = 1` (key for new links).
  **Current production public key: `1:BCx9umaDGyYhZb9OBIM4B0s2pdFCOw8GPgtv912GFAwSfP9mATTreH6DcogMEm40247UrFrKXEdY1hzL0fvPDjo`**
  (not secret). The private half is only in the app build (`micky_link_private_keys`, CI secret
  `MICKY_LINK_PRIVATE_KEYS`, local `~/.config/micky/link_private_keys`).
- A fresh ephemeral key per link (so two links for the same URL differ). Links are ~200 characters.
- The app rejects: other version byte, unknown `key_id`, failed decryption → "invalid link".
- The app stores the **sealed link** and opens it only right before each fetch.
- Revoking a user's subscription changes the token → issue a new link.

### Layer 1 test vectors (sealed with Python `pyhpke`; the app opens them in `test/core/app_info/micky_link_test.dart`)

```
private key 1 = <redacted: ships in the app build>
public  key 1 = <redacted: ships in the app build>
private key 2 = <redacted: ships in the app build>
public  key 2 = <redacted: ships in the app build>
URL           = https://sub.example.net/sub/dGVzdCwxNzU4MjgwMDAw.a1b2c3d4e5
link (key 1)  = micky://s/AwEEBuJEDGYgvy1eDcJbUFKHYITVx-ILUZooV3XXmePphI9eCi4wBImXhopTGjF2bIB6wXILG7tekJCgL1m0odzJWPv1ZCCkVzajiBXfzKnLE6BKbH1hQCF_-KbMExzCIm0jIc_-3ZBAVFQrgMh4WwbxS0G9crZF4xhtzGU3HVjYNy6-lKXGJ4R2dyp-eA
link (key 2)  = micky://s/AwIEJKySO3IR3dHaWNGhusYF3JBgtNpVQshDLxvULEWVMnFqgbHnHQcDjjb0EA-x17xaDl3yLLslzJjAeRwE08WshjIwiYxdApiC06V8v3wjn4f6y19yAvoK0U1RIhSFlmMnsIx_5bNqKvMJjLSJsg1yTBsCIOOCb4kkidjfWvwXC6J0WUWX726BxSjrEA
```
The panel's `make_link` must produce links the app opens with these keys (links differ each time
because of the random ephemeral key — test by opening, not by comparing strings).

---

## Layer 2 — per-device encrypted, signed responses

### Device key
- P-256 key pair created by the app on first launch; the private key never leaves the device.
  Storage: Android Keystore, iOS Keychain (Secure Enclave where possible), Windows DPAPI (current user),
  Linux file `0600` in the app's private directory.
- `device_pub` (65 bytes). Reinstalling the app (or clearing its data) creates a new key pair.

### Device identity (`M-Device-Id`) — survives reinstalls
- `M-Device-Id = b64u(sha256("micky-device-id" | 0x00 | os | 0x00 | lower(trim(platform_id))))` (43 chars),
  empty for a missing id or a known-bogus one (Android `9774d56d682e549c`, all zeros/dashes), where
  `os` is the `M-Device-Os` value and `platform_id` is the OS's own stable id: Android `ANDROID_ID`
  (same for this app on this phone until a factory reset), Windows `MachineGuid`, Linux
  `/etc/machine-id`, macOS `IOPlatformUUID`. The raw id never leaves the device. Absent when the
  platform has none (iOS for now).
- The panel's device key: `device_key = "id:" + M-Device-Id` when present and well-formed (32 bytes),
  else `"pk:" + b64u(sha256(device_pub))` (older builds). Used for blocking, reset and the device limit.
- A known `id:` device arriving with a **different** `device_pub` (reinstall) is **rebound**: the slot keeps
  its place, the stored `device_pub` is replaced, and no new slot is used. At most **3 rebinds per fixed
  24 h window (starting at the first rebind)** per device; beyond that → `refused`, code `device_limit` (an id copied onto several devices
  would flip the key on every refresh).
- Upgrade: a request with `M-Device-Id` whose `device_pub` matches an existing `pk:` slot turns that slot
  into the `id:` slot (no new slot).
- Like every request header it is not authenticated (a modified app can copy it); the device key still
  decides who can read the answer.

### Request — one GET of the URL from layer 1
| Header | Value |
|---|---|
| `User-Agent` | `Micky/<version> (<os>) like ClashMeta v2ray sing-box` |
| `M-Device-Pub` | `b64u(device_pub)` |
| `M-Req-Nonce` | `b64u(16 random bytes)`, new for every request |
| `M-Device-Id` | stable device id (see above); optional |
| `M-Device-Os`, `M-Ver-Os`, `M-Device-Model`, `M-Device-Locale` | descriptive only (shown to admins) |

No timestamp, no request signature: only the holder of the device private key can read the answer,
so replays gain nothing and the device clock doesn't matter. `M-Hwid` and the HMAC headers are gone.

### Response — always HTTP 200, `Content-Type: application/octet-stream`, `Cache-Control: no-store, private`
Plaintext payload (UTF-8 JSON), one of:
```json
{"status": "ok", "headers": {"profile-title": "…", "subscription-userinfo": "…", "support-url": "…", "announce": "…", "new-url": "micky://s/…"}, "body": "<v2ray URI list>", "issued_at": 1760000000}
{"status": "refused", "code": "device_limit", "message": "<announcement text>"}
```
`code` ∈ `device_limit`, `device_blocked`, `expired`, `limited`, `disabled`, `on_hold`, `refused`.

`issued_at` (since 2026-10-09, `ok` only): an integer, unix seconds, the panel's render time. The app
uses it only to order `new-url` against Google copies (see *Google copies*, app rule 5); absent = 0.

`body` (since 2026-09-30): the plain v2ray URI list, one link per line, not base64 — the same lines Happ
gets; the engine detects the format. It **may be empty** (a user with no visible hosts): the app then
keeps the servers it already has on a refresh (still saving the headers, incl. `new-url`) and refuses
to add a new subscription with "this subscription has no servers yet". Lines the engine can't parse
(e.g. `splithttp`, `h2`) are skipped; hysteria2 `mport`/`up`/`down`/`pinSHA256`/`fm` are ignored.

An unknown, revoked or deleted link is answered with a plain, unsigned HTTP 404; the app shows "this
link no longer works — ask for a new one".
All subscription headers travel **inside** the payload (the relay learns nothing about the account);
never include `profile-web-page-url`.

Encryption: `(enc, ct) = HPKE.seal(device_pub, payload, info = "micky-sub", aad = LP(device_pub) | LP(req_nonce))`;
**HTTP body = `enc (65) | ct`**.

Signature (Ed25519, panel key):
```
transcript = LP("micky-sub") | LP(key_id as ASCII) | LP(device_pub) | LP(req_nonce) | LP(sha256(http_body))
headers:     M-Micky-Key-Id: <key_id>      M-Micky-Sig: b64u(ed25519_sign(transcript))
```

### App verification order
1. Pinned signing public key for `M-Micky-Key-Id`; verify `M-Micky-Sig` over the transcript built from
   **its own** `device_pub` and `req_nonce` and the received body. Fail → "not an official Micky subscription".
2. HPKE-open with the device private key and the same `aad`. Fail → reject.
3. Parse JSON: `refused` → show the app's translation of `code` (else `message`); `ok` → use `headers` +
   `body` exactly like a normal subscription response today (empty `body`: see above).

### `new-url` — moving a subscription to a new domain
When a subscription domain is blocked, the panel moves the user to a new one. The app learns the new
address from the next `ok` answer, which it can still fetch via any working connection: another server
of the same subscription, or a standalone VPN key the user added (`vless://`, `vmess://`, `trojan://`,
`ss://`, `hysteria2://`/`hy2://`, `hysteria://`, `tuic://`; one per line).
- `new-url` is **optional; absent = keep the current link**. It is only read from `payload["headers"]` of a
  verified, sealed `{"status":"ok"}` answer; never a plain HTTP header, never on refusals.
- Value: a `micky://s/…` link (layer 1). The panel sends the user's stored link on every `ok` answer.
- The app compares **decrypted** URLs (the same URL sealed twice gives different links). Different →
  the profile stores the new link from now on and a short "address updated" note is shown (not on the
  first add). Same, malformed, or unopenable with the app's link keys → ignored.
- The old token may stay valid; a revoked token cannot learn the new address (by design).

### Panel rules
- A request is a Micky request when the User-Agent matches `^MickyX?/` (case-insensitive). A Micky request
  **must** carry a valid `M-Device-Pub` (on the curve) and a 16-byte `M-Req-Nonce`; otherwise → empty,
  unsigned 200 (the app shows "not an official Micky subscription").
- Order: refused-client → device block (`device_key`) → subscription status → device limit (rebind a known
  `id:` device with a new key; claim a free slot for an unknown `device_key`, else `device_limit`) → serve. **Every** answer to a valid Micky
  request — refusals included — is sealed and signed.
- Signing private keys live on the panel only — **never on the relay**.

### Keys and configuration
| Setting | Where | Format |
|---|---|---|
| `MICKY_LINK_PUBLIC_KEYS`, `MICKY_LINK_KEY_ID` | panel | `"<id>:<b64u 65 bytes>,…"`, int |
| `micky_link_private_keys` | app build (CI `MICKY_LINK_PRIVATE_KEYS`) | `"<id>:<b64u 32 bytes>,…"` |
| `MICKY_SIGNING_KEYS`, `MICKY_SIGNING_KEY_ID` | **panel only** | `"<id>:<b64u Ed25519 seed 32 bytes>,…"`, int |
| signing public keys | app source, pinned (`MickyProtocol.productionSigningKeys`) — public, no secret needed | `"<id>:<b64u 32 bytes>,…"` (current + next, for rotation). Production: `1:yig9Y642fRsYfPgZ-YlzRtkls-4ha6lHdDSQX7yiXh0` |

### Layer 2 test vectors
Two fixed sets, each pinned by **both** sides' tests (verified 2026-09-29, byte for byte):
- the panel's: `docs/micky-vectors.json` in the Marzban repo (`scripts/micky_keys.py --vectors`), copied to
  the app as `test/core/app_info/fixtures/micky_vectors_panel.json`;
- the app's: `test/core/app_info/fixtures/micky_vectors_app.json` (Python pyhpke reference), pinned in the
  panel as `tests/app_vectors.json`.

### Relay
Forward `User-Agent` and all `M-*` request headers and the response body/headers unchanged; no caching;
don't log request headers.

### Accepted residual risk
The VPN engine needs the plain config, so a skilled owner of a registered device can extract proxy
credentials; limited server-side (source-IP limit per user = `device_limit`).

---

## Google copies — backup subscriptions on Google (G1 Firestore, G2 Drive)

_Status: agreed 2026-10-09 (app + panel). Panel design: `analysis/google-copies-plan.md` in the Marzban repo._

When a user's own subscription domain is blocked, Micky can still update through Google hosts, which
can't be blocked without breaking much of Google. Each Micky subscription can have up to three profiles
in the app. The **user chooses** which one is active; the app never switches between them by itself.

| Profile | Fetched from |
|---|---|
| `<name>` | the subscription's own domain (layers 1 + 2 above) |
| `<name> - G1` | Firestore, `firestore.googleapis.com` |
| `<name> - G2` | Drive, `www.googleapis.com` |

### Google project (built into the app, public)
| | Value |
|---|---|
| Project | `micky-sub` |
| API key | `<GOOGLE_API_KEY>`, restricted to the Firestore and Drive APIs |

The Firestore `(default)` database (Native mode) was created in production mode with exactly these rules:
`match /copies/{id} { allow get: if true; allow list, create, update, delete: if false; }`.
Only the panel's sync service writes: a service account for Firestore, the dedicated account's OAuth
token for Drive. Drive files are shared "anyone with the link: reader".

### Headers in the sealed `ok` answer
Read only from `payload["headers"]` of a verified, sealed `{"status":"ok"}` answer, never from plain HTTP
headers. All values are JSON strings, matched by exact lowercase name and not trimmed; each pattern must
match the whole string.

| Header | Value |
|---|---|
| `micky-g1` | Firestore document id, `^[A-Za-z0-9_-]{1,128}$`, or the empty string (removal, below) |
| `micky-g2` | Drive file id, `^[A-Za-z0-9_-]{1,128}$` |
| `micky-gkey` | `^[A-Za-z0-9_-]{43}$`: b64u of the 32-byte content key |
| `micky-gbase` | optional: a string holding serialized JSON that parses to an object, e.g. `"{\"project\":\"micky-sub-2\"}"`. `project` (`^[a-z][a-z0-9-]{4,28}[a-z0-9]$`) replaces the built-in project (G1 only); `key` (`^[A-Za-z0-9_-]{20,128}$`) replaces the built-in API key (G1 and G2). Other fields are ignored; `{}` means no override |

On every verified, sealed `ok` answer the app does exactly one of:
1. `micky-g1` is the empty string → delete this subscription's G profiles. The panel sends exactly
   `"micky-g1": ""` when the user definitively has no copies (the feature is switched off, or the user has
   no copy).
2. `micky-g1`, `micky-g2` and `micky-gkey` are all present and well-formed, and `micky-gbase` is absent or
   well-formed → create the two G profiles if missing, else update the ids, key and base in place
   (`micky-gbase` absent → clear a stored override).
3. Anything else (none of them, or one missing or malformed) → change nothing. The panel leaves the
   headers out in transient states, e.g. after a revoke until the re-encrypted copies are uploaded.

A sealed refusal with code `device_blocked` or `device_limit` deletes the subscription's G profiles (they
come back with the next `ok` answer that carries the headers). Other refusals and failed fetches change
nothing.

**Panel:** sends `micky-g1`, `micky-g2` and `micky-gkey` only to Micky users, and only while both stores hold
a copy encrypted with the key being sent.

### Copy plaintext (UTF-8 JSON object)
```json
{"status": "ok", "headers": {"profile-title": "…", "subscription-userinfo": "…", "new-url": "micky://s/…"}, "body": "<v2ray URI list>", "blocked": ["id:…", "pk:…"], "issued_at": 1760000000}
{"status": "refused", "code": "expired", "message": "…", "blocked": [], "issued_at": 1760000000}
```
- `ok` copies have `headers` (an object of string values: the same set as the sealed answer, `new-url`
  included, never `micky-g*`) and `body` (a string). `refused` copies have `code` (a string) and an
  optional `message`. Unknown fields are ignored.
- `blocked`: an array of strings. For every blocked device row of the user: its `device_key` when that is
  an `id:` or `pk:` key, plus the row's bound key identity (`pk:…`, stored as is) when it has one. Other
  prefixes (`hwid:`, `ua:`) are never listed. May be empty.
- `issued_at`: a JSON integer, unix seconds, `1 700 000 000 ≤ issued_at < 2^32`, strictly increasing per
  user (the panel uses `max(now, last + 1)`). The app never compares it with its own clock.
- A copy is never refused for `device_limit`. The device limit is enforced by the own domain (see the
  refusal rule above).

### Encryption
```
blob = 0x01 | nonce (12 bytes) | AES-256-GCM(key = content key, nonce, plaintext, aad = "micky-copy")
```
- The GCM output is the ciphertext followed by the 16-byte tag.
- **The nonce is 12 fresh bytes from the OS CSPRNG for every encryption** (`os.urandom`, `secrets`,
  Rust `OsRng`): never a seeded or render RNG, a counter or the time. Fixed nonces only in vector tests.
- One content key per user, the same for G1 and G2, created once and replaced only on revoke. After a
  revoke the ids stay and the copies are re-encrypted; a phone learns the new key from the **new** link
  (the old link's token is revoked).
- A blob is at most 512 KiB; the sync service doesn't upload a larger copy and logs it.

### Where the copies live
| Copy | Written by the panel | Read by the app (no login) |
|---|---|---|
| G1 | document `copies/<micky-g1>`, one field `blob` of Firestore type Bytes: `{"fields":{"blob":{"bytesValue":"<standard base64>"}}}` | `GET https://firestore.googleapis.com/v1/projects/<project>/databases/(default)/documents/copies/<micky-g1>?key=<api key>` → JSON; the blob is `fields.blob.bytesValue` |
| G2 | Drive file, the raw blob, `mimeType` `application/octet-stream`, no conversion | `GET https://www.googleapis.com/drive/v3/files/<micky-g2>?alt=media&key=<api key>` → the raw blob |

- G1 and G2 hold the same blob.
- The app ignores `Content-Type`, decodes `bytesValue` as standard or url-safe base64 with or without
  padding, and rejects a response body over 2 MiB.
- G requests carry no `M-*` headers and a generic `User-Agent`.

### App rules for a G profile
1. **Errors.**
   - HTTP 404, or GCM authentication fails → "this link no longer works — ask for a new one". The copy
     was deleted, or a revoke rotated the key.
   - Any other non-200 status (400, 403, 429, 5xx), or a network or TLS error → the usual fetch error.
   - A G1 response that isn't JSON or has no string `fields.blob.bytesValue`, a body over 2 MiB, a first
     byte other than `0x01`, a blob shorter than 29 bytes, or a plaintext that isn't a valid copy →
     "not an official Micky subscription".
   - In every case the profile keeps its servers and nothing is deleted. A failure is never treated as
     `device_blocked`. A refusal read from a copy (including rule 3) never deletes G profiles either;
     only the own domain's `device_blocked`/`device_limit` refusals do.
2. `issued_at` strictly less than the last value accepted for this G profile → ignore the copy (no
   error, no change). Every copy that reaches rule 3 becomes the last accepted value.
3. `blocked` contains, compared exactly and case-sensitively, `"id:" + M-Device-Id` (the value the app
   sends; skipped when it sends none) or `"pk:" + b64u(sha256(device_pub))` (its current key) → handle
   it as a refusal with code `device_blocked`.
4. Otherwise handle it exactly like a verified sealed answer: `refused` → the app's text for `code`;
   `ok` → `headers` + `body`. An empty `body` keeps the servers it has.
5. **`new-url`** from an `ok` copy that passed rule 3 is applied to the **parent** subscription, and only
   if the copy's `issued_at` is greater than the parent's link stamp. The link stamp is the `issued_at` of
   the last own-domain answer or copy whose `new-url` the parent processed. An own-domain answer's
   `new-url` is always applied, as before, and raises the stamp to its `issued_at` (never lowers it). A G profile has no link of its own.
6. The app never shows, copies, shares, QR-exports, backs up or logs the ids, the key or the base (logs
   say only `G1`/`G2` and a status). The key is stored protected like the device key: Android Keystore,
   Windows DPAPI, the app's private storage elsewhere.
7. **Refresh.** A G profile refreshes in the background only while it is active, every 2 hours, plus once
   when the user switches to it. The parent keeps its normal schedule, because it is the only source of
   the G headers.
8. **A new G profile starts with a copy of the parent's servers.** It is named `<parent name> - G1` /
   `- G2` and follows the parent's renames; `profile-title` from a copy doesn't rename it.
9. **Deleting a subscription deletes its G profiles**; a G profile can't be deleted on its own. If a
   deleted G profile was active, its parent becomes active, and a running connection reconnects with
   the parent's servers.

### Panel rules
- One copy per user in each store, encrypted with the user's content key.
- **Freshness:**
  - within about a minute of a change to the user, including its devices' blocks and its link;
  - within minutes of a host change;
  - within 30 minutes for servers mirrored from other panels;
  - the usage line about every 6 hours.
- User deleted, or no longer a Micky user → both copies are deleted.
- Blocking a device never changes the ids or the key, so the user's other devices are unaffected.
- At most one upload in flight per user; a stored copy is never overwritten by one with a lower `issued_at`.

### Test vectors
`test/core/app_info/fixtures/micky_copy_vectors.json` (Python `cryptography`):
- a fixed key;
- per vector: the nonce, the exact plaintext bytes, the blob, and the same blob as a Firestore
  `bytesValue`.

The app decrypts each blob to exactly that plaintext. The panel, given the same key, nonce and
plaintext, must produce exactly that blob. Pinned in both repos.

### Accepted residual risk
- **Key extraction.** Someone who extracts the content key and the ids from a device (a rooted phone, an
  emulator, the desktop app) can read that user's copy until the next revoke. The `blocked` check and
  the `device_limit` rule run in the app, so for a modified app only a revoke cuts it off. This is the
  same tier as extracting the proxy configs (above). The server-side fix is a per-user source-IP limit.
- **Public API key.** Anyone with it can spend the free daily Firestore read quota (a denied request
  still costs a read). G1 then answers 429 until the daily reset, and G2 is the independent fallback.
  The remedy is billing with a budget cap, or a new project via `micky-gbase`.
- **Revoke during a block** cuts every device off from all three profiles, because a revoke also
  changes the proxy credentials. To recover, give the user a new link on a working domain, plus a
  standalone key if they have no working connection. While the domain is blocked, block the device
  instead of revoking.
