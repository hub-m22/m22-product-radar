"""Пересмотр цен: по каждому товару M22 — где рынок, и что делать с ценой (поднять / снизить / держать).

Считается только по надёжным сопоставлениям: идентичные модели, точные модели, одинаковое фото и прямые аналоги
с совместимыми характеристиками (уверенность ≥ 0.7). Нужны минимум 3 предложения минимум от 2 продавцов.
"""
from __future__ import annotations

import sqlite3
import statistics

from . import config, db, matching
from .normalize import CATEGORY_NAMES

STRONG = ("identical", "exact_model")


def review(conn: sqlite3.Connection, categories: list[str] | None = None, only_with_market: bool = False) -> list[dict]:
    where = ["p.is_active=1", "p.in_scope=1", "p.parent_url IS NULL", "p.price IS NOT NULL"]
    params: list = []
    if categories:
        where.append(f"p.category_slug IN ({','.join('?' * len(categories))})")
        params += categories
    prods = db.rows(conn, f"SELECT * FROM m22_products p WHERE {' AND '.join(where)} ORDER BY p.category_slug, p.model_key, p.site", params)
    out = []
    for p in prods:
        def _uniq(lst):
            u: dict = {}
            for c in lst:
                u.setdefault((c["competitor_id"], c["name"]), c)
            return sorted(u.values(), key=lambda c: c["price"])

        firm = _uniq(matching.comparables_for(conn, p["id"], min_conf=0.7))     # надёжно: идентичные, точные, одинаковое фото, прямые аналоги с совместимыми характеристиками
        approx = False
        comps = firm
        if len(firm) < config.MIN_COMPARABLES or len({c["competitor_id"] for c in firm}) < 2:
            loose = _uniq(matching.comparables_for(conn, p["id"], min_conf=0.6))  # ориентир: прямые аналоги той же категории и типа
            if len(loose) >= config.MIN_COMPARABLES:
                comps, approx = loose, True
        sellers = {c["competitor_id"] for c in comps}
        strong = [c for c in comps if c["match_type"] in STRONG]
        row = {"p": p, "category": CATEGORY_NAMES.get(p["category_slug"], p["category_slug"]), "n": len(comps), "sellers": len(sellers), "strong": len(strong), "comps": comps[:8], "approx": approx}
        # для идентичных/точных моделей достаточно двух предложений: это тот же товар, а не «похожий»
        need = 2 if (strong and len(strong) == len(comps)) else config.MIN_COMPARABLES
        if len(comps) < need:
            row.update(verdict="no_data", verdict_ru="мало данных", gap=None, median=None, pmin=None, pmax=None,
                       why=f"сопоставимых предложений: {len(comps)}; нужно минимум {config.MIN_COMPARABLES}")
            if only_with_market:
                continue
            out.append(row)
            continue
        prices = [c["price"] for c in comps]
        med = statistics.median(prices)
        gap = (p["price"] - med) / med * 100
        strong_med = statistics.median([c["price"] for c in strong]) if strong else None
        if gap >= config.MARKET_GAP_THRESHOLD_PCT:
            verdict, ru = "lower", "снизить или обосновать"
            why = f"дороже медианы рынка на {gap:.0f} %; диапазон рынка {min(prices):,.0f}–{max(prices):,.0f} ₽"
        elif gap <= -config.MARKET_GAP_THRESHOLD_PCT:
            verdict, ru = "raise", "поднять"
            why = f"дешевле медианы рынка на {abs(gap):.0f} %; можно поднять до {med * 0.95:,.0f}–{med:,.0f} ₽ без потери позиции"
        else:
            verdict, ru = "keep", "держать"
            why = f"в рынке (отклонение {gap:+.0f} %)"
        if strong_med is not None and strong:
            sg = (p["price"] - strong_med) / strong_med * 100
            why += f"; по идентичным/точным моделям ({len(strong)}) медиана {strong_med:,.0f} ₽ ({sg:+.0f} %)"
        if approx:
            ru += " (ориентировочно)"
            why += "; ориентир по аналогам той же категории и типа, а не по идентичным моделям — проверьте предложения справа"
        elif len(sellers) < 2:
            why += "; все предложения от одного продавца"
        row.update(verdict=verdict, verdict_ru=ru, gap=round(gap), median=med, pmin=min(prices), pmax=max(prices), strong_median=strong_med,
                   why=why.replace(",", " "), target_low=med * 0.95, target_high=med * 1.05)
        out.append(row)
    order = {"lower": 0, "raise": 1, "keep": 2, "no_data": 3}
    out.sort(key=lambda r: (order[r["verdict"]], -(abs(r["gap"] or 0))))
    return out


def summary(rows: list[dict]) -> dict:
    s = {"lower": 0, "raise": 0, "keep": 0, "no_data": 0, "total": len(rows)}
    for r in rows:
        s[r["verdict"]] += 1
    return s
