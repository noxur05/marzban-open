#!/bin/bash
# Auto-rollback: revert to whatever was running before this deploy — SCHEMA
# INCLUDED.
#
# Usage: bash rollback.sh [<image-that-was-being-deployed>]
#
# Reverting only the image is not a rollback once a migration removes anything.
# b8d4f2c7a913 is the first fork migration whose upgrade() DROPS a column
# (admins.can_use_external_hosts); every earlier one only added, and extra
# columns are invisible to an older ORM, which is why image-only rollback was
# safe until now. Old code against the new schema means SQLAlchemy still selects
# the dropped column, so `crud.get_admin` and the joinedload(User.admin) on every
# /sub/<token> fetch both die with MySQL 1054 — admin login and every
# subscription 500 while the container sits there reporting Up.
#
# So: downgrade the schema FIRST, using the NEW image (the old one cannot
# downgrade a revision it has never heard of), then restore the old image.
set -e

if [ ! -s /opt/marzban/.previous-image-tag ]; then
  echo 'FATAL: /opt/marzban/.previous-image-tag missing or empty — cannot auto-rollback'
  exit 1
fi
PREV=$(cat /opt/marzban/.previous-image-tag)
echo "[rollback] reverting to: $PREV"

# --- schema first ---------------------------------------------------------
# The deploy records the pre-migration revision before touching anything. If the
# migration moved us off it, walk the schema back with the image that knows both
# revisions — the one we were deploying.
NEW_IMAGE="${1:-}"
PREV_REV_FILE=/opt/marzban/.previous-alembic-rev
if [ -s "$PREV_REV_FILE" ] && [ -n "$NEW_IMAGE" ]; then
  PREV_REV=$(cat "$PREV_REV_FILE")
  CUR_REV=$(docker exec marzban-mysql-1 sh -c \
    'mysql -u root --password=$MYSQL_ROOT_PASSWORD $MYSQL_DATABASE -Nse "SELECT version_num FROM alembic_version"' \
    2>/dev/null | grep -v 'Using a password' | tail -1 || true)
  echo "  schema: current=$CUR_REV previous=$PREV_REV"
  if [ -n "$CUR_REV" ] && [ "$CUR_REV" != "$PREV_REV" ]; then
    echo "  downgrading schema to $PREV_REV using $NEW_IMAGE"
    # Host networking + the panel's environment so it reaches the same MySQL.
    # NOT `--env-file /opt/marzban/.env`: docker run keeps quotes and rejects
    # spaces around "=", both of which the prod .env uses (compose tolerates
    # them), so the downgrade could never run. Feed it the environment exactly
    # as compose parses it for the panel, via a root-only file that is always
    # removed.
    ENVF=$(mktemp /root/.rollback-env.XXXXXX)
    chmod 600 "$ENVF"
    trap 'rm -f "$ENVF"' EXIT
    docker compose -f /opt/marzban/docker-compose.yml config --format json | python3 -c '
import json, sys
env = json.load(sys.stdin)["services"]["marzban"]["environment"]
print("\n".join(f"{k}={v}" for k, v in env.items() if v is not None and "\n" not in str(v)))
' > "$ENVF"
    docker run --rm --network host --env-file "$ENVF" \
      -v /var/lib/marzban:/var/lib/marzban \
      "$NEW_IMAGE" alembic downgrade "$PREV_REV" \
      || { echo 'FATAL: schema downgrade failed — NOT reverting the image, the'
           echo '       new code matches the current schema. Fix forward.'; exit 3; }
    rm -f "$ENVF"
  else
    echo "  schema unchanged by this deploy; nothing to downgrade"
  fi
else
  echo "  WARNING: no .previous-alembic-rev or no image argument — skipping the"
  echo "           schema check. If this deploy ran a destructive migration the"
  echo "           reverted image will 500 on every DB read."
fi

LATEST_BAK=$(ls -t /opt/marzban/docker-compose.yml.bak.* 2>/dev/null | head -1)
if [ -n "$LATEST_BAK" ]; then
  cp "$LATEST_BAK" /opt/marzban/docker-compose.yml
  echo "  restored compose from $LATEST_BAK"
else
  sed -i "s|image: .*marzban.*|image: $PREV|" /opt/marzban/docker-compose.yml
  echo "  patched compose image line directly"
fi

docker compose -f /opt/marzban/docker-compose.yml config >/dev/null && echo '  compose valid'

marzban restart -n 2>&1 | tail -3

sleep 6
STATUS=$(docker ps --filter name=marzban-marzban-1 --format '{{.Status}}')
echo "  post-rollback status: $STATUS"
echo "$STATUS" | grep -q '^Up' || { echo 'FATAL: container not up after rollback'; exit 2; }

# "Up" is not "working": a schema/ORM mismatch leaves the process happily running
# and every DB-backed request 500ing. Replay a real subscription fetch, which is
# the highest-volume endpoint AND touches admins via joinedload(User.admin) —
# exactly the query a bad rollback breaks.
TOKEN=$(docker logs --tail 500 marzban-marzban-1 2>&1 \
  | grep -oE 'GET /sub/[A-Za-z0-9_-]+ HTTP' | tail -1 | sed 's|GET /sub/||;s| HTTP||' || true)
if [ -n "$TOKEN" ]; then
  for _ in $(seq 1 20); do
    CODE=$(curl -s -o /dev/null -w '%{http_code}' --max-time 5 "http://127.0.0.1:8000/sub/$TOKEN" || true)
    [ "$CODE" = "200" ] && break
    sleep 2
  done
  echo "  post-rollback /sub/<real_token>: $CODE"
  test "$CODE" = "200" || { echo 'FATAL: panel is up but serving errors after rollback'; exit 4; }
else
  echo '  (no recent /sub/ token in logs to verify with)'
fi
echo '[rollback] done'
