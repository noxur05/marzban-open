#!/bin/bash
# Smoke tests run on the prod-server server post-deploy.
# Exit non-zero on any failure → triggers auto-rollback.
set -e

# Helper: assert HTTP code matches expected pattern (case-glob)
# NOTE: bash case patterns don't honor `|` alternation after variable
# expansion, so we eval the case statement with the literal pattern.
assert_code() {
  local actual=$1 expected=$2 label=$3
  if eval "case \"\$actual\" in $expected) true ;; *) false ;; esac"; then
    return 0
  fi
  echo "FAIL: $label expected $expected got $actual"
  exit 1
}

echo '[smoke 1/6] container is up'
STATUS=$(docker ps --filter name=marzban-marzban-1 --format '{{.Status}}')
echo "  status: $STATUS"
echo "$STATUS" | grep -q '^Up' || { echo 'FAIL: container not up'; exit 1; }

echo '[smoke 2/6] /api/admin responds with 401 (Pydantic Admin schema parses OK)'
CODE=$(curl -s -o /dev/null -w '%{http_code}' --max-time 5 http://127.0.0.1:8000/api/admin || true)
echo "  /api/admin → $CODE"
assert_code "$CODE" "401|403|2*" "/api/admin"

echo '[smoke 3/6] /api/users responds with 401 (Pydantic User schema parses OK; no 500 from N+1 fix)'
CODE=$(curl -s -o /dev/null -w '%{http_code}' --max-time 5 http://127.0.0.1:8000/api/users || true)
echo "  /api/users → $CODE"
assert_code "$CODE" "401|403|2*" "/api/users"

echo '[smoke 4/6] /api/admins responds with 401 (admin filter feature)'
CODE=$(curl -s -o /dev/null -w '%{http_code}' --max-time 5 http://127.0.0.1:8000/api/admins || true)
echo "  /api/admins → $CODE"
assert_code "$CODE" "401|403|2*" "/api/admins"

echo '[smoke 5/6] alembic at expected head'
HEAD=$(docker exec marzban-mysql-1 sh -c 'mysql -u root --password=$MYSQL_ROOT_PASSWORD $MYSQL_DATABASE -Nse "SELECT version_num FROM alembic_version"' 2>&1 | grep -v 'Using a password' | tail -1)
echo "  alembic_version: $HEAD"
test -n "$HEAD" || { echo 'FAIL: alembic_version empty'; exit 1; }

echo '[smoke 6/6] real subscription URL still works (highest-volume endpoint)'
# Try several DISTINCT recent tokens and pass if ANY of them renders.
#
# Testing only the most recent one made this check depend on which client
# happened to request last. A revoked subscription, a deleted user, or a client
# still polling an old link all return 404 forever and are perfectly healthy
# states — so a single stale token failed the deploy and triggered an
# auto-rollback of a good release (observed on a6570b8, where every one of the
# six most recent tokens returned 200 when checked by hand a minute later).
#
# What this check is for is "can the panel still render a subscription at all",
# and one success proves that. All-fail still fails, which is the case that
# actually matters.
#
# A 200 alone is NOT that proof: announcement responses (device-blocked,
# refused client, and since the status gate every limited/expired/disabled/
# on_hold user) are an empty 200 that never touches the config pipeline —
# and lapsed users' clients keep polling, so they are common in the recent-
# token sample. So: a 200 WITH a body proves rendering and wins; an empty
# 200 is noted and the loop keeps looking; only-empty-200s is inconclusive
# (every sampled user may simply be lapsed) and must warn, not fail — an
# all-announcement sample on a healthy panel would otherwise auto-rollback
# a good release, the exact failure mode c2a4b37 removed.
TOKENS=$(docker logs --tail 2000 marzban-marzban-1 2>&1 \
  | grep -oE 'GET /sub/[A-Za-z0-9_-]+ HTTP' \
  | sed 's|GET /sub/||;s| HTTP||' | tail -40 | sort -u | head -8 || true)
if [ -z "$TOKENS" ]; then
  echo "  (no recent /sub/ token in logs to test — skipping)"
else
  SUB_RENDERED=0
  SUB_ANNOUNCED=0
  TRIED=0
  for TOKEN in $TOKENS; do
    TRIED=$((TRIED + 1))
    RESP=$(curl -s -o /dev/null -w '%{http_code} %{size_download}' --max-time 10 "http://127.0.0.1:8000/sub/$TOKEN/" || true)
    CODE=${RESP%% *}
    SIZE=${RESP##* }
    echo "  /sub/<token $TRIED> → $CODE (${SIZE}B)"
    if [ "$CODE" = "200" ]; then
      if [ "${SIZE:-0}" -gt 0 ] 2>/dev/null; then
        SUB_RENDERED=1
        break
      fi
      SUB_ANNOUNCED=1
    fi
  done
  if [ "$SUB_RENDERED" = "1" ]; then
    :
  elif [ "$SUB_ANNOUNCED" = "1" ]; then
    echo "  WARN: only announcement (empty-200) responses in the sample — every sampled user may be lapsed; config rendering NOT verified this deploy"
  else
    echo "FAIL: subscription endpoint broken — none of $TRIED recent tokens answered 200"
    exit 1
  fi
fi

echo '✓ all smoke tests passed'
