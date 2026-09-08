"""Категории, задекларированные на сайтах: меню сайта + хлебные крошки на страницах товаров.

Нужно, чтобы сравнивать не наши внутренние категории, а то, как сами продавцы называют разделы каталога,
и сколько разделов у каждого. Результат — таблица site_categories и поле site_category_path у товаров конкурентов.
"""
from __future__ import annotations

import hashlib
import json
import logging
import re
import sqlite3
from urllib.parse import urljoin, urlparse

from bs4 import BeautifulSoup

from . import config, db, http
from .normalize import CATEGORY_NAMES, classify_category, clean_text

log = logging.getLogger(__name__)

SKIP = re.compile(r"контакт|о компании|о нас|доставк|оплат|гарант|отзыв|новост|блог|стать|вакан|портфолио|проект|акци|услуг|партн|faq|вопрос|главная|корзин|вход|войти|личный|кабинет|сравнен|избранн|поиск|политик|карта сайта|дилер|сотрудни|about|contact|delivery|payment|news|blog|cart|login|search|телефон|заказ|скидк|бренд|производител|реквизит|города|москва|санкт|казань|подробнее|инфо$|видео|опыт|медиацентр|поддержк|пользовател|кто уже|публикац|english|version|сферы применения|посещение завода|туризм|школа|ресторан|отель|медицина|склад|розничн|спа-центр|подготовка|просвещение|expand_more|chevron_right|^\d|₽|лет$|раз$|решения для|баров|переговорн|учебных|учережден|гостиниц|музеев|театров|обмен|возврат|тендер|бизнесу|соглашение|оферт|автотовар", re.I)
MODEL_LIKE = re.compile(r"\b[A-Za-z]{1,6}[- ]?\d{2,4}[A-Za-z]?\b|\bmk\s?ii\b|арт\.", re.I)
ICON_WORDS = re.compile(r"\b(expand_more|chevron_right|keyboard_arrow_down|menu)\b", re.I)
BRANDS = ("reinvox", "spbaudio", "beyerdynamic", "sennheiser", "bosch", "soolai", "vesco", "cromi", "retekess", "okayo", "touraudio", "zoweetek", "rolton", "rоlton", "shidu", "radioguide", "crestron")


def _clean_name(t: str) -> str:
    t = ICON_WORDS.sub(" ", t)
    t = re.sub(r"\s+", " ", t).strip(" -–—:·|")
    return t


def _is_category_name(t: str) -> bool:
    if not t or len(t) < 3 or len(t) > 45 or "?" in t:
        return False
    if SKIP.search(t) or MODEL_LIKE.search(t):
        return False
    low = t.lower()
    if any(low.startswith(b) or low == b for b in BRANDS):
        return False  # пункт меню = бренд/модель, а не категория
    if not re.search(r"[а-яё]", low) and len(low.split()) <= 2:
        return False  # латиница в одно-два слова на русском сайте — почти всегда бренд (Barco, BenQ, Life Size)
    if re.match(r"^(каталог|весь каталог|все товары|товары|продукция|оборудование|компания|магазин|обзоры|инфо|tproduct|другие|прочее)$", low):
        return False
    if re.search(r"\s[–—-]\s|выгодно|удобно|лучшее|незаменим|максимум|техника будущего|для экскурсионных целей|для музеев и|на производстве|в крыму|в мурманске|международного", low):
        return False  # заголовки статей из боковых меню
    return True


def fetch_menu(site_url: str) -> list[dict]:
    """Пункты меню сайта, похожие на категории каталога: [{name, url, level}]."""
    res = http.fetch(site_url, "menu", save=False, timeout=25)
    soup = BeautifulSoup(res.text, "lxml")
    dom = urlparse(site_url).netloc.lower().replace("www.", "")
    found: list[dict] = []
    seen: set[str] = set()
    for nav in soup.select("nav, header, [class*=menu], [class*=nav], [id*=menu], [id*=nav], [class*=catalog], aside"):
        for a in nav.find_all("a", href=True):
            name = _clean_name(clean_text(a.get_text(" ", strip=True)))
            url = urljoin(site_url, a["href"]).split("#")[0]
            if urlparse(url).netloc.lower().replace("www.", "") != dom or urlparse(url).path.strip("/") == "":
                continue  # чужой домен или ссылка на главную (логотип, «вызов персонала» → корень сайта)
            key = name.lower()
            if key in seen or not _is_category_name(name):
                continue
            seen.add(key)
            found.append({"name": name, "url": url, "level": 1})
    # уровень: если адрес категории начинается с адреса другой категории — это подкатегория
    urls = [f["url"].rstrip("/") for f in found]
    for f in found:
        u = f["url"].rstrip("/")
        parents = [p for p in urls if p and p != u and u.startswith(p + "/")]
        if parents:
            f["level"] = 2
            f["parent"] = next(x["name"] for x in found if x["url"].rstrip("/") == max(parents, key=len))
    return found


def _raw_html(cid: int, url: str) -> str | None:
    name = hashlib.sha1(url.encode("utf-8")).hexdigest()[:16] + ".html"
    folder = config.RAW_DIR / f"competitor_{cid}"
    if not folder.exists():
        return None
    for day in sorted(folder.iterdir(), reverse=True):
        p = day / name
        if p.exists():
            return p.read_text(encoding="utf-8", errors="replace")
    return None


def breadcrumbs(html: str) -> list[str]:
    """Хлебные крошки: JSON-LD BreadcrumbList или блок с классом breadcrumb; без «Главная» и без последнего элемента (сам товар)."""
    soup = BeautifulSoup(html, "lxml")
    path: list[str] = []
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
            if isinstance(it.get("@graph"), list):
                stack.extend(it["@graph"])
            if it.get("@type") == "BreadcrumbList":
                for e in sorted(it.get("itemListElement", []), key=lambda x: x.get("position", 0) if isinstance(x, dict) else 0):
                    if not isinstance(e, dict):
                        continue
                    nm = e.get("name") or (e.get("item", {}) or {}).get("name") if isinstance(e.get("item"), dict) else e.get("name")
                    if nm:
                        path.append(clean_text(str(nm)))
    if not path:
        for el in soup.select('[itemtype*="BreadcrumbList"], [class*="breadcrumb"], [class*="crumb"], nav[aria-label*="readcrumb"]'):
            items = []
            for a in el.find_all(["a", "span", "li"]):
                t = clean_text(a.get_text(" ", strip=True))
                if t and t not in ("—", "-", "/", "»", ">") and (not items or items[-1] != t):
                    items.append(t)
            if items:
                path = items
                break
    path = [p for p in path if p and not re.match(r"^(главная|home|main|каталог|продукты|продажа|купить|все товары|магазин)$", p, re.I)]
    if len(path) >= 2:
        path = path[:-1]  # последний элемент — название товара
    elif len(path) == 1:
        path = []
    return path[:4]


def assign_product_paths(conn: sqlite3.Connection, competitor_id: int, force: bool = False) -> int:
    """Заполняет site_category_path у товаров конкурента из хлебных крошек сохранённых страниц."""
    n = 0
    cache: dict[str, str | None] = {}
    for r in db.rows(conn, "SELECT id, url FROM competitor_products WHERE competitor_id=? AND is_active=1" + ("" if force else " AND site_category_path IS NULL"), (competitor_id,)):
        url = r["url"].split("#")[0]
        if url not in cache:
            html = _raw_html(competitor_id, url)
            crumbs = breadcrumbs(html) if html else []
            cache[url] = " / ".join(crumbs) if crumbs else ""  # '' = страница разобрана, крошек нет (чтобы не разбирать повторно)
        conn.execute("UPDATE competitor_products SET site_category_path=? WHERE id=?", (cache[url], r["id"]))
        n += 1 if cache[url] else 0
    conn.commit()
    return n


def _count_products(conn: sqlite3.Connection, competitor_id: int | None, site: str, cat: dict) -> int:
    if competitor_id is None:
        return db.row(conn, "SELECT COUNT(*) n FROM m22_products WHERE site=? AND is_active=1 AND parent_url IS NULL AND site_category_path LIKE ?", (site, f"%{cat['name']}%"))["n"]
    u = cat["url"].rstrip("/")
    return db.row(conn, """SELECT COUNT(*) n FROM competitor_products WHERE competitor_id=? AND is_active=1
                           AND ((site_category_path IS NOT NULL AND site_category_path LIKE ?) OR (? != '' AND (url LIKE ? OR url LIKE ?)))""",
                  (competitor_id, f"%{cat['name']}%", u, u + "/%", u.replace("https://", "http://") + "/%"))["n"]


def scan_site(conn: sqlite3.Connection, site: str, competitor_id: int | None, site_url: str) -> dict:
    """Собирает категории с сайта (меню) и, для конкурентов, хлебные крошки; ручные записи (source='manual') не трогает."""
    try:
        menu = fetch_menu(site_url)
    except Exception as exc:  # noqa: BLE001
        log.warning("menu %s: %s", site_url, exc)
        menu = []
    if competitor_id is not None:
        assign_product_paths(conn, competitor_id)
        # хлебные крошки дают категории, которых может не быть в меню
        for r in db.rows(conn, "SELECT site_category_path p, COUNT(*) n FROM competitor_products WHERE competitor_id=? AND is_active=1 AND site_category_path IS NOT NULL GROUP BY 1", (competitor_id,)):
            parts = [x.strip() for x in r["p"].split(" / ") if x.strip()]
            for i, part in enumerate(parts):
                if _is_category_name(part) and part.lower() not in {m["name"].lower() for m in menu}:
                    menu.append({"name": part, "url": "", "level": i + 1, "parent": parts[i - 1] if i else None, "from_crumbs": True})
    else:
        for r in db.rows(conn, "SELECT DISTINCT site_category_path p FROM m22_products WHERE site=? AND is_active=1 AND site_category_path IS NOT NULL", (site,)):
            for i, part in enumerate([x.strip() for x in re.split(r"\s*[/>»]\s*", r["p"]) if x.strip()]):
                if _is_category_name(part) and part.lower() not in {m["name"].lower() for m in menu}:
                    menu.append({"name": part, "url": "", "level": i + 1, "from_crumbs": True})
    conn.execute("DELETE FROM site_categories WHERE site=? AND source!='manual'", (site,))
    for m in menu:
        conn.execute("""INSERT OR REPLACE INTO site_categories(site, competitor_id, name, url, parent, level, our_slug, product_count, source, checked_at)
                        VALUES(?,?,?,?,?,?,?,?,?,datetime('now'))""",
                     (site, competitor_id, m["name"], m.get("url"), m.get("parent"), m.get("level", 1), classify_category(m["name"]),
                      _count_products(conn, competitor_id, site, m), "breadcrumbs" if m.get("from_crumbs") else "menu"))
    conn.commit()
    return {"site": site, "categories": len(menu)}


def scan_tier(conn: sqlite3.Connection, tier: str = "A") -> list[dict]:
    out = [scan_site(conn, "m22.ru", None, "https://m22.ru/"), scan_site(conn, "radiosync.ru", None, "https://radiosync.ru/")]
    for c in db.rows(conn, "SELECT id, website FROM competitors WHERE is_active=1 AND tier=? AND website NOT LIKE 'tender:%' ORDER BY id", (tier,)):
        site = urlparse(c["website"]).netloc.lower().replace("www.", "")
        out.append(scan_site(conn, site, c["id"], c["website"]))
    return out


def comparison(conn: sqlite3.Connection) -> dict:
    """Данные для вкладки «Категории на сайтах»: по каждому сайту список категорий; матрица «наша категория × сайт»."""
    rows = db.rows(conn, """SELECT sc.*, COALESCE(c.group_name, c.name) AS seller, c.tier FROM site_categories sc LEFT JOIN competitors c ON c.id=sc.competitor_id
                            ORDER BY CASE WHEN sc.competitor_id IS NULL THEN 0 ELSE 1 END, c.tier, sc.site, sc.level, sc.id""")
    sites: dict[str, dict] = {}
    for r in rows:
        s = sites.setdefault(r["site"], {"site": r["site"], "competitor_id": r["competitor_id"], "seller": r["seller"] or "M22", "tier": r["tier"] or "M22", "cats": [], "top": 0})
        s["cats"].append(r)
        s["top"] += 1 if r["level"] == 1 else 0
    slugs = [s for s in CATEGORY_NAMES if any(r["our_slug"] == s for r in rows)] + ["_none"]
    matrix = {slug: {site: [r["name"] for r in s["cats"] if (r["our_slug"] or "_none") == slug] for site, s in sites.items()} for slug in slugs}
    return {"sites": list(sites.values()), "slugs": slugs, "matrix": matrix, "names": {**CATEGORY_NAMES, "_none": "Вне нашего контура / не сопоставлено"}}
