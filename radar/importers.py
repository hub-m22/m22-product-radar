"""Импорт данных: реестр конкурентов (JSON/CSV), семантическая карта (JSON/CSV), Wordstat CSV/XLSX, Google Trends CSV, наблюдения рынка."""
from __future__ import annotations

import csv
import io
import json
import re
import sqlite3
from collections import Counter
from datetime import date
from pathlib import Path

from . import db, normalize
from .collectors.common import upsert_competitor_product

TYPE_MAP = {
    "прямой продавец": "direct_seller", "производитель": "brand_owner", "владелец бренда": "brand_owner", "интегратор": "integrator",
    "готовых b2b": "b2b_solutions", "для музеев": "museum_supplier", "синхронного перевода": "sync_translation_supplier", "арендн": "rental",
    "для мероприятий": "events_supplier", "маркетплейс": "marketplace_seller", "зарубежн": "foreign", "косвенн": "indirect", "заменител": "substitute",
}
TYPE_NAMES = {
    "direct_seller": "Прямой продавец аналогичного оборудования", "brand_owner": "Производитель / владелец бренда", "integrator": "Интегратор",
    "b2b_solutions": "Поставщик готовых B2B-решений", "museum_supplier": "Поставщик оборудования для музеев", "sync_translation_supplier": "Поставщик систем синхронного перевода",
    "rental": "Арендная компания", "events_supplier": "Поставщик оборудования для мероприятий", "marketplace_seller": "Маркетплейс-продавец", "foreign": "Зарубежная компания",
    "indirect": "Косвенный конкурент", "substitute": "Технологический заменитель",
}


def normalize_type(t: str) -> str:
    low = t.lower()
    for k, v in TYPE_MAP.items():
        if k in low:
            return v
    return t if t in TYPE_NAMES else "indirect"


def _scrapable(text: str | None) -> str:
    low = (text or "").lower()
    if "js" in low or "динамич" in low or "антибот" in low or "403" in low:
        return "js" if "js" in low or "динамич" in low else "blocked"
    if "n/a" in low:
        return "unknown"
    if "статич" in low:
        return "static"
    return "unknown"


def _root(url: str) -> str:
    return re.sub(r"^(https?://[^/]+).*$", r"\1", url.strip())


def import_competitors_json(conn: sqlite3.Connection, path: Path | str, added_by: str = "research") -> dict:
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    comps = data.get("competitors", data if isinstance(data, list) else [])
    checked = (data.get("meta") or {}).get("date_checked") if isinstance(data, dict) else None
    created = pages = products = 0
    for c in comps:
        website = _root(c["website"])
        types = [normalize_type(t) for t in (c.get("types") or [])]
        rental = c.get("rental_available")
        rental_s = "yes" if rental is True else ("no" if rental is False else "unknown")
        fields = dict(name=c["name"], types_json=db.j(sorted(set(types))), geography=c.get("geography"), categories_json=db.j(c.get("categories") or []),
                      brands_json=db.j(c.get("brands") or []), rental_available=rental_s, advantages=c.get("advantages_positioning") or c.get("advantages"),
                      target_segments=", ".join(c.get("target_segments") or []) if isinstance(c.get("target_segments"), list) else c.get("target_segments"),
                      new_products=", ".join(c.get("new_products") or []) if isinstance(c.get("new_products"), list) else c.get("new_products"),
                      notes=c.get("notes") or c.get("scrapability"), source_urls_json=db.j(c.get("sources") or []), scrapable=_scrapable(c.get("scrapability")),
                      checked_at=checked or date.today().isoformat(), added_by=added_by)
        row = db.row(conn, "SELECT id FROM competitors WHERE website=?", (website,))
        if row:
            sets = ", ".join(f"{k}=?" for k in fields)
            conn.execute(f"UPDATE competitors SET {sets}, updated_at=datetime('now') WHERE id=?", (*fields.values(), row["id"]))
            cid = row["id"]
        else:
            cols = ", ".join(["website", *fields])
            conn.execute(f"INSERT INTO competitors({cols}) VALUES({', '.join('?' for _ in range(len(fields) + 1))})", (website, *fields.values()))
            cid = db.row(conn, "SELECT id FROM competitors WHERE website=?", (website,))["id"]
            created += 1
        # страницы мониторинга: URL, встречающийся у нескольких товаров, — каталог
        prods = c.get("products") or []
        url_counts = Counter(p["url"] for p in prods if p.get("url"))
        for p in prods:
            url = p.get("url")
            if not url:
                continue
            kind = "catalog" if url_counts[url] > 1 or url.rstrip("/").endswith(("/catalog", "/radioguide", "/rent", "/arenda", "/shop", "/products")) else "product"
            cat = normalize.classify_category(p["name"], p.get("note"))
            if not db.row(conn, "SELECT id FROM monitored_pages WHERE url=?", (url,)):
                conn.execute("INSERT INTO monitored_pages(competitor_id, url, kind, name, category_slug, parser) VALUES(?,?,?,?,?,?)",
                             (cid, url, kind, p["name"][:120], cat, "auto"))
                pages += 1
            page = db.row(conn, "SELECT id FROM monitored_pages WHERE url=?", (url,))
            price = p.get("price_rub") if p.get("price_rub") is not None else p.get("price")
            note = p.get("note") or ""
            currency = "RUB"
            if p.get("currency"):
                currency = p["currency"]
            item = {"url": url, "name": p["name"], "price": float(price) if price else None, "currency": currency, "brand": None,
                    "description": f"Замечено при исследовании {checked or date.today().isoformat()}. {note}".strip(), "fetched_at": (checked or date.today().isoformat()) + "T00:00:00Z",
                    "category_slug": cat}
            upsert_competitor_product(conn, cid, page["id"], item, None)
            products += 1
    # наблюдения рынка
    obs = 0
    if isinstance(data, dict):
        for t in data.get("tenders") or []:
            key = f"tender:{t.get('number') or t.get('url') or t.get('subject')}"
            if not db.row(conn, "SELECT id FROM market_observations WHERE dedupe_key=?", (key,)):
                conn.execute("INSERT INTO market_observations(kind,title,party,region,price,observed_date,url,source,note,dedupe_key) VALUES('tender',?,?,?,?,?,?,?,?,?)",
                             (t.get("subject") or t.get("title") or key, t.get("customer"), t.get("region"), t.get("price_rub"), t.get("date"), t.get("url"), "поисковые сниппеты/ЕИС", t.get("note"), key))
                obs += 1
        for m in data.get("marketplace_listings") or []:
            key = f"mp:{m.get('url') or m.get('product')}"
            if not db.row(conn, "SELECT id FROM market_observations WHERE dedupe_key=?", (key,)):
                conn.execute("INSERT INTO market_observations(kind,title,party,price,url,source,note,dedupe_key,category_slug) VALUES('marketplace_listing',?,?,?,?,?,?,?,?)",
                             (m.get("product") or key, f"{m.get('marketplace')}: {m.get('seller')}", m.get("price_rub"), m.get("url"), m.get("marketplace"), m.get("note"), key,
                              normalize.classify_category(m.get("product") or "")))
                obs += 1
    conn.commit()
    return {"competitors_created": created, "pages_created": pages, "products": products, "observations": obs}


def import_semantic_map_json(conn: sqlite3.Connection, path: Path | str) -> dict:
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    seeds = set(q.lower() for q in data.get("seed_terms", []))
    n = 0
    for q in data.get("queries", []):
        text = normalize.clean_text(q["query"]).lower()
        if not text:
            continue
        if db.row(conn, "SELECT id FROM search_queries WHERE query=?", (text,)):
            conn.execute("UPDATE search_queries SET category_slug=?, intent=?, lang=?, is_brand=?, brand_or_model=?, notes=?, is_seed=? WHERE query=?",
                         (q.get("category"), q.get("intent"), q.get("lang", "ru"), 1 if q.get("is_brand") else 0, q.get("brand_or_model"), q.get("notes"), 1 if text in seeds else 0, text))
        else:
            conn.execute("INSERT INTO search_queries(query, category_slug, intent, lang, is_brand, brand_or_model, notes, is_seed) VALUES(?,?,?,?,?,?,?,?)",
                         (text, q.get("category"), q.get("intent"), q.get("lang", "ru"), 1 if q.get("is_brand") else 0, q.get("brand_or_model"), q.get("notes"), 1 if text in seeds else 0))
            n += 1
    for s in data.get("seed_terms", []):
        t = normalize.clean_text(s).lower()
        if not db.row(conn, "SELECT id FROM search_queries WHERE query=?", (t,)):
            conn.execute("INSERT INTO search_queries(query, category_slug, intent, is_seed) VALUES(?,?,?,1)", (t, normalize.classify_category(t), "commercial"))
            n += 1
        else:
            conn.execute("UPDATE search_queries SET is_seed=1 WHERE query=?", (t,))
    conn.commit()
    return {"queries_created": n}


# ---------- CSV / XLSX ----------
def _read_table(filename: str, content: bytes) -> list[dict]:
    if filename.lower().endswith((".xlsx", ".xlsm")):
        import openpyxl

        wb = openpyxl.load_workbook(io.BytesIO(content), read_only=True, data_only=True)
        ws = wb.active
        rows = list(ws.iter_rows(values_only=True))
        if not rows:
            return []
        header = [str(h).strip().lower() if h is not None else f"col{i}" for i, h in enumerate(rows[0])]
        return [dict(zip(header, r)) for r in rows[1:] if any(v is not None for v in r)]
    text = content.decode("utf-8-sig", errors="replace")
    sample = text[:2000]
    delim = ";" if sample.count(";") > sample.count(",") else ("\t" if sample.count("\t") > sample.count(",") else ",")
    reader = csv.DictReader(io.StringIO(text), delimiter=delim)
    return [{(k or "").strip().lower(): v for k, v in r.items()} for r in reader]


def _pick(row: dict, *names):
    for n in names:
        for k, v in row.items():
            if k == n or (n in k):
                return v
    return None


def _period(value) -> tuple[str, str] | None:
    """'2026-08' | '08.2026' | 'август 2026' | date -> (start, end)"""
    if value is None:
        return None
    if hasattr(value, "strftime"):
        d = value
        return d.strftime("%Y-%m-01"), d.strftime("%Y-%m-28")
    s = str(value).strip()
    m = re.match(r"^(\d{4})-(\d{2})(?:-(\d{2}))?$", s)
    if m:
        y, mo = m.group(1), m.group(2)
        return f"{y}-{mo}-01", f"{y}-{mo}-28"
    m = re.match(r"^(\d{2})[./](\d{4})$", s)
    if m:
        return f"{m.group(2)}-{m.group(1)}-01", f"{m.group(2)}-{m.group(1)}-28"
    months = ["январ", "феврал", "март", "апрел", "ма", "июн", "июл", "август", "сентябр", "октябр", "ноябр", "декабр"]
    m = re.match(r"^([а-яё]+)\s+(\d{4})$", s.lower())
    if m:
        for i, mm in enumerate(months, 1):
            if m.group(1).startswith(mm):
                return f"{m.group(2)}-{i:02d}-01", f"{m.group(2)}-{i:02d}-28"
    return None


def import_wordstat_table(conn: sqlite3.Connection, filename: str, content: bytes, default_period: str | None = None) -> dict:
    """Импорт выгрузки Wordstat: колонки query|запрос, impressions|показы|число запросов, period|период|месяц (опц.), region (опц.)."""
    rows = _read_table(filename, content)
    added = skipped = 0
    for r in rows:
        q = _pick(r, "query", "запрос", "фраза", "ключ")
        v = _pick(r, "impressions", "показ", "число запросов", "count", "частот")
        p = _pick(r, "period", "период", "месяц", "date", "дата")
        geo = _pick(r, "region", "регион", "geo") or "RU"
        if not q or v in (None, ""):
            skipped += 1
            continue
        try:
            val = float(str(v).replace(" ", "").replace(" ", "").replace(",", "."))
        except ValueError:
            skipped += 1
            continue
        per = _period(p) or _period(default_period) or (date.today().strftime("%Y-%m-01"), date.today().strftime("%Y-%m-28"))
        text = normalize.clean_text(str(q)).lower()
        qrow = db.row(conn, "SELECT id FROM search_queries WHERE query=?", (text,))
        if not qrow:
            conn.execute("INSERT INTO search_queries(query, category_slug, intent, added_by) VALUES(?,?,?,?)", (text, normalize.classify_category(text), "commercial", "import"))
            qrow = db.row(conn, "SELECT id FROM search_queries WHERE query=?", (text,))
        conn.execute("""INSERT INTO demand_observations(query_id, source, period_start, period_end, value, unit, geo, meta_json) VALUES(?,?,?,?,?,?,?,?)
                        ON CONFLICT(query_id, source, period_start, period_end, geo) DO UPDATE SET value=excluded.value, fetched_at=datetime('now')""",
                     (qrow["id"], "wordstat_import", per[0], per[1], val, "impressions", str(geo), db.j({"file": filename})))
        added += 1
    conn.execute("INSERT INTO imports(kind, filename, rows, status, message) VALUES('wordstat',?,?,?,?)", (filename, added, "ok", f"добавлено {added}, пропущено {skipped}"))
    conn.execute("UPDATE sources SET status='ok', last_success_at=datetime('now'), last_run_at=datetime('now') WHERE key='wordstat_import'")
    conn.commit()
    return {"added": added, "skipped": skipped}


def import_trends_csv(conn: sqlite3.Connection, filename: str, content: bytes) -> dict:
    """Импорт CSV, выгруженного из trends.google.com (первые строки — заголовок 'Категория', далее 'Неделя,запрос: (Россия)')."""
    text = content.decode("utf-8-sig", errors="replace")
    lines = [l for l in text.splitlines() if l.strip()]
    start = next((i for i, l in enumerate(lines) if re.match(r"^(Неделя|Week|Месяц|Month|День|Day)", l)), None)
    if start is None:
        return {"added": 0, "error": "Не найдена строка заголовка 'Неделя,...'"}
    reader = csv.reader(io.StringIO("\n".join(lines[start:])))
    header = next(reader)
    names = [re.sub(r":\s*\(.*\)$", "", h).strip().lower() for h in header[1:]]
    qids = []
    for nme in names:
        qrow = db.row(conn, "SELECT id FROM search_queries WHERE query=?", (nme,))
        if not qrow:
            conn.execute("INSERT INTO search_queries(query, category_slug, intent, added_by) VALUES(?,?,?,?)", (nme, normalize.classify_category(nme), "commercial", "import"))
            qrow = db.row(conn, "SELECT id FROM search_queries WHERE query=?", (nme,))
        qids.append(qrow["id"])
    added = 0
    for row in reader:
        if not row or not row[0]:
            continue
        d = row[0].strip()
        for qid, v in zip(qids, row[1:]):
            v = v.strip().replace("<", "")
            if not v.isdigit():
                continue
            conn.execute("""INSERT INTO demand_observations(query_id, source, period_start, period_end, value, unit, geo, meta_json) VALUES(?,?,?,?,?,?,?,?)
                            ON CONFLICT(query_id, source, period_start, period_end, geo) DO UPDATE SET value=excluded.value, fetched_at=datetime('now')""",
                         (qid, "google_trends", d, d, float(v), "index", "RU", db.j({"file": filename})))
            added += 1
    conn.execute("INSERT INTO imports(kind, filename, rows, status, message) VALUES('trends_csv',?,?,?,?)", (filename, added, "ok", f"добавлено {added}"))
    conn.commit()
    return {"added": added}


def import_queries_table(conn: sqlite3.Connection, filename: str, content: bytes) -> dict:
    rows = _read_table(filename, content)
    n = 0
    for r in rows:
        q = _pick(r, "query", "запрос")
        if not q:
            continue
        text = normalize.clean_text(str(q)).lower()
        if db.row(conn, "SELECT id FROM search_queries WHERE query=?", (text,)):
            continue
        conn.execute("INSERT INTO search_queries(query, category_slug, intent, added_by) VALUES(?,?,?,?)",
                     (text, _pick(r, "category", "категор") or normalize.classify_category(text), _pick(r, "intent", "намерен") or "commercial", "import"))
        n += 1
    conn.execute("INSERT INTO imports(kind, filename, rows, status) VALUES('queries',?,?,'ok')", (filename, n))
    conn.commit()
    return {"added": n}


def import_competitors_table(conn: sqlite3.Connection, filename: str, content: bytes) -> dict:
    """CSV/XLSX: name; website; types (через |); geography; product_url (опц.); product_name (опц.); price (опц.)"""
    rows = _read_table(filename, content)
    created = pages = 0
    for r in rows:
        name, website = _pick(r, "name", "назван"), _pick(r, "website", "сайт", "url")
        if not name or not website:
            continue
        website = _root(str(website) if str(website).startswith("http") else "https://" + str(website))
        row = db.row(conn, "SELECT id FROM competitors WHERE website=?", (website,))
        if not row:
            types = [normalize_type(t) for t in re.split(r"[|;,]", str(_pick(r, "types", "тип") or "direct_seller"))]
            conn.execute("INSERT INTO competitors(name, website, types_json, geography, added_by) VALUES(?,?,?,?,'import')",
                         (str(name), website, db.j(types), _pick(r, "geography", "геогр")))
            row = db.row(conn, "SELECT id FROM competitors WHERE website=?", (website,))
            created += 1
        purl = _pick(r, "product_url", "страница")
        if purl and not db.row(conn, "SELECT id FROM monitored_pages WHERE url=?", (purl,)):
            conn.execute("INSERT INTO monitored_pages(competitor_id, url, kind, name) VALUES(?,?,?,?)", (row["id"], purl, "product", _pick(r, "product_name", "товар")))
            pages += 1
    conn.execute("INSERT INTO imports(kind, filename, rows, status) VALUES('competitors',?,?,'ok')", (filename, created))
    conn.commit()
    return {"competitors_created": created, "pages_created": pages}


def import_competitor_prices_table(conn: sqlite3.Connection, filename: str, content: bytes) -> dict:
    """Ручной импорт цен (например с маркетплейсов/Alibaba): competitor; url; name; price; currency (опц.); date (опц.)"""
    rows = _read_table(filename, content)
    n = 0
    for r in rows:
        cname, url, name, price = _pick(r, "competitor", "конкурент", "продавец"), _pick(r, "url", "ссылка"), _pick(r, "name", "товар", "назван"), _pick(r, "price", "цена")
        if not (cname and name):
            continue
        comp = db.row(conn, "SELECT id FROM competitors WHERE name=? OR website=?", (str(cname), str(cname)))
        if not comp:
            conn.execute("INSERT INTO competitors(name, website, types_json, added_by) VALUES(?,?,?,'import')", (str(cname), f"manual://{cname}", db.j(["marketplace_seller"])))
            comp = db.row(conn, "SELECT id FROM competitors WHERE name=?", (str(cname),))
        item = {"url": str(url or f"manual://{cname}/{name}"), "name": str(name), "price": normalize.parse_price(str(price)) if price else None,
                "currency": str(_pick(r, "currency", "валюта") or "RUB"), "description": f"Ручной импорт из {filename}",
                "fetched_at": (str(_pick(r, "date", "дата") or date.today().isoformat()))[:10] + "T00:00:00Z"}
        upsert_competitor_product(conn, comp["id"], None, item, None)
        n += 1
    conn.execute("INSERT INTO imports(kind, filename, rows, status) VALUES('competitor_prices',?,?,'ok')", (filename, n))
    conn.commit()
    return {"added": n}


def import_sources_audit_json(conn: sqlite3.Connection, path: Path | str) -> dict:
    """Импорт аудита источников (JSON): обновляет описательные поля реестра, добавляет отсутствующие источники как справочные записи."""
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    key_map = {"wordstat api": "wordstat", "wordstat — веб": "wordstat", "pytrends": "google_trends", "search.wb.ru": "wildberries", "ozon": "ozon", "яндекс маркет": "yandex_market",
               "расширенный поиск закупок": "zakupki", "alibaba": "alibaba", "1688": "1688", "made-in-china": "made_in_china", "m22.ru": "m22.ru",
               "поисковая выдача": "serp", "yandex search api": "serp", "per-shipment": "customs", "агрегированная таможенная": "customs"}
    n = 0
    for s in data:
        name = s.get("source") or ""
        low = name.lower()
        key = next((v for k, v in key_map.items() if k in low), None)
        fields = dict(data_available=s.get("data_available"), official_api=s.get("official_api"), needs_parsing=1 if str(s.get("needs_parsing")).lower() in ("true", "1", "да", "yes") else 0,
                      needs_auth=1 if str(s.get("needs_auth")).lower() in ("true", "1", "да", "yes") else 0, cost=s.get("cost"), limits=s.get("limits"),
                      update_frequency=s.get("update_frequency"), stability=s.get("stability"), risks=s.get("legal_technical_risks"), how_to_connect=s.get("how_to_connect"),
                      notes=("Проверка 2026-09-04: " + (s.get("test_result") or ""))[:1500], mvp_suitability=s.get("mvp_suitability"))
        if key and db.row(conn, "SELECT id FROM sources WHERE key=?", (key,)):
            existing = db.row(conn, "SELECT * FROM sources WHERE key=?", (key,))
            # не понижаем статус уже подключённого источника
            if existing["status"] == "ok":
                fields.pop("mvp_suitability", None)
            db.upsert_source(conn, key, **{k: v for k, v in fields.items() if v is not None})
        else:
            slug = "audit_" + re.sub(r"[^a-z0-9]+", "_", low)[:40].strip("_")
            status = {"ready": "unknown", "manual_import": "manual_import", "needs_key": "needs_auth", "blocked": "blocked", "paid": "paid"}.get(s.get("mvp_suitability"), "unknown")
            kind = "manufacturer" if any(w in low for w in ("производител", "retekess", "okayo", "williams", "sennheiser", "listen", "mipro", "vox", "china", "alibaba", "1688")) else (
                "competitor" if any(w in low for w in (".ru", "конкурент")) else ("tenders" if "тендер" in low or "zakupki" in low else ("customs" if "фтс" in low else ("marketplace" if any(w in low for w in ("ozon", "маркет", "wildberries", "прайс")) else "demand"))))
            db.upsert_source(conn, slug, name=name[:120], kind=kind, url=s.get("url"), status=status, **fields)
        n += 1
    conn.commit()
    return {"sources": n}
