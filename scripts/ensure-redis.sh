#!/usr/bin/env bash
# Idempotently provision the Redis cache service on the prod host.
#
# The deploy only seds the panel image line + `marzban restart`; it does NOT
# add new services. This script (piped via `ssh ... "bash -s"` from the deploy
# workflow) ensures the redis service + REDIS_URL exist and that redis is up,
# BEFORE the panel restarts. Safe to re-run — it only appends what's missing.
#
# The panel degrades to direct DB queries if Redis is down, so a failure here
# never leaves the panel broken; the migration/health/smoke gates still guard
# the actual deploy.
set -euo pipefail

cd /opt/marzban

if ! grep -qE '^[[:space:]]*redis:[[:space:]]*$' docker-compose.yml; then
  echo "ensure-redis: adding redis service to docker-compose.yml"
  cp docker-compose.yml "docker-compose.yml.bak.redis.$(date +%Y%m%d-%H%M%S)"
  cat >> docker-compose.yml <<'YML'

  # Redis cache for the subscription hot path (announcements + device-block).
  # Ephemeral (no persistence); bound to localhost. Added by ensure-redis.sh.
  redis:
    image: redis:7-alpine
    restart: always
    command: ["redis-server", "--save", "", "--appendonly", "no", "--maxmemory", "64mb", "--maxmemory-policy", "allkeys-lru"]
    ports:
      - "127.0.0.1:6379:6379"
YML
else
  echo "ensure-redis: redis service already present"
fi

if ! grep -q '^REDIS_URL=' .env 2>/dev/null; then
  echo "ensure-redis: adding REDIS_URL to .env"
  echo 'REDIS_URL=redis://127.0.0.1:6379/0' >> .env
fi

# Validate the compose (a bad append fails here, before the panel restart) and
# bring redis up. `up -d redis` is a no-op if it's already running/current.
docker compose config >/dev/null
docker compose up -d redis
docker compose ps redis 2>/dev/null | tail -n +1 || true
echo "ensure-redis: done"
