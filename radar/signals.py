"""Обнаружение рыночных сигналов (15 типов) с порогами значимости и защитой от повторов.

Каждый сигнал: факт / аналитический вывод / гипотеза, со степенью уверенности, источником и подтверждающими данными.
"""
from __future__ import annotations

import logging
import re
import sqlite3
import statistics
from datetime import datetime, timedelta, timezone

from . import config, db, feedback
from . import specs as specmod
from .matching import comparables_for, spec_summary as specmod_summary
from .normalize import CATEGORY_NAMES

log = logging.getLogger(__name__)

# Типы, которые больше не создаются как сигналы: у каждой темы теперь одно место.
MOVED_TYPES = {
    "m22_price_above_market": "раздел «Пересмотр цен»",
    "m22_price_below_market": "раздел «Пересмотр цен»",
    "cross_site_discrepancy": "раздел «Наши сайты»",
    "source_error": "раздел «Источники»",
    "new_competitor": "реестр «Конкуренты» (конкурентов добавляем сами — это не событие рынка)",
    "product_appeared": "«Матрица конкурентов» → «Что появилось / исчезло»",
    "new_kit_solution": "«Матрица конкурентов» → «Что появилось / исчезло»",
    "product_disappeared": "«Матрица конкурентов» → «Что появилось / исчезло»",
}
SIGNAL_TYPES = {
    "competitor_price_change": "Изменение цены конкурента",
    "product_appeared": "Появление товара у конкурента",
    "product_disappeared": "Исчезновение товара у конкурента",
    "new_category": "Категория конкурента, которой нет у M22",
    "new_competitor": "Новый конкурент",
    "new_kit_solution": "Новый комплект / B2B-решение у конкурента",
    "multi_competitor_product": "Один товар у нескольких конкурентов",
    "m22_price_above_market": "Товар M22 заметно дороже рынка",
    "m22_price_below_market": "Товар M22 заметно дешевле рынка",
    "demand_change": "Рост или падение поискового спроса",
    "demand_anomaly": "Аномальный всплеск запроса",
    "category_growth_existing": "Рост категории, товар которой есть у M22",
    "category_growth_gap": "Рост категории, которой нет у M22",
    "new_use_case": "Новый сценарий применения существующего товара",
    "potential_new_category": "Потенциальная новая категория для исследования",
    "cross_site_discrepancy": "Расхождение между m22.ru и radiosync.ru",
    "source_error": "Ошибка источника данных",
}


def _emit(conn: sqlite3.Connection, **s) -> bool:
    """Создаёт сигнал, если с таким dedupe_key его ещё нет. Если есть и он ещё не разобран (статус new) —
    обновляет факты (цены, медиану, уверенность), не плодя дублей. Возвращает True только при создании."""
    ex = db.row(conn, "SELECT id, status FROM signals WHERE dedupe_key=?", (s["dedupe_key"],))
    if ex:
        if ex["status"] == "new":
            ev = s.get("evidence_json")
            conn.execute("""UPDATE signals SET title=?, what_happened=?, old_value=?, new_value=?, observed_at=?, evidence_json=?, confidence=?, severity=?,
                            why_matters=?, recommended_action=?, source_url=COALESCE(?, source_url) WHERE id=?""",
                         (s.get("title"), s.get("what_happened"), s.get("old_value"), s.get("new_value"), s.get("observed_at") or db.now_iso(),
                          db.j(ev) if isinstance(ev, (dict, list)) else ev, s.get("confidence"), s.get("severity"), s.get("why_matters"), s.get("recommended_action"),
                          s.get("source_url"), ex["id"]))
        return False
    cols = ["type", "severity", "fact_kind", "category_slug", "m22_product_id", "competitor_id", "competitor_product_id", "query_id", "title",
            "what_happened", "old_value", "new_value", "observed_at", "period", "source", "source_url", "evidence_json", "confidence", "why_matters",
            "recommended_action", "dedupe_key"]
    vals = [s.get(c) for c in cols]
    conf, note = feedback.calibrate(conn, s["type"], s.get("category_slug"), s.get("confidence"))
    vals[cols.index("confidence")] = conf
    if note:
        ev = s.get("evidence_json")
        if isinstance(ev, dict):
            ev = {**ev, "calibration": note}
        elif ev is None:
            ev = {"calibration": note}
        s["evidence_json"] = ev
        vals[cols.index("why_matters")] = (s.get("why_matters") or "") + f" [{note}]"
    if isinstance(s.get("evidence_json"), (dict, list)):
        vals[cols.index("evidence_json")] = db.j(s["evidence_json"])
    if not vals[cols.index("observed_at")]:
        vals[cols.index("observed_at")] = db.now_iso()
    conn.execute(f"INSERT INTO signals({', '.join(cols)}) VALUES({', '.join('?' for _ in cols)})", vals)
    return True


def _cat(slug: str | None) -> str:
    return CATEGORY_NAMES.get(slug or "", slug or "без категории")


def _fmt(p: float | None) -> str:
    return "—" if p is None else f"{p:,.0f} ₽".replace(",", " ")


# ---------- 1. Изменение цены конкурента ----------
def detect_competitor_price_changes(conn: sqlite3.Connection, days: int = 30) -> int:
    n = 0
    since = (datetime.now(timezone.utc) - timedelta(days=days)).strftime("%Y-%m-%d")
    baseline = (db.get_setting(conn, "baseline_date") or "")[:10]
    products = db.rows(conn, """SELECT cp.*, c.name AS competitor_name FROM competitor_products cp JOIN competitors c ON c.id=cp.competitor_id
                                WHERE cp.is_active=1 AND cp.price IS NOT NULL""")
    for cp in products:
        hist = db.rows(conn, "SELECT id, price, observed_at FROM competitor_price_history WHERE competitor_product_id=? AND price IS NOT NULL AND run_id IS NOT NULL ORDER BY observed_at DESC, id DESC LIMIT 2", (cp["id"],))
        if len(hist) < 2 or hist[0]["observed_at"] < since:
            continue
        new, old = hist[0]["price"], hist[1]["price"]
        if not old or not new:
            continue
        # два замера в один день — это не изменение цены, а разные страницы/варианты одного товара; замер до базовой даты не считается
        if hist[0]["observed_at"][:10] == hist[1]["observed_at"][:10] or hist[1]["observed_at"] < baseline:
            continue
        # отношение цен > 5x — почти наверняка ошибка разбора (артикул, склеенный с ценой), а не рыночное событие
        if max(new, old) / min(new, old) > 5:
            continue
        pct = (new - old) / old * 100
        if abs(pct) < config.PRICE_CHANGE_THRESHOLD_PCT:
            continue
        matches = db.rows(conn, """SELECT pm.m22_product_id, pm.match_type, pm.confidence, m.name AS m22_name, m.price AS m22_price FROM product_matches pm
                                   JOIN m22_products m ON m.id=pm.m22_product_id WHERE pm.competitor_product_id=? AND pm.review_status!='rejected' ORDER BY pm.confidence DESC LIMIT 1""", (cp["id"],))
        m = matches[0] if matches else None
        severity = "high" if abs(pct) >= 15 and m else ("medium" if abs(pct) >= 7 else "low")
        direction = "снизил" if pct < 0 else "повысил"
        why = (f"Конкурент {direction} цену на сопоставимый товар ({m['match_type']}, уверенность сопоставления {m['confidence']:.0%}); "
               f"цена M22 на «{m['m22_name']}» — {_fmt(m['m22_price'])}." if m else "Товар не сопоставлен с матрицей M22: влияние косвенное.")
        action = (f"Сравнить цену «{m['m22_name']}» ({_fmt(m['m22_price'])}) с новой ценой конкурента {_fmt(new)} и решить, менять ли цену или усилить аргументацию ценности."
                  if m else "Проверить, есть ли у M22 аналог; если да — выполнить сопоставление вручную в разделе «Конкуренты».")
        if _emit(conn, type="competitor_price_change", severity=severity, fact_kind="fact", category_slug=cp["category_slug"],
                 m22_product_id=m["m22_product_id"] if m else None, competitor_id=cp["competitor_id"], competitor_product_id=cp["id"],
                 title=f"{cp['competitor_name']} {direction} цену на «{cp['name'][:70]}» на {abs(pct):.0f}%",
                 what_happened=f"Цена изменилась с {_fmt(old)} на {_fmt(new)} ({pct:+.1f}%).", old_value=_fmt(old), new_value=_fmt(new),
                 observed_at=hist[0]["observed_at"], period=f"{hist[1]['observed_at'][:10]} → {hist[0]['observed_at'][:10]}",
                 source=cp["competitor_name"], source_url=cp["url"], evidence_json={"history": hist, "match": m, "photo": cp.get("image_url"), "url": cp["url"], "name": cp["name"]}, confidence=0.9,
                 why_matters=why, recommended_action=action, dedupe_key=f"cpc:{cp['id']}:{hist[0]['id']}"):
            n += 1
    return n


# ---------- 2. Появление / исчезновение товара ----------
def detect_product_appear_disappear(conn: sqlite3.Connection) -> int:
    n = 0
    comps = db.rows(conn, "SELECT id, name FROM competitors WHERE is_active=1")
    for c in comps:
        first_run = db.row(conn, "SELECT MIN(fetched_at) AS t FROM competitor_products WHERE competitor_id=?", (c["id"],))
        base = first_run["t"] if first_run else None
        if not base:
            continue
        base_dt = base[:10]
        # появившиеся после первого замера
        for cp in db.rows(conn, "SELECT * FROM competitor_products WHERE competitor_id=? AND is_active=1 AND substr(first_seen_at,1,10) > ?", (c["id"], base_dt)):
            kind_note = " Это комплект/решение." if cp["kind"] == "kit" else ""
            if _emit(conn, type="product_appeared", severity="medium", fact_kind="fact", category_slug=cp["category_slug"], competitor_id=c["id"],
                     competitor_product_id=cp["id"], title=f"Новый товар у {c['name']}: «{cp['name'][:70]}»",
                     what_happened=f"Товар впервые обнаружен {cp['first_seen_at'][:10]} по цене {_fmt(cp['price'])}.{kind_note}", old_value="отсутствовал",
                     new_value=_fmt(cp["price"]), observed_at=cp["first_seen_at"], period=f"базовый замер {base_dt} → {cp['first_seen_at'][:10]}",
                     source=c["name"], source_url=cp["url"], evidence_json={"product": {k: cp[k] for k in ("name", "price", "brand", "model_key", "category_slug")}},
                     confidence=0.85, why_matters=f"Расширение ассортимента конкурента в категории «{_cat(cp['category_slug'])}» может отобрать спрос у товаров M22.",
                     recommended_action=f"Проверить, есть ли у M22 аналог «{cp['name'][:50]}»; если нет — запросить цену у поставщика и оценить маржу.",
                     dedupe_key=f"appear:{cp['id']}"):
                n += 1
            if cp["kind"] == "kit" or re.search(r"решени|комплект для|для делегац|для музе|для заводов|для производств", cp["name"].lower()):
                if _emit(conn, type="new_kit_solution", severity="medium", fact_kind="fact", category_slug=cp["category_slug"], competitor_id=c["id"],
                         competitor_product_id=cp["id"], title=f"Новый комплект/решение у {c['name']}: «{cp['name'][:70]}»",
                         what_happened=f"Обнаружено готовое решение по цене {_fmt(cp['price'])}.", new_value=_fmt(cp["price"]), observed_at=cp["first_seen_at"],
                         source=c["name"], source_url=cp["url"], confidence=0.8, why_matters="Готовые решения повышают средний чек и закрывают потребность «под ключ» у B2B-клиентов.",
                         recommended_action="Сравнить состав и цену с комплектами Radiosync Прайм/Профи и при необходимости собрать аналогичный комплект.",
                         dedupe_key=f"kit:{cp['id']}"):
                    n += 1
        for cp in db.rows(conn, "SELECT * FROM competitor_products WHERE competitor_id=? AND is_active=0", (c["id"],)):
            if _emit(conn, type="product_disappeared", severity="low", fact_kind="fact", category_slug=cp["category_slug"], competitor_id=c["id"],
                     competitor_product_id=cp["id"], title=f"Товар исчез у {c['name']}: «{cp['name'][:70]}»",
                     what_happened=f"Товар не найден при последних проверках (последний раз виден {cp['last_seen_at'][:10]}).", old_value=_fmt(cp["price"]),
                     new_value="отсутствует", observed_at=db.now_iso(), source=c["name"], source_url=cp["url"], confidence=0.7,
                     why_matters="Возможен уход конкурента из позиции или временный дефицит — окно для продаж M22.",
                     recommended_action="Проверить наличие аналога у M22 и усилить его продвижение по соответствующему запросу.", dedupe_key=f"disappear:{cp['id']}:{cp['last_seen_at'][:10]}"):
                n += 1
    return n


# ---------- 3. Новая категория у конкурента ----------
def detect_new_categories(conn: sqlite3.Connection) -> int:
    n = 0
    m22_cats = {r["category_slug"] for r in db.rows(conn, "SELECT DISTINCT category_slug FROM m22_products WHERE is_active=1 AND in_scope=1 AND category_slug IS NOT NULL")}
    rows = db.rows(conn, """SELECT cp.category_slug, c.id AS competitor_id, c.name AS competitor_name, COUNT(*) AS n, MIN(cp.price) AS pmin, MAX(cp.price) AS pmax,
                            GROUP_CONCAT(cp.name, ' | ') AS names, MIN(cp.url) AS url
                            FROM competitor_products cp JOIN competitors c ON c.id=cp.competitor_id WHERE cp.is_active=1 AND cp.category_slug IS NOT NULL
                            GROUP BY cp.category_slug, c.id""")
    for r in rows:
        if r["category_slug"] in m22_cats:
            continue
        if _emit(conn, type="new_category", severity="medium", fact_kind="fact", category_slug=r["category_slug"], competitor_id=r["competitor_id"],
                 title=f"{r['competitor_name']} продаёт «{_cat(r['category_slug'])}» — категории нет у M22",
                 what_happened=f"У конкурента {r['n']} товар(ов) в категории, цены {_fmt(r['pmin'])} – {_fmt(r['pmax'])}. Примеры: {r['names'][:300]}",
                 new_value=f"{r['n']} товаров", observed_at=db.now_iso(), source=r["competitor_name"], source_url=r["url"],
                 evidence_json={"names": r["names"][:1000]}, confidence=0.75,
                 why_matters="Смежная категория у конкурента — кандидат в товарные пробелы M22 (смежный спрос той же аудитории).",
                 recommended_action=f"Открыть карточку гипотезы по категории «{_cat(r['category_slug'])}»: собрать цены ≥3 конкурентов и запросить спрос (Wordstat).",
                 dedupe_key=f"newcat:{r['competitor_id']}:{r['category_slug']}"):
            n += 1
    return n


# ---------- 4. Новый конкурент ----------
def detect_new_competitors(conn: sqlite3.Connection) -> int:
    n = 0
    baseline = db.get_setting(conn, "baseline_date")
    if not baseline:
        return 0
    for c in db.rows(conn, "SELECT * FROM competitors WHERE is_active=1 AND substr(created_at,1,10) > ?", (baseline[:10],)):
        if _emit(conn, type="new_competitor", severity="medium", fact_kind="fact", competitor_id=c["id"], title=f"Новый конкурент в реестре: {c['name']}",
                 what_happened=f"Добавлен {c['created_at'][:10]} ({c['added_by']}). Типы: {', '.join(db.uj(c['types_json'], []) or [])}.", new_value=c["website"],
                 observed_at=c["created_at"], source=c["added_by"], source_url=c["website"], confidence=0.9,
                 why_matters="Новый игрок может менять цены и занимать выдачу по ключевым запросам категории.",
                 recommended_action="Добавить 3–5 страниц товаров конкурента в мониторинг и выполнить сопоставление с матрицей M22.", dedupe_key=f"newcomp:{c['id']}"):
            n += 1
    return n


# ---------- 6. Один товар у нескольких конкурентов ----------
def _distinctive_key(key: str | None) -> bool:
    """Код модели пригоден для сопоставления без бренда: >=2 букв в префиксе и >=4 знаков (RG18, SGTR02, WT300R), но не T100/R100."""
    if not key or len(key) < 4:
        return False
    m = re.match(r"^([A-Z]+)", key)
    return bool(m) and len(m.group(1)) >= 2


def _junk_url(u: str) -> bool:
    from urllib.parse import urlparse as _up
    pth = _up(u).path.rstrip("/")
    return pth == "" or "/cart" in pth


COMPARE_FIELDS = [("unit", "Цена за ед./место, ₽"), ("range_m", "Дальность, м"), ("channels", "Каналов"), ("battery_h", "Автономность, ч"), ("weight_g", "Вес, г"),
                  ("freq_band", "Диапазон"), ("two_way", "Двусторонняя связь"), ("display", "Экран"), ("capacity", "Вместимость")]


def _justify_strict(conn: sqlite3.Connection, full: list[dict], cat: str | None) -> dict:
    """Обоснование «стоит ли вводить модель» только против сопоставимых моделей M22: тот же тип изделия,
    для комплектов — та же вместимость. Причина засчитывается, только если у конкурента есть данные по этому полю.
    Возвращает reasons (пусто — сигнала быть не должно), таблицу сравнения и список моделей M22, с которыми сравнивали."""
    from collections import Counter
    from . import categories as catmod

    kind = Counter(f["kind"] for f in full).most_common(1)[0][0]
    offers = []
    for f in [x for x in full if x["kind"] == kind]:
        norm = specmod.normalize(f["name"], f["description"], f["specs_json"], f["price"], f["capacity"])
        unit, _q = catmod.unit_price(f["name"], f["price"], cat, f["kind"], norm.get("capacity") or f["capacity"])
        offers.append({"id": f["id"], "name": f["name"], "price": f["price"], "url": f.get("url"), "capacity": norm.get("capacity") or f["capacity"], "norm": norm, "unit": unit,
                       "specs_n": sum(1 for k in ("range_m", "channels", "battery_h", "weight_g", "freq_band") if norm.get(k))})
    priced = [o for o in offers if o["unit"]]
    best = min(priced, key=lambda o: o["unit"]) if priced else max(offers, key=lambda o: o["specs_n"])
    # сводим характеристики по всем предложениям модели (у одного продавца может быть полнее описание)
    merged = dict(best["norm"])
    for o in offers:
        for k, v in o["norm"].items():
            if merged.get(k) in (None, False) and v not in (None, False):
                merged[k] = v
    best = {**best, "norm": merged}
    m22 = db.rows(conn, "SELECT id, name, url, site, description, specs_json, price, capacity, kind, images_json FROM m22_products WHERE is_active=1 AND in_scope=1 AND parent_url IS NULL AND price IS NOT NULL AND site='m22.ru' AND category_slug=? AND kind=? ORDER BY price", (cat, kind))
    peers = []
    for m in m22:
        norm = specmod.normalize(m["name"], m["description"], m["specs_json"], m["price"], m["capacity"])
        unit, _q = catmod.unit_price(m["name"], m["price"], cat, m["kind"], norm.get("capacity") or m["capacity"])
        imgs = db.uj(m["images_json"], []) or []
        img = (f"https://{m['site']}{imgs[0]}" if imgs and imgs[0].startswith("/") else (imgs[0] if imgs else None))
        peers.append({"id": m["id"], "name": m["name"], "url": m["url"], "price": m["price"], "capacity": norm.get("capacity") or m["capacity"], "norm": norm, "unit": unit, "image": img})
    cap = best.get("capacity")
    if kind in ("system", "kit"):
        price_peers = [p for p in peers if cap and p["capacity"] and abs(p["capacity"] - cap) <= max(2, 0.2 * cap) and p["unit"]]
    else:
        price_peers = [p for p in peers if p["unit"]]
    spec_peers = peers
    reasons: list[dict] = []
    # нет сопоставимых моделей M22 этого типа — обоснования по цене/характеристикам быть не может; такие группы — тема раздела «категории и ниши», не событие
    # цена: только за сопоставимую единицу (за приёмник/передатчик, для комплектов — за место при той же вместимости)
    if best.get("unit") and price_peers:
        pm = min(price_peers, key=lambda p: p["unit"])
        gap = (best["unit"] - pm["unit"]) / pm["unit"] * 100
        if gap <= -25:
            unit_lbl = "за место в комплекте" if kind in ("system", "kit") else "за единицу"
            reasons.append({"kind": "price", "field": "unit", "peer_id": pm["id"], "gap_pct": round(gap),
                            "text": f"Цена {unit_lbl}: {best['unit']:,.0f} ₽ против {pm['unit']:,.0f} ₽ у «{pm['name'][:60]}» — дешевле на {abs(gap):.0f}%.".replace(",", " ")})
    elif kind in ("system", "kit") and not cap and peers:
        reasons_note = "вместимость комплекта конкурента неизвестна — по цене не сравнивается"
    # характеристики: против лучшего значения среди сопоставимых моделей M22; нужны данные с обеих сторон
    for field, label in (("range_m", "Дальность, м"), ("channels", "Каналов"), ("battery_h", "Автономность, ч")):
        a = best["norm"].get(field)
        vals = [(p["norm"].get(field), p) for p in spec_peers if p["norm"].get(field)]
        if not a or not vals:
            continue
        b, pb = max(vals, key=lambda x: x[0])
        if a >= b * 1.3:
            reasons.append({"kind": "spec", "field": field, "peer_id": pb["id"], "text": f"{label}: {a:g} против {b:g} у лучшей модели M22 «{pb['name'][:50]}» (+{(a / b - 1) * 100:.0f}%)."})
    a = best["norm"].get("weight_g")
    vals = [(p["norm"].get("weight_g"), p) for p in spec_peers if p["norm"].get("weight_g")]
    if a and vals:
        b, pb = min(vals, key=lambda x: x[0])
        if a <= b * 0.7:
            reasons.append({"kind": "spec", "field": "weight_g", "peer_id": pb["id"], "text": f"Вес: {a:g} г против {b:g} г у самой лёгкой модели M22 «{pb['name'][:50]}»."})
    if best["norm"].get("two_way") and spec_peers and not any(p["norm"].get("two_way") for p in spec_peers):
        reasons.append({"kind": "spec", "field": "two_way", "text": "Двусторонняя связь, которой нет у сопоставимых моделей M22 этого типа."})
    if best["norm"].get("freq_band") == "2.4 ГГц" and spec_peers and all(p["norm"].get("freq_band") not in (None, "2.4 ГГц") for p in spec_peers):
        reasons.append({"kind": "spec", "field": "freq_band", "text": "Цифровой диапазон 2,4 ГГц, тогда как сопоставимые модели M22 работают в UHF/VHF."})
    shown = (price_peers or spec_peers)
    shown = sorted(shown, key=lambda p: (abs((p["capacity"] or 0) - (cap or 0)) if cap else 0, p["price"]))[:3]
    if reasons:
        verdict = "есть основание рассмотреть ввод: " + ("; ".join(r["kind"] == "price" and "цена" or r["kind"] == "spec" and "характеристики" or "новая группа" for r in reasons))
    elif not peers:
        verdict = f"у M22 нет изделий этого типа ({catmod.KIND_LABELS.get(kind, kind).lower()}) в категории — сравнить не с чем; смотрите «категории и ниши»"
    elif not best.get("unit") and best["specs_n"] == 0:
        verdict = "у продавцов нет ни цены за сопоставимую единицу, ни характеристик — сравнить не с чем"
    else:
        verdict = "преимуществ перед сопоставимыми моделями M22 по цене и характеристикам не найдено"
    table = {"fields": COMPARE_FIELDS, "competitor": {"name": best["name"], "url": best.get("url"), "price": best["price"], "vals": {**{k: best["norm"].get(k) for k, _ in COMPARE_FIELDS}, "unit": best.get("unit")}},
             "peers": [{"id": p["id"], "name": p["name"], "url": p["url"], "price": p["price"], "image": p.get("image"), "vals": {**{k: p["norm"].get(k) for k, _ in COMPARE_FIELDS}, "unit": p.get("unit")}} for p in shown],
             "kind": catmod.KIND_LABELS.get(kind, kind), "cap": cap,
             "basis": ("тот же тип изделия и та же вместимость (±20 %), цена за место в комплекте" if kind in ("system", "kit") else "тот же тип изделия, цена за единицу")}
    return {"reasons": reasons, "verdict": verdict, "table": table, "peers": [{"id": p["id"], "name": p["name"], "price": p["price"], "image": p.get("image")} for p in shown]}


def detect_multi_competitor_products(conn: sqlite3.Connection) -> int:
    n = 0
    rows = db.rows(conn, """SELECT cp.id, cp.name, cp.brand, cp.model_key, cp.category_slug, cp.price, cp.url, cp.image_url, cp.fetched_at, cp.kind,
                                   c.id AS competitor_id, c.name AS competitor, c.website, COALESCE(c.group_name, c.name) AS seller
                            FROM competitor_products cp JOIN competitors c ON c.id=cp.competitor_id
                            WHERE cp.is_active=1 AND c.is_active=1 AND cp.model_key IS NOT NULL AND cp.kind IN ('system','transmitter','receiver','audioguide','kit')""")
    groups: dict[tuple, list[dict]] = {}
    all_sellers = {(r["seller"] or "").lower(): r["seller"] for r in rows}
    for r in rows:
        brand = (r["brand"] or "").strip().lower().replace(" ", "")
        if not brand and not _distinctive_key(r["model_key"]):
            continue  # короткий код без бренда (T100, R100) - нельзя утверждать, что это одна модель
        if _junk_url(r["url"]):
            continue  # запись без страницы товара (адрес главной, корзина) — не подтверждённое предложение
        # продавец на маркетплейсе («… — продавец Retekess Official») — это тот же продавец, что и его сайт
        m = re.search(r"— продавец (.+)$", r["name"] or "")
        if m:
            tag = m.group(1).strip().lower()
            first = re.split(r"[\s_\-]+", tag)[0]
            own = next((orig for low, orig in all_sellers.items() if first and len(first) >= 4 and first in low and "wildberries" not in low and "ozon" not in low), None)
            r = {**r, "seller": (own if own else f"{r['seller']} · {m.group(1).strip()}")}
        groups.setdefault((brand, r["model_key"]), []).append(r)
    m22_keys = {x["model_key"] for x in db.rows(conn, "SELECT DISTINCT model_key FROM m22_products WHERE is_active=1 AND model_key IS NOT NULL")}
    aliases = db.uj(db.get_setting(conn, "model_aliases"), {}) or {}
    m22_keys |= {a for a, b in aliases.items() if b in m22_keys}  # идентичные модели под другой маркой считаются «есть у M22»
    emitted_multi: set[str] = set()
    for (brand, key), offers in groups.items():
        sellers = sorted({o["seller"] for o in offers})
        if len(sellers) < 2:
            continue
        has = key in m22_keys
        if has and key in aliases:
            # идентичная модель под другой маркой уже есть у M22 — это не «пробел», а прямая ценовая конкуренция; см. «Пересмотр цен»
            conn.execute("UPDATE signals SET status='done', comment=? WHERE dedupe_key=? AND status='new'",
                         (f"закрыт автоматически: модель {key} идентична {aliases[key]} у M22, сравнение цен — в разделе «Пересмотр цен»", f"multi:{brand}:{key}"))
            continue
        prices = [o["price"] for o in offers if o["price"]]
        pmin, pmax = (min(prices), max(prices)) if prices else (None, None)
        sev = "high" if len(sellers) >= 3 and not has else "medium"
        label = f"{offers[0]['brand'] or ''} {key}".strip()
        items, seen_urls = [], set()
        for o in sorted(offers, key=lambda x: (x["url"], x["price"] is None, x["image_url"] is None)):
            if o["url"] in seen_urls:
                continue
            seen_urls.add(o["url"])
            items.append({"id": o["id"], "competitor_id": o["competitor_id"], "competitor": o["competitor"], "seller": o["seller"], "website": o["website"], "name": o["name"],
                          "price": o["price"], "url": o["url"], "image": o["image_url"], "fetched_at": o["fetched_at"], "kind": o["kind"]})
        good = {it["competitor_id"] for it in items if not _junk_url(it["url"])}
        items = [it for it in items if not _junk_url(it["url"]) or it["competitor_id"] not in good]
        items.sort(key=lambda x: (x["seller"], x["price"] is None, x["price"] or 0))
        own = next((o["url"] for o in offers if o["website"] and o["url"].split("/")[2].replace("www.", "") in o["website"]), offers[0]["url"])
        # обоснование: только против сопоставимых моделей M22 (тот же тип, та же вместимость); без обоснования сигнала нет
        full = db.rows(conn, "SELECT id, name, url, description, specs_json, price, capacity, kind FROM competitor_products WHERE id IN (%s)" % ",".join(str(o["id"]) for o in offers))
        cat = offers[0]["category_slug"]
        if not cat:
            continue
        just = _justify_strict(conn, full, cat)
        dedupe = f"multi:{brand}:{key}"
        if not just["reasons"] or has:
            conn.execute("UPDATE signals SET status='done', comment=?, updated_at=datetime('now') WHERE dedupe_key=? AND status IN ('new','in_research')",
                         ("закрыт автоматически: модель есть у M22" if has else f"закрыт автоматически: нет обоснования по цене или характеристикам против сопоставимых моделей M22 ({just['verdict']})", dedupe))
            continue
        sev = "high" if len(sellers) >= 3 else "medium"
        photo = next((it["image"] for it in items if it.get("image") and not _junk_url(it["url"])), None)
        just["table"]["competitor"]["image"] = photo
        evidence = {"brand": offers[0]["brand"], "model_key": key, "sellers": sellers, "n_comp": len(sellers), "comps": ", ".join(sellers), "pmin": pmin, "pmax": pmax,
                    "category_slug": cat, "name": offers[0]["name"], "url": own, "items": items, "justification": just, "comparison": just["table"], "photo": photo,
                    "m22_photos": [{"id": p["id"], "name": p["name"], "image": p.get("image"), "price": p["price"]} for p in just["peers"]],
                    "m22_candidates": just["peers"],
                    "rule": "одинаковый бренд и код модели; сайты одной группы компаний считаются одним продавцом"}
        title = f"Модель {label} продают {len(sellers)} независимых продавца" + ("" if has else ", у M22 её нет")
        reasons_txt = " ".join(r["text"] for r in just["reasons"])
        peers_txt = ", ".join(f"«{p['name'][:45]}» ({_fmt(p['price'])})" for p in just["peers"]) or "нет"
        what = (f"Продавцы: {', '.join(sellers)}. Цены {_fmt(pmin)} - {_fmt(pmax)}. Правило: одинаковый бренд и код модели ({label}); сайты одной группы считаются одним продавцом. "
                f"Сравнивали с моделями M22 того же типа ({just['table']['kind'].lower()}{', вместимость ' + str(just['table']['cap']) if just['table']['cap'] else ''}): {peers_txt}.")
        why = "Основание для ввода: " + reasons_txt
        action = f"Запросить у 2-3 поставщиков цену и образец {label}; проверить обоснование в таблице сравнения ниже; посчитать маржу при цене на 5 % ниже минимальной рыночной ({_fmt(pmin)})."
        emitted_multi.add(dedupe)
        existing = db.row(conn, "SELECT id, status FROM signals WHERE dedupe_key=?", (dedupe,))
        if existing:
            conn.execute("UPDATE signals SET title=?, what_happened=?, new_value=?, severity=?, evidence_json=?, source_url=?, why_matters=?, recommended_action=?, updated_at=datetime('now')" +
                         (", status='new', comment=NULL" if existing["status"] == "done" and "закрыт автоматически" in (db.row(conn, "SELECT comment FROM signals WHERE id=?", (existing["id"],))["comment"] or "") else "") + " WHERE id=?",
                         (title, what, f"{len(sellers)} продавцов", sev, db.j(evidence), own, why, action, existing["id"]))
            continue
        if _emit(conn, type="multi_competitor_product", severity=sev, fact_kind="fact", category_slug=offers[0]["category_slug"], title=title, what_happened=what,
                 new_value=f"{len(sellers)} продавцов", observed_at=db.now_iso(), source="мониторинг конкурентов", source_url=own, evidence_json=evidence, confidence=0.8,
                 why_matters=why, recommended_action=action, dedupe_key=dedupe):
            n += 1
    # модели, которые больше не продают 2+ продавца (или сигнал устарел после пересбора), закрываем
    for old_sig in db.rows(conn, "SELECT id, dedupe_key FROM signals WHERE type='multi_competitor_product' AND status='new'"):
        if old_sig["dedupe_key"] not in emitted_multi:
            conn.execute("UPDATE signals SET status='done', comment='закрыт автоматически: условие больше не выполняется' WHERE id=?", (old_sig["id"],))
    return n


# ---------- 7/8. Товар M22 дороже/дешевле рынка ----------
def detect_price_vs_market(conn: sqlite3.Connection) -> int:
    n = 0
    products = db.rows(conn, "SELECT * FROM m22_products WHERE is_active=1 AND in_scope=1 AND price IS NOT NULL AND (parent_url IS NULL OR site='radiosync.ru')")
    m22_skus = {p["sku"].strip().upper() for p in products if p["site"] == "m22.ru" and p["sku"]}
    emitted: set[str] = set()
    for p in products:
        if p["site"] == "radiosync.ru" and p["sku"] and p["sku"].strip().upper() in m22_skus:
            continue  # двойник на m22.ru уже проверен
        comps = comparables_for(conn, p["id"], min_conf=0.7)  # только точные модели, одинаковые фото и прямые аналоги с совместимыми характеристиками
        # исключаем дубли одного и того же предложения (один конкурент — одна цена на товар)
        uniq = {}
        for c in comps:
            uniq.setdefault((c["competitor_id"], c["name"]), c)
        comps = list(uniq.values())
        if len(comps) < config.MIN_COMPARABLES or len({c["competitor_id"] for c in comps}) < 2:
            continue  # нужны минимум 3 предложения минимум от 2 продавцов, иначе «рынок» — это один сайт
        prices = [c["price"] for c in comps]
        med = statistics.median(prices)
        gap = (p["price"] - med) / med * 100
        if abs(gap) < config.MARKET_GAP_THRESHOLD_PCT:
            continue
        avg_conf = sum(c["confidence"] for c in comps) / len(comps)
        # уверенность ограничена качеством сопоставлений: разнородные аналоги не дают >75% даже при большом числе предложений
        conf = round(min(avg_conf + 0.15, 0.45 + 0.05 * len(comps), 0.95), 2)
        above = gap > 0
        exact = sum(1 for c in comps if c["match_type"] in ("exact_model", "identical"))
        emitted.add(f"pvm:{p['id']}:{'above' if above else 'below'}")
        conn.execute("UPDATE signals SET status='done', comment='условие изменилось: теперь цена по другую сторону от рынка' WHERE dedupe_key=? AND status='new'",
                     (f"pvm:{p['id']}:{'below' if above else 'above'}",))
        m_norm = specmod.normalize(p["name"], p["description"], p["specs_json"], p["price"], p["capacity"])
        note = (f"{exact} точных совпадений модели, {len(comps) - exact} прямых аналогов. Правило: тот же тип изделия ({p['kind']}) и совместимые ключевые характеристики "
                f"(двусторонняя связь, класс диапазона, порядок дальности). Характеристики M22: {specmod_summary(m_norm)}. "
                f"Аналоги: " + "; ".join(f"{c['competitor_name'][:25]} — {c['name'][:40]} ({c['specs']}, {_fmt(c['price'])})" for c in comps[:6]) + ("…" if len(comps) > 6 else ""))
        if _emit(conn, type="m22_price_above_market" if above else "m22_price_below_market", severity="high" if abs(gap) >= 20 else "medium",
                 fact_kind="inference", category_slug=p["category_slug"], m22_product_id=p["id"],
                 title=f"«{p['name'][:60]}» {'дороже' if above else 'дешевле'} медианы рынка на {abs(gap):.0f}%",
                 what_happened=f"Цена M22 {_fmt(p['price'])} ({p['site']}); медиана {len(comps)} сопоставимых предложений — {_fmt(med)} ({note}).",
                 old_value=_fmt(med), new_value=_fmt(p["price"]), observed_at=db.now_iso(), period="текущий замер", source="сопоставление с конкурентами",
                 source_url=p["url"], evidence_json={"median": med, "comparables": comps, "m22_specs": specmod_summary(m_norm), "rule": "тот же тип изделия и совместимые ключевые характеристики (двусторонняя связь, класс диапазона, порядок дальности)"}, confidence=conf,
                 why_matters=("Премия к рынку без подтверждённого преимущества снижает конверсию B2B-запросов в заказы." if above
                              else "Цена ниже рынка — недополученная маржа, если спрос не падает."),
                 recommended_action=(f"Проверить обоснованность цены: либо снизить до диапазона {_fmt(med * 0.95)} – {_fmt(med * 1.05)}, либо явно показать в карточке преимущества (гарантия 2 года, поддержка, наличие)."
                                     if above else f"Проверить повышение цены на «{p['name'][:40]}»: медиана {len(comps)} конкурентов выше на {abs(gap):.0f}% ({_fmt(med)})."),
                 dedupe_key=f"pvm:{p['id']}:{'above' if above else 'below'}"):
            n += 1
    # сигналы, условие которых больше не выполняется (цена или рынок изменились), закрываем автоматически
    for old_sig in db.rows(conn, "SELECT id, dedupe_key FROM signals WHERE type IN ('m22_price_above_market','m22_price_below_market') AND status='new'"):
        if old_sig["dedupe_key"] not in emitted:
            conn.execute("UPDATE signals SET status='done', comment='закрыт автоматически: разрыв с рынком исчез или сопоставимых предложений стало меньше 3' WHERE id=?", (old_sig["id"],))
    return n


# ---------- 9–12. Спрос ----------
def _series(conn: sqlite3.Connection, query_id: int, source: str) -> list[tuple[str, float]]:
    return [(r["period_start"], r["value"]) for r in db.rows(conn, "SELECT period_start, value FROM demand_observations WHERE query_id=? AND source=? AND value IS NOT NULL ORDER BY period_start", (query_id, source))]


def detect_demand(conn: sqlite3.Connection) -> int:
    n = 0
    queries = db.rows(conn, "SELECT * FROM search_queries WHERE is_active=1")
    cat_growth: dict[str, list[float]] = {}
    for q in queries:
        for source in ("google_trends", "wordstat_import", "wordstat"):
            s = _series(conn, q["id"], source)
            if len(s) < 8:
                continue
            vals = [v for _, v in s]
            unit = "индекс" if source == "google_trends" else "показов"
            window = 4 if source == "google_trends" else 3  # недели для Trends, месяцы для Wordstat
            recent = vals[-window:]
            prev = vals[-2 * window:-window]
            if sum(prev) > 0 and len(prev) == window:
                a, b = sum(prev) / window, sum(recent) / window
                pct = (b - a) / a * 100 if a else 0
                cat_growth.setdefault(q["category_slug"] or "none", []).append(pct)
                if abs(pct) >= config.DEMAND_CHANGE_THRESHOLD_PCT and min(a, b) >= 5:
                    conf = 0.55 if source == "google_trends" else 0.8
                    up = pct > 0
                    if _emit(conn, type="demand_change", severity="medium" if abs(pct) >= 50 else "low", fact_kind="inference", category_slug=q["category_slug"], query_id=q["id"],
                             title=f"Спрос по «{q['query']}» {'вырос' if up else 'упал'} на {abs(pct):.0f}% ({source})",
                             what_happened=f"Среднее за последние {window} периодов: {b:.1f} {unit} против {a:.1f} за предыдущие {window}.", old_value=f"{a:.1f}", new_value=f"{b:.1f}",
                             observed_at=db.now_iso(), period=f"{s[-2 * window][0]} → {s[-1][0]}", source=source, source_url="https://trends.google.com/trends/explore?geo=RU&q=" + q["query"] if source == "google_trends" else None,
                             evidence_json={"series": s[-2 * window:]}, confidence=conf,
                             why_matters=("Относительный индекс Google Trends: показывает динамику, а не абсолютный объём. " if source == "google_trends" else "") +
                             (f"Категория «{_cat(q['category_slug'])}» " + ("есть в матрице M22 — рост спроса можно монетизировать рекламой и наличием." if up else "есть у M22 — падение спроса требует проверки плана закупок.")),
                             recommended_action=(f"Увеличить ставки/бюджет по запросу «{q['query']}» и проверить наличие товаров категории на складе." if up
                                                 else f"Не наращивать закупки по категории «{_cat(q['category_slug'])}» до подтверждения тренда вторым источником (Wordstat)."),
                             dedupe_key=f"demand:{q['id']}:{source}:{s[-1][0]}"):
                        n += 1
            # аномалия
            if len(vals) >= 13:
                base = vals[-13:-1]
                mu = statistics.mean(base)
                sd = statistics.pstdev(base) or 1.0
                z = (vals[-1] - mu) / sd
                if z >= config.ANOMALY_Z and vals[-1] >= 20 and mu >= 3:
                    if _emit(conn, type="demand_anomaly", severity="medium", fact_kind="fact", category_slug=q["category_slug"], query_id=q["id"],
                             title=f"Всплеск запроса «{q['query']}»: {vals[-1]:.0f} против среднего {mu:.0f} (z={z:.1f})",
                             what_happened=f"Последнее значение {vals[-1]:.0f} {unit} при среднем {mu:.1f} и σ={sd:.1f} за 12 предыдущих периодов.", old_value=f"{mu:.1f}", new_value=f"{vals[-1]:.0f}",
                             observed_at=db.now_iso(), period=s[-1][0], source=source, evidence_json={"series": s[-13:]}, confidence=0.6,
                             why_matters="Всплеск может означать событие (тендер, сезон, новость) — окно для быстрого предложения.",
                             recommended_action=f"Проверить в Wordstat/новостях причину всплеска по «{q['query']}» и, если это тендерный сезон, подготовить коммерческое предложение.",
                             dedupe_key=f"anom:{q['id']}:{source}:{s[-1][0]}"):
                        n += 1
    # рост категорий
    m22_cats = {r["category_slug"] for r in db.rows(conn, "SELECT DISTINCT category_slug FROM m22_products WHERE is_active=1 AND in_scope=1 AND category_slug IS NOT NULL")}
    for cat, pcts in cat_growth.items():
        if cat == "none" or len(pcts) < 3:
            continue
        avg = statistics.median(pcts)
        if avg < config.DEMAND_CHANGE_THRESHOLD_PCT:
            continue
        has = cat in m22_cats
        stamp = datetime.now(timezone.utc).strftime("%Y-%m")
        if _emit(conn, type="category_growth_existing" if has else "category_growth_gap", severity="medium" if has else "high", fact_kind="inference", category_slug=cat,
                 title=f"Категория «{_cat(cat)}» растёт: медианный рост спроса {avg:.0f}% по {len(pcts)} запросам" + ("" if has else " — товаров M22 нет"),
                 what_happened=f"Рост по {sum(1 for p in pcts if p > 0)} из {len(pcts)} запросов категории.", new_value=f"{avg:+.0f}%", observed_at=db.now_iso(), period="последние периоды vs предыдущие",
                 source="поисковый спрос", evidence_json={"pcts": pcts}, confidence=0.55,
                 why_matters="Рост категории — сигнал усилить продвижение (если товар есть) или открыть карточку гипотезы (если нет).",
                 recommended_action=(f"Запустить отдельную посадочную страницу/кампанию по категории «{_cat(cat)}»." if has else f"Открыть карточку гипотезы по категории «{_cat(cat)}» и собрать предложения конкурентов."),
                 dedupe_key=f"catgrowth:{cat}:{stamp}"):
            n += 1
    return n


# ---------- 13. Новый сценарий применения ----------
USE_CASES = {
    "промышленн": "промышленные экскурсии", "завод": "экскурсии на заводы", "производств": "экскурсии на производство", "музе": "музеи",
    "выставк": "выставки", "храм": "храмы и паломничество", "церк": "храмы", "школ": "школы и образование", "универс": "вузы", "конференц": "конференции",
    "делегац": "делегации", "перевод": "синхронный перевод", "фитнес": "фитнес и тренировки", "спорт": "спорт", "теплоход": "речные прогулки", "автобус": "автобусные туры",
    "пешеход": "пешеходные экскурсии", "медицин": "медицина и клиники", "суд": "суды", "тренинг": "тренинги", "аудит": "аудит и инспекции", "тих": "тихая дискотека",
    "дискотек": "тихая дискотека", "квест": "квесты", "аэропорт": "аэропорты", "вокзал": "вокзалы", "стройк": "стройплощадки", "дайв": "дайвинг", "верхов": "верховая езда",
}


def detect_new_use_cases(conn: sqlite3.Connection) -> int:
    n = 0
    m22_text = " ".join((r["name"] or "") + " " + (r["description"] or "") for r in db.rows(conn, "SELECT name, description FROM m22_products WHERE is_active=1 AND in_scope=1")).lower()
    comps = db.rows(conn, "SELECT cp.name, cp.description, cp.url, cp.image_url, cp.price, cp.category_slug, c.name AS competitor_name, c.id AS competitor_id FROM competitor_products cp JOIN competitors c ON c.id=cp.competitor_id WHERE cp.is_active=1")
    found: dict[str, list[dict]] = {}
    for cp in comps:
        text = ((cp["name"] or "") + " " + (cp["description"] or "")).lower()
        for k, label in USE_CASES.items():
            if k in text and k not in m22_text:
                found.setdefault(label, []).append(cp)
    for label, items in found.items():
        comp_names = sorted({i["competitor_name"] for i in items})
        if len(comp_names) < 2:
            continue
        cat = items[0]["category_slug"]
        items = sorted(items, key=lambda i: (not i.get("image_url"), _junk_url(i["url"])))  # сначала позиции с фото и настоящей страницей
        ev_items = [{"name": i["name"], "url": i["url"], "competitor": i["competitor_name"], "image": (i.get("image_url") if not _junk_url(i["url"]) else None), "price": i.get("price")} for i in items[:10]]
        conn.execute("UPDATE signals SET evidence_json=?, updated_at=datetime('now') WHERE dedupe_key=? AND status!='done'", (db.j({"items": ev_items, "photo": next((x["image"] for x in ev_items if x["image"]), None)}), f"usecase:{label}"))
        if _emit(conn, type="new_use_case", severity="medium", fact_kind="inference", category_slug=cat,
                 title=f"Сценарий «{label}» упоминают {len(comp_names)} конкурентов, M22 — нет",
                 what_happened=f"Упоминания в описаниях товаров: {', '.join(comp_names)}. Пример: {items[0]['name'][:80]}", new_value=f"{len(comp_names)} конкурентов",
                 observed_at=db.now_iso(), source="описания товаров конкурентов", source_url=items[0]["url"], evidence_json={"items": ev_items, "photo": next((x["image"] for x in ev_items if x["image"]), None)},
                 confidence=0.6, why_matters="Существующие радиогиды M22 закрывают этот сценарий, но он не отражён в описаниях и посадочных страницах — теряется поисковый трафик.",
                 recommended_action=f"Добавить сценарий «{label}» в описания SGTR02/SGTR03/SGTR13 и создать посадочную страницу под запрос «радиогид для {label.split()[0]}…».",
                 dedupe_key=f"usecase:{label}"):
            n += 1
    return n


# ---------- 15. Расхождения между сайтами ----------
def detect_cross_site(conn: sqlite3.Connection) -> int:
    n = 0
    a = db.rows(conn, "SELECT * FROM m22_products WHERE site='m22.ru' AND is_active=1 AND in_scope=1 AND price IS NOT NULL")
    b = db.rows(conn, "SELECT * FROM m22_products WHERE site='radiosync.ru' AND is_active=1 AND price IS NOT NULL")
    by_key: dict[tuple, list[dict]] = {}
    by_sku: dict[str, dict] = {}
    for p in b:
        by_key.setdefault((p["model_key"], p["capacity"]), []).append(p)
        if p["sku"]:
            by_sku.setdefault(p["sku"].strip().upper(), p)
    matched_b = set()
    emitted_xsite: set[str] = set()
    for p in a:
        q = None
        if p["sku"] and p["sku"].strip().upper() in by_sku:
            q = by_sku[p["sku"].strip().upper()]  # точное совпадение артикула — надёжнее всего
        else:
            if not p["model_key"] or p["parent_url"]:
                continue
            cands = [c for c in (by_key.get((p["model_key"], p["capacity"])) or []) if c["kind"] == p["kind"] and not c["parent_url"]]
            if not cands:
                continue
            if any(c["price"] == p["price"] for c in cands):
                continue  # есть двойник с той же ценой — расхождения нет
            def _sim(a, b):
                ta, tb = set(re.findall(r"[a-zа-яё0-9]+", a.lower())), set(re.findall(r"[a-zа-яё0-9]+", b.lower()))
                return len(ta & tb) / max(1, len(ta | tb))
            cands.sort(key=lambda c: -_sim(c["name"], p["name"]))
            if _sim(cands[0]["name"], p["name"]) < 0.5:
                continue
            q = cands[0]
        matched_b.add(q["id"])
        if q["price"] and p["price"] and abs(q["price"] - p["price"]) >= 1:
            diff = p["price"] - q["price"]
            # округление (70 300 против 70 200) — не расхождение; разные единицы (за штуку на m22.ru, за упаковку на radiosync.ru) — тоже
            if abs(diff) < 500 and abs(diff) / q["price"] < 0.01:
                continue
            from .categories import unit_price as _unit
            up, _ = _unit(p["name"], p["price"], p["category_slug"], p["kind"], p["capacity"])
            uq, _ = _unit(q["name"], q["price"], q["category_slug"], q["kind"], q["capacity"])
            if max(p["price"], q["price"]) / min(p["price"], q["price"]) > 5:
                if up and uq and max(up, uq) / min(up, uq) <= 1.05:
                    continue  # за штуку цены совпадают, отличается лишь фасовка
                continue  # цены отличаются более чем в 5 раз — это разные единицы измерения, а не расхождение
            emitted_xsite.add(f"xsite:{p['id']}:{q['id']}")
            if _emit(conn, type="cross_site_discrepancy", severity="medium" if abs(diff) / q["price"] >= 0.03 else "low", fact_kind="fact", category_slug=p["category_slug"], m22_product_id=p["id"],
                     title=f"Цена «{p['name'][:55]}» отличается: m22.ru {_fmt(p['price'])} vs radiosync.ru {_fmt(q['price'])}",
                     what_happened=f"Разница {_fmt(abs(diff))} ({diff / q['price'] * 100:+.1f}%). Модель {p['model_key']}, вместимость {p['capacity'] or '—'}.", old_value=_fmt(q["price"]), new_value=_fmt(p["price"]),
                     observed_at=db.now_iso(), period="текущий замер", source="m22.ru + radiosync.ru", source_url=p["url"], evidence_json={"m22": p["url"], "radiosync": q["url"]},
                     confidence=0.95, why_matters="Клиент, сравнивший два сайта компании, теряет доверие; прайс-листы для тендеров должны совпадать.",
                     recommended_action=f"Синхронизировать цену: установить одинаковую цену на m22.ru и radiosync.ru для «{p['name'][:40]}» (сейчас {_fmt(p['price'])} / {_fmt(q['price'])}).",
                     dedupe_key=f"xsite:{p['id']}:{q['id']}"):
                n += 1
    for old_sig in db.rows(conn, "SELECT id, dedupe_key FROM signals WHERE type='cross_site_discrepancy' AND status='new' AND dedupe_key LIKE 'xsite:%' AND dedupe_key NOT LIKE 'xsite-missing:%'"):
        if old_sig["dedupe_key"] not in emitted_xsite:
            conn.execute("UPDATE signals SET status='done', comment='закрыт автоматически: цены совпали или отличаются лишь округлением/фасовкой' WHERE id=?", (old_sig["id"],))
    # товары radiosync.ru, которых нет на m22.ru по ключу модели
    a_keys = {(p["model_key"]) for p in a if p["model_key"]}
    for q in b:
        if q["id"] in matched_b or not q["model_key"] or q["model_key"] in a_keys or q["parent_url"]:
            continue
        if _emit(conn, type="cross_site_discrepancy", severity="low", fact_kind="fact", category_slug=q["category_slug"], m22_product_id=q["id"],
                 title=f"«{q['name'][:60]}» есть на radiosync.ru, но не найден на m22.ru",
                 what_happened=f"Модель {q['model_key']} ({_fmt(q['price'])}) отсутствует в контуре каталога m22.ru.", new_value="только radiosync.ru", observed_at=db.now_iso(),
                 source="m22.ru + radiosync.ru", source_url=q["url"], confidence=0.7, why_matters="Разный ассортимент на двух сайтах — потеря продаж на основном магазине.",
                 recommended_action=f"Проверить, должен ли «{q['name'][:40]}» продаваться на m22.ru; если да — добавить карточку.", dedupe_key=f"xsite-missing:{q['id']}"):
            n += 1
    return n


# ---------- Ошибки источников ----------
def detect_source_errors(conn: sqlite3.Connection) -> int:
    n = 0
    for s in db.rows(conn, "SELECT * FROM sources WHERE consecutive_failures>=2"):
        if _emit(conn, type="source_error", severity="low", fact_kind="fact", title=f"Источник «{s['name']}» не работает ({s['consecutive_failures']} сбоев подряд)",
                 what_happened=(s["last_error"] or "")[:300], observed_at=db.now_iso(), source=s["key"], source_url=s["url"], confidence=1.0,
                 why_matters="Без источника часть сигналов не обновляется.", recommended_action="Проверить доступность сайта и настройки страницы мониторинга в разделе «Источники».",
                 dedupe_key=f"srcerr:{s['key']}"):
            n += 1
    pages = db.rows(conn, "SELECT mp.*, c.name AS cname FROM monitored_pages mp JOIN competitors c ON c.id=mp.competitor_id WHERE mp.fail_count>=3 AND mp.is_active=1 AND mp.last_status IN ('error','robots_disallowed')")
    for p in pages:
        if _emit(conn, type="source_error", severity="low", fact_kind="fact", competitor_id=p["competitor_id"], title=f"Страница {p['cname']} недоступна {p['fail_count']} раз подряд",
                 what_happened=(p["last_error"] or "")[:300], observed_at=db.now_iso(), source=p["cname"], source_url=p["url"], confidence=1.0,
                 why_matters="Цены этого конкурента не обновляются.", recommended_action="Открыть страницу вручную; при смене адреса — обновить URL страницы мониторинга.",
                 dedupe_key=f"pageerr:{p['id']}"):
            n += 1
    # источники и страницы, которые снова работают или выключены, — закрываем сигналы
    conn.execute("""UPDATE signals SET status='done', comment='закрыт автоматически: источник снова работает' WHERE type='source_error' AND status='new' AND dedupe_key LIKE 'srcerr:%'
                    AND substr(dedupe_key, 8) IN (SELECT key FROM sources WHERE consecutive_failures=0)""")
    conn.execute("""UPDATE signals SET status='done', comment='закрыт автоматически: страница снова доступна или выключена' WHERE type='source_error' AND status='new' AND dedupe_key LIKE 'pageerr:%'
                    AND CAST(substr(dedupe_key, 9) AS INTEGER) NOT IN (SELECT id FROM monitored_pages WHERE fail_count>=3 AND is_active=1 AND last_status IN ('error','robots_disallowed'))""")
    return n


def retire_moved_types(conn: sqlite3.Connection) -> int:
    """Темы, у которых появился свой раздел (цены против рынка, расхождения между сайтами, ошибки сбора),
    в сигналах не дублируются: открытые сигналы этих типов закрываются с пометкой, куда смотреть."""
    n = 0
    for t, where in MOVED_TYPES.items():
        cur = conn.execute("UPDATE signals SET status='done', comment=?, updated_at=datetime('now') WHERE type=? AND status IN ('new','in_research','accepted')",
                           (f"перенесено: теперь это {where}", t))
        n += cur.rowcount
    return n


ACTIVE_SIGNAL_TYPES = {k: v for k, v in SIGNAL_TYPES.items() if k not in MOVED_TYPES}


def run_all(conn: sqlite3.Connection) -> dict:
    if not db.get_setting(conn, "baseline_date"):
        db.set_setting(conn, "baseline_date", db.now_iso())
    retire_moved_types(conn)
    stats = {
        "competitor_price_change": detect_competitor_price_changes(conn),
        "new_category": detect_new_categories(conn),
        "multi_competitor": detect_multi_competitor_products(conn),
        "demand": detect_demand(conn),
        "new_use_case": detect_new_use_cases(conn),
    }
    conn.commit()
    log.info("signals: %s", stats)
    return stats
