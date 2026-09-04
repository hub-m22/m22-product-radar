"""Вежливый HTTP-клиент: robots.txt, задержки по домену, повторы, сохранение сырых страниц."""
from __future__ import annotations

import hashlib
import logging
import threading
import time
import urllib.robotparser as robotparser
from dataclasses import dataclass
from datetime import datetime, timezone
from urllib.parse import urlparse

import requests

from . import config

log = logging.getLogger(__name__)

_lock = threading.Lock()
_last_request_at: dict[str, float] = {}
_robots_cache: dict[str, robotparser.RobotFileParser | None] = {}

BROWSER_HEADERS = {
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "ru-RU,ru;q=0.9,en;q=0.5",
}


class FetchError(Exception):
    def __init__(self, message: str, status: int | None = None, url: str | None = None):
        super().__init__(message)
        self.status = status
        self.url = url


class RobotsDisallowed(FetchError):
    pass


@dataclass
class FetchResult:
    url: str
    final_url: str
    status: int
    text: str
    fetched_at: str
    raw_path: str | None = None


def _domain(url: str) -> str:
    return urlparse(url).netloc.lower()


def _robots_for(url: str) -> robotparser.RobotFileParser | None:
    dom = _domain(url)
    if dom in _robots_cache:
        return _robots_cache[dom]
    rp = robotparser.RobotFileParser()
    robots_url = f"{urlparse(url).scheme}://{dom}/robots.txt"
    try:
        resp = requests.get(robots_url, headers={"User-Agent": config.USER_AGENT, **BROWSER_HEADERS}, timeout=15)
        if resp.status_code == 200:
            rp.parse(resp.text.splitlines())
        else:
            rp = None  # нет robots — считаем разрешённым
    except requests.RequestException:
        rp = None
    _robots_cache[dom] = rp
    return rp


def robots_allows(url: str) -> bool:
    rp = _robots_for(url)
    if rp is None:
        return True
    # Проверяем как для '*', так и для нашего UA
    return rp.can_fetch("*", url) and rp.can_fetch(config.USER_AGENT, url)


def _throttle(url: str, delay: float | None = None) -> None:
    d = config.REQUEST_DELAY_SEC if delay is None else delay
    dom = _domain(url)
    with _lock:
        last = _last_request_at.get(dom, 0.0)
        wait = last + d - time.monotonic()
        if wait > 0:
            time.sleep(wait)
        _last_request_at[dom] = time.monotonic()


def save_raw(url: str, text: str, source_key: str) -> str:
    day = datetime.now().strftime("%Y-%m-%d")
    folder = config.RAW_DIR / source_key / day
    folder.mkdir(parents=True, exist_ok=True)
    name = hashlib.sha1(url.encode("utf-8")).hexdigest()[:16] + ".html"
    path = folder / name
    path.write_text(text, encoding="utf-8", errors="replace")
    return str(path.relative_to(config.RAW_DIR.parent.parent)) if str(path).startswith(str(config.RAW_DIR.parent.parent)) else str(path)


def fetch(url: str, source_key: str = "misc", *, respect_robots: bool = True, save: bool = True,
          delay: float | None = None, timeout: float | None = None, extra_headers: dict | None = None,
          allow_status: tuple[int, ...] = (200,)) -> FetchResult:
    """GET с robots.txt, задержкой по домену, повторами при временных ошибках."""
    if respect_robots and not robots_allows(url):
        raise RobotsDisallowed(f"robots.txt запрещает: {url}", url=url)
    headers = {"User-Agent": config.USER_AGENT, **BROWSER_HEADERS, **(extra_headers or {})}
    last_exc: Exception | None = None
    for attempt in range(1, config.MAX_RETRIES + 1):
        _throttle(url, delay)
        try:
            resp = requests.get(url, headers=headers, timeout=timeout or config.REQUEST_TIMEOUT_SEC, allow_redirects=True)
        except requests.RequestException as exc:
            last_exc = exc
            log.warning("fetch error (%s/%s) %s: %s", attempt, config.MAX_RETRIES, url, exc)
            time.sleep(min(2 ** attempt, 20))
            continue
        if resp.status_code in allow_status:
            if resp.encoding is None or resp.encoding.lower() == "iso-8859-1":
                resp.encoding = resp.apparent_encoding or "utf-8"
            text = resp.text
            raw_path = save_raw(url, text, source_key) if save else None
            return FetchResult(url=url, final_url=resp.url, status=resp.status_code, text=text,
                               fetched_at=datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "") + "Z", raw_path=raw_path)
        if resp.status_code in (429, 500, 502, 503, 504):
            last_exc = FetchError(f"HTTP {resp.status_code}", status=resp.status_code, url=url)
            time.sleep(min(5 * attempt, 30))
            continue
        raise FetchError(f"HTTP {resp.status_code} для {url}", status=resp.status_code, url=url)
    raise FetchError(f"Не удалось получить {url}: {last_exc}", status=getattr(last_exc, "status", None), url=url)


def fetch_json(url: str, source_key: str = "misc", **kw) -> dict | list:
    res = fetch(url, source_key, save=False, extra_headers={"Accept": "application/json,*/*"}, **kw)
    import json

    return json.loads(res.text)
