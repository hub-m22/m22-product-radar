"""Конфигурация M22 Product Radar через переменные окружения (.env)."""
from __future__ import annotations

import os
from pathlib import Path

from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent.parent
load_dotenv(BASE_DIR / ".env")


def _p(value: str) -> Path:
    path = Path(value)
    return path if path.is_absolute() else BASE_DIR / path


def _f(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, default))
    except (TypeError, ValueError):
        return default


DB_PATH = _p(os.getenv("RADAR_DB_PATH", "data/radar.sqlite3"))
RAW_DIR = _p(os.getenv("RADAR_RAW_DIR", "data/raw"))
EXPORT_DIR = _p(os.getenv("RADAR_EXPORT_DIR", "data/exports"))
BACKUP_DIR = _p(os.getenv("RADAR_BACKUP_DIR", "data/backups"))
IMPORT_DIR = _p(os.getenv("RADAR_IMPORT_DIR", "data/imports"))
LOG_DIR = _p(os.getenv("RADAR_LOG_DIR", "data/logs"))

HOST = os.getenv("RADAR_HOST", "127.0.0.1")
PORT = int(os.getenv("RADAR_PORT", "8022"))

USER_AGENT = os.getenv(
    "RADAR_USER_AGENT",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) M22ProductRadar/0.1 (market research; contact: zakaz@m22.ru)",
)
REQUEST_DELAY_SEC = _f("RADAR_REQUEST_DELAY_SEC", 2.0)
REQUEST_TIMEOUT_SEC = _f("RADAR_REQUEST_TIMEOUT_SEC", 30)
MAX_RETRIES = int(os.getenv("RADAR_MAX_RETRIES", "3"))
LOG_LEVEL = os.getenv("RADAR_LOG_LEVEL", "INFO")

SCHEDULE_ENABLED = os.getenv("RADAR_SCHEDULE_ENABLED", "1") == "1"
COMPETITORS_CRON_HOUR = int(os.getenv("RADAR_COMPETITORS_CRON_HOUR", "6"))
M22_CRON_HOUR = int(os.getenv("RADAR_M22_CRON_HOUR", "5"))
TRENDS_CRON_DOW = os.getenv("RADAR_TRENDS_CRON_DOW", "mon")
REPORT_CRON_DOW = os.getenv("RADAR_REPORT_CRON_DOW", "mon")

# Пороги значимости сигналов
PRICE_CHANGE_THRESHOLD_PCT = _f("RADAR_PRICE_CHANGE_THRESHOLD_PCT", 3.0)
MARKET_GAP_THRESHOLD_PCT = _f("RADAR_MARKET_GAP_THRESHOLD_PCT", 10.0)
MIN_COMPARABLES = int(os.getenv("RADAR_MIN_COMPARABLES", "3"))
DEMAND_CHANGE_THRESHOLD_PCT = _f("RADAR_DEMAND_CHANGE_THRESHOLD_PCT", 25.0)
ANOMALY_Z = _f("RADAR_ANOMALY_Z", 2.5)

PDF_FONT_PATH = os.getenv("RADAR_PDF_FONT_PATH", "C:/Windows/Fonts/arial.ttf")

# Секреты (никогда не логируются и не показываются в интерфейсе)
YANDEX_WORDSTAT_TOKEN = os.getenv("YANDEX_WORDSTAT_TOKEN", "").strip()
ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY", "").strip()

for _d in (RAW_DIR, EXPORT_DIR, BACKUP_DIR, IMPORT_DIR, LOG_DIR, DB_PATH.parent):
    _d.mkdir(parents=True, exist_ok=True)
