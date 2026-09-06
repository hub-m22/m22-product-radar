"""Настройка логирования: консоль + файл data/logs/radar.log."""
from __future__ import annotations

import logging
import logging.handlers
import sys

from . import config


def setup_logging(level: str | None = None) -> None:
    root = logging.getLogger()
    if getattr(root, "_radar_configured", False):
        return
    root.setLevel(level or config.LOG_LEVEL)
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s")
    if sys.stdout is not None:
        sh = logging.StreamHandler(sys.stdout)
        sh.setFormatter(fmt)
        root.addHandler(sh)
    config.LOG_DIR.mkdir(parents=True, exist_ok=True)
    fh = logging.handlers.RotatingFileHandler(config.LOG_DIR / "radar.log", maxBytes=5_000_000, backupCount=5, encoding="utf-8")
    fh.setFormatter(fmt)
    root.addHandler(fh)
    root._radar_configured = True  # type: ignore[attr-defined]
