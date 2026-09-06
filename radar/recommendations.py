"""Рекомендуемые действия: формируются из сигналов по правилам с порогами.

Правило: сильная рекомендация только при сильном факте (≥3 сопоставимых, подтверждённая цена) или ≥2 согласующихся сигналах.
Один слабый сигнал даёт только карточку гипотезы / «на исследование», а не рекомендацию.
"""
from __future__ import annotations

import sqlite3
from datetime import date, timedelta

from . import db
from .normalize import CATEGORY_NAMES

OWNERS = {"pricing": "Собственник", "product": "Продуктовая команда", "marketing": "Маркетинг", "sales": "Продажи", "purchasing": "Закупки", "data": "Продуктовая команда"}


def _due(days: int) -> str:
    return (date.today() + timedelta(days=days)).isoformat()


def _emit(conn: sqlite3.Connection, **r) -> bool:
    if db.row(conn, "SELECT id FROM recommendations WHERE dedupe_key=?", (r["dedupe_key"],)):
        return False
    cols = ["title", "action", "priority", "basis", "expected_effect", "confidence", "owner", "due_date", "sources_json", "signal_ids_json", "category_slug", "m22_product_id", "dedupe_key"]
    vals = [r.get(c) for c in cols]
    for k in ("sources_json", "signal_ids_json"):
        if isinstance(r.get(k), (list, dict)):
            vals[cols.index(k)] = db.j(r[k])
    conn.execute(f"INSERT INTO recommendations({', '.join(cols)}) VALUES({', '.join('?' for _ in cols)})", vals)
    return True


def _cat(slug):
    return CATEGORY_NAMES.get(slug or "", slug or "")


def generate(conn: sqlite3.Connection) -> int:
    n = 0
    sig = lambda t: db.rows(conn, "SELECT * FROM signals WHERE type=? AND status!='rejected' ORDER BY created_at DESC", (t,))  # noqa: E731

    # 1. Цена относительно рынка (сильный факт: ≥3 сопоставимых)
    for s in sig("m22_price_above_market") + sig("m22_price_below_market"):
        ev = db.uj(s["evidence_json"], {}) or {}
        comps = ev.get("comparables") or []
        if len(comps) < 3 or (s["confidence"] or 0) < 0.5:
            continue
        p = db.row(conn, "SELECT * FROM m22_products WHERE id=?", (s["m22_product_id"],))
        if not p:
            continue
        above = s["type"] == "m22_price_above_market"
        gap = abs((p["price"] - ev["median"]) / ev["median"] * 100)
        prio = "P1" if gap >= 20 else "P2"
        srcs = [{"name": c["competitor_name"], "url": c["url"], "price": c["price"]} for c in comps]
        if above:
            action = (f"Провести ценовой разбор «{p['name']}»: цена {p['price']:,.0f} ₽ против медианы {ev['median']:,.0f} ₽ у {len(comps)} конкурентов. "
                      f"Решение: снизить до {ev['median'] * 0.97:,.0f}–{ev['median'] * 1.05:,.0f} ₽ или добавить в карточку явные преимущества (гарантия 2 года, наличие, поддержка) и проверить конверсию 2 недели.").replace(",", " ")
            effect = "Рост конверсии карточки и B2B-запросов в заказы; защита маржи при осознанном премиум-позиционировании."
        else:
            action = (f"Проверить повышение цены на «{p['name']}»: медиана {len(comps)} сопоставимых конкурентов выше цены M22 на {gap:.0f}% "
                      f"({ev['median']:,.0f} ₽ против {p['price']:,.0f} ₽). Поднять цену на 5–10% и следить за конверсией 2 недели.").replace(",", " ")
            effect = f"Прирост маржи до {gap:.0f}% на позицию без потери спроса (при сохранении конверсии)."
        if _emit(conn, title=f"{'Проверить обоснованность цены' if above else 'Проверить повышение цены'}: {p['name'][:60]}", action=action, priority=prio,
                 basis=s["what_happened"], expected_effect=effect, confidence=s["confidence"], owner=OWNERS["pricing"], due_date=_due(7), sources_json=srcs,
                 signal_ids_json=[s["id"]], category_slug=s["category_slug"], m22_product_id=p["id"], dedupe_key=f"rec:{s['dedupe_key']}"):
            n += 1

    # 2. Расхождение цен между сайтами — сильный факт
    xs = [s for s in sig("cross_site_discrepancy") if s["old_value"] and s["new_value"] and "отличается" in s["title"]]
    if xs:
        items = "; ".join(f"{s['title'].split('«')[1].split('»')[0]}: {s['new_value']} / {s['old_value']}" for s in xs[:12])
        if _emit(conn, title=f"Синхронизировать цены между m22.ru и radiosync.ru ({len(xs)} позиций)",
                 action=f"Установить одинаковые цены на обоих сайтах для позиций: {items}. Назначить один источник истины (прайс-лист) и обновлять оба сайта из него.",
                 priority="P2", basis=f"Подтверждённое расхождение цен по {len(xs)} позициям при сборе {xs[0]['observed_at'][:10]}.",
                 expected_effect="Исключение потери доверия клиентов и ошибок в тендерных прайс-листах.", confidence=0.95, owner=OWNERS["product"], due_date=_due(5),
                 sources_json=[{"name": "m22.ru / radiosync.ru", "url": s["source_url"]} for s in xs[:12]], signal_ids_json=[s["id"] for s in xs],
                 dedupe_key="rec:xsite:" + ":".join(str(s["id"]) for s in xs[:12])):
            n += 1
    miss = [s for s in sig("cross_site_discrepancy") if "не найден на m22.ru" in s["title"]]
    if miss:
        if _emit(conn, title=f"Выровнять ассортимент: {len(miss)} позиций radiosync.ru нет на m22.ru",
                 action="Проверить список: " + "; ".join(s["title"].split("«")[1].split("»")[0] for s in miss[:10]) + ". Добавить карточки на m22.ru или снять с radiosync.ru.",
                 priority="P3", basis=f"{len(miss)} позиций radiosync.ru без соответствия по ключу модели на m22.ru.", expected_effect="Единый ассортимент; меньше потерянных заказов на основном магазине.",
                 confidence=0.7, owner=OWNERS["product"], due_date=_due(14), sources_json=[{"name": "radiosync.ru", "url": s["source_url"]} for s in miss[:10]],
                 signal_ids_json=[s["id"] for s in miss], dedupe_key="rec:xsite-missing:" + ":".join(str(s["id"]) for s in miss[:10])):
            n += 1

    # 3. Модель у ≥3 конкурентов, нет у M22 — сильный факт
    for s in sig("multi_competitor_product"):
        ev = db.uj(s["evidence_json"], {}) or {}
        if ev.get("n_comp", 0) < 3 or "у M22 её нет" not in s["title"]:
            continue
        label = f"{ev.get('brand') or ''} {ev.get('model_key')}".strip()
        if _emit(conn, title=f"Запросить у поставщиков модель {label}: продают {ev['n_comp']} независимых продавца, у M22 её нет",
                 action=f"Запросить у 2-3 поставщиков цену и образец модели {label} (продавцы в РФ: {ev.get('comps')}; цены {s['what_happened'].split('Цены')[-1].split('.')[0].strip()}). "
                        f"Сравнить характеристики с ближайшей моделью Radiosync и посчитать маржу при цене на 5% ниже минимальной рыночной.",
                 priority="P2", basis=s["what_happened"], expected_effect="Закрытие пробела ассортимента по модели с подтверждённым предложением у нескольких продавцов.", confidence=s["confidence"],
                 owner=OWNERS["purchasing"], due_date=_due(14), sources_json=[{"name": "мониторинг конкурентов", "url": s["source_url"]}], signal_ids_json=[s["id"]],
                 category_slug=s["category_slug"], dedupe_key=f"rec:{s['dedupe_key']}"):
            n += 1

    # 4. Категория у ≥2 конкурентов — только карточка гипотезы (слабый сигнал), рекомендация «исследовать» с P3
    by_cat: dict[str, list[dict]] = {}
    for s in sig("new_category"):
        by_cat.setdefault(s["category_slug"], []).append(s)
    for cat, ss in by_cat.items():
        if len(ss) < 2:
            continue
        comps = ", ".join(sorted({db.row(conn, "SELECT name FROM competitors WHERE id=?", (s["competitor_id"],))["name"] for s in ss}))
        if _emit(conn, title=f"Исследовать категорию «{_cat(cat)}»: есть у {len(ss)} конкурентов, нет у M22",
                 action=f"Открыть карточку гипотезы «{_cat(cat)}»: собрать цены ≥5 предложений ({comps}), выгрузить спрос из Wordstat по 5 запросам категории и оценить маржу при закупке у текущих поставщиков. Решение — через 2 недели.",
                 priority="P3", basis=f"Категория представлена у конкурентов: {comps}.", expected_effect="Обоснованное решение о расширении матрицы смежной категорией.",
                 confidence=0.6, owner=OWNERS["product"], due_date=_due(14), sources_json=[{"name": s["source"], "url": s["source_url"]} for s in ss], signal_ids_json=[s["id"] for s in ss],
                 category_slug=cat, dedupe_key=f"rec:newcat:{cat}:{len(ss)}"):
            n += 1

    # 5. Спрос по категории (рост) — при наличии товаров M22
    for s in sig("category_growth_existing"):
        if _emit(conn, title=f"Запустить кампанию/посадочную страницу: {_cat(s['category_slug'])}",
                 action=f"Создать отдельную посадочную страницу и рекламную кампанию по категории «{_cat(s['category_slug'])}» с товарами M22 из матрицы; бюджет тестовый на 2 недели, KPI — заявки.",
                 priority="P2", basis=s["what_happened"] + " " + (s["period"] or ""), expected_effect="Захват растущего спроса существующими товарами без затрат на новый ассортимент.",
                 confidence=s["confidence"], owner=OWNERS["marketing"], due_date=_due(10), sources_json=[{"name": s["source"], "url": s["source_url"]}], signal_ids_json=[s["id"]],
                 category_slug=s["category_slug"], dedupe_key=f"rec:{s['dedupe_key']}"):
            n += 1
    for s in sig("category_growth_gap"):
        if _emit(conn, title=f"Открыть карточку гипотезы: растущая категория «{_cat(s['category_slug'])}» без товаров M22",
                 action=f"Собрать ≥5 предложений конкурентов и цены по категории «{_cat(s['category_slug'])}», подтвердить рост вторым источником (Wordstat) и принять решение о тестовой закупке.",
                 priority="P2", basis=s["what_happened"], expected_effect="Своевременный вход в растущую категорию.", confidence=s["confidence"], owner=OWNERS["product"], due_date=_due(14),
                 sources_json=[{"name": s["source"], "url": s["source_url"]}], signal_ids_json=[s["id"]], category_slug=s["category_slug"], dedupe_key=f"rec:{s['dedupe_key']}"):
            n += 1

    # 6. Сценарии применения (≥2 конкурентов) — контент-действие
    uc = sig("new_use_case")
    if uc:
        labels = "; ".join(s["title"].split("«")[1].split("»")[0] for s in uc[:8])
        if _emit(conn, title=f"Добавить сценарии применения в карточки и посадочные: {len(uc)} сценариев",
                 action=f"Дописать в описания SGTR02/SGTR03/SGTR13/UG-10 и создать посадочные страницы под сценарии: {labels}. Каждый сценарий — с фото/кейсом и запросом в заголовке H1.",
                 priority="P2", basis=f"Сценарии упоминают ≥2 конкурентов, в описаниях M22 отсутствуют ({len(uc)} шт.).", expected_effect="Дополнительный поисковый трафик по запросам «радиогид для …» без изменения ассортимента.",
                 confidence=0.6, owner=OWNERS["marketing"], due_date=_due(14), sources_json=[{"name": s["source"], "url": s["source_url"]} for s in uc[:8]], signal_ids_json=[s["id"] for s in uc],
                 dedupe_key="rec:usecases:" + ":".join(str(s["id"]) for s in uc[:8])):
            n += 1

    # 7. Снижение цены конкурента на сопоставимый товар ≥10%
    for s in sig("competitor_price_change"):
        if not s["m22_product_id"] or "снизил" not in s["title"]:
            continue
        p = db.row(conn, "SELECT name, price FROM m22_products WHERE id=?", (s["m22_product_id"],))
        if not p:
            continue
        if _emit(conn, title=f"Отреагировать на снижение цены конкурента: {p['name'][:50]}",
                 action=f"{s['recommended_action']} Если разница > 10% — подготовить ответ (акция на комплект, бонус наушниками) в течение недели.",
                 priority="P2" if s["severity"] == "high" else "P3", basis=s["what_happened"], expected_effect="Удержание доли в позиции, где конкурент давит ценой.",
                 confidence=s["confidence"], owner=OWNERS["pricing"], due_date=_due(7), sources_json=[{"name": s["source"], "url": s["source_url"]}], signal_ids_json=[s["id"]],
                 category_slug=s["category_slug"], m22_product_id=s["m22_product_id"], dedupe_key=f"rec:{s['dedupe_key']}"):
            n += 1

    # 8. Ошибки источников
    errs = sig("source_error")
    if errs:
        if _emit(conn, title=f"Восстановить {len(errs)} неработающих источников/страниц",
                 action="Проверить: " + "; ".join(s["title"] for s in errs[:6]) + ". Обновить URL или отключить страницу в разделе «Источники».",
                 priority="P3", basis="Повторные сбои при сборе.", expected_effect="Полнота сигналов по ценам конкурентов.", confidence=1.0, owner=OWNERS["data"], due_date=_due(3),
                 sources_json=[{"name": s["source"], "url": s["source_url"]} for s in errs[:6]], signal_ids_json=[s["id"] for s in errs], dedupe_key="rec:srcerr:" + ":".join(str(s["id"]) for s in errs[:6])):
            n += 1
    conn.commit()
    return n
