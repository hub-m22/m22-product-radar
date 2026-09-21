"""AI-ступень: классификация уже найденных изменений на сайтах конкурентов.

Модель не читает сайты целиком. Ей передают только найденные технически изменения (что было / что стало,
адрес, вид страницы, конкурент), а она отвечает, значимо ли изменение, какого оно типа и как его описать
в одну-две фразы. Без ключа ANTHROPIC_API_KEY работает эвристика (radar.news.heuristic_classify).
"""
from __future__ import annotations

import json
import logging
import re

import requests

from . import config

log = logging.getLogger(__name__)
API_URL = "https://api.anthropic.com/v1/messages"
EVENT_TYPES = ["NEW_PRODUCT", "NEW_CATEGORY", "NEW_BRAND", "PRODUCT_UPDATE", "PRODUCT_REMOVED", "PRICE_CHANGE", "NEW_SOLUTION", "NEW_CASE",
               "NEW_PARTNERSHIP", "PROMOTION", "COMPANY_NEWS", "SITE_CHANGE", "OTHER"]

SYSTEM = (
    "Ты аналитик рынка экскурсионного и конференц-оборудования (радиогиды, аудиогиды, синхронный перевод, рации, вызов персонала) для компании M22. "
    "Тебе дают технически найденные изменения на сайтах конкурентов: адрес страницы, её вид, что было и что стало. "
    "Задача: для каждого изменения решить, значимо ли оно для бизнеса (новый товар, категория, бренд, решение, кейс, партнёрство, акция, новость, "
    "существенное изменение сайта) или это технический шум (счётчики, даты, cookie, перестановка блоков, мелкая правка текста). "
    "Не придумывай факты: описывай только то, что видно в переданном тексте. Если дата публикации не видна, не называй дату события. "
    "Отвечай строго JSON-массивом объектов без пояснений: "
    '[{"id": <id изменения>, "significant": true|false, "event_type": <один из типов>, "title": <заголовок до 90 символов, по-русски>, '
    '"summary": <1–2 фразы: что появилось/изменилось>, "what_changed": <чем отличается от прошлого состояния, 1 фраза>, '
    '"product_name": <название товара/модели, если есть, иначе null>, "importance": 1|2|3}]. '
    "Типы: " + ", ".join(EVENT_TYPES) + ". Значимость: 3 — новый товар/категория/бренд/решение, 2 — новость, кейс, акция, партнёрство, обновление, 1 — прочее."
)


def enabled() -> bool:
    return bool(config.ANTHROPIC_API_KEY)


def classify_changes(changes: list[dict], competitor_name: str) -> list[dict] | None:
    """changes: [{"id", "url", "kind", "before", "after", "items"}]. Возвращает список решений модели или None при ошибке/отсутствии ключа."""
    if not enabled() or not changes:
        return None
    payload = []
    for c in changes:
        payload.append({"id": c["id"], "url": c["url"], "page_kind": c["kind"], "change_kind": c["change_kind"],
                        "before": (c.get("before") or "")[:1500], "after": (c.get("after") or "")[:2500], "items": (c.get("items") or [])[:40]})
    user = f"Конкурент: {competitor_name}\nИзменения:\n{json.dumps(payload, ensure_ascii=False)}"
    body = {"model": config.AI_MODEL, "max_tokens": 4000, "system": SYSTEM, "messages": [{"role": "user", "content": user}]}
    try:
        resp = requests.post(API_URL, json=body, timeout=90,
                             headers={"x-api-key": config.ANTHROPIC_API_KEY, "anthropic-version": "2023-06-01", "content-type": "application/json"})
        if resp.status_code != 200:
            log.warning("AI %s: %s", resp.status_code, resp.text[:300])
            return None
        text = "".join(b.get("text", "") for b in resp.json().get("content", []) if b.get("type") == "text")
        m = re.search(r"\[.*\]", text, re.S)
        out = json.loads(m.group(0) if m else text)
        return [o for o in out if isinstance(o, dict) and "id" in o]
    except Exception as exc:  # noqa: BLE001
        log.warning("AI classify failed: %s", exc)
        return None
