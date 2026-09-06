"""Сопоставление товаров конкурентов с товарной матрицей M22.

Типы сопоставления: exact_model | direct_analog | functional | kit | accessory | substitute | adjacent | new_category.
Учитываются бренд, ключ модели, категория, тип (система/приёмник/…), вместимость, цена. Сомнительные — на ручную проверку.
"""
from __future__ import annotations

import sqlite3

from . import db
from . import specs as specmod


def _band_class(b):
    if not b:
        return None
    return "digital" if b == "2.4 ГГц" else ("ir" if b == "ИК" else "analog")


def spec_summary(norm: dict) -> str:
    parts = []
    if norm.get("freq_band"):
        parts.append(norm["freq_band"])
    if norm.get("range_m"):
        parts.append(f"до {norm['range_m']:g} м")
    if norm.get("channels"):
        parts.append(f"{norm['channels']:g} кан.")
    if norm.get("battery_h"):
        parts.append(f"{norm['battery_h']:g} ч")
    parts.append("двусторонняя связь" if norm.get("two_way") else "без двусторонней связи")
    return ", ".join(parts)


def feature_compat(m_norm: dict, c_norm: dict) -> tuple[float, list[str]]:
    """Штраф к уверенности и причины, если ключевые характеристики несовместимы."""
    penalty, reasons = 0.0, []
    if m_norm.get("two_way") and not c_norm.get("two_way"):
        penalty += 0.35
        reasons.append("у M22 двусторонняя связь, у конкурента не заявлена — другой класс изделия")
    elif c_norm.get("two_way") and not m_norm.get("two_way"):
        penalty += 0.25
        reasons.append("у конкурента двусторонняя связь, у M22 нет")
    bm, bc = _band_class(m_norm.get("freq_band")), _band_class(c_norm.get("freq_band"))
    if bm and bc and bm != bc:
        penalty += 0.2
        reasons.append(f"разный класс диапазона ({m_norm.get('freq_band')} против {c_norm.get('freq_band')})")
    rm, rc = m_norm.get("range_m"), c_norm.get("range_m")
    if rm and rc and max(rm, rc) / min(rm, rc) > 2:
        penalty += 0.1
        reasons.append(f"дальность отличается более чем вдвое ({rm:g} м против {rc:g} м)")
    return penalty, reasons

KIND_GROUPS = {
    "system": "system", "kit": "system", "transmitter": "transmitter", "receiver": "receiver", "case_charger": "accessory",
    "headphones": "headphones", "microphone": "microphone", "audioguide": "audioguide", "accessory": "accessory",
    "rental": "rental", "other": "other",
}


def _price_ratio(a: float | None, b: float | None) -> float | None:
    if not a or not b:
        return None
    return max(a, b) / min(a, b)


def match_one(cp: dict, m22_list: list[dict], m22_categories: set[str]) -> list[dict]:
    """Возвращает список сопоставлений для одного товара конкурента (0..3 шт.)."""
    out: list[dict] = []
    cat = cp.get("category_slug")
    kind = KIND_GROUPS.get(cp.get("kind") or "other", "other")
    brand_low = (cp.get("brand") or "").lower()
    if any(b in brand_low for b in ("radiosync", "kromix")) or "radiosync" in (cp.get("name") or "").lower():
        return [{"m22_product_id": None, "match_type": "adjacent", "confidence": 0.3, "reasons": ["перепродажа собственного бренда M22 (Radiosync/Kromix) — не рыночный аналог"]}]
    if cat == "rental" or kind == "rental":
        return []  # арендные позиции с ценой за день не сопоставляются с розницей
    cap = cp.get("capacity")
    mkey = cp.get("model_key")
    brand = (cp.get("brand") or "").lower()

    # 1. Точное совпадение модели
    if mkey and len(mkey) >= 4:
        for m in m22_list:
            if m.get("model_key") == mkey:
                conf = 0.85
                reasons = [f"совпадает ключ модели {mkey}"]
                mb = (m.get("brand") or "").lower()
                if brand and mb and (brand in mb or mb in brand):
                    conf += 0.1
                    reasons.append("совпадает бренд")
                elif brand and mb:
                    conf -= 0.25
                    reasons.append(f"бренд отличается ({cp.get('brand')} vs {m.get('brand')})")
                if cap and m.get("capacity") and cap != m["capacity"]:
                    conf -= 0.15
                    reasons.append(f"вместимость отличается ({cap} vs {m['capacity']})")
                elif cap and m.get("capacity") and cap == m["capacity"]:
                    conf += 0.05
                    reasons.append(f"совпадает вместимость {cap}")
                mtype = "exact_model"
                if KIND_GROUPS.get(m.get("kind") or "other") != kind:
                    conf = min(conf, 0.4)
                    mtype = "functional"
                    reasons.append("тип товара отличается (система/приёмник/аксессуар) — не сопоставимо по цене")
                elif cap and m.get("capacity") and cap != m["capacity"]:
                    conf = min(conf, 0.55)
                    mtype = "kit"
                elif kind == "system" and m.get("capacity") and not cap:
                    conf = min(conf, 0.55)
                    reasons.append("вместимость комплекта конкурента неизвестна")
                out.append({"m22_product_id": m["id"], "match_type": mtype, "confidence": round(max(0.2, min(conf, 0.98)), 2), "reasons": reasons})
        if out:
            out.sort(key=lambda x: -x["confidence"])
            return out[:3]

    if cat is None:
        return out
    if cat == "substitutes_apps":
        return [{"m22_product_id": None, "match_type": "substitute", "confidence": 0.6, "reasons": ["технологический заменитель радиогида/аудиогида"]}]
    if cat not in m22_categories:
        return [{"m22_product_id": None, "match_type": "new_category", "confidence": 0.7, "reasons": [f"категория «{cat}» отсутствует в матрице M22"]}]

    # 2. Аналоги внутри категории и типа
    candidates = []
    for m in m22_list:
        if m.get("category_slug") != cat:
            continue
        mk = KIND_GROUPS.get(m.get("kind") or "other", "other")
        if mk != kind:
            continue
        score = 0.4 if kind == "system" else 0.5  # для приёмников/передатчиков/аксессуаров совпадение категории и типа — сильный признак
        reasons = [f"одна категория ({cat}) и тип ({kind})"]
        if cp.get("norm") is not None and m.get("norm") is not None:
            pen, why = feature_compat(m["norm"], cp["norm"])
            score -= pen
            reasons += why
            if not pen:
                reasons.append(f"характеристики совместимы: конкурент — {spec_summary(cp['norm'])}; M22 — {spec_summary(m['norm'])}")
        mcap = m.get("capacity")
        if mcap and not cap and kind == "system":
            score -= 0.15
            reasons.append("вместимость комплекта конкурента неизвестна")
        if cap and mcap:
            if cap == mcap:
                score += 0.25
                reasons.append(f"совпадает вместимость {cap}")
            elif abs(cap - mcap) <= max(2, 0.2 * mcap):
                score += 0.1
                reasons.append(f"близкая вместимость ({cap} vs {mcap})")
            else:
                score -= 0.15
                reasons.append(f"вместимость отличается ({cap} vs {mcap})")
        pr = _price_ratio(cp.get("price"), m.get("price"))
        if kind == "system":
            if pr is not None:
                if pr <= 1.3:
                    score += 0.15
                    reasons.append("цены в одном диапазоне (±30%)")
                elif pr <= 2:
                    score += 0.05
                else:
                    score -= 0.1
                    reasons.append("цены сильно отличаются (>2×)")
        else:
            # для приёмников/передатчиков/аксессуаров цена не является критерием сопоставления (иначе медиана рынка «подстраивается» под цену M22)
            score += 0.1
            if pr is not None and pr > 3:
                score -= 0.15
                reasons.append("цены отличаются более чем в 3 раза — возможно, другой класс изделия")
        candidates.append((score, m, reasons))
    if candidates:
        candidates.sort(key=lambda x: -x[0])
        best_score = candidates[0][0]
        # системы сопоставляются адресно (вместимость), приёмники/передатчики/аксессуары — со всеми аналогами M22 того же типа
        pool = candidates[:2] if kind == "system" else [c for c in candidates[:8] if c[0] >= 0.55]
        for score, m, reasons in pool:
            if kind == "system" and score < best_score - 0.15:
                break
            mtype = "direct_analog" if score >= 0.6 else "functional"
            if kind == "accessory" or kind == "headphones" or kind == "microphone":
                mtype = "accessory" if score >= 0.55 else "functional"
            if kind == "system" and (cp.get("kind") == "kit" or m.get("kind") == "kit"):
                mtype = "kit"
            out.append({"m22_product_id": m["id"], "match_type": mtype, "confidence": round(max(0.2, min(score, 0.8)), 2), "reasons": reasons})
        return out
    # Категория есть у M22, но такого типа товара нет — смежный товар
    return [{"m22_product_id": None, "match_type": "adjacent", "confidence": 0.5, "reasons": [f"в категории «{cat}» у M22 нет товаров типа «{kind}»"]}]


def run_matching(conn: sqlite3.Connection) -> dict:
    m22_list = db.rows(conn, "SELECT id, name, brand, model_key, category_slug, kind, capacity, price, description, specs_json FROM m22_products WHERE is_active=1 AND in_scope=1")
    for m in m22_list:
        m["norm"] = specmod.normalize(m["name"], m["description"], m["specs_json"], m["price"], m["capacity"])
    m22_categories = {m["category_slug"] for m in m22_list if m["category_slug"]}
    cps = db.rows(conn, "SELECT id, name, brand, model_key, category_slug, kind, capacity, price, description, specs_json FROM competitor_products WHERE is_active=1")
    for cp in cps:
        cp["norm"] = specmod.normalize(cp["name"], cp["description"], cp["specs_json"], cp["price"], cp["capacity"])
    created = updated = 0
    for cp in cps:
        matches = match_one(cp, m22_list, m22_categories)
        for mt in matches:
            needs_review = 1 if mt["confidence"] < 0.7 else 0
            existing = db.row(conn, "SELECT id, review_status FROM product_matches WHERE competitor_product_id=? AND m22_product_id IS ?", (cp["id"], mt["m22_product_id"]))
            if existing:
                if existing["review_status"] == "auto":
                    conn.execute("UPDATE product_matches SET match_type=?, confidence=?, reasons_json=?, needs_review=?, updated_at=datetime('now') WHERE id=?",
                                 (mt["match_type"], mt["confidence"], db.j(mt["reasons"]), needs_review, existing["id"]))
                    updated += 1
            else:
                conn.execute("INSERT INTO product_matches(m22_product_id, competitor_product_id, match_type, confidence, method, reasons_json, needs_review) VALUES(?,?,?,?,?,?,?)",
                             (mt["m22_product_id"], cp["id"], mt["match_type"], mt["confidence"], "rule", db.j(mt["reasons"]), needs_review))
                created += 1
    conn.commit()
    return {"competitor_products": len(cps), "created": created, "updated": updated}


def comparables_for(conn: sqlite3.Connection, m22_product_id: int, min_conf: float = 0.6) -> list[dict]:
    """Сопоставимые предложения конкурентов с ценой (для сравнения с рынком)."""
    rows = db.rows(conn, """
        SELECT pm.match_type, pm.confidence, pm.reasons_json, cp.id AS competitor_product_id, cp.name, cp.price, cp.url, cp.capacity, cp.description, cp.specs_json, cp.image_url,
               c.name AS competitor_name, c.id AS competitor_id
        FROM product_matches pm JOIN competitor_products cp ON cp.id=pm.competitor_product_id JOIN competitors c ON c.id=cp.competitor_id
        WHERE pm.m22_product_id=? AND pm.review_status!='rejected' AND pm.confidence>=? AND cp.price IS NOT NULL AND cp.is_active=1
          AND cp.currency='RUB' AND pm.match_type IN ('exact_model','direct_analog') AND COALESCE(cp.category_slug,'')!='rental' AND cp.price>=10
        ORDER BY pm.confidence DESC""", (m22_product_id, min_conf))
    for r in rows:
        norm = specmod.normalize(r["name"], r.pop("description"), r.pop("specs_json"), r["price"], r["capacity"])
        r["specs"] = spec_summary(norm)
        r["norm"] = {k: v for k, v in norm.items() if k != "price"}
        r["reasons"] = db.uj(r.pop("reasons_json"), []) or []
    return rows
