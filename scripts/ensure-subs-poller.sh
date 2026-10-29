#!/usr/bin/env bash
# Idempotently provision the external-subscription poller service on prod.
#
# The deploy only seds image lines + `marzban restart`; it cannot ADD a service.
# This script (piped via `ssh ... "bash -s"` from the deploy workflow) appends
# the subs-poller service if it is missing and brings it up. Safe to re-run.
#
# Usage: bash ensure-subs-poller.sh <full-image-ref>
#
# DELIBERATELY TAKES NO BACKUP of docker-compose.yml, unlike ensure-redis.sh.
# rollback.sh restores `ls -t docker-compose.yml.bak.* | head -1` — the NEWEST
# backup. The deploy's own "Update compose file" step already backed the file up
# *before* rewriting the image tag, so that backup is the one a rollback must
# restore. A backup taken here would be newer AND would contain the new image
# tag, so a rollback would silently keep the new image instead of reverting it.
# The deploy's backup already predates this append, so a rollback correctly
# removes this service too.
#
# (The same hazard is latent in ensure-redis.sh; it does not fire only because
# redis is already present on prod, so its backup branch never runs.)
set -euo pipefail

IMAGE="${1:-}"
if [ -z "$IMAGE" ]; then
  echo "ensure-subs-poller: FATAL: no image ref given" >&2
  exit 1
fi

cd /opt/marzban

if ! grep -qE '^[[:space:]]*subs-poller:[[:space:]]*$' docker-compose.yml; then
  echo "ensure-subs-poller: adding subs-poller service (image $IMAGE)"
  cat >> docker-compose.yml <<YML

  # External-subscription poller — mirrors other panels' subscription hosts into
  # Redis for admins granted can_use_external_hosts. Same image as the panel,
  # different binary. Added by ensure-subs-poller.sh; later deploys retag this
  # image line along with the panel's.
  subs-poller:
    image: $IMAGE
    command: ["/usr/local/bin/marzban-subs-poller"]
    restart: always
    env_file: .env
    network_mode: host
    environment:
      - RUST_LOG=info
YML
else
  echo "ensure-subs-poller: subs-poller service already present"
fi

# A malformed append must fail HERE, before the panel restart — this is the one
# failure mode worth failing the deploy over.
docker compose config >/dev/null

# Bring it up. Not fatal if the poller itself misbehaves: the panel degrades to
# serving local hosts only, and failing the deploy would roll the whole panel
# back for a feature that is additive. Its status and logs are printed instead,
# so a broken poller is visible in the deploy output rather than silent.
if docker compose up -d subs-poller; then
  sleep 3
  docker compose ps subs-poller || true
  echo "ensure-subs-poller: recent logs ---"
  docker compose logs --tail 15 subs-poller 2>&1 || true
else
  echo "ensure-subs-poller: WARNING: could not start subs-poller; panel unaffected" >&2
fi

echo "ensure-subs-poller: done"
