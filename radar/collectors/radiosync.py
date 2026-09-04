"""Сборщик radiosync.ru (Tilda): sitemap-store.xml -> страницы tproduct -> schema.org offers + характеристики."""
from __future__ import annotations

import logging
import re
import sqlite3

from bs4 import BeautifulSoup

from .. import db, http, normalize
from .common import upsert_m22_product

log = logging.getLogger(__name__)
SOURCE_KEY = "radiosync.ru"
SITEMAP_STORE = "https://radiosync.ru/sitemap-store.xml"
RENT_PAGE = "https://radiosync.ru/rent"


def product_urls_from_sitemap(xml: str) -> list[str]:
    return sorted(set(u for u in re.findall(r"<loc>([^<]+)</loc>", xml) if "/tproduct/" in u))


def parse_product_page(html: str, url: str) -> dict | None:
    soup = BeautifulSoup(html, "lxml")
    name_el = soup.select_one(".js-store-prod-name, .js-product-name, h1")
    name = normalize.clean_text(name_el.get_text()) if name_el else ""
    if not name:
        og = soup.find("meta", property="og:title")
        name = normalize.clean_text(og["content"]) if og and og.get("content") else ""
    if not name:
        return None
    brand_el = soup.select_one(".js-product-brand")
    brand = normalize.clean_text(brand_el.get_text()) if brand_el else None
    sku_el = soup.select_one(".js-store-prod-sku")
    sku = normalize.clean_text(sku_el.get_text()) if sku_el else None
    price = None
    pv = soup.select_one(".js-store-prod-price-val")
    if pv is not None:
        price = normalize.parse_price(pv.get("data-product-price-def") or pv.get_text())
    low = soup.find("meta", itemprop="lowPrice")
    high = soup.find("meta", itemprop="highPrice")
    if price is None and low is not None:
        price = normalize.parse_price(low.get("content"))
    old_price = None
    old = soup.select_one(".js-store-prod-price-old-val")
    if old is not None and normalize.clean_text(old.get_text()):
        old_price = normalize.parse_price(old.get_text())
    avail_el = soup.find("link", itemprop="availability")
    availability = avail_el["href"].split("/")[-1] if avail_el and avail_el.get("href") else None
    desc_el = soup.select_one(".js-store-prod-all-text")
    description = normalize.clean_text(desc_el.get_text(" ")) if desc_el else ""
    specs: dict[str, dict[str, str]] = {}
    for p in soup.select(".js-store-prod-charcs"):
        t = normalize.clean_text(p.get_text())
        if ":" in t:
            k, v = t.split(":", 1)
            specs.setdefault("Характеристики", {})[k.strip()] = v.strip()
    # Варианты (offers)
    variants = []
    for off in soup.select('div[itemprop="offers"][itemtype*="/Offer"]'):
        sk = off.find("meta", itemprop="sku")
        pr = off.find("meta", itemprop="price")
        variants.append({"sku": sk.get("content") if sk else None, "price": normalize.parse_price(pr.get("content")) if pr else None})
    # Опции (например «на 5 персон») — Tilda хранит в data-атрибутах/JSON
    opts = []
    for o in soup.select(".t-product__option-select option, .t-store__options option"):
        opts.append(normalize.clean_text(o.get_text()))
    images = []
    og_img = soup.find("meta", property="og:image")
    if og_img and og_img.get("content"):
        images.append(og_img["content"])
    high_price = normalize.parse_price(high.get("content")) if high is not None else None
    # Категория на сайте по URL
    site_path = url.split("radiosync.ru/", 1)[1].split("/tproduct/")[0] or "root"
    site_path = {"comparisontable": "Радиогиды", "accessories": "Аксессуары", "11213123132": "Одноразовые наушники", "root": "Комплексные решения"}.get(site_path, site_path)
    return {
        "site": "radiosync.ru", "url": url, "external_id": url.split("/tproduct/")[1].split("-")[0], "sku": sku,
        "name": name, "brand": brand or "Radiosync", "site_category_path": site_path, "price": price, "old_price": old_price,
        "availability": availability, "description": description, "specs": specs,
        "kit": {"Варианты (SKU)": ", ".join(f"{v['sku']}: {v['price']:.0f} ₽" for v in variants if v.get("price"))} if variants else {},
        "variants": [{"name": f"{name} — {v['sku']}", "url": url + "#" + (v["sku"] or ""), "price": v["price"]} for v in variants if len(variants) > 1],
        "images": images, "high_price": high_price, "options": opts,
    }


def parse_rent_page(html: str) -> dict:
    """Аренда: минимальная сумма аренды и перечень позиций."""
    text = re.sub(r"<script.*?</script>", " ", html, flags=re.S)
    text = re.sub(r"<[^>]+>", " ", text)
    text = normalize.clean_text(text)
    m = re.search(r"Минимальная аренда от\s*([\d\s]+)\s*₽", text)
    items = []
    soup = BeautifulSoup(html, "lxml")
    for el in soup.select(".js-product-name"):
        t = normalize.clean_text(el.get_text())
        if t and t not in items:
            items.append(t)
    return {"min_rent_price": normalize.parse_price(m.group(1) + " ₽") if m else None, "items": items}


def run(conn: sqlite3.Connection, limit: int | None = None) -> dict:
    run_id = db.start_run(conn, SOURCE_KEY)
    conn.commit()
    seen = changed = errors = 0
    try:
        sm = http.fetch(SITEMAP_STORE, SOURCE_KEY, save=False)
        urls = product_urls_from_sitemap(sm.text)
        if limit:
            urls = urls[:limit]
        log.info("radiosync.ru: %s страниц товаров", len(urls))
        for url in urls:
            try:
                res = http.fetch(url, SOURCE_KEY)
                data = parse_product_page(res.text, url)
                if not data:
                    errors += 1
                    db.log_error(conn, SOURCE_KEY, url, "Не удалось разобрать страницу товара")
                    continue
                data["fetched_at"] = res.fetched_at
                seen += 1
                if upsert_m22_product(conn, data, run_id):
                    changed += 1
                conn.commit()
            except http.FetchError as exc:
                errors += 1
                db.log_error(conn, SOURCE_KEY, url, str(exc))
                conn.commit()
        # Аренда
        try:
            res = http.fetch(RENT_PAGE, SOURCE_KEY)
            rent = parse_rent_page(res.text)
            data = {
                "site": "radiosync.ru", "url": RENT_PAGE, "external_id": "rent", "sku": None,
                "name": "Аренда радиогидов Radiosync (SGTR03, кейсы, докстанции, наушники)", "brand": "Radiosync",
                "site_category_path": "Аренда", "price": rent["min_rent_price"], "old_price": None, "availability": "InStock",
                "description": "Минимальная сумма аренды: " + (f"{rent['min_rent_price']:.0f} ₽" if rent["min_rent_price"] else "не указана")
                + ". Позиции: " + "; ".join(rent["items"]),
                "specs": {}, "kit": {f"Позиция {i+1}": it for i, it in enumerate(rent["items"])}, "variants": [], "images": [],
                "fetched_at": res.fetched_at, "category_slug": "rental",
            }
            seen += 1
            if upsert_m22_product(conn, data, run_id):
                changed += 1
            conn.commit()
        except http.FetchError as exc:
            errors += 1
            db.log_error(conn, SOURCE_KEY, RENT_PAGE, str(exc))
        status = "ok" if errors == 0 else ("partial" if seen else "error")
        db.finish_run(conn, run_id, status, seen, changed, errors, f"{seen} товаров, {changed} изменений, {errors} ошибок")
    except Exception as exc:  # noqa: BLE001
        log.exception("radiosync.ru run failed")
        db.log_error(conn, SOURCE_KEY, SITEMAP_STORE, str(exc))
        db.finish_run(conn, run_id, "error", seen, changed, errors + 1, str(exc))
    conn.commit()
    return {"seen": seen, "changed": changed, "errors": errors}
