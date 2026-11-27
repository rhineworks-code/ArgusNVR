#!/usr/bin/env bash
# CamSpire NVR aktualisieren: neuestes Image von ghcr.io holen und Container neu starten.
set -euo pipefail
cd "$(dirname "$0")"

echo "==> Repo aktualisieren"
git pull --ff-only

echo "==> Image laden"
docker compose pull

echo "==> Neustart"
docker compose up -d --remove-orphans   # entfernt auch den alten Container "argus"

echo "==> Alte Images aufräumen"
docker image prune -f >/dev/null

sleep 5
docker compose ps
echo "==> Fertig. Logs: docker compose logs -f camspire"
