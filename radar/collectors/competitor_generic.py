"""Универсальный сборщик страниц конкурентов.

Поддерживает:
- страницы товара (kind=product): JSON-LD Product/Offer, microdata itemprop, OpenGraph product:price, CSS-конфиг, эвристика по цене;
- страницы каталога (kind=catalog/listing): элементы с ценой и ссылкой; CSS-конфиг {"items","name","price","link"};
- Tilda-магазины (parser=tilda): schema.org Offer + js-store классы.

Новую страницу мониторинга можно добавить из интерфейса без изменения кода; при необходимости
указать CSS-селекторы в parser_config_json.
"""
from __future__ import annotations

import json
import logging
import re
import sqlite3
from datetime import datetime, timedelta, timezone
from urllib.parse import urljoin

from bs4 import BeautifulSoup

from .. import db, http, normalize
from .common import upsert_competitor_product

log = logging.getLogger(__name__)
SOURCE_KEY = "competitors"

PRICE_TEXT_RE = re.compile(r"(?<![\d.,])(\d{1,3}(?:[\s\u00a0\u202f]\d{3})+|\d{1,3}(?:\.\d{3})+|\d+)(?:[.,]\d{1,2})?\s*(?:₽|руб\.?|р\.|RUB)", re.I)
NOISE_WORDS = ("доставка", "корзин", "скидк", "кредит", "рассрочк", "бонус", "в месяц", "/мес", "минимальн", "от суммы")
BAD_NAMES = {"главная", "подробнее", "купить", "в корзину", "заказать", "запросить кп", "перейти на страницу модели", "каталог", "подробности", "смотреть",
             "узнать больше", "добавить", "о компании", "контакты", "аренда", "все товары", "сравнить", "в избранное", "выбрать", "оформить заказ", "перейти", "далее", "количество :", "количество", "цена", "цена:"}


def good_name(text) -> bool:
    t = normalize.clean_text(text or "").lower().strip(" .:")
    return len(t) >= 6 and t not in BAD_NAMES and not PRICE_TEXT_RE.search(t) and not t.startswith(("перейти", "запросить", "подробнее", "купить", "каталог", "главная", "количество"))


PRICE_TAGS = ["span", "div", "p", "b", "strong", "ins", "bdi", "td", "li", "a"]


def price_elements(root):
    """Внутренние элементы с ценой (число и знак валюты могут быть в разных текстовых узлах)."""
    out = []
    for el in root.find_all(PRICE_TAGS):
        text = normalize.clean_text(el.get_text(" "))
        if len(text) > 60 or not PRICE_TEXT_RE.search(text):
            continue
        if any(PRICE_TEXT_RE.search(normalize.clean_text(ch.get_text(" "))) for ch in el.find_all(PRICE_TAGS)):
            continue  # есть более внутренний элемент с ценой
        out.append(el)
    return out


def card_name(card, url: str):
    """Название и ссылка карточки: заголовок → title/name/alt → самая длинная осмысленная ссылка."""
    a_first = card.find("a", href=True)

    def link_of(el):
        a = el.find("a", href=True) if el.name != "a" else el
        if a is None and el.parent is not None and el.parent.name == "a" and el.parent.get("href"):
            a = el.parent
        a = a or a_first
        return urljoin(url, a["href"]) if a is not None and a.get("href") else url

    for h in card.find_all(["h1", "h2", "h3", "h4", "h5"]):
        t = normalize.clean_text(h.get_text(" "))
        if good_name(t):
            return t, link_of(h)
    for sel in ("[class*=title]", "[class*=name]", "[itemprop=name]"):
        for el in card.select(sel):
            t = normalize.clean_text(el.get_text(" "))
            if good_name(t):
                return t, link_of(el)
    best = None
    for a in card.find_all("a", href=True):
        t = normalize.clean_text(a.get_text(" ")) or normalize.clean_text(a.get("title") or "")
        if not good_name(t):
            img = a.find("img")
            t = normalize.clean_text(img.get("alt") or img.get("title") or "") if img else ""
        if good_name(t) and (best is None or len(t) > len(best[0])):
            best = (t, urljoin(url, a["href"]))
    if best:
        return best
    return None, (urljoin(url, a_first["href"]) if a_first is not None else url)


def _jsonld_products(soup: BeautifulSoup) -> list[dict]:
    out = []
    for tag in soup.find_all("script", type="application/ld+json"):
        try:
            data = json.loads(tag.string or "")
        except (ValueError, TypeError):
            continue
        stack = data if isinstance(data, list) else [data]
        while stack:
            it = stack.pop()
            if not isinstance(it, dict):
                continue
            if "@graph" in it and isinstance(it["@graph"], list):
                stack.extend(it["@graph"])
            t = it.get("@type")
            types = t if isinstance(t, list) else [t]
            if "Product" in types:
                out.append(it)
            for k in ("itemListElement", "mainEntity"):
                v = it.get(k)
                if isinstance(v, list):
                    stack.extend(v)
                elif isinstance(v, dict):
                    stack.append(v)
    return out


def _offer_price(offers) -> tuple[float | None, str | None, str | None]:
    if not offers:
        return None, None, None
    if isinstance(offers, list):
        offers = offers[0] if offers else {}
    if not isinstance(offers, dict):
        return None, None, None
    price = offers.get("price") or offers.get("lowPrice")
    cur = offers.get("priceCurrency")
    av = (offers.get("availability") or "").split("/")[-1] or None
    return normalize.parse_price(str(price)) if price else None, cur, av


def _first_image(prod: dict) -> str | None:
    img = prod.get("image")
    if isinstance(img, list):
        img = img[0] if img else None
    if isinstance(img, dict):
        img = img.get("url") or img.get("contentUrl")
    return str(img) if img else None


def page_image(soup: BeautifulSoup, url: str) -> str | None:
    for sel, attr in (('meta[property="og:image"]', "content"), ('meta[itemprop="image"]', "content"), ('[itemprop="image"]', "src"), ('link[rel="image_src"]', "href")):
        el = soup.select_one(sel)
        if el is not None and (el.get(attr) or "").strip() and not any(w in el.get(attr).lower() for w in ("logo", "sharing", "share", "icon", "favicon", "placeholder")):
            return urljoin(url, el.get(attr).strip())
    main = soup.find("main") or soup.body or soup
    for img in main.find_all("img", src=True):
        src = img.get("src") or ""
        if any(w in src.lower() for w in ("logo", "icon", "sprite", "pixel", "banner", ".svg", "data:image")):
            continue
        w = img.get("width")
        if w and str(w).isdigit() and int(w) < 80:
            continue
        return urljoin(url, src)
    return None


def card_image(card, url: str) -> str | None:
    for img in card.find_all("img"):
        src = img.get("data-src") or img.get("data-original") or img.get("src") or ""
        if src and not any(w in src.lower() for w in ("logo", "icon", "sprite", "pixel", ".svg", "data:image")):
            return urljoin(url, src)
    return None


SPEC_KEY_HINTS = ("дальност", "радиус", "канал", "частот", "диапазон", "время работы", "автоном", "аккум", "батаре", "вес", "масса", "габарит", "размер",
                  "мощност", "разъём", "разъем", "экран", "дисплей", "приёмник", "приемник", "передатчик", "шумопод", "температур", "гарант", "channel", "range",
                  "battery", "weight", "frequency", "power")


def extract_specs(soup: BeautifulSoup) -> dict[str, str]:
    """Характеристики со страницы: JSON-LD additionalProperty, таблицы th/td, dl/dt/dd, списки «параметр: значение»."""
    specs: dict[str, str] = {}
    for prod in _jsonld_products(soup):
        for ap in prod.get("additionalProperty") or []:
            if isinstance(ap, dict) and ap.get("name") and ap.get("value") is not None:
                specs[normalize.clean_text(str(ap["name"]))] = normalize.clean_text(str(ap["value"]))
    for table in soup.find_all("table"):
        for tr in table.find_all("tr"):
            cells = [normalize.clean_text(c.get_text(" ")) for c in tr.find_all(["th", "td"])]
            cells = [c for c in cells if c]
            if len(cells) == 2 and len(cells[0]) <= 60 and len(cells[1]) <= 120:
                specs.setdefault(cells[0].rstrip(":"), cells[1])
    for dl in soup.find_all("dl"):
        dts, dds = dl.find_all("dt"), dl.find_all("dd")
        for dt, dd in zip(dts, dds):
            k, v = normalize.clean_text(dt.get_text(" ")), normalize.clean_text(dd.get_text(" "))
            if k and v and len(k) <= 60 and len(v) <= 120:
                specs.setdefault(k.rstrip(":"), v)
    for li in soup.find_all(["li", "p"]):
        t = normalize.clean_text(li.get_text(" "))
        if 6 <= len(t) <= 140 and ":" in t and not li.find(["ul", "table"]):
            k, v = t.split(":", 1)
            if 2 <= len(k) <= 50 and v.strip() and any(h in k.lower() for h in SPEC_KEY_HINTS):
                specs.setdefault(k.strip(), v.strip())
    return dict(list(specs.items())[:60])


def parse_product_page(html: str, url: str, cfg: dict | None = None) -> list[dict]:
    soup = BeautifulSoup(html, "lxml")
    cfg = cfg or {}
    items: list[dict] = []
    fallback_img = page_image(soup, url)
    page_specs = extract_specs(soup)
    # 1. CSS-конфиг
    if cfg.get("name"):
        n = soup.select_one(cfg["name"])
        p = soup.select_one(cfg["price"]) if cfg.get("price") else None
        if n:
            items.append({"url": url, "name": normalize.clean_text(n.get_text()),
                          "price": normalize.parse_price(p.get("content") or p.get_text()) if p else None, "image_url": fallback_img})
            return items
    # 2. JSON-LD
    for prod in _jsonld_products(soup):
        price, cur, av = _offer_price(prod.get("offers"))
        brand = prod.get("brand")
        if isinstance(brand, dict):
            brand = brand.get("name")
        items.append({
            "url": prod.get("url") or url, "name": normalize.clean_text(str(prod.get("name") or "")), "brand": brand,
            "sku": prod.get("sku") or prod.get("mpn"), "price": price, "currency": cur or "RUB", "availability": av,
            "description": normalize.clean_text(str(prod.get("description") or ""))[:2000], "image_url": _first_image(prod) or fallback_img,
        })
    items = [i for i in items if i["name"]]
    if items:
        for i in items:
            i.setdefault("specs", page_specs)
        return items
    # 3. Microdata / OpenGraph
    name = ""
    h1 = soup.find("h1")
    if h1 is not None and good_name(h1.get_text(" ")):
        name = normalize.clean_text(h1.get_text(" "))
    if not name:
        og_t = soup.find("meta", property="og:title")
        if og_t is not None and good_name(og_t.get("content", "")):
            name = normalize.clean_text(og_t.get("content", ""))
    if not name:
        for el in soup.find_all(itemprop="name"):
            if el.find_parent(["nav", "ol", "ul"]) is None and good_name(el.get_text(" ")):
                name = normalize.clean_text(el.get_text(" "))
                break
    if not name and soup.title is not None and good_name(soup.title.get_text()):
        name = normalize.clean_text(soup.title.get_text()).split("|")[0].split(" - ")[0].strip()
    price = None
    for sel in ('meta[itemprop="price"]', 'meta[property="product:price:amount"]', 'meta[property="og:price:amount"]', '[itemprop="price"]',
                '[data-product-price-def]'):
        el = soup.select_one(sel)
        if el is not None:
            price = normalize.parse_price(el.get("content") or el.get("data-product-price-def") or el.get_text())
            if price:
                break
    if price is None:
        main = soup.find("main") or soup.body or soup
        for el in price_elements(main):
            ctx = normalize.clean_text(el.get_text(" ")).lower()
            if any(w in ctx for w in NOISE_WORDS):
                continue
            price = normalize.parse_price(normalize.clean_text(el.get_text(" ")))
            if price and price >= 10:
                break
    desc_el = soup.find("meta", attrs={"name": "description"})
    if name:
        body_text = normalize.clean_text((soup.find("main") or soup.body or soup).get_text(" "))[:3000]
        items.append({"url": url, "name": name, "price": price, "description": (normalize.clean_text(desc_el.get("content", "")) if desc_el else "") or body_text[:1500],
                      "image_url": fallback_img, "specs": page_specs})
    return items


def parse_catalog_page(html: str, url: str, cfg: dict | None = None, category_slug: str | None = None) -> list[dict]:
    soup = BeautifulSoup(html, "lxml")
    cfg = cfg or {}
    items: list[dict] = []
    if cfg.get("items"):
        for card in soup.select(cfg["items"]):
            n = card.select_one(cfg.get("name", "a"))
            p = card.select_one(cfg["price"]) if cfg.get("price") else None
            a = card.select_one(cfg.get("link", "a[href]"))
            if not n:
                continue
            items.append({"url": urljoin(url, a["href"]) if a and a.get("href") else url, "name": normalize.clean_text(n.get_text()),
                          "price": normalize.parse_price(p.get("content") or p.get_text()) if p else None, "image_url": card_image(card, url)})
        return [i for i in items if i["name"]]
    # JSON-LD ItemList / Product
    for prod in _jsonld_products(soup):
        price, cur, av = _offer_price(prod.get("offers"))
        if prod.get("name"):
            items.append({"url": prod.get("url") or url, "name": normalize.clean_text(str(prod["name"])), "price": price, "currency": cur or "RUB",
                          "availability": av, "image_url": _first_image(prod)})
    if len(items) >= 2:
        return items
    # Эвристика: карточка = ближайший предок элемента с ценой, содержащий ссылку и короткий текст
    seen = set()
    for pel in price_elements(soup.body or soup):
        node = pel.parent
        card = None
        for _ in range(7):
            if node is None or node.name in ("body", "html"):
                break
            if node.find("a", href=True) and len(normalize.clean_text(node.get_text(" "))) < 700:
                card = node
            node = node.parent
        if card is None:
            continue
        name, link = card_name(card, url)
        if not name:
            continue
        key = (name, link)
        if key in seen:
            continue
        seen.add(key)
        ctx = normalize.clean_text(pel.get_text(" ")).lower()
        if any(w in ctx for w in NOISE_WORDS):
            continue
        items.append({"url": link, "name": name, "price": normalize.parse_price(ctx), "image_url": card_image(card, url)})
    return items


def _relevant(name: str, description: str | None = None) -> bool:
    return normalize.classify_category(name, description) is not None


def _enrich_from_product_pages(conn: sqlite3.Connection, page: dict, items: list[dict], limit: int = 40) -> None:
    """Для позиций каталога открывает страницу товара (тот же домен) и дописывает характеристики, фото и описание.
    Чтобы не нагружать сайт: не чаще раза в 7 дней на товар и не более `limit` страниц за проход."""
    from urllib.parse import urlparse

    dom = urlparse(page["url"]).netloc.lower()
    done = 0
    for it in items:
        if done >= limit:
            break
        url = it.get("url") or ""
        if not url or url == page["url"] or urlparse(url).netloc.lower() != dom or "#" in url or "/cart" in url:
            continue
        if not normalize.classify_category(it["name"], it.get("description")):
            continue
        prev = db.row(conn, "SELECT specs_json, image_url, fetched_at FROM competitor_products WHERE competitor_id=? AND url=? ORDER BY id LIMIT 1", (page["competitor_id"], url))
        if prev and prev["specs_json"] and prev["specs_json"] not in ("{}", "null") and prev["fetched_at"] and prev["fetched_at"] >= db.now_iso()[:10].replace("-", "") and False:
            continue
        if prev and prev["specs_json"] and prev["specs_json"] not in ("{}", "null") and prev["fetched_at"] and prev["fetched_at"][:10] >= (datetime.now(timezone.utc) - timedelta(days=7)).strftime("%Y-%m-%d"):
            continue
        try:
            res = http.fetch(url, f"competitor_{page['competitor_id']}")
        except Exception as exc:  # noqa: BLE001
            log.info("deep fetch skipped %s: %s", url, exc)
            continue
        done += 1
        parsed = parse_product_page(res.text, url)
        if not parsed:
            continue
        d = parsed[0]
        if d.get("specs"):
            it["specs"] = d["specs"]
        if d.get("image_url") and not it.get("image_url"):
            it["image_url"] = d["image_url"]
        if d.get("description") and not it.get("description"):
            it["description"] = d["description"][:2000]
        if d.get("price") and not it.get("price"):
            it["price"] = d["price"]
        if d.get("name") and good_name(d["name"]) and len(d["name"]) > len(it["name"]) + 5:
            it["name"] = d["name"]


def collect_page(conn: sqlite3.Connection, page: dict, run_id: int) -> tuple[int, int]:
    """Обрабатывает одну страницу мониторинга. Возвращает (seen, changed)."""
    cfg = db.uj(page.get("parser_config_json"), {}) or {}
    res = http.fetch(page["url"], f"competitor_{page['competitor_id']}")
    if page["kind"] == "product":
        items = parse_product_page(res.text, page["url"], cfg)
    else:
        items = parse_catalog_page(res.text, page["url"], cfg, page.get("category_slug"))
        items = [i for i in items if _relevant(i["name"], i.get("description")) or page.get("category_slug")]
    seen = changed = 0
    is_rent = page["kind"] == "rent" or bool(re.search(r"(^|[/_.-])(rent|rental|arenda|prokat|аренда)([/_.-]|$)", page["url"].lower()))
    if page["kind"] != "product":
        _enrich_from_product_pages(conn, page, items)
    for it in items:
        it["fetched_at"] = res.fetched_at
        if is_rent:
            it["category_slug"] = "rental"
        if page.get("category_slug") and not it.get("category_slug"):
            it["category_slug"] = normalize.classify_category(it["name"], it.get("description")) or page["category_slug"]
        _, ch = upsert_competitor_product(conn, page["competitor_id"], page["id"], it, run_id, match_by_url=(page["kind"] == "product" and len(items) == 1))
        seen += 1
        changed += 1 if ch else 0
    return seen, changed


SLUG_KEYWORDS = ("radiogu", "radio-gu", "radiogid", "audiogu", "audiogid", "earphone", "headphone", "headset", "naushnik", "charg", "zaryad", "case", "keis", "bag", "sumk",
                 "mic", "mikrofon", "lavalier", "petlich", "synchron", "sinhron", "translat", "perevod", "transmit", "peredat", "receiv", "priem", "tour", "guide", "gid",
                 "reinvox", "retekess", "whisper", "sheptal", "komplekt", "kit", "set", "accessor", "aksessuar", "product", "tovar", "catalog", "shop", "rent", "arenda")
SLUG_EXCLUDE = ("/en/", "/page", "privacy", "offer", "return", "support", "review", "thank", "contact", "about", "news", "blog", "article", "delivery", "payment",
                "policy", "oferta", "vacanc", "sitemap", "login", "cart", "search", "tag/", "faq", "warranty", "garant")


def expand_sitemap(conn: sqlite3.Connection, page: dict, limit: int = 200) -> int:
    """Читает sitemap.xml конкурента и добавляет релевантные страницы товаров как страницы мониторинга (без кода)."""
    from urllib.parse import urlparse

    res = http.fetch(page["url"], f"competitor_{page['competitor_id']}", save=False)
    locs = re.findall(r"<loc>([^<]+)</loc>", res.text)
    # вложенные карты сайта
    nested = [u for u in locs if u.endswith(".xml")]
    for n in nested[:5]:
        try:
            locs += re.findall(r"<loc>([^<]+)</loc>", http.fetch(n, f"competitor_{page['competitor_id']}", save=False).text)
        except Exception:  # noqa: BLE001
            pass
    dom = urlparse(page["url"]).netloc.lower()
    added = 0
    for u in locs:
        if urlparse(u).netloc.lower() != dom or u.endswith(".xml"):
            continue
        path = urlparse(u).path.lower()
        if not path.strip("/") or any(x in u.lower() for x in SLUG_EXCLUDE):
            continue
        if not any(k in path for k in SLUG_KEYWORDS):
            continue
        if db.row(conn, "SELECT id FROM monitored_pages WHERE url=?", (u,)):
            continue
        kind = "rent" if re.search(r"(^|[/_.-])(rent|rental|arenda|prokat)([/_.-]|$)", path) else "product"
        conn.execute("INSERT INTO monitored_pages(competitor_id, url, kind, name, parser) VALUES(?,?,?,?,'auto')", (page["competitor_id"], u, kind, "из карты сайта"))
        added += 1
        if added >= limit:
            break
    conn.execute("UPDATE monitored_pages SET last_checked_at=datetime('now'), last_status='ok', last_error=?, fail_count=0 WHERE id=?", (f"добавлено страниц: {added}", page["id"]))
    conn.commit()
    return added


def run(conn: sqlite3.Connection, competitor_id: int | None = None, page_id: int | None = None) -> dict:
    run_id = db.start_run(conn, SOURCE_KEY)
    conn.commit()
    # сначала раскрываем карты сайтов, чтобы новые страницы попали в этот же проход
    for sp in db.rows(conn, "SELECT * FROM monitored_pages WHERE kind='sitemap' AND is_active=1" + (" AND competitor_id=?" if competitor_id else ""), [competitor_id] if competitor_id else []):
        try:
            log.info("sitemap %s: +%s страниц", sp["url"], expand_sitemap(conn, sp))
        except Exception as exc:  # noqa: BLE001
            db.log_error(conn, SOURCE_KEY, sp["url"], f"sitemap: {exc}")
            conn.commit()
    sql = "SELECT mp.*, c.name AS competitor_name FROM monitored_pages mp JOIN competitors c ON c.id=mp.competitor_id WHERE mp.is_active=1 AND c.is_active=1 AND mp.kind!='sitemap'"
    params: list = []
    if competitor_id:
        sql += " AND mp.competitor_id=?"
        params.append(competitor_id)
    if page_id:
        sql += " AND mp.id=?"
        params.append(page_id)
    pages = db.rows(conn, sql + " ORDER BY mp.competitor_id, mp.id", params)
    seen = changed = errors = 0
    for page in pages:
        try:
            s, c = collect_page(conn, page, run_id)
            seen += s
            changed += c
            status = "ok" if s else "empty"
            conn.execute("UPDATE monitored_pages SET last_checked_at=datetime('now'), last_status=?, last_error=?, fail_count=? WHERE id=?",
                         (status, None if s else "На странице не найдено товаров с ценой", 0 if s else page["fail_count"] + 1, page["id"]))
            if s and page["kind"] != "product":
                # позиция каталога, не встреченная на странице ≥2 дней после успешных проверок, считается исчезнувшей
                conn.execute("UPDATE competitor_products SET is_active=0 WHERE page_id=? AND is_active=1 AND fetched_at IS NOT NULL AND fetched_at < datetime('now', '-2 days') AND url!=?",
                             (page["id"], page["url"]))
            if not s:
                db.log_error(conn, SOURCE_KEY, page["url"], "Не найдено товаров на странице", page["competitor_name"])
        except http.RobotsDisallowed as exc:
            errors += 1
            conn.execute("UPDATE monitored_pages SET last_checked_at=datetime('now'), last_status='robots_disallowed', last_error=?, fail_count=fail_count+1 WHERE id=?",
                         (str(exc), page["id"]))
            db.log_error(conn, SOURCE_KEY, page["url"], str(exc), page["competitor_name"])
        except Exception as exc:  # noqa: BLE001
            errors += 1
            conn.execute("UPDATE monitored_pages SET last_checked_at=datetime('now'), last_status='error', last_error=?, fail_count=fail_count+1 WHERE id=?",
                         (str(exc)[:500], page["id"]))
            db.log_error(conn, SOURCE_KEY, page["url"], str(exc), page["competitor_name"])
        conn.commit()
    # товары конкурентов, которых не видели > 3 запусков подряд их страницы, помечаем неактивными — делает detect_signals
    status = "ok" if errors == 0 else ("partial" if seen else "error")
    db.finish_run(conn, run_id, status, seen, changed, errors, f"{len(pages)} страниц, {seen} товаров, {changed} изменений, {errors} ошибок")
    conn.commit()
    return {"pages": len(pages), "seen": seen, "changed": changed, "errors": errors}
