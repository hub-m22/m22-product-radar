"""Категорийный разрез: наши модели, модели конкурентов, ценовое положение M22 и вердикт по категории."""
from __future__ import annotations

import re
import sqlite3
import statistics

from . import db
from .normalize import CATEGORY_NAMES

PACK_RE = re.compile(r"(?:(\d{2,5})\s*(?:шт|pcs|штук))|(?:уп\w*\s*(?:по|из|на)?\s*(\d{2,5}))|(?:[xх×]\s*(\d{2,5})\b)", re.I)
UNIT_CATEGORIES = {"disposable_headphones", "reusable_headphones"}
KIND_LABELS = {"system": "Системы и комплекты", "receiver": "Приёмники", "transmitter": "Передатчики", "headphones": "Наушники", "microphone": "Микрофоны",
               "case_charger": "Кейсы и зарядка", "audioguide": "Аудиогиды", "kit": "Готовые решения", "accessory": "Аксессуары", "rental": "Аренда", "other": "Прочее"}


def pack_qty(name: str) -> int | None:
    m = PACK_RE.search(name or "")
    if not m:
        return None
    q = next((g for g in m.groups() if g), None)
    return int(q) if q and int(q) >= 2 else None


def unit_price(name: str, price: float | None, category: str | None, kind: str | None = None, capacity: int | None = None) -> tuple[float | None, int | None]:
    """Цена за единицу: наушники — за штуку (цена упаковки / количество); системы и комплекты — за одно место (цена / вместимость)."""
    if price is None:
        return None, None
    if kind in ("system", "kit"):
        if capacity and capacity >= 2:
            return round(price / capacity, 2), capacity
        return None, None  # комплект без известной вместимости несопоставим по цене
    q = pack_qty(name) if category in UNIT_CATEGORIES else None
    if q and price / q >= 1:
        return round(price / q, 2), q
    if category == "disposable_headphones" and price > 500:
        return None, None  # цена явно за упаковку, а количество в названии не указано — за штуку неизвестно
    return price, None


def _pos(m22_min: float | None, comp_prices: list[float]) -> dict:
    if m22_min is None or not comp_prices:
        return {"cheaper_share": None, "median": None, "min": None, "gap_min_pct": None, "gap_median_pct": None}
    med = statistics.median(comp_prices)
    mn = min(comp_prices)
    cheaper = sum(1 for p in comp_prices if p < m22_min)
    return {"cheaper_share": round(cheaper / len(comp_prices) * 100), "median": med, "min": mn,
            "gap_min_pct": round((m22_min - mn) / mn * 100), "gap_median_pct": round((m22_min - med) / med * 100)}


def summary(conn: sqlite3.Connection, slug: str) -> dict:
    m22 = db.rows(conn, "SELECT * FROM m22_products WHERE is_active=1 AND in_scope=1 AND parent_url IS NULL AND category_slug=? ORDER BY price", (slug,))
    for p in m22:
        p["unit_price"], p["pack_qty"] = unit_price(p["name"], p["price"], slug, p["kind"], p["capacity"])
    offers = db.rows(conn, """SELECT cp.*, c.name AS competitor_name, COALESCE(c.group_name, c.name) AS seller, c.website,
                              (SELECT m.name FROM product_matches pm JOIN m22_products m ON m.id=pm.m22_product_id WHERE pm.competitor_product_id=cp.id AND pm.review_status!='rejected' ORDER BY pm.confidence DESC LIMIT 1) AS matched_m22,
                              (SELECT pm.match_type FROM product_matches pm WHERE pm.competitor_product_id=cp.id AND pm.review_status!='rejected' ORDER BY pm.confidence DESC LIMIT 1) AS match_type
                              FROM competitor_products cp JOIN competitors c ON c.id=cp.competitor_id
                              WHERE cp.is_active=1 AND c.is_active=1 AND cp.category_slug=? ORDER BY cp.price""", (slug,))
    for o in offers:
        o["unit_price"], o["pack_qty"] = unit_price(o["name"], o["price"], slug, o["kind"], o["capacity"])
    priced = [o for o in offers if o["unit_price"] and o["unit_price"] >= 1]
    sellers = sorted({o["seller"] for o in offers})
    # ценовое положение по типу изделия: сравниваем M22 min с предложениями конкурентов того же типа
    kinds = {}
    for p in m22:
        if p["unit_price"]:
            kinds.setdefault(p["kind"], {"m22": [], "comp": []})["m22"].append(p)
    for o in priced:
        if o["kind"] in kinds:
            kinds[o["kind"]]["comp"].append(o)
    positions = []
    for kind, d in kinds.items():
        if not d["comp"]:
            continue
        m22_min = min(p["unit_price"] for p in d["m22"])
        pos = _pos(m22_min, [o["unit_price"] for o in d["comp"]])
        cheaper = sorted([o for o in d["comp"] if o["unit_price"] < m22_min], key=lambda o: o["unit_price"])
        positions.append({"kind": kind, "m22_min": m22_min, "m22_n": len(d["m22"]), "comp_n": len(d["comp"]), **pos, "cheaper": cheaper[:8]})
    high = db.rows(conn, "SELECT * FROM signals WHERE category_slug=? AND status NOT IN ('rejected','done') ORDER BY CASE severity WHEN 'high' THEN 0 WHEN 'medium' THEN 1 ELSE 2 END, confidence DESC LIMIT 20", (slug,))
    recs = db.rows(conn, "SELECT * FROM recommendations WHERE category_slug=? AND status IN ('new','accepted','in_progress') ORDER BY priority", (slug,))
    hyps = db.rows(conn, "SELECT * FROM hypotheses WHERE category_slug=? AND decision_status IN ('new','research') ORDER BY created_at DESC", (slug,))
    tenders = db.rows(conn, "SELECT * FROM market_observations WHERE category_slug=? AND kind='tender' ORDER BY observed_date DESC LIMIT 15", (slug,))
    # вердикт
    if slug == "rental":
        status, verdict = "no_data", f"Аренда: у конкурентов цены за приёмник в день, у M22 — минимальная сумма заказа; автоматически несопоставимо. Предложений: {len(offers)} от {len(sellers)} продавцов."
    elif not m22:
        status, verdict = "gap", f"У M22 нет товаров в категории; у конкурентов {len(offers)} предложений от {len(sellers)} продавцов."
    elif len(priced) < 3:
        status, verdict = "no_data", f"Предложений конкурентов с ценой мало ({len(priced)}) — вывод о ценовом положении пока не делается."
    else:
        worst = max(positions, key=lambda p: (p["cheaper_share"] or 0), default=None)
        n_high = sum(1 for s in high if s["severity"] == "high")
        kl = KIND_LABELS.get(worst["kind"], worst["kind"]) if worst else ""
        unit = "за место в комплекте" if worst and worst["kind"] in ("system", "kit") else "за ед."
        if not positions:
            status, verdict = "no_data", "Нет пар «модель M22 — предложение конкурента того же типа» с ценами (для комплектов нужна вместимость в названии)."
        elif (worst["cheaper_share"] or 0) >= 50:
            status = "attention"
            verdict = (f"{kl}: {worst['cheaper_share']}% предложений конкурентов дешевле самой дешёвой модели M22 ({worst['m22_min']:,.0f} ₽ {unit}); "
                       f"минимум рынка {worst['min']:,.0f} ₽, медиана {worst['median']:,.0f} ₽.").replace(",", " ")
        elif (worst["cheaper_share"] or 0) >= 20:
            status = "watch"
            verdict = (f"{kl}: дешевле M22 только {worst['cheaper_share']}% предложений (минимум {worst['min']:,.0f} ₽ против {worst['m22_min']:,.0f} ₽ у M22 {unit}); "
                       f"медиана рынка {worst['median']:,.0f} ₽.").replace(",", " ")
        else:
            status = "ok"
            verdict = (f"{kl}: самая дешёвая модель M22 ({worst['m22_min']:,.0f} ₽ {unit}) ниже {100 - (worst['cheaper_share'] or 0)}% предложений конкурентов; "
                       f"медиана рынка {worst['median']:,.0f} ₽.").replace(",", " ")
        if n_high:
            verdict += f" Сигналов высокой важности: {n_high}."
    return {"slug": slug, "name": CATEGORY_NAMES.get(slug, slug), "m22": m22, "offers": offers, "priced": priced, "sellers": sellers, "positions": positions,
            "signals": high, "recs": recs, "hyps": hyps, "tenders": tenders, "status": status, "verdict": verdict,
            "no_price": [o for o in offers if not o["unit_price"]]}


def overview(conn: sqlite3.Connection) -> list[dict]:
    out = []
    for c in db.rows(conn, "SELECT slug, name_ru, in_scope FROM categories ORDER BY sort_order"):
        s = summary(conn, c["slug"])
        if not s["m22"] and not s["offers"] and not s["signals"]:
            continue
        worst = max(s["positions"], key=lambda p: (p["cheaper_share"] or 0), default=None)
        out.append({"slug": c["slug"], "name": c["name_ru"], "in_scope": c["in_scope"], "m22_n": len(s["m22"]),
                    "m22_min": min((p["unit_price"] for p in s["m22"] if p["unit_price"]), default=None), "m22_max": max((p["unit_price"] for p in s["m22"] if p["unit_price"]), default=None),
                    "sellers": len(s["sellers"]), "offers": len(s["offers"]), "priced": len(s["priced"]), "comp_min": min((o["unit_price"] for o in s["priced"]), default=None),
                    "comp_median": statistics.median([o["unit_price"] for o in s["priced"]]) if s["priced"] else None,
                    "cheaper_share": worst["cheaper_share"] if worst else None, "signals": len(s["signals"]), "high": sum(1 for x in s["signals"] if x["severity"] == "high"),
                    "recs": len(s["recs"]), "tenders": len(s["tenders"]), "status": s["status"], "verdict": s["verdict"]})
    return out
