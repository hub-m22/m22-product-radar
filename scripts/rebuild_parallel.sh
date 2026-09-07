#!/bin/sh
# Параллельный пересбор: по одному процессу на конкурента, не более 4 одновременно (разные домены -> задержки не мешают).
cd "$(dirname "$0")/.."
export PYTHONUTF8=1 PYTHONPATH=.
PY="C:/Users/Adm/AppData/Local/Programs/Python/Python312/python.exe"
IDS="$1"
echo "$IDS" | tr ',' '\n' | xargs -P 4 -I{} sh -c "\"$PY\" scripts/rebuild_competitors.py {} > data/logs/rebuild_{}.log 2>&1; echo done {}"
