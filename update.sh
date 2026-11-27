#!/usr/bin/env bash
# Argus NVR aktualisieren: neuestes Image von ghcr.io holen und Container neu starten.
set -euo pipefail
cd "$(dirname "$0")"

echo "==> Repo aktualisieren"
git pull --ff-only

if [ ! -f config/config.yml ]; then
  echo "!! config/config.yml fehlt – Vorlage: cp config/config.example.yml config/config.yml"
  exit 1
fi

echo "==> Image laden"
docker compose pull

echo "==> Neustart"
docker compose up -d

echo "==> Alte Images aufräumen"
docker image prune -f >/dev/null

sleep 5
docker compose ps
echo "==> Fertig. Logs: docker compose logs -f argus"
