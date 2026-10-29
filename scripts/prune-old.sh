#!/usr/bin/env bash
# Delete old panel images and deploy backups on prod, keeping the newest few.
#
# Every deploy pulls a custom-<sha> image and writes a full mysqldump to
# /opt/marzban/backup-deploy-<date>.sql, and nothing removed the old ones: by
# 2026-10-06 prod held 38 images, and by 2026-10-11 58 dumps (898 MB). The
# images share most layers, so deleting 35 freed ~3 GB. This script (piped via
# `ssh ... "bash -s"` from the deploy workflow, after the deploy has succeeded)
# keeps the KEEP newest images of the panel repository and the KEEP newest
# dumps, and deletes the rest. Deleted images stay in ghcr.io and can be pulled
# again; deleted dumps are gone.
#
# Never deletes, whatever their age:
#   - an image any container uses, running or stopped;
#   - the rollback target in /opt/marzban/.previous-image-tag.
# Tagged images of other repositories (mysql, redis, ...) are left alone.
# Untagged (dangling) images are removed: no tag can reach them and no
# container uses them (`docker image prune` skips used ones).
#
# The kept dumps are made owner-only: they hold the JWT secret, password
# hashes, every subscription token and the Micky copy keys.
# test-migration.sh restores the newest dump; rollback.sh uses none.
#
# Usage: bash prune-old.sh [keep=3] [--dry-run]
set -euo pipefail

REPO="ghcr.io/noxur05/marzban"
KEEP="${1:-3}"
DRY_RUN="${2:-}"
PREV_FILE="/opt/marzban/.previous-image-tag"
# Only the deploy's own names (backup-deploy-YYYYmmdd-HHMMSS.sql): a hand-made
# backup-deploy-<word>.sql would sort above them and push real dumps out.
DUMP_GLOB="/opt/marzban/backup-deploy-[0-9][0-9][0-9][0-9][0-9][0-9][0-9][0-9]-[0-9][0-9][0-9][0-9][0-9][0-9].sql"

if ! [[ "$KEEP" =~ ^[1-9][0-9]*$ ]]; then
  echo "prune-old: FATAL: keep must be a positive integer, got '$KEEP'" >&2
  exit 1
fi

df_before="$(df -h / | awk 'NR==2 {print $4}')"

# --- Images ------------------------------------------------------------------

# Image references and ids that must survive.
declare -A protect=()
while read -r ref; do
  [ -n "$ref" ] && protect["$ref"]=1
done < <(docker ps -a --format '{{.Image}}')
while read -r id; do
  [ -n "$id" ] && protect["$id"]=1
done < <(docker ps -aq | xargs -r docker inspect --format '{{.Image}}')
if [ -f "$PREV_FILE" ]; then
  prev="$(tr -d '[:space:]' < "$PREV_FILE")"
  [ -n "$prev" ] && protect["$prev"]=1
fi

# Newest first. CreatedAt ("2026-10-03 10:46:55 +0800 CST") sorts as text
# because every image on one host carries the same zone.
mapfile -t images < <(
  docker images "$REPO" --format '{{.CreatedAt}}|{{.Repository}}:{{.Tag}}|{{.ID}}' \
    | grep -v ':<none>|' | sort -r
)

kept=0
removed=0
for line in "${images[@]}"; do
  ref="$(cut -d'|' -f2 <<< "$line")"
  id="$(cut -d'|' -f3 <<< "$line")"
  if ! full_id="$(docker image inspect --format '{{.Id}}' "$id" 2> /dev/null)"; then
    echo "prune-old: WARN: could not inspect $ref, skipped" >&2
    continue
  fi
  if [ "$kept" -lt "$KEEP" ]; then
    kept=$((kept + 1))
    echo "keep    $ref (newest $kept)"
  elif [ -n "${protect[$ref]:-}" ] || [ -n "${protect[$full_id]:-}" ]; then
    echo "keep    $ref (in use or rollback target)"
  elif [ "$DRY_RUN" = "--dry-run" ]; then
    echo "would remove $ref"
    removed=$((removed + 1))
  elif docker rmi "$ref" > /dev/null; then
    echo "removed $ref"
    removed=$((removed + 1))
  else
    # docker rmi refuses an image a container still uses; never force it.
    echo "prune-old: WARN: could not remove $ref, kept" >&2
  fi
done
echo "prune-old: images kept $(( ${#images[@]} - removed )), removed $removed"

if [ "$DRY_RUN" = "--dry-run" ]; then
  echo "would remove $(docker images -f dangling=true -q | wc -l) dangling image(s)"
else
  docker image prune -f > /dev/null || echo "prune-old: WARN: dangling image prune failed" >&2
fi

# --- Deploy dumps ------------------------------------------------------------

# Newest first: the names carry the timestamp (backup-deploy-YYYYmmdd-HHMMSS).
mapfile -t dumps < <(compgen -G "$DUMP_GLOB" | LC_ALL=C sort -r || true)
dumps_removed=0
for i in "${!dumps[@]}"; do
  dump="${dumps[$i]}"
  if [ "$i" -lt "$KEEP" ]; then
    [ "$DRY_RUN" = "--dry-run" ] || chmod 600 "$dump"
    echo "keep    ${dump##*/}"
  elif [ "$DRY_RUN" = "--dry-run" ]; then
    echo "would remove ${dump##*/}"
    dumps_removed=$((dumps_removed + 1))
  else
    rm -f -- "$dump"
    echo "removed ${dump##*/}"
    dumps_removed=$((dumps_removed + 1))
  fi
done
echo "prune-old: dumps kept $(( ${#dumps[@]} - dumps_removed )), removed $dumps_removed"

# A failed mysqldump leaves its .tmp behind (the deploy renames it only on
# success). This runs only after a successful deploy, so none is in progress.
while read -r tmp; do
  if [ "$DRY_RUN" = "--dry-run" ]; then
    echo "would remove ${tmp##*/}"
  else
    rm -f -- "$tmp"
    echo "removed ${tmp##*/}"
  fi
done < <(compgen -G "/opt/marzban/backup-deploy-*.sql.tmp" || true)

echo "prune-old: free disk $df_before -> $(df -h / | awk 'NR==2 {print $4}')"
