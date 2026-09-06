"""Общие функции сборщиков: upsert товаров с историей цен и защитой от дублей."""
from __future__ import annotations

import sqlite3

from .. import db, normalize


def upsert_m22_product(conn: sqlite3.Connection, data: dict, run_id: int | None) -> bool:
    """Вставляет/обновляет товар M22. Возвращает True, если что-то изменилось (цена/наличие/контент)."""
    name = data["name"]
    site_path = data.get("site_category_path")
    category = data.get("category_slug") or normalize.classify_category(name, data.get("description"), site_path)
    kind = normalize.detect_kind(name)
    capacity = normalize.detect_capacity(name, data.get("description"))
    mkey = normalize.model_key(name, data.get("sku"))
    in_scope = 1 if normalize.is_in_scope(name, category, site_path) else 0
    specs_json = db.j(data.get("specs") or {})
    kit_json = db.j(data.get("kit") or {})
    chash = normalize.content_hash(name, data.get("description"), specs_json, kit_json, data.get("sku"))
    price = data.get("price")
    old_price = data.get("old_price")
    availability = data.get("availability")
    existing = db.row(conn, "SELECT * FROM m22_products WHERE url=?", (data["url"],))
    changed = False
    if existing:
        changed = (existing["price"] != price) or (existing["availability"] != availability) or (existing["content_hash"] != chash) or not existing["is_active"]
        conn.execute(
            """UPDATE m22_products SET external_id=?, sku=?, name=?, brand=?, category_slug=?, site_category_path=?, model_key=?, kind=?,
               capacity=?, price=?, old_price=?, availability=?, description=?, specs_json=?, kit_json=?, images_json=?, parent_url=?,
               content_hash=?, in_scope=?, is_active=1, last_seen_at=datetime('now'), fetched_at=? WHERE id=?""",
            (data.get("external_id"), data.get("sku"), name, data.get("brand"), category, site_path, mkey, kind, capacity, price,
             old_price, availability, data.get("description"), specs_json, kit_json, db.j(data.get("images") or []),
             data.get("parent_url"), chash, in_scope, data.get("fetched_at"), existing["id"]),
        )
        pid = existing["id"]
        last = db.row(conn, "SELECT price, availability FROM m22_price_history WHERE product_id=? ORDER BY observed_at DESC, id DESC LIMIT 1", (pid,))
        if not last or last["price"] != price or last["availability"] != availability:
            conn.execute("INSERT INTO m22_price_history(product_id, price, old_price, availability, run_id) VALUES(?,?,?,?,?)",
                         (pid, price, old_price, availability, run_id))
    else:
        cur = conn.execute(
            """INSERT INTO m22_products(site,url,external_id,sku,name,brand,category_slug,site_category_path,model_key,kind,capacity,price,old_price,
               availability,description,specs_json,kit_json,images_json,parent_url,content_hash,in_scope,fetched_at)
               VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (data["site"], data["url"], data.get("external_id"), data.get("sku"), name, data.get("brand"), category, site_path, mkey, kind,
             capacity, price, old_price, availability, data.get("description"), specs_json, kit_json, db.j(data.get("images") or []),
             data.get("parent_url"), chash, in_scope, data.get("fetched_at")),
        )
        pid = int(cur.lastrowid)
        conn.execute("INSERT INTO m22_price_history(product_id, price, old_price, availability, run_id) VALUES(?,?,?,?,?)",
                     (pid, price, old_price, availability, run_id))
        changed = True
    # Варианты, которых нет как отдельных страниц (у Tilda — SKU внутри одной страницы)
    for v in data.get("variants") or []:
        if not v.get("url") or "#" not in v["url"]:
            continue  # у m22.ru варианты — отдельные страницы из sitemap, они соберутся сами
        vdata = {**data, "name": v["name"], "url": v["url"], "price": v.get("price"), "variants": [], "parent_url": data["url"],
                 "sku": v["url"].split("#")[-1] or data.get("sku")}
        upsert_m22_product(conn, vdata, run_id)
    return changed


def upsert_competitor_product(conn: sqlite3.Connection, competitor_id: int, page_id: int | None, data: dict, run_id: int | None,
                              match_by_url: bool = False) -> tuple[int, bool]:
    name = normalize.clean_text(data["name"])
    category = data.get("category_slug") or normalize.classify_category(name, data.get("description"))
    kind = normalize.detect_kind(name)
    capacity = normalize.detect_capacity(name, data.get("description"))
    brand = normalize.detect_brand(name, data.get("brand"))
    mkey = normalize.model_key(name, data.get("sku"))
    specs_json = db.j(data.get("specs") or {})
    chash = normalize.content_hash(name, data.get("description"), specs_json)
    price = data.get("price")
    currency = data.get("currency") or "RUB"
    availability = data.get("availability")
    existing = db.row(conn, "SELECT * FROM competitor_products WHERE competitor_id=? AND url=? AND name=?", (competitor_id, data["url"], name))
    if not existing and match_by_url:
        existing = db.row(conn, "SELECT * FROM competitor_products WHERE competitor_id=? AND url=? ORDER BY id LIMIT 1", (competitor_id, data["url"]))
        if existing and (len(name) < 8 or len(existing["name"]) > len(name) + 25):
            name = existing["name"]  # название со страницы хуже зафиксированного при исследовании — оставляем прежнее
    changed = False
    if existing:
        changed = (existing["price"] != price) or (existing["availability"] != availability) or (existing["content_hash"] != chash) or not existing["is_active"]
        conn.execute(
            """UPDATE competitor_products SET page_id=COALESCE(?,page_id), name=?, brand=?, model_key=?, category_slug=COALESCE(?,category_slug), kind=?, capacity=?,
               price=?, currency=?, availability=?, description=?, specs_json=?, content_hash=?, image_url=COALESCE(?, image_url), is_active=1, last_seen_at=datetime('now'), fetched_at=? WHERE id=?""",
            (page_id, name, brand, mkey, category, kind, capacity, price, currency, availability, data.get("description"), specs_json, chash,
             data.get("image_url"), data.get("fetched_at"), existing["id"]),
        )
        cid = existing["id"]
        last = db.row(conn, "SELECT price, availability FROM competitor_price_history WHERE competitor_product_id=? ORDER BY observed_at DESC, id DESC LIMIT 1", (cid,))
        if not last or last["price"] != price or last["availability"] != availability:
            conn.execute("INSERT INTO competitor_price_history(competitor_product_id, price, availability, run_id) VALUES(?,?,?,?)",
                         (cid, price, availability, run_id))
    else:
        cur = conn.execute(
            """INSERT INTO competitor_products(competitor_id,page_id,url,name,brand,model_key,category_slug,kind,capacity,price,currency,availability,
               description,specs_json,content_hash,fetched_at,image_url) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (competitor_id, page_id, data["url"], name, brand, mkey, category, kind, capacity, price, currency, availability,
             data.get("description"), specs_json, chash, data.get("fetched_at"), data.get("image_url")),
        )
        cid = int(cur.lastrowid)
        conn.execute("INSERT INTO competitor_price_history(competitor_product_id, price, availability, run_id) VALUES(?,?,?,?)",
                     (cid, price, availability, run_id))
        changed = True
    return cid, changed


def renormalize(conn: sqlite3.Connection) -> dict:
    """Пересчитывает категорию/тип/вместимость/ключ модели для всех товаров без повторной загрузки страниц."""
    n = 0
    for p in db.rows(conn, "SELECT id, name, description, site_category_path, sku, site, parent_url FROM m22_products"):
        cat = "rental" if p["site"] == "radiosync.ru" and p["site_category_path"] == "Аренда" else normalize.classify_category(p["name"], p["description"], p["site_category_path"])
        conn.execute("UPDATE m22_products SET category_slug=?, kind=?, capacity=?, model_key=?, in_scope=? WHERE id=?",
                     (cat, normalize.detect_kind(p["name"]), normalize.detect_capacity(p["name"]), normalize.model_key(p["name"], p["sku"]),
                      1 if normalize.is_in_scope(p["name"], cat, p["site_category_path"]) else 0, p["id"]))
        n += 1
    # варианты radiosync.ru наследуют вместимость/тип от товара m22.ru с тем же артикулом
    for v in db.rows(conn, "SELECT id, sku FROM m22_products WHERE site='radiosync.ru' AND sku IS NOT NULL AND sku!='' AND capacity IS NULL"):
        m = db.row(conn, "SELECT capacity, kind, category_slug FROM m22_products WHERE site='m22.ru' AND sku=? AND capacity IS NOT NULL", (v["sku"],))
        if m:
            conn.execute("UPDATE m22_products SET capacity=?, kind=?, category_slug=COALESCE(category_slug, ?) WHERE id=?", (m["capacity"], m["kind"], m["category_slug"], v["id"]))
    for p in db.rows(conn, "SELECT id, name, description, NULL AS sku, brand FROM competitor_products"):
        conn.execute("UPDATE competitor_products SET category_slug=COALESCE(?, category_slug), kind=?, capacity=?, model_key=?, brand=COALESCE(brand, ?) WHERE id=?",
                     (normalize.classify_category(p["name"], p["description"]), normalize.detect_kind(p["name"]), normalize.detect_capacity(p["name"]),
                      normalize.model_key(p["name"], p["sku"]), normalize.detect_brand(p["name"]), p["id"]))
        n += 1
    conn.commit()
    return {"updated": n}
