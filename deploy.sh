#!/usr/bin/env bash
# One-shot deploy from the server: pull, rebuild, recreate, reclaim disk.
#
#   cd /root/tgplaylistbot && ./deploy.sh
#
# Handles the recurring "removal of container ... already in progress" race by
# retrying `up -d`, and prunes build leftovers so the disk doesn't creep up.
set -uo pipefail
cd "$(dirname "$0")"

echo "▶ git pull"
git pull --ff-only || { echo "✖ git pull failed"; exit 1; }

echo "▶ docker compose build"
docker compose build || { echo "✖ build failed"; exit 1; }

echo "▶ docker compose up -d (with retry for the recreate race)"
ok=0
for i in 1 2 3 4; do
    if docker compose up -d; then ok=1; break; fi
    echo "  attempt $i hit the recreate race; retrying in 4s..."
    sleep 4
done
[ "$ok" = 1 ] || { echo "✖ up -d failed after retries"; exit 1; }

echo "▶ reclaiming disk (dangling images + build cache)"
docker image prune -f   >/dev/null 2>&1 || true
docker builder prune -f >/dev/null 2>&1 || true

echo "▶ status"
docker compose ps
echo "✓ deploy done"
