"""Product Discovery: карточки продуктовых гипотез. Система собирает доказательства, решение принимает человек."""
from __future__ import annotations

import sqlite3

from . import db
from .normalize import CATEGORY_NAMES


def _cat(slug):
    return CATEGORY_NAMES.get(slug or "", slug or "")


def _emit(conn: sqlite3.Connection, **h) -> bool:
    if db.row(conn, "SELECT id FROM hypotheses WHERE dedupe_key=?", (h["dedupe_key"],)):
        return False
    cols = ["title", "description", "discovered_via", "signals_json", "demand_evidence", "competitors_json", "market_prices", "m22_link", "target_segment", "use_case",
            "pros", "cons", "risks", "missing_data", "next_step", "owner", "category_slug", "dedupe_key"]
    vals = [h.get(c) for c in cols]
    for k in ("signals_json", "competitors_json"):
        if isinstance(h.get(k), (list, dict)):
            vals[cols.index(k)] = db.j(h[k])
    conn.execute(f"INSERT INTO hypotheses({', '.join(cols)}) VALUES({', '.join('?' for _ in cols)})", vals)
    return True


def _fmt(p):
    return "—" if p is None else f"{p:,.0f} ₽".replace(",", " ")


def generate(conn: sqlite3.Connection) -> int:
    n = 0
    # A. Категории конкурентов, которых нет у M22 (≥2 конкурентов)
    rows = db.rows(conn, """SELECT s.category_slug, COUNT(*) AS n, GROUP_CONCAT(s.id) AS sids, GROUP_CONCAT(s.competitor_id) AS cids
                            FROM signals s WHERE s.type='new_category' AND s.status!='rejected' GROUP BY s.category_slug HAVING n>=2""")
    for r in rows:
        cids = [int(x) for x in r["cids"].split(",")]
        comps = db.rows(conn, f"SELECT id, name, website FROM competitors WHERE id IN ({','.join('?' * len(cids))})", cids)
        prods = db.rows(conn, f"SELECT cp.name, cp.price, cp.url, c.name AS competitor FROM competitor_products cp JOIN competitors c ON c.id=cp.competitor_id WHERE cp.category_slug=? AND cp.is_active=1 AND cp.competitor_id IN ({','.join('?' * len(cids))}) ORDER BY cp.price", [r["category_slug"], *cids])
        prices = [p["price"] for p in prods if p["price"]]
        demand = db.rows(conn, "SELECT q.query, COUNT(d.id) AS obs FROM search_queries q LEFT JOIN demand_observations d ON d.query_id=q.id WHERE q.category_slug=? GROUP BY q.id ORDER BY obs DESC LIMIT 5", (r["category_slug"],))
        if _emit(conn, title=f"Новая категория: {_cat(r['category_slug'])}",
                 description=f"Категория представлена у {len(comps)} конкурентов ({', '.join(c['name'] for c in comps)}), в матрице M22 отсутствует. Товаров у конкурентов: {len(prods)}.",
                 discovered_via="мониторинг каталогов конкурентов (сигнал new_category)", signals_json=[int(x) for x in r["sids"].split(",")],
                 demand_evidence=("Запросы категории в семантической карте: " + ", ".join(q["query"] for q in demand) + ". Объёмы спроса — требуется выгрузка Wordstat.") if demand else "Запросов категории в карте нет — добавить.",
                 competitors_json=[{"name": c["name"], "website": c["website"]} for c in comps],
                 market_prices=(f"{_fmt(min(prices))} – {_fmt(max(prices))}, медиана {_fmt(sorted(prices)[len(prices) // 2])} ({len(prices)} предложений)" if prices else "цены не зафиксированы"),
                 m22_link="Прямых аналогов в матрице нет; смежные категории: " + ", ".join(sorted({_cat(x['category_slug']) for x in db.rows(conn, 'SELECT DISTINCT category_slug FROM m22_products WHERE is_active=1 AND in_scope=1')})[:5]),
                 target_segment="Та же аудитория, что у радиогидов: экскурсионные бюро, музеи, промышленные предприятия, организаторы мероприятий",
                 use_case="Смежная потребность в рамках экскурсии/мероприятия (см. примеры товаров конкурентов)",
                 pros="Есть готовый спрос у текущих клиентов; конкуренты уже продают — рынок существует; продажа через тот же канал и ту же базу.",
                 cons="Неизвестна маржа и наличие поставщика; риск распыления фокуса; категория может быть низкочастотной.",
                 risks="Низкая маржа при закупке малыми партиями; сервисная нагрузка; отсутствие подтверждённого объёма спроса.",
                 missing_data="Объём спроса (Wordstat), закупочная цена (Alibaba/1688 — ручная проверка), число тендеров с этой категорией за 12 мес.",
                 next_step="Выгрузить Wordstat по 5 запросам категории; запросить у 2 поставщиков закупочную цену; собрать ≥5 розничных цен конкурентов — и принять решение.",
                 owner="Продуктовая команда", category_slug=r["category_slug"], dedupe_key=f"hyp:newcat:{r['category_slug']}"):
            n += 1

    # B. Модель у ≥2 конкурентов, отсутствующая у M22
    for s in db.rows(conn, "SELECT * FROM signals WHERE type='multi_competitor_product' AND status!='rejected' AND title LIKE '%у M22 её нет%'"):
        ev = db.uj(s["evidence_json"], {}) or {}
        if (ev.get("n_comp") or 0) < 2 or not (ev.get("justification") or {}).get("reasons"):
            continue  # без преимущества по цене/характеристикам карточка не создаётся
        if _emit(conn, title=f"Модель {(ev.get('brand') + ' ') if ev.get('brand') else ''}{ev.get('model_key')} — ввод в матрицу",
                 description=s["what_happened"], discovered_via="мониторинг конкурентов (сигнал multi_competitor_product)", signals_json=[s["id"]],
                 demand_evidence="Брендовые запросы по модели — добавить в семантическую карту и выгрузить Wordstat.",
                 competitors_json=[{"name": c} for c in (ev.get("comps") or "").split(",")], market_prices=f"{_fmt(ev.get('pmin'))} – {_fmt(ev.get('pmax'))}",
                 m22_link="Ближайшие по типу товары Radiosync — см. сопоставления в разделе «Конкуренты»", target_segment="Покупатели, ищущие конкретную модель (брендовый спрос)",
                 use_case="Замена/дополнение линейки радиогидов", pros="Модель продают несколько независимых продавцов - предложение и, вероятно, спрос подтверждены рынком.",
                 cons="Прямая ценовая конкуренция по одинаковому товару; нет отстройки.", risks="Демпинг конкурентов; зависимость от одного поставщика.",
                 missing_data="Закупочная цена, MOQ, сертификация, объём брендовых запросов.", next_step="Запросить цену и образец у 2–3 поставщиков; посчитать маржу при цене −5% к минимальной рыночной.",
                 owner="Закупки", category_slug=s["category_slug"], dedupe_key=f"hyp:model:{ev.get('model_key')}"):
            n += 1

    # C. Растущая категория без товаров M22
    for s in db.rows(conn, "SELECT * FROM signals WHERE type='category_growth_gap' AND status!='rejected'"):
        if _emit(conn, title=f"Растущий спрос: {_cat(s['category_slug'])}", description=s["what_happened"], discovered_via="поисковый спрос (сигнал category_growth_gap)",
                 signals_json=[s["id"]], demand_evidence=s["what_happened"] + " Источник: " + (s["source"] or ""), competitors_json=[], market_prices="не собраны",
                 m22_link="Товаров категории в матрице нет", target_segment="определить по запросам категории", use_case="определить", pros="Рост спроса подтверждён динамикой запросов.",
                 cons="Индекс Google Trends относительный — нужен Wordstat для абсолютных объёмов.", risks="Ложный рост из-за сезонности или новости.",
                 missing_data="Абсолютный объём (Wordstat), предложения конкурентов, закупочные цены.", next_step="Подтвердить рост Wordstat; собрать 5 предложений конкурентов.",
                 owner="Продуктовая команда", category_slug=s["category_slug"], dedupe_key=f"hyp:growth:{s['category_slug']}"):
            n += 1

    # D. Технологические заменители (конкуренты типа substitute)
    subs = db.rows(conn, "SELECT c.* FROM competitors c WHERE c.is_active=1 AND c.types_json LIKE '%substitute%'")
    if subs:
        if _emit(conn, title="Технологические заменители: мобильные аудиогиды и приложения",
                 description="В реестре " + str(len(subs)) + " заменителей: " + ", ".join(c["name"] for c in subs) + ". Они закрывают Job «провести экскурсию без раздачи оборудования» через смартфон посетителя.",
                 discovered_via="классификация конкурентов", signals_json=[], demand_evidence="Запросы «аудиогид приложение», «мобильный гид», «QR аудиогид» в семантической карте (объёмы — Wordstat).",
                 competitors_json=[{"name": c["name"], "website": c["website"]} for c in subs], market_prices="подписочные модели / бесплатно для посетителя; см. сайты",
                 m22_link="Аудиогид AG-300 (аппаратный) — прямая альтернатива; радиогиды — для живого гида", target_segment="Музеи и объекты с самостоятельным осмотром",
                 use_case="Самостоятельный осмотр без гида", pros="Возможность партнёрства или гибридного решения (аппаратный + мобильный); защита музейного сегмента AG-300.",
                 cons="Не аппаратный бизнес; другая компетенция.", risks="Отток музеев к мобильным гидам снижает спрос на AG-300 и одноразовые наушники.",
                 missing_data="Доля музеев, перешедших на мобильные гиды; ценовые модели заменителей.", next_step="Опросить 5 текущих музейных клиентов: используют ли мобильные гиды и почему.",
                 owner="Продуктовая команда", category_slug="substitutes_apps", dedupe_key="hyp:substitutes"):
            n += 1
    conn.commit()
    return n
