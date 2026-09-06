"""Нормализация характеристик радиогидов/аудиогидов в сопоставимые поля и обоснование «стоит ли выводить модель».

Поля: range_m (дальность, м), channels (число каналов), battery_h (автономность приёмника, ч), freq_band (UHF/VHF/2.4G/FM/IR),
weight_g (вес приёмника, г), capacity (вместимость комплекта), two_way (двусторонняя связь), display (экран), charging_case (кейс в комплекте).
"""
from __future__ import annotations

import json
import re

FIELDS = [("price", "Цена, ₽"), ("range_m", "Дальность, м"), ("channels", "Каналов"), ("battery_h", "Автономность, ч"), ("freq_band", "Диапазон"),
          ("weight_g", "Вес приёмника, г"), ("capacity", "Вместимость, чел."), ("two_way", "Двусторонняя связь"), ("display", "Экран"), ("charging_case", "Зарядный кейс")]
BETTER_HIGH = {"range_m", "channels", "battery_h", "capacity"}
BETTER_LOW = {"price", "weight_g"}


def _num(pattern: str, text: str, flags=re.I) -> float | None:
    m = re.search(pattern, text, flags)
    if not m:
        return None
    try:
        return float(m.group(1).replace(",", ".").replace(" ", ""))
    except ValueError:
        return None


def flatten_specs(specs_json: str | None) -> dict[str, str]:
    """{"группа": {"параметр": "значение"}} или {"параметр": "значение"} -> плоский словарь."""
    try:
        data = json.loads(specs_json) if specs_json else {}
    except (ValueError, TypeError):
        return {}
    out = {}
    for k, v in (data or {}).items():
        if isinstance(v, dict):
            for k2, v2 in v.items():
                out[str(k2).strip()] = str(v2).strip()
        else:
            out[str(k).strip()] = str(v).strip()
    return out


def normalize(name: str, description: str | None, specs_json: str | None, price: float | None = None, capacity: int | None = None) -> dict:
    flat = flatten_specs(specs_json)
    spec_text = " ; ".join(f"{k}: {v}" for k, v in flat.items())
    text = " ; ".join(x for x in (name, spec_text, description or "") if x)
    low = text.lower()
    out: dict = {"price": price}
    # дальность
    r = _num(r"(?:дальност|радиус|расстоян|range|до)\D{0,25}?(\d{2,4})\s*(?:м\b|метр|m\b)", text) or _num(r"(\d{2,4})\s*(?:м\b|метров|метра)\s*(?:дальност|радиус|приём)", text)
    if r and 20 <= r <= 3000:
        out["range_m"] = r
    # каналы
    c = _num(r"(\d{1,4})\s*(?:канал|channel|ch\b)", text) or _num(r"(?:канал\w*|channels?)\D{0,15}?(\d{1,4})\b", text)
    if c and 1 < c <= 2000:
        out["channels"] = c
    # автономность
    b = _num(r"(?:приёмник\w*|приемник\w*|receiver)\D{0,40}?(\d{1,3})\s*(?:ч\b|час|h\b|hours?)", text) or _num(r"(?:до|until|up to)\s*(\d{1,3})\s*(?:ч\b|час|h\b|hours?)", text) or _num(r"(\d{1,3})\s*(?:ч\b|час)\w*\s*(?:работ|автоном)", text)
    if b and 1 <= b <= 200:
        out["battery_h"] = b
    # диапазон
    if re.search(r"2[.,]4\s*(?:ггц|ghz|g\b)", low):
        out["freq_band"] = "2.4 ГГц"
    elif "uhf" in low or re.search(r"\b(4\d\d|5\d\d|8\d\d|9\d\d)\s*[-–]\s*\d{3}\s*(?:мгц|mhz)", low) or re.search(r"\b(4\d\d|8\d\d|9\d\d)\s*(?:мгц|mhz)", low):
        out["freq_band"] = "UHF"
    elif "vhf" in low or re.search(r"\b(1\d\d|2\d\d)\s*[-–]\s*\d{3}\s*(?:мгц|mhz)", low):
        out["freq_band"] = "VHF"
    elif re.search(r"\bfm\b|фм[- ]диапазон|8[78][.,]\d\s*[-–]\s*108", low):
        out["freq_band"] = "FM"
    elif re.search(r"инфракрасн|\bir\b|ик[- ]", low):
        out["freq_band"] = "ИК"
    # вес приёмника
    w = _num(r"(?:вес|масса|weight)\D{0,30}?(\d{2,3})\s*(?:г\b|гр\b|g\b|грамм)", text)
    if w and 10 <= w <= 500:
        out["weight_g"] = w
    # вместимость
    cap = capacity or _num(r"(?:на|для|до)\s*(\d{1,3})\s*(?:персон|человек|чел|экскурсант|слушател|участник|приемник|приёмник)", name)
    if cap:
        out["capacity"] = int(cap)
    out["two_way"] = bool(re.search(r"двустор|двухстор|two[- ]way|обратн\w* связ|full[- ]duplex", low))
    out["display"] = bool(re.search(r"дисплей|экран|display|lcd|oled", low))
    out["charging_case"] = bool(re.search(r"кейс|докстанц|док-станц|charging case|dock", low))
    return out


def better(field: str, a, b) -> bool | None:
    """True, если значение a лучше b по смыслу поля; None — несравнимо."""
    if a is None or b is None:
        return None
    if field in BETTER_HIGH:
        return a > b
    if field in BETTER_LOW:
        return a < b
    if field in ("two_way", "display", "charging_case"):
        return bool(a) and not bool(b)
    return None


def justify(offers: list[dict], m22_candidates: list[dict], category_in_m22: bool) -> dict:
    """Обоснование: почему модель конкурентов стоит (или не стоит) рассматривать к выводу.

    offers/m22_candidates — списки {"name","price","norm":{...}}. Возвращает {"reasons":[...], "verdict": "...", "table": {...}}.
    """
    reasons: list[dict] = []
    if not category_in_m22:
        reasons.append({"kind": "new_group", "text": "Новая для M22 группа товаров — в матрице нет изделий этой категории."})
    best_price = min((o["price"] for o in offers if o.get("price")), default=None)
    m22_prices = [m["price"] for m in m22_candidates if m.get("price")]
    m22_min = min(m22_prices) if m22_prices else None
    if best_price and m22_min:
        gap = (best_price - m22_min) / m22_min * 100
        if gap <= -25:
            reasons.append({"kind": "price", "text": f"Минимальная цена {best_price:,.0f} ₽ ниже самой дешёвой сопоставимой модели M22 ({m22_min:,.0f} ₽) на {abs(gap):.0f}%.".replace(",", " "), "gap_pct": round(gap)})
    # характеристики: лучшее значение среди предложений против лучшего у M22
    for field, label in FIELDS:
        if field == "price" or field in ("capacity", "display"):
            continue
        vals = [o["norm"].get(field) for o in offers if o["norm"].get(field) is not None]
        m22_vals = [m["norm"].get(field) for m in m22_candidates if m["norm"].get(field) is not None]
        if not vals or not m22_vals:
            continue
        if field in BETTER_HIGH:
            a, b = max(vals), max(m22_vals)
            if b and a >= b * 1.3:
                reasons.append({"kind": "spec", "field": field, "text": f"{label}: {a:g} против {b:g} у лучшей модели M22 (+{(a / b - 1) * 100:.0f}%)."})
        elif field == "weight_g":
            a, b = min(vals), min(m22_vals)
            if b and a <= b * 0.7:
                reasons.append({"kind": "spec", "field": field, "text": f"{label}: {a:g} г против {b:g} г у самой лёгкой модели M22."})
        elif field == "two_way":
            if any(vals) and not any(m22_vals):
                reasons.append({"kind": "spec", "field": field, "text": "Двусторонняя связь, которой нет у сопоставимых моделей M22."})
        elif field == "freq_band":
            if "2.4 ГГц" in vals and "2.4 ГГц" not in m22_vals:
                reasons.append({"kind": "spec", "field": field, "text": "Цифровой диапазон 2,4 ГГц, тогда как сопоставимые модели M22 работают в UHF/VHF."})
    if reasons:
        verdict = "есть основание рассмотреть вывод аналога"
    elif not offers or not any(o.get("price") for o in offers) or not m22_candidates:
        verdict = "недостаточно данных для сравнения (нет цен или характеристик)"
    else:
        verdict = "преимуществ перед моделями M22 по цене и характеристикам не найдено — выводить аналог нет оснований"
    return {"reasons": reasons, "verdict": verdict, "best_price": best_price, "m22_min_price": m22_min}
