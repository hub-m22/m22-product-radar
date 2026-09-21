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
            # одно предложение = один продавец (сайты одной группы — один продавец) + одна модель, берётся низшая цена:
            # тот же товар на cromi.ru, sin24.ru и spbaudio.ru или две страницы одной модели не должны входить в медиану дважды
            u: dict = {}
            for c in sorted(lst, key=lambda c: c["price"]):
                u.setdefault((c["seller_key"], c.get("model_key") or c["name"]), c)
            return sorted(u.values(), key=lambda c: c["price"])

        firm = _uniq(matching.comparables_for(conn, p["id"], min_conf=0.7))     # надёжно: идентичные, точные, одинаковое фото, прямые аналоги с совместимыми характеристиками
        approx = False
        comps = firm
        if len(firm) < config.MIN_COMPARABLES or len({c["seller_key"] for c in firm}) < 2:
            loose = _uniq(matching.comparables_for(conn, p["id"], min_conf=0.6))  # ориентир: прямые аналоги той же категории и типа
            if len(loose) >= config.MIN_COMPARABLES:
                comps, approx = loose, True
        sellers = {c["seller_key"] for c in comps}
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
        row["reco"] = recommended_price(verdict, p["price"], med, approx)
        out.append(row)
    order = {"lower": 0, "raise": 1, "keep": 2, "no_data": 3}
    out.sort(key=lambda r: (order[r["verdict"]], -(abs(r["gap"] or 0))))
    return out


def round_price(v: float) -> float:
    """Красивая розничная цена: до 1 000 ₽ — шаг 10, до 10 000 — шаг 50, выше — шаг 100."""
    step = 10 if v < 1000 else (50 if v < 10000 else 100)
    return float(round(v / step) * step)


RAISE_STEP_MAX = 0.25         # за один шаг цену не поднимаем больше чем на 25 %
RAISE_STEP_MAX_APPROX = 0.15  # если вывод по аналогам, а не по идентичным моделям — не больше 15 %


def recommended_price(verdict: str, price: float, median: float | None, approx: bool = False) -> float | None:
    """Рекомендованная цена M22.

    - снизить: к медиане рынка (покупатель сравнивает именно с ней);
    - поднять: 95 % медианы — остаёмся чуть ниже рынка, но за один шаг не больше +25 % (по аналогам — +15 %) и не меньше текущей;
    - держать: текущая цена; мало данных — рекомендации нет.
    """
    if not median:
        return None
    if verdict == "lower":
        return round_price(median)
    if verdict == "raise":
        cap = price * (1 + (RAISE_STEP_MAX_APPROX if approx else RAISE_STEP_MAX))
        return max(price, round_price(min(median * 0.95, cap)))
    if verdict == "keep":
        return price
    return None


def report_html(rows: list[dict], decisions: dict | None = None, title: str = "Рекомендации по ценам M22") -> str:
    """Отчёт с рекомендованными ценами и ссылками — HTML, который Google Диск открывает как Google Документ."""
    import html
    from datetime import datetime

    decisions = decisions or {}
    order = [("lower", "Снизить или обосновать — дороже рынка"), ("raise", "Поднять — дешевле рынка"), ("keep", "Держать — в рынке")]
    names = {"approved": "Согласовано", "accepted": "Принято в исполнение", "rejected": "Отклонено", "deferred": "Отложено"}

    def money(v):
        return "—" if v is None else f"{v:,.0f} ₽".replace(",", " ")

    def esc(s):
        return html.escape(str(s or ""))

    s = summary(rows)
    parts = [f"<html><head><meta charset='utf-8'><title>{esc(title)}</title></head><body style='font-family:Arial,sans-serif;font-size:11pt'>",
             f"<h1>{esc(title)}</h1>",
             f"<p>Дата: {datetime.now().strftime('%d.%m.%Y %H:%M')}. Товаров с выводом: снизить {s['lower']}, поднять {s['raise']}, держать {s['keep']}; мало данных — {s['no_data']}.</p>",
             "<p>Как считается: по каждому товару M22 берутся только надёжные сопоставления (идентичные и точные модели, одинаковое фото, прямые аналоги с совместимыми характеристиками), "
             "минимум 3 предложения от 2+ продавцов (сайты одной группы — один продавец). Медиана рынка — середина их цен. Порог отклонения 10 %. "
             "Рекомендованная цена: «снизить» — к медиане рынка; «поднять» — 95 % медианы, чуть ниже рынка, но за один шаг не больше +25 % (по аналогам +15 %); «держать» — текущая. "
             "Вердикты с пометкой «ориентировочно» построены по аналогам того же типа без идентичных моделей — перед изменением цены проверьте состав аналогов по ссылкам.</p>"]
    for key, head in order:
        sect = [r for r in rows if r["verdict"] == key]
        if not sect:
            continue
        parts.append(f"<h2>{esc(head)} ({len(sect)})</h2>")
        parts.append("<table border='1' cellspacing='0' cellpadding='4' style='border-collapse:collapse;font-size:10pt'>"
                     "<tr><th>Товар M22</th><th>Сейчас</th><th>Медиана рынка</th><th>Рекомендовано</th><th>Изменение</th><th>Предл./продавцов</th><th>Решение</th></tr>")
        for r in sect:
            p = r["p"]
            reco = r.get("reco")
            delta = "" if reco is None or reco == p["price"] else f"{reco - p['price']:+,.0f} ₽ ({(reco - p['price']) / p['price'] * 100:+.0f} %)".replace(",", " ")
            d = decisions.get(p["id"])
            dtxt = f"{names.get(d['status'], d['status'])} · {d['author'] or ''} · {d['updated_at'][:10]}" if d else ""
            parts.append(f"<tr><td><a href='{esc(p['url'])}'>{esc(p['name'])}</a><br><small>{esc(p['site'])}{' · ' + esc(p['model_key']) if p['model_key'] else ''}</small></td>"
                         f"<td>{money(p['price'])}</td><td>{money(r['median'])}<br><small>{money(r['pmin'])} – {money(r['pmax'])}</small></td>"
                         f"<td><b>{money(reco)}</b></td><td>{esc(delta)}</td><td>{r['n']} / {r['sellers']}</td><td>{esc(dtxt)}</td></tr>")
            offers = "; ".join(f"<a href='{esc(c['url'])}'>{esc(c['competitor_name'][:30])}: {esc(c['name'][:60])}</a> — {money(c['price'])}"
                               + (" (маркетплейс, цена динамическая)" if c.get("competitor_tier") == "M" else "") + (f" ({esc(c['price_range'])})" if c.get("price_range") else "")
                               for c in r["comps"][:6])
            parts.append(f"<tr><td colspan='7' style='background:#f7f7f7'><small><b>Почему:</b> {esc(r['why'])}{'; ' + esc(r['verdict_ru']) if 'ориентировочно' in r['verdict_ru'] else ''}.<br><b>Предложения:</b> {offers}</small></td></tr>")
        parts.append("</table>")
    parts.append("</body></html>")
    return "\n".join(parts)


def summary(rows: list[dict]) -> dict:
    s = {"lower": 0, "raise": 0, "keep": 0, "no_data": 0, "total": len(rows)}
    for r in rows:
        s[r["verdict"]] += 1
    return s
