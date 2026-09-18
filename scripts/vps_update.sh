#!/usr/bin/env bash
# Обновление радара на VPS: забрать новую версию из git, пересобрать и перезапустить контейнер (данные в ./data не трогаются).
set -euo pipefail
cd "$(dirname "$0")/.."
git pull --ff-only
docker compose up -d --build
sleep 8
curl -fsS http://127.0.0.1:8022/health && echo
docker image prune -f >/dev/null 2>&1 || true
