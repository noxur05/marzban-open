"""Endpoints for the panel's own sidecars, never for the outside world.

GET /internal/micky-copy/{user_id}: a Micky user's Google copy, encrypted
(docs/micky-protocol.md "Google copies"), for the marzban-google-sync service,
which uploads it to Firestore and Drive. The panel builds and encrypts the
copy; the service only moves bytes.

The service talks to uvicorn directly on 127.0.0.1. A request that came
through a proxy (Caddy, Cloudflare: they add forwarding headers) is from
outside and gets the same 404 as an unknown path, before any other check, so
the endpoint can't be told apart from nothing. Answers the service relies on:
  200  the blob, `x-copy-sha` = what the copy says, minus volatile parts
  404 + x-copy-gone: 1   no such copy (user deleted, or not on Micky): the
                         service deletes the Google copies and the row. It
                         deletes on nothing else, not even another 404.
  503  the feature is off (MICKY_GOOGLE_COPIES) or not configured
  401  wrong secret
"""
import hashlib
import hmac

from fastapi import APIRouter, Depends, HTTPException, Request, Response

from app.db import Session, crud, get_db
from app.routers.subscription import build_micky_copy
from app.utils import micky_copy
from config import GOOGLE_SYNC_SECRET, MICKY_GOOGLE_COPIES

router = APIRouter(include_in_schema=False)

GONE_HEADER = "x-copy-gone"

_PROXY_HEADERS = ("x-forwarded-for", "x-forwarded-host", "x-forwarded-proto", "x-real-ip",
                  "forwarded", "via", "cf-connecting-ip", "cdn-loop")


_METHODS = ["GET", "HEAD", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"]


# Both spellings, so a trailing slash gets the same 404 instead of a 307 that
# would reveal the route.
@router.api_route("/internal/micky-copy/{user_id}/", methods=_METHODS)
@router.api_route("/internal/micky-copy/{user_id}", methods=_METHODS)
def micky_copy_blob(user_id: str, request: Request, db: Session = Depends(get_db)):
    # Every method and any id are accepted here and refused with a plain 404,
    # so a 405 or a validation 422 can't reveal the route.
    if (request.method != "GET" or request.url.path.endswith("/") or not (user_id.isascii() and user_id.isdigit())
            or any(h in request.headers for h in _PROXY_HEADERS)):
        raise HTTPException(status_code=404, detail="Not Found")
    if not (MICKY_GOOGLE_COPIES and GOOGLE_SYNC_SECRET):
        return Response(status_code=503)
    sent = request.headers.get("authorization", "").encode()
    if not hmac.compare_digest(sent, f"Bearer {GOOGLE_SYNC_SECRET}".encode()):
        return Response(status_code=401)
    row = crud.get_micky_copy(db, int(user_id))
    if row is not None:
        key, key_changed_at = row.content_key, row.key_changed_at
        # Before the copy's data is read (see subscription.py, issued_at).
        issued_at = crud.stamp_copy_issued_at(db, row)
    payload = build_micky_copy(db, int(user_id)) if row is not None else None
    if payload is None:
        # The ONLY 404 that lets the service delete the copies and the row: an
        # unmarked 404 (an older image without this route, a proxied request)
        # must never cost a user their copies and key.
        return Response(status_code=404, headers={GONE_HEADER: "1"})
    payload = {**payload, "issued_at": issued_at}
    blob = micky_copy.encrypt(key, micky_copy.serialize(payload))
    # The key is part of what changed: a revoke must re-upload even when the
    # plaintext is the same (e.g. a disabled user's refusal).
    sha = hashlib.sha256(f"{micky_copy.content_sha(payload)}|{key_changed_at.isoformat()}".encode()).hexdigest()
    return Response(content=blob, media_type="application/octet-stream",
                    headers={"x-copy-sha": sha, "cache-control": "no-store"})
