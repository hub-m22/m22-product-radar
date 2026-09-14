"""Ассортимент: что ввести. Полный разбор моделей конкурентов против матрицы M22.

Не «сколько продавцов держат модель» (в этом рынке почти каждый продавец торгует своим брендом), а по существу:
для каждой модели конкурента (бренд + код модели, иначе название) той же категории и типа изделия —
есть ли у M22 такое вообще, а если есть, дешевле ли конкурент за единицу/место и лучше ли по характеристикам.
"""
from __future__ import annotations

import re
import sqlite3
import statistics
from collections import defaultdict

from . import categories as catmod
from . import db
from . import specs as specmod
from .matching import KIND_GROUPS, spec_summary
from .normalize import CATEGORY_NAMES

KIND_LABELS = catmod.KIND_LABELS
VERDICTS = {"gap": "нет у M22 — кандидат на ввод", "consider": "есть аналог, но конкурент выигрывает — рассмотреть", "covered": "у M22 есть аналог не хуже", "no_data": "мало данных"}
SKIP_KINDS = ("rental", "other")


def _norm_name(name: str) -> str:
    n = re.sub(r"[^a-zа-я0-9]+", " ", (name or "").lower())
    n = re.sub(r"\b(радиогид|аудиогид|система|комплект|для|экскурс\w*|шт|штук|на|персон|человек|чел)\b", " ", n)
    return re.sub(r"\s+", " ", n).strip()[:60]


def _group_key(r: dict) -> tuple:
    return ((r["brand"] or "").lower(), r["model_key"] or _norm_name(r["name"]), r["category_slug"], KIND_GROUPS.get(r["kind"] or "other", "other"))


def candidates(conn: sqlite3.Connection, categories: list[str] | None = None, tiers: tuple[str, ...] = ("A", "B", "M")) -> list[dict]:
    m22 = db.rows(conn, "SELECT id, name, price, capacity, category_slug, kind, url, images_json, description, specs_json, model_key, brand FROM m22_products WHERE is_active=1 AND in_scope=1 AND price IS NOT NULL")
    for m in m22:
        m["norm"] = specmod.normalize(m["name"], m["description"], m["specs_json"], m["price"], m["capacity"])
        imgs = db.uj(m["images_json"], []) or []
        img = imgs[0] if imgs and isinstance(imgs[0], str) else None
        m["image"] = (img if img and img.startswith("http") else ("https://m22.ru" + img if img else None))
        m["kg"] = KIND_GROUPS.get(m["kind"] or "other", "other")
        m["unit"], _ = catmod.unit_price(m["name"], m["price"], m["category_slug"], m["kind"], m["capacity"])
    m22_by = defaultdict(list)
    for m in m22:
        m22_by[(m["category_slug"], m["kg"])].append(m)
    m22_cats = {m["category_slug"] for m in m22}
    m22_keys = {m["model_key"] for m in m22 if m["model_key"]}
    aliases = db.uj(db.get_setting(conn, "model_aliases"), {}) or {}
    m22_keys |= {a for a, b in aliases.items() if b in m22_keys}

    where = ["cp.is_active=1", "co.is_active=1", "cp.price IS NOT NULL", "cp.price>=10", "cp.category_slug IS NOT NULL", "cp.category_slug!='rental'", "COALESCE(cp.currency,'RUB')='RUB'"]
    params: list = []
    if categories:
        where.append(f"cp.category_slug IN ({','.join('?' * len(categories))})")
        params += categories
    where.append(f"co.tier IN ({','.join('?' * len(tiers))})")
    params += list(tiers)
    rows = db.rows(conn, f"""SELECT cp.*, COALESCE(co.group_name, co.name) AS seller, co.tier FROM competitor_products cp JOIN competitors co ON co.id=cp.competitor_id
                             WHERE {' AND '.join(where)}""", params)
    groups: dict[tuple, list[dict]] = defaultdict(list)
    for r in rows:
        low = (r["name"] or "").lower() + " " + (r["brand"] or "").lower()
        if "radiosync" in low or "kromix" in low:
            continue  # перепродажа собственного бренда
        if r["kind"] in SKIP_KINDS:
            continue
        groups[_group_key(r)].append(r)

    out = []
    for (brand, key, cat, kg), items in groups.items():
        sellers = sorted({i["seller"] for i in items})
        prices = [i["price"] for i in items]
        for i in items:
            i["norm"] = specmod.normalize(i["name"], i["description"], i["specs_json"], i["price"], i["capacity"])
            i["unit"], i["pack"] = catmod.unit_price(i["name"], i["price"], i["category_slug"], i["kind"], i["capacity"])
        cap = next((i["capacity"] for i in sorted(items, key=lambda x: x["price"]) if i["capacity"]), None)
        units = [i["unit"] for i in items if i["unit"]]
        # ближайшие модели M22: та же категория и тип; для комплектов — та же/близкая вместимость
        pool = m22_by.get((cat, kg), [])
        kind_gap = False
        if kg == "system" and cap:
            near = [m for m in pool if m["capacity"] and abs(m["capacity"] - cap) <= max(2, 0.2 * cap)]
            if pool and not near:
                kind_gap = True  # у M22 есть такие системы, но нет комплекта на столько мест
            pool = near or []
        elif not pool and cat in m22_cats:
            if kg in ("system", "audioguide", "voice_amp"):
                kind_gap = True  # основной тип изделия, которого у M22 в этой категории нет
            elif kg in ("headphones", "microphone", "accessory"):
                # наушники/микрофоны/кейсы у M22 лежат в своих категориях — сравниваем с изделиями того же типа из любой категории
                pool = [m for m in m22 if m["kg"] == kg] or [m for m in m22 if m["category_slug"] == cat]
            else:
                # приёмник/передатчик: тип у конкурента часто определён по названию неточно — сравниваем со всей категорией M22
                pool = [m for m in m22 if m["category_slug"] == cat]
        pool = sorted(pool, key=lambda m: m["price"])
        offers_n = [{"name": i["name"], "price": i["unit"] or i["price"], "norm": i["norm"]} for i in items]
        m22_n = [{"name": m["name"], "price": m["unit"] or m["price"], "norm": m["norm"]} for m in pool]
        cat_in = cat in m22_cats
        just = specmod.justify(offers_n, m22_n, cat_in)
        if kg in ("accessory", "headphones", "microphone"):
            just["reasons"] = [x for x in just["reasons"] if x["kind"] in ("price", "new_group")]  # у аксессуаров и наушников дальность/каналы не сравниваем
        reasons = [x["text"] for x in just["reasons"]]
        if not cat_in:
            verdict = "gap"
            reasons.insert(0, f"категории «{CATEGORY_NAMES.get(cat, cat)}» у M22 нет")
        elif kind_gap:
            verdict = "gap"
            reasons.insert(0, (f"у M22 есть {KIND_LABELS.get(kg, kg).lower()} в категории «{CATEGORY_NAMES.get(cat, cat)}», но нет комплекта на {cap} мест" if kg == "system" and cap
                               else f"в категории «{CATEGORY_NAMES.get(cat, cat)}» у M22 нет изделий типа «{KIND_LABELS.get(kg, kg)}»"))
        elif not pool:
            verdict = "no_data"
            reasons.insert(0, "у M22 в этой категории нет товаров с ценой для сравнения")
        elif key.upper() in m22_keys:
            verdict = "covered"
            reasons.insert(0, "это та же модель, что у M22 (идентичное оборудование)")
        elif just["reasons"]:
            verdict = "consider"
        else:
            verdict = "covered"
        best = min(items, key=lambda x: x["price"])
        m22_best = pool[0] if pool else None
        unit_min = min(units) if units else None
        m22_unit = m22_best["unit"] if m22_best and m22_best["unit"] else (m22_best["price"] if m22_best else None)
        gap_pct = round((unit_min - m22_unit) / m22_unit * 100) if unit_min and m22_unit else None
        score = {"gap": 3.0, "consider": 2.0, "covered": 0.0, "no_data": 0.5}[verdict] + 0.5 * len(sellers) + 0.3 * len(just["reasons"])
        if not (items[0]["brand"] or items[0]["model_key"]):
            score -= 1.0  # безымянная позиция без кода модели — ниже в списке
        out.append({
            "brand": items[0]["brand"] or "", "model": key if items[0]["model_key"] else "", "name": best["name"], "category": cat, "category_name": CATEGORY_NAMES.get(cat, cat),
            "kind": kg, "kind_name": KIND_LABELS.get(kg, kg), "capacity": cap, "sellers": sellers, "n": len(items), "pmin": min(prices), "pmed": statistics.median(prices), "pmax": max(prices),
            "unit_min": unit_min, "image": next((i["image_url"] for i in sorted(items, key=lambda x: x["price"]) if i["image_url"]), None),
            "specs": spec_summary(max(items, key=lambda i: sum(v is not None for v in i["norm"].values()))["norm"]),
            "offers": sorted(items, key=lambda x: x["price"])[:4], "m22_best": m22_best, "m22_unit": m22_unit, "gap_pct": gap_pct, "m22_pool": len(pool),
            "reasons": reasons, "verdict": verdict, "verdict_ru": VERDICTS[verdict], "score": score, "tier": min(i["tier"] for i in items),
            "marketplace_only": all(i["tier"] == "M" for i in items),
        })
    out.sort(key=lambda x: (-x["score"], x["category_name"], x["pmin"]))
    return out


def summary(rows: list[dict]) -> dict:
    s = {"gap": 0, "consider": 0, "covered": 0, "no_data": 0, "total": len(rows)}
    for r in rows:
        s[r["verdict"]] += 1
    by_cat: dict[str, dict] = {}
    for r in rows:
        d = by_cat.setdefault(r["category"], {"name": r["category_name"], "gap": 0, "consider": 0, "covered": 0, "no_data": 0})
        d[r["verdict"]] += 1
    s["by_cat"] = sorted(by_cat.values(), key=lambda d: -(d["gap"] * 3 + d["consider"]))
    return s
