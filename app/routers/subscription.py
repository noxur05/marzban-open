import re
import time
from distutils.version import LooseVersion
from typing import Mapping

from fastapi import APIRouter, BackgroundTasks, Depends, Header, HTTPException, Path, Request, Response
from fastapi.responses import HTMLResponse

from app import logger
from app.db import GetDB, Session, crud, get_db
from app.dependencies import get_validated_sub, validate_dates
from app.models.user import ClientType, SubscriptionUser, SubscriptionUserResponse, UserResponse, UserStatus
from app.subscription.share import encode_title, generate_subscription, seeded_randomness
from app.templates import render_template
from app.utils import cache
from app.utils import micky as micky_mod
from app.utils import micky_copy
from app.utils.device_fingerprint import is_blocked_client
from app.utils.device_fingerprint import parse as parse_device
from config import (
    MICKY_GOOGLE_COPIES,
    SUB_SUPPORT_URL,
    SUB_SUPPORT_URL_LINK_USERS,
    SUB_UPDATE_INTERVAL,
    SUBSCRIPTION_PAGE_TEMPLATE,
    USE_CUSTOM_JSON_DEFAULT,
    USE_CUSTOM_JSON_FOR_HAPP,
    USE_CUSTOM_JSON_FOR_STREISAND,
    USE_CUSTOM_JSON_FOR_NPVTUNNEL,
    USE_CUSTOM_JSON_FOR_V2RAYN,
    USE_CUSTOM_JSON_FOR_V2RAYNG,
    XRAY_SUBSCRIPTION_PATH,
)

client_config = {
    "clash-meta": {"config_format": "clash-meta", "media_type": "text/yaml", "as_base64": False, "reverse": False},
    "sing-box": {"config_format": "sing-box", "media_type": "application/json", "as_base64": False, "reverse": False},
    "clash": {"config_format": "clash", "media_type": "text/yaml", "as_base64": False, "reverse": False},
    "v2ray": {"config_format": "v2ray", "media_type": "text/plain", "as_base64": True, "reverse": False},
    "outline": {"config_format": "outline", "media_type": "application/json", "as_base64": False, "reverse": False},
    "v2ray-json": {"config_format": "v2ray-json", "media_type": "application/json", "as_base64": False,
                   "reverse": False}
}

router = APIRouter(tags=['Subscription'], prefix=f'/{XRAY_SUBSCRIPTION_PATH}')


def _record_device_bg(user_id: int, headers: Mapping[str, str], ip: str | None) -> None:
    """Background-task wrapper: parse headers and upsert one device row.

    Runs after the HTTP response is sent, with its own DB session so it
    never blocks subscription delivery and never leaks the request session.
    Any error is swallowed — device tracking must never break subscriptions.
    """
    try:
        fp = parse_device(headers)
        if fp is None:
            return
        with GetDB() as bg_db:
            crud.record_subscription_device(bg_db, user_id, fp, ip=ip)
    except Exception:
        logger.exception("device tracking failed for user_id=%s", user_id)


def _request_device_model(request: Request) -> str | None:
    """Best-effort device model for the global model-block check (Dalvik /
    generic Android clients that send no X-Hwid). Reuses the telemetry
    parser so the value matches exactly what record_subscription_device
    stored. Never raises — a parse failure just skips the model block."""
    try:
        fp = parse_device(dict(request.headers))
        return fp.device_model if fp else None
    except Exception:
        return None


def get_subscription_user_info(user: SubscriptionUser) -> dict:
    """Retrieve user subscription information including upload, download, total data, and expiry."""
    return {
        "upload": 0,
        "download": user.used_traffic,
        "total": user.data_limit if user.data_limit is not None else 0,
        "expire": user.expire if user.expire is not None else 0,
    }


DEVICE_LIMIT_ANNOUNCEMENT_TK = "Enjam çägi doly — admin bilen habarlaşyň"
HAPP_ONLY_ANNOUNCEMENT_TK = "Diňe Happ programmasy goldanýar"
DEVICE_BLOCKED_ANNOUNCEMENT_TK = "Blok Edildiniz"
# Sent when a client application is refused globally (see
# device_fingerprint.BLOCKED_CLIENT_RE).
#
# Kept SHORT because Happ truncates `profile-title` at ~25 characters, and a
# message that gets cut loses exactly the part that tells the user what to do —
# "Bu programma goldanmaýar — Happ ulanyň" would have rendered as
# "Bu programma goldanmaýar ", dropping the remedy.
#
# Do NOT assume the user reads it at all. `profile-title` is a
# subscription-client convention and the refused population is by definition
# apps implementing none of them; what they actually receive is an empty 200.
# The real signal is that it stops working.
CLIENT_BLOCKED_ANNOUNCEMENT_TK = "Diňe Happ ulanyň"
# A Micky user's subscription fetched by any other app (Happ, V2Box, a
# browser): each subscription belongs to exactly one app. Same ~25-char budget.
MICKY_ONLY_ANNOUNCEMENT_TK = "Diňe Micky programmasy goldanýar"

# Non-active subscription statuses → what the client shows instead of hosts.
# Wording mirrors the info page's status vocabulary
# (templates/subscription/index.html) so the announcement and the page the
# user opens in a browser name the state identically, under the same ~25-char
# Happ `profile-title` budget as CLIENT_BLOCKED_ANNOUNCEMENT_TK above.
#
# THESE EXACT STRINGS ARE A CROSS-REPO CONTRACT: the external-subscription
# poller matches them (stats-collector/src/poller/fetch.rs,
# REMOTE_DEAD_TITLES) to recognise that a subscription it mirrors from
# another panel of this family belongs to a dead account — change both
# together, byte for byte.
#
# `active` is deliberately absent: `.get()` returning None is the
# fall-through that serves the real config.
STATUS_ANNOUNCEMENT_TK = {
    UserStatus.limited: "Maglumat çägi gutardy",
    UserStatus.expired: "Abonement möhleti tamam",
    UserStatus.disabled: "Hasap öçürildi",
    UserStatus.on_hold: "Garaşmakda",
}

# Micky refusals are sealed codes the app translates itself (message = the
# announcement text above, shown when the app doesn't know the code).
_REFUSAL_CODES = {
    DEVICE_LIMIT_ANNOUNCEMENT_TK: "device_limit",
    DEVICE_BLOCKED_ANNOUNCEMENT_TK: "device_blocked",
    STATUS_ANNOUNCEMENT_TK[UserStatus.limited]: "limited",
    STATUS_ANNOUNCEMENT_TK[UserStatus.expired]: "expired",
    STATUS_ANNOUNCEMENT_TK[UserStatus.disabled]: "disabled",
    STATUS_ANNOUNCEMENT_TK[UserStatus.on_hold]: "on_hold",
}


def _client_refused(request: Request, user_id: int, outline_key: bool = False) -> bool:
    """Whether this request's client application is refused outright.

    Checked before every per-user gate on both subscription routes: it is a
    property of the app doing the fetching, not of the user being fetched, so a
    user's device_limit, HWID or block rows are all irrelevant to it.

    Logged rather than recorded. A refused fetch writes no
    `user_subscription_devices` row — the point is to stop accumulating rows
    nobody can ever serve — but that also removes it from every admin view, so
    without this line a newly-added blocking policy would be entirely
    unobservable and a false positive after deploy would be silent. Grep
    `refused client` to count what is being turned away.
    """
    ua = request.headers.get("user-agent")
    if not is_blocked_client(ua, outline_key):
        return False
    logger.info("refused client for user %s: %r", user_id, (ua or "")[:120])
    return True


def _announcement_response(message: str, micky=None) -> Response:
    """Empty 200 body with the message in `profile-title` so subscription
    clients (Happ) surface it as the subscription's display name.

    The EMPTY body is load-bearing, not laziness. Every major client
    early-returns on an empty subscription without touching stored servers
    (v2rayNG, NekoBox, sing-box-for-Android, Clash Meta and Hiddify all
    validate before writing), whereas v2rayNG <= 2.0.10 DELETES the group when
    the body is non-empty but unparseable. Putting the message text — or even a
    stray newline — in the body would turn a refusal into data loss for those
    installs.
    """
    # A Micky refusal must be sealed to the device and signed, or the app
    # rejects it as "not an official subscription" and never shows the text.
    if micky is not None:
        return _micky_response(
            micky, micky_mod.refusal_payload(_REFUSAL_CODES.get(message, "refused"), message))
    return Response(
        content="",
        media_type="text/plain",
        headers={"profile-title": encode_title(message)},
    )


def _micky_response(req: "micky_mod.Request", payload: dict) -> Response:
    """The payload HPKE-sealed to the requesting device and signed
    (docs/micky-protocol.md, layer 2)."""
    body, headers = micky_mod.seal(req, payload)
    return Response(content=body, media_type="application/octet-stream", headers=headers)


def _device_blocked(db, user_id: int, device_key: str, micky) -> bool:
    """Exact-identity block check. A Micky device that now sends M-Device-Id is
    also checked under its key identity, so a block placed before the app
    started sending the id still holds."""
    if cache.is_device_key_blocked(db, user_id, device_key):
        return True
    if micky is None:
        return False
    if micky.device_id and cache.is_device_key_blocked(db, user_id, micky.pub_key):
        return True
    # A blocked id: slot keeps the key it was bound to: the same phone must stay
    # blocked when its M-Device-Id goes missing or changes.
    return crud.is_bound_key_blocked(db, user_id, micky.pub_key)


def _with_new_url(headers: dict, dbuser) -> dict:
    """`new-url` for a sealed OK answer (docs/micky-protocol.md): the user's
    STORED micky://s/ link. Stable until the link is regenerated (revoke, or a
    domain change), so the app — which compares the decrypted URLs — only
    switches when the panel has actually moved the user; a URL rebuilt per
    request would carry a fresh token every second. Only ever inside the
    sealed payload, never a plain header, so only a signed answer can move a
    user. Omitted for users with no stored link (legacy users, no link key)."""
    link = getattr(dbuser, "encrypted_subscription_url", None)
    if dbuser.client_type == ClientType.micky and link:
        return {**headers, micky_mod.NEW_URL_HEADER: link}
    return headers



def _finish_micky_ok(payload: dict, db, user_id: int, client_type, issued_at: int) -> dict:
    """The last touches of a sealed OK answer (docs/micky-protocol.md): its
    render time, `issued_at`, which the app uses to order this answer's
    new-url against a Google copy's, and the Google copy headers."""
    payload = {**payload, "issued_at": issued_at}
    google = _google_copy_headers(db, user_id, client_type)
    if google:
        payload["headers"] = {**payload["headers"], **google}
    return payload


def _google_copy_headers(db, user_id: int, client_type) -> dict:
    """micky-g1 / micky-g2 / micky-gkey (+ micky-gbase), "Google copies":
    - the three ids and the key, once both copies exist and were written with
      the user's CURRENT key: after a revoke, an answer must never hand out a
      key the copies aren't encrypted with yet;
    - {"micky-g1": ""} when the user definitively has no copies (the feature
      is off, or there is no copy row): the app deletes its G profiles;
    - nothing while that is unknown or not ready yet (a lookup error, the
      copies not uploaded with the current key): the app keeps what it has.
    Only inside the sealed payload, never a plain header."""
    none = {micky_copy.G1_HEADER: ""}
    if not MICKY_GOOGLE_COPIES or client_type != ClientType.micky:
        return none
    try:
        row = crud.get_micky_copy(db, user_id)
    except Exception:
        db.rollback()
        logger.exception("Google copy lookup failed for user id %s", user_id)
        return {}
    if row is None:
        return none
    if (not row.firestore_doc_id or not row.drive_file_id
            or row.synced_at is None or row.synced_at < row.key_changed_at):
        return {}
    if not micky_copy.valid_refs(row.firestore_doc_id, row.drive_file_id, row.content_key):
        logger.error("Google copy row of user id %s holds a malformed id or key; G headers withheld", user_id)
        return {}
    headers = {micky_copy.G1_HEADER: row.firestore_doc_id, micky_copy.G2_HEADER: row.drive_file_id,
               micky_copy.KEY_HEADER: row.content_key}
    if micky_copy.BASE:
        headers[micky_copy.BASE_HEADER] = micky_copy.BASE
    return headers


def build_micky_copy(db, user_id: int) -> dict | None:
    """The plaintext of a Micky user's Google copy, or None when there must be
    none (user deleted, or not a Micky user). It is what a sealed answer to
    that user says: the same refusal for a non-active status, or the same
    headers (new-url included) and v2ray list as an OK answer, plus the
    user's blocked devices (the endpoint adds `issued_at`). Per-device answers (device
    limit, device block) have no place here: the copy is per user, and the app
    checks its own identity against `blocked`.

    Randomness (wildcard salts, SNI/host/short-id picks) is seeded per user, so
    an unchanged copy renders identically and is not uploaded again."""
    dbuser = crud.get_user_for_sub_by_id(db, user_id)
    if dbuser is None or dbuser.client_type != ClientType.micky:
        return None
    user = SubscriptionUser.model_validate(dbuser)
    if (status_message := STATUS_ANNOUNCEMENT_TK.get(dbuser.status)):
        payload = micky_mod.refusal_payload(_REFUSAL_CODES.get(status_message, "refused"), status_message)
    else:
        headers = {
            "support-url": SUB_SUPPORT_URL,
            "profile-title": encode_title(user.username),
            "profile-update-interval": SUB_UPDATE_INTERVAL,
            "subscription-userinfo": "; ".join(
                f"{key}={val}" for key, val in get_subscription_user_info(user).items()),
        }
        # From the database, not the cache: a copy is rendered right after a
        # change, and the cache is only invalidated after the commit.
        announcement = crud.get_effective_announcement_text(db, dbuser.admin_id)
        if announcement:
            headers["announce"] = encode_title(announcement)
        with seeded_randomness(f"micky-copy:{user_id}"):
            conf = generate_subscription(user=user, config_format="v2ray", as_base64=False, reverse=False)
        payload = micky_mod.ok_payload(_with_new_url(headers, user), conf)
    return {**payload, "blocked": crud.get_blocked_copy_identities(db, user_id)}


def _support_url(user, user_agent: str) -> str:
    """support-url of an OK answer. For the test users in
    SUB_SUPPORT_URL_LINK_USERS, fetching with Happ, it is their own stored
    happ:// link: does Happ's support button open it as a subscription? Only a
    stored happ:// link, never the plain URL; anything else means the usual
    SUB_SUPPORT_URL."""
    link = user.encrypted_subscription_url or ""
    if (user.username.lower() in SUB_SUPPORT_URL_LINK_USERS and user.client_type == ClientType.happ
            and link.startswith("happ://") and user_agent.startswith("Happ/")):
        return link
    return SUB_SUPPORT_URL


def _micky_gate(request: Request, user_agent: str, dbuser):
    """Who is asking, and whether this subscription may be served to them.

    Returns (micky, device_key, refusal):
      micky      None, or the Micky request every later answer is sealed to;
      device_key exact identity for the block / slot checks — "hwid:<X-Hwid>"
                 for Happ, "pk:<hash of its key>" for Micky — or None when the
                 client exposes none (Dalvik: blocked by model instead);
      refusal    a response to return immediately, or None.

    Micky is recognised by its User-Agent and proven by its device key: a
    Micky request never falls back to X-Hwid, and M- headers from any other
    app are ignored. Then "each subscription belongs to one app": a Micky user
    is served only to Micky, a Happ user never to Micky. Legacy users
    (client_type NULL) are served to both, unchanged.
    """
    if not micky_mod.is_micky(user_agent):
        if dbuser.client_type == ClientType.micky:
            return None, None, _announcement_response(MICKY_ONLY_ANNOUNCEMENT_TK)
        hwid = (request.headers.get("X-Hwid") or "").strip()
        return None, (crud.DEVICE_KEY_HWID_PREFIX + hwid if hwid else None), None

    req = micky_mod.parse_request(request.headers)
    if req is None or not micky_mod.can_sign():
        # Nothing to seal to (malformed key/nonce) or no keys on this panel:
        # an empty unsigned body, which the app reports as "not an official
        # Micky subscription".
        logger.warning(
            "refused micky request: %s (user_id=%s, ua=%r)",
            "malformed device key or nonce" if req is None else "MICKY_SIGNING_KEYS not configured",
            dbuser.id, user_agent,
        )
        return None, None, Response(content=b"", media_type="application/octet-stream",
                                    headers={"Cache-Control": "no-store, private"})
    if dbuser.client_type == ClientType.happ:
        return req, None, _announcement_response(HAPP_ONLY_ANNOUNCEMENT_TK, req)
    return req, req.device_key, None


@router.get("/{token}/")
@router.get("/{token}", include_in_schema=False)
def user_subscription(
    request: Request,
    background_tasks: BackgroundTasks,
    db: Session = Depends(get_db),
    dbuser: UserResponse = Depends(get_validated_sub),
    user_agent: str = Header(default="")
):
    """Provides a subscription link based on the user agent (Clash, V2Ray, etc.)."""
    # Before anything is read: issued_at orders this answer's new-url against
    # Google copies, so it must not be later than the data it carries.
    issued_at = int(time.time())
    # Only what the answer needs (no links rendered, no token minted); the
    # info page below builds the full UserResponse itself.
    user: SubscriptionUser = SubscriptionUser.model_validate(dbuser)

    # Refused client application. First gate in the handler, deliberately ahead
    # of the HTML branch below: that branch dispatches on nothing but an
    # `Accept` header, so leaving this after it would let a refused client skip
    # every gate — including the device block and the device limit — by adding
    # one header, and read the account's usage and expiry from the info page.
    # It also runs before device recording, so these fetches stop adding rows
    # nobody can ever serve.
    if _client_refused(request, dbuser.id):
        return _announcement_response(CLIENT_BLOCKED_ANNOUNCEMENT_TK)

    # Which app is asking and may it have this subscription. Everything
    # returned to Micky from here on — refusals included — is sealed to its
    # device and signed. See _micky_gate.
    micky, device_key, refusal = _micky_gate(request, user_agent, dbuser)
    if refusal is not None:
        return refusal

    # The info page is plain HTML: never for Micky, whose every answer is
    # sealed (and which must not skip the device checks below).
    accept_header = request.headers.get("Accept", "")
    if micky is None and "text/html" in accept_header:
        # The page shows lifetime usage, the next plan and the device count,
        # which the lean subscription load leaves out: load the full user, in
        # its own session so none of the lean object's state carries over.
        with GetDB() as page_db:
            full = crud.get_user(page_db, dbuser.username)
            if full is None:
                raise HTTPException(status_code=404, detail="Not Found")
            page_user = UserResponse.model_validate(full)
        return HTMLResponse(
            render_template(
                SUBSCRIPTION_PAGE_TEMPLATE,
                {"user": page_user}
            )
        )

    # Global device block: independent of device_limit, fires for any user
    # whose matching device row has is_blocked=TRUE. Admin clicking Block in
    # the modal expects the device cut off regardless of policy state.
    if device_key:
        # Precise identity (Happ / V2RayTun HWID, Micky key): block exactly.
        if _device_blocked(db, dbuser.id, device_key, micky):
            return _announcement_response(DEVICE_BLOCKED_ANNOUNCEMENT_TK, micky)
    else:
        # No unique id (Dalvik / generic Android): block by device model.
        # Every device reporting that model is cut off together — the only
        # identifier such clients expose.
        model = _request_device_model(request)
        if model and cache.is_model_blocked(db, dbuser.id, model):
            return _announcement_response(DEVICE_BLOCKED_ANNOUNCEMENT_TK, micky)

    # Subscription status. A non-active user gets the announcement, never
    # hosts: limited/expired/disabled users are already off every node
    # (include_db_users provisions only active + on_hold), so their config
    # would be a list of dead endpoints clients keep retrying — and an
    # on_hold user's hosts WOULD work, which is exactly what an unstarted
    # subscription must not get. On-hold activation therefore happens only
    # via on_hold_timeout or an admin; review_users' online_at path needs a
    # connection this response no longer enables.
    #
    # Ordering is load-bearing: after the HTML branch (the info page is where
    # the user reads the details of exactly this state, and it carries no
    # keys), after the device block (a banned device stays "Blok Edildiniz"
    # whatever the subscription state), and before claim_or_check_hwid so a
    # lapsed subscription cannot burn device slots — slots are freed only by
    # an admin block, never by time.
    if (status_message := STATUS_ANNOUNCEMENT_TK.get(dbuser.status)):
        return _announcement_response(status_message, micky)

    # Device-limit policy. Both NULL = legacy, no enforcement. When set,
    # only clients with an exact device identity are accepted — Happ (X-Hwid)
    # or Micky (its device key) — and per-user slots cap the count.
    if dbuser.device_limit is not None:
        if not device_key:
            return _announcement_response(HAPP_ONLY_ANNOUNCEMENT_TK, micky)
        if micky is not None:
            # Micky: slot by M-Device-Id when sent (reinstalls rebind), else by key.
            outcome = crud.claim_or_check_micky_device(
                db, dbuser.id, device_key if micky.device_id else None, micky.pub_key, dbuser.device_limit)
        else:
            outcome = crud.claim_or_check_device_key(db, dbuser.id, device_key, dbuser.device_limit)
        if outcome == "blocked":
            # Defense-in-depth: the global check above already caught
            # this case. Keeping the branch so claim_or_check_hwid's
            # contract still returns meaningful outcomes for any
            # future direct caller.
            return _announcement_response(DEVICE_BLOCKED_ANNOUNCEMENT_TK, micky)
        if outcome == "denied":
            return _announcement_response(DEVICE_LIMIT_ANNOUNCEMENT_TK, micky)
        # 'matched' or 'claimed' → fall through and serve the real config

    # update_user_sub commits, and the commit expires dbuser: the first
    # attribute read after it would re-SELECT the user row and re-run its
    # eager loads (4 queries). Take what the rest of the handler needs first,
    # and don't touch dbuser below this line (`user`, the Pydantic copy made
    # above, is unaffected).
    user_id, admin_id = dbuser.id, dbuser.admin_id
    device_limit, outline_enabled = dbuser.device_limit, dbuser.outline_enabled
    crud.update_user_sub(db, dbuser, user_agent)
    background_tasks.add_task(
        _record_device_bg,
        user_id=user_id,
        headers=dict(request.headers),
        ip=None,
    )
    response_headers = {
        "content-disposition": f'attachment; filename="{user.username}"',
        "profile-web-page-url": str(request.url),
        "support-url": _support_url(user, user_agent),
        # The account's own name, matching the content-disposition filename
        # above. One shared brand title (SUB_PROFILE_TITLE, "VIP" on prod)
        # told every customer apart by nothing; non-active users instead get
        # the status wording via _announcement_response.
        "profile-title": encode_title(user.username),
        "profile-update-interval": SUB_UPDATE_INTERVAL,
        "subscription-userinfo": "; ".join(
            f"{key}={val}"
            for key, val in get_subscription_user_info(user).items()
        )
    }
    # Tell Happ to keep sending X-Hwid for this subscription. Only
    # meaningful for users on the device-limit policy — sending it for
    # legacy users would still be harmless but adds clutter, so gate it.
    if device_limit is not None:
        response_headers["subscription-always-hwid-enable"] = "true"

    # Per-admin (or global-fallback) announcement banner — Happ `announce`
    # header (base64). Resolved + cached by the user's owning admin_id;
    # empty/None => no header sent.
    announcement = cache.get_effective_announcement(db, admin_id)
    if announcement:
        response_headers["announce"] = encode_title(announcement)

    if micky is not None:
        # Micky gets what Happ gets: the v2ray URI list — the only format
        # external and manual hosts are merged into. Plain, not base64: the
        # answer is sealed anyway and the app's engine sniffs the format. The
        # subscription headers travel inside the sealed payload
        # (micky_mod.ok_payload keeps only the ones it uses).
        conf = generate_subscription(user=user, config_format="v2ray", as_base64=False, reverse=False)
        payload = micky_mod.ok_payload(_with_new_url(response_headers, user), conf)
        return _micky_response(micky, _finish_micky_ok(payload, db, user_id, user.client_type, issued_at))

    if re.match(r'^([Cc]lash-verge|[Cc]lash[-\.]?[Mm]eta|[Ff][Ll][Cc]lash|[Mm]ihomo)', user_agent):
        conf = generate_subscription(user=user, config_format="clash-meta", as_base64=False, reverse=False)
        return Response(content=conf, media_type="text/yaml", headers=response_headers)

    elif re.match(r'^([Cc]lash|[Ss]tash)', user_agent):
        conf = generate_subscription(user=user, config_format="clash", as_base64=False, reverse=False)
        return Response(content=conf, media_type="text/yaml", headers=response_headers)

    elif re.match(r'^(SFA|SFI|SFM|SFT|[Kk]aring|[Hh]iddify[Nn]ext)|.*sing[-b]?ox.*', user_agent, re.IGNORECASE):
        conf = generate_subscription(user=user, config_format="sing-box", as_base64=False, reverse=False)
        return Response(content=conf, media_type="application/json", headers=response_headers)

    elif re.match(r'^(SS|SSR|SSD|SSS|Outline|Shadowsocks|SSconf)', user_agent):
        if not outline_enabled:
            # Outline access is opt-in per user (no X-Hwid = uncountable
            # device). Disabled users get an empty config Outline rejects.
            return Response(content="{}", media_type="application/json", headers=response_headers)
        conf = generate_subscription(user=user, config_format="outline", as_base64=False, reverse=False)
        return Response(content=conf, media_type="application/json", headers=response_headers)

    elif (USE_CUSTOM_JSON_DEFAULT or USE_CUSTOM_JSON_FOR_V2RAYN) and re.match(r'^v2rayN/(\d+\.\d+)', user_agent):
        version_str = re.match(r'^v2rayN/(\d+\.\d+)', user_agent).group(1)
        if LooseVersion(version_str) >= LooseVersion("6.40"):
            conf = generate_subscription(user=user, config_format="v2ray-json", as_base64=False, reverse=False)
            return Response(content=conf, media_type="application/json", headers=response_headers)
        else:
            conf = generate_subscription(user=user, config_format="v2ray", as_base64=True, reverse=False)
            return Response(content=conf, media_type="text/plain", headers=response_headers)

    elif (USE_CUSTOM_JSON_DEFAULT or USE_CUSTOM_JSON_FOR_V2RAYNG) and re.match(r'^v2rayNG/(\d+\.\d+\.\d+)', user_agent):
        version_str = re.match(r'^v2rayNG/(\d+\.\d+\.\d+)', user_agent).group(1)
        if LooseVersion(version_str) >= LooseVersion("1.8.29"):
            conf = generate_subscription(user=user, config_format="v2ray-json", as_base64=False, reverse=False)
            return Response(content=conf, media_type="application/json", headers=response_headers)
        elif LooseVersion(version_str) >= LooseVersion("1.8.18"):
            conf = generate_subscription(user=user, config_format="v2ray-json", as_base64=False, reverse=True)
            return Response(content=conf, media_type="application/json", headers=response_headers)
        else:
            conf = generate_subscription(user=user, config_format="v2ray", as_base64=True, reverse=False)
            return Response(content=conf, media_type="text/plain", headers=response_headers)

    elif re.match(r'^[Ss]treisand', user_agent):
        if USE_CUSTOM_JSON_DEFAULT or USE_CUSTOM_JSON_FOR_STREISAND:
            conf = generate_subscription(user=user, config_format="v2ray-json", as_base64=False, reverse=False)
            return Response(content=conf, media_type="application/json", headers=response_headers)
        else:
            conf = generate_subscription(user=user, config_format="v2ray", as_base64=True, reverse=False)
            return Response(content=conf, media_type="text/plain", headers=response_headers)

    elif (USE_CUSTOM_JSON_DEFAULT or USE_CUSTOM_JSON_FOR_HAPP) and re.match(r'^Happ/(\d+\.\d+\.\d+)', user_agent):
        version_str = re.match(r'^Happ/(\d+\.\d+\.\d+)', user_agent).group(1)
        if LooseVersion(version_str) >= LooseVersion("1.11.0"):
            conf = generate_subscription(user=user, config_format="v2ray-json", as_base64=False, reverse=False)
            return Response(content=conf, media_type="application/json", headers=response_headers)
        else:
            conf = generate_subscription(user=user, config_format="v2ray", as_base64=True, reverse=False)
            return Response(content=conf, media_type="text/plain", headers=response_headers)

    elif USE_CUSTOM_JSON_DEFAULT or USE_CUSTOM_JSON_FOR_NPVTUNNEL:
        if "ktor-client" in user_agent:
            conf = generate_subscription(user=user, config_format="v2ray-json", as_base64=False, reverse=False)
            return Response(content=conf, media_type="application/json", headers=response_headers)
        else:
            conf = generate_subscription(user=user, config_format="v2ray", as_base64=True, reverse=False)
            return Response(content=conf, media_type="text/plain", headers=response_headers)

    else:
        conf = generate_subscription(user=user, config_format="v2ray", as_base64=True, reverse=False)
        return Response(content=conf, media_type="text/plain", headers=response_headers)


# NOTE: /{token}/info and /{token}/usage are disabled. Both required only
# token validation and ran NO device-block / device-limit checks. /info
# returned SubscriptionUserResponse including `links` and `subscription_url`
# (the full working config), letting a blocked or over-limit device fetch
# credentials by hitting /info directly. Prod Caddy logs over ~2 days:
# 13,854 /sub/ fetches, 1 hit to /info (a manual test), 0 to /usage.
# Re-enable only after stripping links/subscription_url from
# SubscriptionUserResponse.
#
# @router.get("/{token}/info", response_model=SubscriptionUserResponse)
# def user_subscription_info(
#     dbuser: UserResponse = Depends(get_validated_sub),
# ):
#     """Retrieves detailed information about the user's subscription."""
#     return dbuser
#
#
# @router.get("/{token}/usage")
# def user_get_usage(
#     dbuser: UserResponse = Depends(get_validated_sub),
#     start: str = "",
#     end: str = "",
#     db: Session = Depends(get_db)
# ):
#     """Fetches the usage statistics for the user within a specified date range."""
#     start, end = validate_dates(start, end)
#
#     usages = crud.get_user_usages(db, dbuser, start, end)
#
#     return {"usages": usages, "username": dbuser.username}


@router.get("/{token}/{client_type}")
def user_subscription_with_client_type(
    request: Request,
    dbuser: UserResponse = Depends(get_validated_sub),
    client_type: str = Path(..., regex="sing-box|clash-meta|clash|outline|v2ray|v2ray-json"),
    db: Session = Depends(get_db),
    user_agent: str = Header(default="")
):
    """Provides a subscription link based on the specified client type (e.g., Clash, V2Ray)."""
    # Before anything is read: issued_at orders this answer's new-url against
    # Google copies, so it must not be later than the data it carries.
    issued_at = int(time.time())
    user: SubscriptionUser = SubscriptionUser.model_validate(dbuser)

    # Refused client application — same rule as the auto-dispatch endpoint.
    # Omitting it here would leave this route as a bypass: a refused client
    # only has to append /v2ray to the same URL. /outline lets the Outline
    # client's Go default UA through (device_fingerprint.OUTLINE_KEY_CLIENT_RE).
    if _client_refused(request, dbuser.id, outline_key=client_type == "outline"):
        return _announcement_response(CLIENT_BLOCKED_ANNOUNCEMENT_TK)

    # Which app is asking and may it have this subscription. Everything
    # returned to Micky from here on — refusals included — is sealed to its
    # device and signed. See _micky_gate.
    micky, device_key, refusal = _micky_gate(request, user_agent, dbuser)
    if refusal is not None:
        return refusal

    # Global device block — independent of device_limit, mirrors the
    # auto-dispatch handler. Must run before any policy gating so a
    # blocked device on a legacy user is still cut off.
    if device_key:
        if _device_blocked(db, dbuser.id, device_key, micky):
            return _announcement_response(DEVICE_BLOCKED_ANNOUNCEMENT_TK, micky)
    else:
        # No unique id (Dalvik / generic Android): block by device model.
        model = _request_device_model(request)
        if model and cache.is_model_blocked(db, dbuser.id, model):
            return _announcement_response(DEVICE_BLOCKED_ANNOUNCEMENT_TK, micky)

    # Subscription status — same rule and ordering rationale as the
    # auto-dispatch endpoint. Omitting it here would make /{client_type} the
    # bypass that keeps serving hosts to lapsed users.
    if (status_message := STATUS_ANNOUNCEMENT_TK.get(dbuser.status)):
        return _announcement_response(status_message, micky)

    # Device-limit policy applies here too — same rules as the auto-dispatch
    # endpoint, otherwise this is a bypass.
    if dbuser.device_limit is not None:
        if not device_key:
            return _announcement_response(HAPP_ONLY_ANNOUNCEMENT_TK, micky)
        if micky is not None:
            # Micky: slot by M-Device-Id when sent (reinstalls rebind), else by key.
            outcome = crud.claim_or_check_micky_device(
                db, dbuser.id, device_key if micky.device_id else None, micky.pub_key, dbuser.device_limit)
        else:
            outcome = crud.claim_or_check_device_key(db, dbuser.id, device_key, dbuser.device_limit)
        if outcome == "blocked":
            return _announcement_response(DEVICE_BLOCKED_ANNOUNCEMENT_TK, micky)
        if outcome == "denied":
            return _announcement_response(DEVICE_LIMIT_ANNOUNCEMENT_TK, micky)

    response_headers = {
        "content-disposition": f'attachment; filename="{user.username}"',
        "profile-web-page-url": str(request.url),
        "support-url": _support_url(user, user_agent),
        # The account's own name, matching the content-disposition filename
        # above. One shared brand title (SUB_PROFILE_TITLE, "VIP" on prod)
        # told every customer apart by nothing; non-active users instead get
        # the status wording via _announcement_response.
        "profile-title": encode_title(user.username),
        "profile-update-interval": SUB_UPDATE_INTERVAL,
        "subscription-userinfo": "; ".join(
            f"{key}={val}"
            for key, val in get_subscription_user_info(user).items()
        )
    }
    if dbuser.device_limit is not None:
        response_headers["subscription-always-hwid-enable"] = "true"

    # Per-admin (or global-fallback) announcement banner — Happ `announce`
    # header (base64). Resolved + cached by the user's owning admin_id;
    # empty/None => no header sent.
    announcement = cache.get_effective_announcement(db, dbuser.admin_id)
    if announcement:
        response_headers["announce"] = encode_title(announcement)

    if micky is not None:
        # Micky gets the plain v2ray URI list whatever format the path names
        # (same as the auto-dispatch route), and every answer to it is sealed —
        # including the cases below.
        conf = generate_subscription(user=user, config_format="v2ray", as_base64=False, reverse=False)
        payload = micky_mod.ok_payload(_with_new_url(response_headers, dbuser), conf)
        return _micky_response(micky, _finish_micky_ok(payload, db, dbuser.id, dbuser.client_type, issued_at))

    if client_type == "outline" and not dbuser.outline_enabled:
        # Outline access is opt-in per user (no X-Hwid = uncountable
        # device). Disabled users get an empty config Outline rejects.
        return Response(content="{}", media_type="application/json", headers=response_headers)

    config = client_config.get(client_type)
    conf = generate_subscription(user=user,
                                 config_format=config["config_format"],
                                 as_base64=config["as_base64"],
                                 reverse=config["reverse"])

    return Response(content=conf, media_type=config["media_type"], headers=response_headers)
