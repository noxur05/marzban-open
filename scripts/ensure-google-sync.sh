#!/usr/bin/env bash
# Idempotently provision the Google copies sync service on prod.
#
# The deploy only seds image lines + `marzban restart`; it cannot ADD a service.
# This script (piped via `ssh ... "bash -s"` from the deploy workflow) appends
# the google-sync service if it is missing and brings it up. Safe to re-run.
#
# Usage: bash ensure-google-sync.sh <full-image-ref>
#
# Needs the Firestore service-account key at $SA_KEY on the host (root-only,
# copied there by hand; see analysis/google-copies-plan.md §10). Without it the
# service is NOT added and the deploy carries on: a deploy without Google
# secrets must still work. The Drive OAuth values, GOOGLE_SYNC_SECRET and
# FIRESTORE_PROJECT come from /opt/marzban/.env like every other setting; with
# any of them missing the service starts, logs one WARN and idles.
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
  echo "ensure-google-sync: FATAL: no image ref given" >&2
  exit 1
fi

SA_KEY=/opt/marzban/secrets/google-sa.json

cd /opt/marzban

# Checked before anything else, including bringing up an already-added service.
# The mount below also sets create_host_path: false, so Docker refuses to start
# the service rather than create a DIRECTORY where the key file belongs.
if [ ! -f "$SA_KEY" ]; then
  echo "ensure-google-sync: NOTICE: $SA_KEY not found; Google copies sync not provisioned (panel unaffected)"
  exit 0
fi

if ! grep -qE '^[[:space:]]*google-sync:[[:space:]]*$' docker-compose.yml; then
  echo "ensure-google-sync: adding google-sync service (image $IMAGE)"
  cat >> docker-compose.yml <<YML

  # Google copies sync — writes each Micky user's encrypted backup subscription
  # to Firestore (G1) and Drive (G2), fetching the copy from the panel's
  # internal endpoint. Same image as the panel, different binary. Added by
  # ensure-google-sync.sh; later deploys retag this image line along with the
  # panel's.
  google-sync:
    image: $IMAGE
    command: ["/usr/local/bin/marzban-google-sync"]
    restart: always
    env_file: .env
    network_mode: host
    environment:
      - RUST_LOG=info
      - FIRESTORE_SERVICE_ACCOUNT=/run/secrets/google-sa.json
    volumes:
      - type: bind
        source: $SA_KEY
        target: /run/secrets/google-sa.json
        read_only: true
        bind:
          create_host_path: false
YML
else
  echo "ensure-google-sync: google-sync service already present"
fi

# A malformed append must fail HERE, before the panel restart — this is the one
# failure mode worth failing the deploy over.
docker compose config >/dev/null

# Bring it up. Not fatal if the service itself misbehaves: the panel keeps
# serving subscriptions from its own domain, and failing the deploy would roll
# the whole panel back for a feature that is additive. Its status and logs are
# printed instead, so a broken sync is visible in the deploy output rather than
# silent.
if docker compose up -d google-sync; then
  sleep 3
  docker compose ps google-sync || true
  echo "ensure-google-sync: recent logs ---"
  docker compose logs --tail 15 google-sync 2>&1 || true
else
  echo "ensure-google-sync: WARNING: could not start google-sync; panel unaffected" >&2
fi

echo "ensure-google-sync: done"
