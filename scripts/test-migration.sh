#!/bin/bash
# Tests alembic migrations against a real DB snapshot.
# Run on prod-server (or any host with docker) BEFORE deploying.
#
# Usage: bash scripts/test-migration.sh <image_tag> [path/to/backup.sql]
#
# Spins up a temp MySQL with the backup loaded, runs the new image's
# `alembic upgrade head`, then `downgrade -1`, then `upgrade head` again.
# Exits non-zero if any step fails.
set -e

IMAGE="${1:-ghcr.io/noxur05/marzban:custom-latest}"
BACKUP="${2:-$(ls -t /opt/marzban/backup-*.sql 2>/dev/null | head -1)}"

if [ -z "$BACKUP" ] || [ ! -f "$BACKUP" ]; then
  echo "FATAL: no backup file found ($BACKUP). Run mysqldump first."
  exit 1
fi

echo "[migration-test] image:  $IMAGE"
echo "[migration-test] backup: $BACKUP"

NET="migration-test-$$"
MYSQL_NAME="migration-test-mysql-$$"
APP_NAME="migration-test-app-$$"

cleanup() {
  echo "[cleanup]"
  # -v: the mysql image keeps its data in an anonymous volume; without it every
  # run left a full copy of the restored prod backup behind (~290 MB each).
  docker rm -f -v $MYSQL_NAME $APP_NAME 2>/dev/null || true
  docker network rm $NET 2>/dev/null || true
}
trap cleanup EXIT

docker network create $NET >/dev/null

echo "[1/5] start temp mysql"
docker run -d --name $MYSQL_NAME --network $NET \
  -e MYSQL_ROOT_PASSWORD=test \
  -e MYSQL_DATABASE=marzban \
  mysql:lts --bind-address=0.0.0.0 >/dev/null

# Wait for the *real* server (post-init) to accept TCP — the mysql image
# briefly runs a socket-only init mysqld then restarts; checking via TCP
# on 127.0.0.1 ensures we're past that race.
ready=0
for i in $(seq 1 90); do
  if docker exec $MYSQL_NAME mysql -h127.0.0.1 -P3306 --protocol=TCP \
       -uroot -ptest -e 'SELECT 1' 2>/dev/null | grep -q '^1$'; then
    echo "  mysql ready after ${i}s"
    ready=1
    break
  fi
  sleep 1
done
[ $ready = 1 ] || { echo 'FATAL: temp mysql never became ready'; exit 1; }

echo "[2/5] load snapshot"
docker exec -i $MYSQL_NAME mysql -h127.0.0.1 -P3306 --protocol=TCP \
  -uroot -ptest marzban < "$BACKUP"

echo "[3/5] alembic upgrade head"
docker run --rm --name $APP_NAME --network $NET \
  -e SQLALCHEMY_DATABASE_URL="mysql+pymysql://root:test@$MYSQL_NAME:3306/marzban" \
  -e XRAY_SUBSCRIPTION_URL_PREFIX="" \
  -e XRAY_SUBSCRIPTION_PATH="sub" \
  --entrypoint sh \
  $IMAGE -c 'cd /code && alembic upgrade head'

echo "[4/5] alembic downgrade -1"
docker run --rm --network $NET \
  -e SQLALCHEMY_DATABASE_URL="mysql+pymysql://root:test@$MYSQL_NAME:3306/marzban" \
  --entrypoint sh \
  $IMAGE -c 'cd /code && alembic downgrade -1'

echo "[5/5] alembic upgrade head (again)"
docker run --rm --network $NET \
  -e SQLALCHEMY_DATABASE_URL="mysql+pymysql://root:test@$MYSQL_NAME:3306/marzban" \
  --entrypoint sh \
  $IMAGE -c 'cd /code && alembic upgrade head'

echo "✓ migration test passed"
