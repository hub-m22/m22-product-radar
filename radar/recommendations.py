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
    ex = db.row(conn, "SELECT id, status FROM recommendations WHERE dedupe_key=?", (r["dedupe_key"],))
    if ex:
        if ex["status"] == "new":  # ещё не разобрана — обновляем факты, не плодим дублей
            conn.execute("UPDATE recommendations SET title=?, action=?, priority=?, basis=?, expected_effect=?, confidence=?, sources_json=?, signal_ids_json=?, updated_at=datetime('now') WHERE id=?",
                         (r.get("title"), r.get("action"), r.get("priority"), r.get("basis"), r.get("expected_effect"), r.get("confidence"),
                          db.j(r.get("sources_json")) if isinstance(r.get("sources_json"), (list, dict)) else r.get("sources_json"),
                          db.j(r.get("signal_ids_json")) if isinstance(r.get("signal_ids_json"), (list, dict)) else r.get("signal_ids_json"), ex["id"]))
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
    sig = lambda t: db.rows(conn, "SELECT * FROM signals WHERE type=? AND status NOT IN ('rejected','done') ORDER BY created_at DESC", (t,))  # noqa: E731

    # Цены против рынка — раздел «Пересмотр цен», а не действия: там вердикт по каждому товару и он обновляется сам.
    # 1а. рекомендации, все сигналы которых закрыты или отклонены, закрываем (любой тип)
    for r in db.rows(conn, "SELECT id, signal_ids_json FROM recommendations WHERE status='new'"):
        ids = [int(x) for x in (db.uj(r["signal_ids_json"], []) or []) if str(x).isdigit()]
        if not ids:
            continue
        open_n = db.row(conn, f"SELECT COUNT(*) n FROM signals WHERE id IN ({','.join('?' * len(ids))}) AND status IN ('new','in_research','accepted')", ids)["n"]
        if open_n == 0:
            conn.execute("UPDATE recommendations SET status='done', comment=COALESCE(comment,'') || ' [закрыта автоматически: сигналы-основания закрыты]', updated_at=datetime('now') WHERE id=?", (r["id"],))

    # Расхождения между m22.ru и radiosync.ru — раздел «Наши сайты».
    # 3. Модель у ≥3 конкурентов, нет у M22 — сильный факт
    for s in sig("multi_competitor_product"):
        ev = db.uj(s["evidence_json"], {}) or {}
        just = (ev.get("justification") or {})
        if ev.get("n_comp", 0) < 3 or "у M22 её нет" not in s["title"] or not just.get("reasons"):
            continue  # без преимущества по цене/характеристикам/группе рекомендация не формируется
        label = f"{ev.get('brand') or ''} {ev.get('model_key')}".strip()
        if _emit(conn, title=f"Запросить у поставщиков модель {label}: продают {ev['n_comp']} независимых продавца, у M22 её нет",
                 action=f"Запросить у 2-3 поставщиков цену и образец модели {label} (продавцы в РФ: {ev.get('comps')}; цены {s['what_happened'].split('Цены')[-1].split('.')[0].strip()}). "
                        f"Сравнить характеристики с ближайшей моделью Radiosync и посчитать маржу при цене на 5% ниже минимальной рыночной.",
                 priority="P2", basis="Основание: " + " ".join(r["text"] for r in just["reasons"]) + " " + s["what_happened"], expected_effect="Закрытие пробела ассортимента по модели с подтверждённым предложением у нескольких продавцов.", confidence=s["confidence"],
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

    # Ошибки сбора — раздел «Источники»; в действия не попадают.
    conn.commit()
    return n
