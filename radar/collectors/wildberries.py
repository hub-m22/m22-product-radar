"""Wildberries: публичный JSON-endpoint поиска витрины (неофициальный, без ключей).

Ограничения: жёсткий лимит частоты (429 на второй запрос подряд) — не чаще одного запроса в 20 с;
endpoint может измениться. Цена в копейках: sizes[0].price.product.
"""
from __future__ import annotations

import logging
import sqlite3
import time
from urllib.parse import quote

from .. import db, http, normalize
from .common import upsert_competitor_product

log = logging.getLogger(__name__)
SOURCE_KEY = "wildberries"
ENDPOINT = ("https://search.wb.ru/exactmatch/ru/common/v9/search?ab_testing=false&appType=1&curr=rub&dest=-1257786&lang=ru&page=1&query={q}"
            "&resultset=catalog&sort=popular&spp=30&suppressSpellcheck=false")
QUERIES = ["радиогид", "радиогид система для экскурсий", "аудиогид для музея", "одноразовые наушники для экскурсий", "система синхронного перевода", "приемник радиогид"]
COMPETITOR_NAME = "Wildberries — продавцы экскурсионного оборудования"


def _competitor_id(conn: sqlite3.Connection) -> int:
    row = db.row(conn, "SELECT id FROM competitors WHERE website=?", ("https://www.wildberries.ru",))
    if row:
        return row["id"]
    conn.execute("INSERT INTO competitors(name, website, types_json, geography, notes, added_by, checked_at, scrapable) VALUES(?,?,?,?,?,?,date('now'),'static')",
                 (COMPETITOR_NAME, "https://www.wildberries.ru", db.j(["marketplace_seller"]), "Россия", "Агрегированные предложения продавцов WB по поисковым запросам (публичный JSON поиска витрины)", "collector"))
    return db.row(conn, "SELECT id FROM competitors WHERE website=?", ("https://www.wildberries.ru",))["id"]


def parse_products(data: dict) -> list[dict]:
    out = []
    products = (data.get("data") or {}).get("products") or data.get("products") or []
    for p in products:
        sizes = p.get("sizes") or []
        price = None
        if sizes and isinstance(sizes[0].get("price"), dict):
            pr = sizes[0]["price"]
            val = pr.get("product") or pr.get("total") or pr.get("basic")
            price = val / 100 if val else None
        elif p.get("salePriceU"):
            price = p["salePriceU"] / 100
        elif p.get("priceU"):
            price = p["priceU"] / 100
        name = normalize.clean_text(p.get("name") or "")
        supplier = (p.get("supplier") or "").lower()
        if not name or any(w in supplier for w in ("m22", "м22", "маркет22", "radiosync")):
            continue  # собственные листинги M22 — не конкуренты
        out.append({"url": f"https://www.wildberries.ru/catalog/{p.get('id')}/detail.aspx", "name": f"{p.get('brand') or ''} {name}".strip() + (f" — продавец {p.get('supplier')}" if p.get("supplier") else ""),
                    "brand": p.get("brand"), "price": price, "currency": "RUB", "availability": "InStock" if (p.get("totalQuantity") or 0) > 0 else "unknown",
                    "description": f"WB id {p.get('id')}; продавец: {p.get('supplier')}; рейтинг {p.get('reviewRating') or p.get('rating')}; отзывов {p.get('feedbacks')}"})
    return out


def run(conn: sqlite3.Connection, queries: list[str] | None = None, per_query: int = 30) -> dict:
    run_id = db.start_run(conn, SOURCE_KEY)
    conn.commit()
    cid = _competitor_id(conn)
    seen = changed = errors = 0
    last_error = ""
    for i, q in enumerate(queries or QUERIES):
        if i:
            time.sleep(20)
        try:
            data = http.fetch_json(ENDPOINT.format(q=quote(q)), SOURCE_KEY, respect_robots=False, delay=20, timeout=30)
            items = parse_products(data)[:per_query]
            relevant = [it for it in items if normalize.classify_category(it["name"]) is not None]
            for it in relevant:
                it["fetched_at"] = db.now_iso()
                _, ch = upsert_competitor_product(conn, cid, None, it, run_id)
                seen += 1
                changed += 1 if ch else 0
            conn.commit()
            log.info("WB «%s»: %s товаров, %s релевантных", q, len(items), len(relevant))
        except Exception as exc:  # noqa: BLE001
            errors += 1
            last_error = f"{type(exc).__name__}: {exc}"[:300]
            db.log_error(conn, SOURCE_KEY, q, last_error)
            conn.commit()
    status = "ok" if errors == 0 else ("partial" if seen else "error")
    db.finish_run(conn, run_id, status, seen, changed, errors, f"{seen} предложений, {changed} изменений, {errors} ошибок. {last_error}")
    conn.commit()
    return {"seen": seen, "changed": changed, "errors": errors}
