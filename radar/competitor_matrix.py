"""Матрица конкурентов: все товары всех конкурентов с нормализованными параметрами, покрытие категорий, изменения."""
from __future__ import annotations

import re
import sqlite3
from urllib.parse import urljoin

from . import categories as catmod
from . import db
from . import specs as specmod
from .normalize import CATEGORY_NAMES

KIND_LABELS = catmod.KIND_LABELS
MATRIX_COLS = [("range_m", "Дальность, м"), ("channels", "Каналов"), ("freq_band", "Диапазон"), ("display", "Экран"), ("battery_h", "Автономность, ч"),
               ("weight_g", "Вес, г"), ("two_way", "Двустор. связь"), ("capacity", "Вместимость")]


SPEC_FIELDS = ("range_m", "channels", "freq_band", "battery_h", "weight_g")  # поля, по которым считаем «характеристики сняты»
TIER_ORDER = {"A": 0, "B": 1, "C": 2}


def rows(conn: sqlite3.Connection, competitor_id: int | None = None, category: str | None = None, kind: str | None = None, include_inactive: bool = True, q: str | None = None,
         tier: str | None = None, in_scope_only: bool = False) -> list[dict]:
    where, params = ["c.is_active=1"], []
    if competitor_id:
        # продавец = вся группа сайтов (Cromi: cromi.ru, spbaudio.ru, sin24.ru …), если у конкурента задана группа
        where.append("(cp.competitor_id=? OR (c.group_name IS NOT NULL AND c.group_name=(SELECT group_name FROM competitors WHERE id=?)))")
        params += [competitor_id, competitor_id]
    if tier:
        where.append("c.tier=?")
        params.append(tier)
    if in_scope_only:
        where.append("cp.category_slug IS NOT NULL")
    if category:
        cats = [category] if isinstance(category, str) else list(category)
        where.append(f"cp.category_slug IN ({','.join('?' * len(cats))})")
        params += cats
    if kind:
        where.append("cp.kind=?")
        params.append(kind)
    if not include_inactive:
        where.append("cp.is_active=1")
    if q:
        where.append("(cp.name LIKE ? OR cp.model_key LIKE ? OR c.name LIKE ?)")
        params += [f"%{q}%"] * 3
    baseline = (db.get_setting(conn, "baseline_date") or "")[:10]
    out = db.rows(conn, f"""SELECT cp.*, c.name AS competitor_name, COALESCE(c.group_name, c.name) AS seller, c.website, c.tier,
                           (SELECT COUNT(*) FROM competitor_price_history h WHERE h.competitor_product_id=cp.id) AS obs,
                           (SELECT m.id FROM product_matches pm JOIN m22_products m ON m.id=pm.m22_product_id WHERE pm.competitor_product_id=cp.id AND pm.review_status!='rejected' ORDER BY pm.confidence DESC LIMIT 1) AS m22_id,
                           (SELECT m.name FROM product_matches pm JOIN m22_products m ON m.id=pm.m22_product_id WHERE pm.competitor_product_id=cp.id AND pm.review_status!='rejected' ORDER BY pm.confidence DESC LIMIT 1) AS m22_name,
                           (SELECT m.price FROM product_matches pm JOIN m22_products m ON m.id=pm.m22_product_id WHERE pm.competitor_product_id=cp.id AND pm.review_status!='rejected' ORDER BY pm.confidence DESC LIMIT 1) AS m22_price,
                           (SELECT m.images_json FROM product_matches pm JOIN m22_products m ON m.id=pm.m22_product_id WHERE pm.competitor_product_id=cp.id AND pm.review_status!='rejected' ORDER BY pm.confidence DESC LIMIT 1) AS m22_images_json,
                           (SELECT m.url FROM product_matches pm JOIN m22_products m ON m.id=pm.m22_product_id WHERE pm.competitor_product_id=cp.id AND pm.review_status!='rejected' ORDER BY pm.confidence DESC LIMIT 1) AS m22_url,
                           (SELECT pm.match_type FROM product_matches pm WHERE pm.competitor_product_id=cp.id AND pm.review_status!='rejected' ORDER BY pm.confidence DESC LIMIT 1) AS match_type,
                           (SELECT pm.confidence FROM product_matches pm WHERE pm.competitor_product_id=cp.id AND pm.review_status!='rejected' ORDER BY pm.confidence DESC LIMIT 1) AS match_conf,
                           (SELECT im.verdict FROM image_matches im WHERE im.competitor_product_id=cp.id ORDER BY CASE im.verdict WHEN 'same' THEN 0 ELSE 1 END, im.phash_dist LIMIT 1) AS photo_verdict,
                           (SELECT m.name FROM image_matches im JOIN m22_products m ON m.id=im.m22_product_id WHERE im.competitor_product_id=cp.id ORDER BY CASE im.verdict WHEN 'same' THEN 0 ELSE 1 END, im.phash_dist LIMIT 1) AS photo_m22_name
                           FROM competitor_products cp JOIN competitors c ON c.id=cp.competitor_id WHERE {' AND '.join(where)}
                           ORDER BY c.tier, seller, cp.category_slug, cp.kind, cp.price""", params)
    for r in out:
        r["norm"] = specmod.normalize(r["name"], r["description"], r["specs_json"], r["price"], r["capacity"])
        r["unit_price"], r["pack_qty"] = catmod.unit_price(r["name"], r["price"], r["category_slug"], r["kind"], r["capacity"])
        r["state"] = "gone" if not r["is_active"] else ("new" if baseline and (r["first_seen_at"] or "")[:10] > baseline else "ok")
        r["category_name"] = CATEGORY_NAMES.get(r["category_slug"], r["category_slug"] or "без категории")
        r["kind_name"] = KIND_LABELS.get(r["kind"], r["kind"] or "не определён")
        imgs = db.uj(r.get("m22_images_json"), []) or []
        img = imgs[0] if imgs and isinstance(imgs[0], str) else (imgs[0].get("url") if imgs and isinstance(imgs[0], dict) else None)
        if img and not img.startswith("http"):
            img = urljoin(r.get("m22_url") or "https://m22.ru/", img)  # на m22.ru пути к фото относительные
        r["m22_image"] = img
        r["spec_count"] = sum(1 for f in SPEC_FIELDS if r["norm"].get(f) is not None)
        r["raw_spec_count"] = len(specmod.flatten_specs(r["specs_json"]))
    return _collapse_duplicates(out)


def summary(conn: sqlite3.Connection) -> dict:
    """Сводка по продавцам: сколько товаров, с ценой, с характеристиками (по каждому полю), категории, дата последнего снятия."""
    sellers: dict[str, dict] = {}
    for r in rows(conn, include_inactive=False):
        s = sellers.setdefault(r["seller"], {"seller": r["seller"], "competitor_id": r["competitor_id"], "tier": r["tier"], "website": r["website"], "n": 0, "priced": 0,
                                             "in_scope": 0, "any_spec": 0, "fields": {f: 0 for f, _ in MATRIX_COLS}, "cats": set(), "last": None, "matched": 0, "new": 0})
        s["n"] += 1
        s["priced"] += 1 if r["price"] else 0
        s["in_scope"] += 1 if r["category_slug"] else 0
        s["any_spec"] += 1 if r["spec_count"] else 0
        s["matched"] += 1 if r["m22_id"] else 0
        s["new"] += 1 if r["state"] == "new" else 0
        for f, _ in MATRIX_COLS:
            v = r["norm"].get(f)
            if v is not None and v is not False:
                s["fields"][f] += 1
        if r["category_slug"]:
            s["cats"].add(r["category_slug"])
        if r["fetched_at"] and (not s["last"] or r["fetched_at"] > s["last"]):
            s["last"] = r["fetched_at"]
    out = sorted(sellers.values(), key=lambda s: (TIER_ORDER.get(s["tier"], 9), -s["n"]))
    for s in out:
        s["cats"] = sorted(CATEGORY_NAMES.get(c, c) for c in s["cats"])
        s["spec_pct"] = round(s["any_spec"] / s["n"] * 100) if s["n"] else 0
        s["priced_pct"] = round(s["priced"] / s["n"] * 100) if s["n"] else 0
    totals = {"n": sum(s["n"] for s in out), "priced": sum(s["priced"] for s in out), "any_spec": sum(s["any_spec"] for s in out), "sellers": len(out),
              "fields": {f: sum(s["fields"][f] for s in out) for f, _ in MATRIX_COLS}}
    return {"sellers": out, "totals": totals}


def _dup_key(r: dict) -> tuple:
    """Одна и та же позиция, попавшая с двух страниц сайта (раздел + магазин): продавец, нормализованное имя, цена."""
    name = re.sub(r"[^a-zа-я0-9]+", " ", (r["name"] or "").lower()).strip()
    name = re.sub(r"(радиогид|аудиогид|система|комплект|шт|штук)", " ", name)
    name = re.sub(r"\s+", " ", name).strip()
    return (r["competitor_id"], name, round(r["price"] or 0))


def _collapse_duplicates(rows_: list[dict]) -> list[dict]:
    seen: dict[tuple, dict] = {}
    out = []
    for r in rows_:
        if not r["is_active"] or not r["price"]:
            out.append(r)
            continue
        k = _dup_key(r)
        if k[1] and k in seen:
            keep = seen[k]
            keep["dup_count"] = keep.get("dup_count", 1) + 1
            keep.setdefault("dup_urls", []).append(r["url"])
            continue
        r["dup_count"] = 1
        seen[k] = r
        out.append(r)
    return out


def coverage(conn: sqlite3.Connection) -> dict:
    """Карта «продавец × категория»: число активных товаров; строка M22 — первой."""
    cats = [c for c in db.rows(conn, "SELECT slug, name_ru FROM categories ORDER BY sort_order")]
    m22 = {r["category_slug"]: r["n"] for r in db.rows(conn, "SELECT category_slug, COUNT(*) n FROM m22_products WHERE is_active=1 AND in_scope=1 AND parent_url IS NULL GROUP BY 1")}
    # считаем модели, а не строки: у Retekess одна модель радиогида продаётся в 20 вариантах комплектации, у группы Cromi одна модель — на 8 сайтах
    comp = db.rows(conn, """SELECT COALESCE(c.group_name, c.name) AS seller, MIN(c.id) AS competitor_id, cp.category_slug, COUNT(*) n, SUM(cp.price IS NOT NULL) priced,
                                   COUNT(DISTINCT COALESCE(cp.brand, '') || ':' || COALESCE(cp.model_key, lower(cp.name))) models
                            FROM competitor_products cp JOIN competitors c ON c.id=cp.competitor_id WHERE cp.is_active=1 AND c.is_active=1 GROUP BY seller, cp.category_slug""")
    sellers: dict[str, dict] = {}
    for r in comp:
        s = sellers.setdefault(r["seller"], {"seller": r["seller"], "competitor_id": r["competitor_id"], "cats": {}, "total": 0, "models": 0})
        s["cats"][r["category_slug"]] = {"n": r["n"], "priced": r["priced"], "models": r["models"]}
        s["total"] += r["n"]
        s["models"] += r["models"]
    used = {slug for s in sellers.values() for slug in s["cats"]} | set(m22)
    cats = [c for c in cats if c["slug"] in used]
    seller_rows = sorted(sellers.values(), key=lambda s: -s["total"])
    gaps = [c for c in cats if not m22.get(c["slug"]) and any(c["slug"] in s["cats"] for s in seller_rows)]
    return {"cats": cats, "m22": m22, "sellers": seller_rows, "gaps": gaps}


def changes(conn: sqlite3.Connection, days: int = 30) -> dict:
    baseline = (db.get_setting(conn, "baseline_date") or "")[:10]
    added = db.rows(conn, """SELECT cp.id, cp.name, cp.price, cp.url, cp.category_slug, cp.first_seen_at, COALESCE(c.group_name, c.name) AS seller, c.id AS competitor_id
                             FROM competitor_products cp JOIN competitors c ON c.id=cp.competitor_id WHERE cp.is_active=1 AND substr(cp.first_seen_at,1,10) > ? ORDER BY cp.first_seen_at DESC LIMIT 100""", (baseline,))
    gone = db.rows(conn, """SELECT cp.id, cp.name, cp.price, cp.url, cp.category_slug, cp.last_seen_at, COALESCE(c.group_name, c.name) AS seller, c.id AS competitor_id
                            FROM competitor_products cp JOIN competitors c ON c.id=cp.competitor_id WHERE cp.is_active=0 ORDER BY cp.last_seen_at DESC LIMIT 100""")
    price_moves = db.rows(conn, """SELECT s.id, s.title, s.observed_at, s.competitor_id FROM signals s WHERE s.type='competitor_price_change' AND s.status!='rejected'
                                   AND substr(s.created_at,1,10) >= date('now', ?) ORDER BY s.created_at DESC LIMIT 50""", (f"-{days} days",))
    return {"added": added, "gone": gone, "price_moves": price_moves, "baseline": baseline}


def export_rows(conn: sqlite3.Connection) -> list[dict]:
    out = []
    for r in rows(conn):
        n = r["norm"]
        out.append({"продавец": r["seller"], "конкурент": r["competitor_name"], "категория": r["category_name"], "тип": r["kind_name"], "модель": r["model_key"], "товар": r["name"],
                    "цена": r["price"], "цена_за_ед": r["unit_price"], "валюта": r["currency"], "дальность_м": n.get("range_m"), "каналов": n.get("channels"), "диапазон": n.get("freq_band"),
                    "экран": n.get("display"), "автономность_ч": n.get("battery_h"), "вес_г": n.get("weight_g"), "двусторонняя_связь": n.get("two_way"), "вместимость": n.get("capacity"),
                    "сопоставлено_с_M22": r["m22_name"], "тип_сопоставления": r["match_type"], "уверенность": r["match_conf"], "статус": r["state"], "url": r["url"],
                    "впервые": r["first_seen_at"], "последний_раз": r["last_seen_at"], "снято": r["fetched_at"]})
    return out
