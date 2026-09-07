"""Извлечение технических характеристик со страниц товаров конкурентов.

Работает с двумя форматами: «Метка: значение» в одной строке и «Метка» / «значение» на соседних строках (Tilda, лендинги),
плюс прозаические формулировки («работает до 150 часов», «до 150 метров», «весит 40 грамм», «более 5000 каналов»).
Результат — словарь характеристик с русскими ключами; далее specs.normalize приводит их к полям матрицы.
"""
from __future__ import annotations

import html
import re
import sqlite3
import time

from . import db, http

LABELS = {
    "Дальность": re.compile(r"^(дальност\w*(?:\s+(?:передачи|приёма|приема|действия|сигнала|связи))?(?:\s+сигнала)?|радиус\s+действ\w*|расстояние\s+приёма|рабочая\s+дистанция|range)\s*[:：]?\s*(.*)$", re.I),
    "Каналов": re.compile(r"^((?:цифровых\s+|количество\s+|число\s+)?канал\w*|channels?)\s*[:：]?\s*(.*)$", re.I),
    "Диапазон частот": re.compile(r"^((?:рабочий\s+)?(?:диапазон|частот\w*)(?:\s+частот)?|frequency(?:\s+range)?)\s*[:：]?\s*(.*)$", re.I),
    "Экран": re.compile(r"^(экран(?!ированн)|дисплей|display)\b\s*[:：]?\s*(.*)$", re.I),
    "Работа от аккумулятора": re.compile(r"^(работа\s+от\s+аккумулятора|время\s+(?:автономной\s+)?работы|автономн\w+(?:\s+работ\w*)?|время\s+работы\s+от\s+(?:батареи|аккумулятора)|battery\s+life|автономность)\s*[:：]?\s*(.*)$", re.I),
    "Вес": re.compile(r"^(вес|масса|weight)\s*[:：]?\s*(.*)$", re.I),
    "Аккумулятор": re.compile(r"^(аккумулятор|батарея|ёмкость\s+аккумулятора|емкость\s+аккумулятора|battery)\s*[:：]?\s*(.*)$", re.I),
    "Время зарядки": re.compile(r"^(время\s+(?:полной\s+)?зарядки|зарядка|charging\s+time)\s*[:：]?\s*(.*)$", re.I),
    "Разъём зарядки": re.compile(r"^(разъ[её]м\s+(?:для\s+)?зарядки|разъ[её]м|порт\s+зарядки)\s*[:：]?\s*(.*)$", re.I),
    "Память": re.compile(r"^((?:встроенная\s+)?память|объ[её]м\s+памяти|memory)\s*[:：]?\s*(.*)$", re.I),
}
VALUE_RX = re.compile(r"\d|нет|есть|да\b|micro|usb|type-c|oled|lcd|ггц|мгц|ghz|mhz|uhf|vhf", re.I)
SECTION_RX = re.compile(r"^(при[её]мник|передатчик|аудиогид|зарядн\w+\s+(?:кейс|станци\w+)|микрофон|гарнитура|наушник\w*)\s*(?:[A-Za-z]{1,5}-?\d{2,4}[A-Za-z]{0,2})?\s*$", re.I)
PROSE = [
    ("Дальность", re.compile(r"(?:дальност\w*|радиус\w*|на\s+расстоянии|работает)\D{0,30}?до\s+(\d{2,4})\s*(?:м\b|метр)", re.I)),
    ("Дальность", re.compile(r"(?:в\s+радиусе|радиус\w*(?:\s+действия)?|на\s+расстояни\w+|дальност\w*(?:\s+\w+){0,2})\s*(?:до|—|-|:)?\s*(?:\d{1,3}\s*[-–]\s*)?(\d{2,4})\s*(?:м\b|метр)", re.I)),
    ("Дальность", re.compile(r"до\s+(\d{2,4})\s*(?:м\b|метров|метра)(?!\s*\w*(?:ин|ат))", re.I)),
    ("Работа от аккумулятора", re.compile(r"(?:без\s+подзарядки|без\s+перерыва|работа\w*|автономн\w*|держит\s+зарядку|без\s+розетки)\D{0,30}?до\s+(\d{1,3})\s*(?:ч\b|час)", re.I)),
    ("Работа от аккумулятора", re.compile(r"(\d{1,3})\s*(?:ч\b|часов|часа)\s+(?:без\s+подзарядки|автономн|работы)", re.I)),
    ("Вес", re.compile(r"(?:вес\w*|весит|масс\w*|всего)\s*(\d{2,3})\s*(?:г\b|гр\b|грамм)", re.I)),
    ("Каналов", re.compile(r"(?:более|до|свыше)?\s*(\d{2,5})\s*(?:цифровых\s+)?канал", re.I)),
    ("Каналов", re.compile(r"канал\w*\s*(?:более|до|свыше|—|-|:)?\s*(\d{2,5})\b", re.I)),
    ("Диапазон частот", re.compile(r"(\d(?:[.,]\d+)?\s*(?:—|-|–)\s*\d(?:[.,]\d+)?\s*ГГц|\d{3,4}\s*(?:—|-|–)\s*\d{3,4}\s*МГц|2[.,]4\s*ГГц|UHF|VHF|\bFM\b)", re.I)),
]


def _lines(page_html: str) -> list[str]:
    t = re.sub(r"<script.*?</script>|<style.*?</style>|<noscript.*?</noscript>", " ", page_html, flags=re.S)
    t = re.sub(r"<br\s*/?>|</(?:p|div|li|h[1-6]|td|th|tr|dt|dd|span)>", "\n", t, flags=re.I)
    txt = html.unescape(re.sub(r"<[^>]+>", " ", t))
    out = []
    for raw in txt.split("\n"):
        line = re.sub(r"\s+", " ", raw).strip(" \t:：·•-–—")
        if 1 < len(line) < 200:
            out.append(line)
    return out


def extract_specs(page_html: str) -> dict[str, str]:
    """Характеристики со страницы: {метка: значение}; для компонентов комплекта ключ с префиксом («Приёмник · Вес»)."""
    lines = _lines(page_html)
    specs: dict[str, str] = {}
    section = ""
    for i, line in enumerate(lines):
        m_sec = SECTION_RX.match(line)
        if m_sec and len(line) <= 40 and not VALUE_RX.search(line[len(m_sec.group(1)):]):
            section = m_sec.group(1).capitalize().rstrip(":")
        for key, rx in LABELS.items():
            m = rx.match(line)
            if not m:
                continue
            value = m.group(2).strip()
            if not value or not VALUE_RX.search(value):
                # значение на следующей строке
                nxt = lines[i + 1] if i + 1 < len(lines) else ""
                if nxt and VALUE_RX.search(nxt) and len(nxt) <= 60 and not any(r.match(nxt) for r in LABELS.values()):
                    value = nxt
                else:
                    continue
            if len(value) > 80:
                continue
            k = f"{section} · {key}" if section else key
            specs.setdefault(k, value)
            break
    # прозаические формулировки — только если явной метки нет
    full = " ".join(lines)
    for key, rx in PROSE:
        if any(kk.endswith(key) for kk in specs):
            continue
        m = rx.search(full)
        if m:
            specs[key] = m.group(1) if key != "Диапазон частот" else m.group(1)
            if key in ("Дальность",):
                specs[key] += " м"
            elif key == "Работа от аккумулятора":
                specs[key] += " ч"
            elif key == "Вес":
                specs[key] += " г"
    return specs


def enrich_competitor(conn: sqlite3.Connection, competitor_id: int, only_missing: bool = False, delay: float = 1.5) -> dict:
    rows = db.rows(conn, "SELECT id, name, url, specs_json FROM competitor_products WHERE competitor_id=? AND is_active=1", (competitor_id,))
    done = updated = 0
    cache: dict[str, dict] = {}
    for r in rows:
        url = r["url"].split("#")[0]
        if not url.startswith("http"):
            continue
        old = db.uj(r["specs_json"], {}) or {}
        if only_missing and old:
            continue
        if url not in cache:
            try:
                res = http.fetch(url, f"competitor_{competitor_id}", save=False, timeout=25)
                cache[url] = extract_specs(res.text) if res.status == 200 else {}
            except Exception:  # noqa: BLE001
                cache[url] = {}
            time.sleep(delay)
        found = cache[url]
        done += 1
        if found:
            merged = {**found, **{k: v for k, v in old.items() if k not in found}}
            if merged != old:
                conn.execute("UPDATE competitor_products SET specs_json=? WHERE id=?", (db.j(merged), r["id"]))
                updated += 1
    conn.commit()
    return {"products": len(rows), "checked": done, "updated": updated}
