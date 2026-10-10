#!/usr/bin/env bash
# One-shot deploy from the server: pull, rebuild, recreate, reclaim disk.
#
#   cd /root/tgplaylistbot && ./deploy.sh
#
# Also run by CI (.github/workflows/deploy.yml) on every push. The image is
# built while the old bot keeps running; only `up -d` swaps containers, so
# downtime is seconds, not the whole build.
#
# Handles the recurring "removal of container ... already in progress" race by
# retrying `up -d`, and prunes build leftovers so the disk doesn't creep up.
set -uo pipefail
cd "$(dirname "$0")"

# One deploy at a time: a manual run racing the CI deploy used to hit
# "container name already in use".
exec 9>/tmp/tgplaylistbot-deploy.lock
flock -w 900 9 || { echo "✖ another deploy is still running"; exit 1; }

echo "▶ git pull"
git pull --ff-only || { echo "✖ git pull failed"; exit 1; }

echo "▶ docker compose build (old bot keeps running)"
docker compose build || { echo "✖ build failed"; exit 1; }

before=$(docker inspect -f '{{.Id}}' musicbot-platform 2>/dev/null || true)

echo "▶ docker compose up -d (with retry for the recreate race)"
ok=0
for i in 1 2 3 4; do
    if docker compose up -d; then ok=1; break; fi
    echo "  attempt $i hit the recreate race; retrying in 4s..."
    sleep 4
done
[ "$ok" = 1 ] || { echo "✖ up -d failed after retries"; exit 1; }

# A recreated bot must reach polling; otherwise fail loudly (CI turns red) and
# keep the old image around for a rollback instead of pruning it.
after=$(docker inspect -f '{{.Id}}' musicbot-platform 2>/dev/null || true)
if [ "$after" != "$before" ]; then
    echo "▶ waiting for the new bot to start polling"
    healthy=0
    for i in $(seq 1 20); do
        if docker logs musicbot-platform 2>&1 | grep -q "Run polling"; then
            healthy=1; break
        fi
        sleep 3
    done
    if [ "$healthy" != 1 ]; then
        echo "✖ bot did not start polling within 60s; last logs:"
        docker logs --tail 40 musicbot-platform 2>&1
        exit 1
    fi
    echo "✓ bot is polling"
fi

echo "▶ reclaiming disk (dangling images + build cache)"
docker image prune -f   >/dev/null 2>&1 || true
docker builder prune -f >/dev/null 2>&1 || true

echo "▶ status"
docker compose ps
echo "✓ deploy done"
