"""Сборщик каталога m22.ru: sitemap.xml -> страницы товаров -> JSON-LD Product + характеристики + варианты."""
from __future__ import annotations

import json
import logging
import re
import sqlite3

from bs4 import BeautifulSoup

from .. import db, http, normalize
from .common import upsert_m22_product

log = logging.getLogger(__name__)
SOURCE_KEY = "m22.ru"
SITEMAP = "https://m22.ru/sitemap.xml"

# Разделы каталога m22.ru, входящие в контур исследования (экскурсионная деятельность и B2B-решения)
IN_SCOPE_SECTIONS = {
    "sistemi-radiogid", "sinhronniy-perevod", "gotovie-resheniya", "odnorazovie-naushniki",
    "komplektuyuschie-dlya-gidov", "mikrofoni-petlichnie", "mikrofoni", "interkom-sistemi",
    "racii-i-aksessuari", "zaryadnie-ustroystva", "garnituri", "category",
}


def product_urls_from_sitemap(xml: str) -> list[str]:
    urls = re.findall(r"<loc>([^<]+)</loc>", xml)
    out = []
    for u in urls:
        if "/catalog/" not in u:
            continue
        parts = u.split("/catalog/", 1)[1].strip("/").split("/")
        if len(parts) < 2:
            continue  # раздел
        section = parts[0]
        # собираем ВСЕ разделы каталога (в том числе вызов персонала, пейджеры): полнота матрицы M22;
        # принадлежность к контуру радара определяется потом флагом in_scope
        if section in ("category",) and len(parts) == 2:
            continue
        # страница товара: /catalog/<section>/products/<slug> или /catalog/<section>/<sub>/<slug>
        if len(parts) >= 3 or (len(parts) == 2 and parts[1] not in ("products",)):
            if len(parts) == 2:
                continue  # подкатегория
            out.append(u)
    return sorted(set(out))


def parse_product_page(html: str, url: str) -> dict | None:
    soup = BeautifulSoup(html, "lxml")
    ld = None
    for tag in soup.find_all("script", type="application/ld+json"):
        try:
            data = json.loads(tag.string or "")
        except (ValueError, TypeError):
            continue
        items = data if isinstance(data, list) else [data]
        for it in items:
            if isinstance(it, dict) and it.get("@type") == "Product":
                ld = it
    h1 = soup.find("h1")
    name = normalize.clean_text((ld or {}).get("name") or (h1.get_text() if h1 else ""))
    if not name:
        return None
    offers = (ld or {}).get("offers") or {}
    price = normalize.parse_price(str(offers.get("price"))) if offers.get("price") else None
    availability = (offers.get("availability") or "").split("/")[-1] or None
    if price is None:
        box = soup.select_one(".product-page-desc-price .price-value-default, .product-page-desc-price .price-value-sale")
        if box:
            price = normalize.parse_price(box.get_text())
    old_price = None
    old_box = soup.select_one(".product-page-desc-price .price-value-old")
    if old_box:
        old_price = normalize.parse_price(old_box.get_text())
    brand = ((ld or {}).get("brand") or {}).get("name") if isinstance((ld or {}).get("brand"), dict) else None
    sku_el = soup.select_one(".product-page-desc-artikul")
    sku = None
    if sku_el:
        m = re.search(r"Артикул:\s*([\w\-./]+)", sku_el.get_text())
        sku = m.group(1) if m else normalize.clean_text(sku_el.get_text())
    category_path = (ld or {}).get("category")
    description = normalize.clean_text((ld or {}).get("description") or "")
    if not description:
        d = soup.select_one(".product-page-text, .product-description")
        description = normalize.clean_text(d.get_text(" ")) if d else ""

    # Характеристики: <ul><li><strong>Группа</strong></li><li><span>Параметр</span><span></span><span>Значение</span></li>
    specs: dict[str, dict[str, str]] = {}
    kit: dict[str, str] = {}
    group = "Основные"
    for ul in soup.find_all("ul"):
        lis = ul.find_all("li", recursive=False)
        if not lis or not any(li.find("span") for li in lis):
            continue
        if not any("Показать" in ul.parent.get_text() or "характеристик" in ul.parent.get_text().lower() for _ in [0]):
            pass
        local: dict[str, dict[str, str]] = {}
        g = group
        for li in lis:
            strong = li.find("strong")
            spans = [normalize.clean_text(s.get_text()) for s in li.find_all("span", recursive=False)]
            spans = [s for s in spans if s]
            if strong and len(spans) == 0:
                g = normalize.clean_text(strong.get_text())
                continue
            if len(spans) >= 2:
                local.setdefault(g, {})[spans[0]] = spans[-1]
        if sum(len(v) for v in local.values()) >= 3:
            specs = local
            break
    if "Комплектация" in specs:
        kit = specs.get("Комплектация", {})

    # Варианты (модификации)
    variants = []
    for pos in soup.select(".product-mods-pos"):
        a = pos.select_one(".product-mods-name a")
        p = pos.select_one(".price-value-default, .price-value-sale")
        if a and a.get("href"):
            variants.append({
                "name": normalize.clean_text(a.get_text()),
                "url": "https://m22.ru" + a["href"] if a["href"].startswith("/") else a["href"],
                "price": normalize.parse_price(p.get_text()) if p else None,
            })
    images = [img.get("src") for img in soup.select(".product-page-image-box img, .product-images-other-pos img") if img.get("src")]
    external_id = (ld or {}).get("sku")
    return {
        "site": "m22.ru", "url": url, "external_id": external_id, "sku": sku, "name": name, "brand": brand,
        "site_category_path": category_path, "price": price, "old_price": old_price, "availability": availability,
        "description": description, "specs": specs, "kit": kit, "variants": variants, "images": images[:8],
    }


def run(conn: sqlite3.Connection, limit: int | None = None) -> dict:
    run_id = db.start_run(conn, SOURCE_KEY)
    conn.commit()
    seen = changed = errors = 0
    try:
        sm = http.fetch(SITEMAP, SOURCE_KEY, save=False)
        urls = product_urls_from_sitemap(sm.text)
        if limit:
            urls = urls[:limit]
        log.info("m22.ru: %s страниц товаров в контуре", len(urls))
        for url in urls:
            try:
                res = http.fetch(url, SOURCE_KEY, respect_robots=False)
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
        # Товары, не встреченные в этом запуске, помечаем как исчезнувшие
        conn.execute(
            "UPDATE m22_products SET is_active=0 WHERE site='m22.ru' AND is_active=1 AND (fetched_at IS NULL OR fetched_at < (SELECT started_at FROM source_runs WHERE id=?))",
            (run_id,),
        ) if seen > 10 else None
        status = "ok" if errors == 0 else ("partial" if seen else "error")
        db.finish_run(conn, run_id, status, seen, changed, errors, f"{seen} товаров, {changed} изменений, {errors} ошибок")
    except Exception as exc:  # noqa: BLE001
        log.exception("m22.ru run failed")
        db.log_error(conn, SOURCE_KEY, SITEMAP, str(exc))
        db.finish_run(conn, run_id, "error", seen, changed, errors + 1, str(exc))
    conn.commit()
    return {"seen": seen, "changed": changed, "errors": errors}
