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

SKIP = re.compile(r"контакт|о компании|о нас|доставк|оплат|гарант|отзыв|новост|блог|стать|вакан|портфолио|проект|акци|услуг|партн|faq|вопрос|главная|корзин|вход|войти|личный|кабинет|сравнен|избранн|поиск|политик|карта сайта|дилер|сотрудни|about|contact|delivery|payment|news|blog|cart|login|search|телефон|заказ|скидк|бренд|производител|реквизит|города|москва|санкт|казань|подробнее|инфо$|видео|опыт|медиацентр|поддержк|пользовател|кто уже|публикац|english|version|сферы применения|посещение завода|туризм|школа|ресторан|отель|медицина|склад|розничн|спа-центр|подготовка|просвещение|expand_more|chevron_right|^\d|₽|лет$|раз$|баров|переговорн|учебных|учережден|гостиниц|музеев|театров|обмен|возврат|тендер|бизнесу|соглашение|оферт|автотовар|согласие|перейти|^купить$|^продажа|производство|аудиозапис|переводы на|под ключ|аутсорсинг|^с |^по ", re.I)
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
    if not re.search(r"[а-яё]", low) and not re.search(r"gps|guide|tour", low):
        return False  # латиница на русском сайте — бренд (Barco, BenQ) или англоязычная страница
    if re.match(r"^(каталог|весь каталог|все товары|товары|продукция|оборудование|компания|магазин|обзоры|инфо|tproduct|другие|прочее)$", low):
        return False
    if re.search(r"выгодно|удобно|лучшее|незаменим|максимум|техника будущего|для экскурсионных целей|для музеев и|на производстве|в крыму|в мурманске|международного|преимуществ|стоимость|организация|конференция с|"
                 r"приемник синхронного|синхронный перевод речи|мобильное оборудование|рекомендуем|^для |что такое|как выбрать|max-?\d|iso\s?\d", low):
        return False  # заголовки статей и подпункты из боковых меню, отдельные модели
    return True

# Разделы сайтов M22 → наши категории (вручную, точнее автоматики; несколько категорий через запятую)
M22_OVERRIDES = {
    "радиогиды и аудиогиды для экскурсий": "radiogid,audiogid",
    "готовые решения для экскурсий": "kits_solutions",
    "одноразовые наушники": "disposable_headphones",
    "многоразовые наушники": "reusable_headphones",
    "комплектующие для гидов": "reusable_headphones,microphones_guide,charging_cases",
    "синхронный перевод": "sync_translation",
    "интерком - системы": "intercom_events",
    "интерком-системы": "intercom_events",
    "гарнитуры": "reusable_headphones,microphones_guide",
    "беспроводные наушники": "reusable_headphones",
    "беспроводные петличные микрофоны": "microphones_guide",
    "микрофоны конденсаторные": "microphones_guide",
    "рации и аксессуары": "radio_walkie",
    "рации": "radio_walkie",
    "гарнитуры для раций": "radio_walkie",
    "зарядные устройства": "charging_cases",
    "зарядные станции": "charging_cases",
    "элементы питания": "charging_cases",
    "наушники и аксессуары": "reusable_headphones",
    "аксессуары": "reusable_headphones,charging_cases",
    "радиогиды": "radiogid",
    "аренда": "rental",
}


# Разделы конкурентов, которые автоматика по словам не распознаёт
COMPETITOR_OVERRIDES = {
    "приложения": "substitutes_apps",
    "gps-гид": "audiogid",
    "gps гид": "audiogid",
    "трансляция": "adjacent_new",
    "доступная среда": "adjacent_new",
    "расходные материалы": "reusable_headphones,microphones_guide,disposable_headphones",
    "аксессуары и наушники": "reusable_headphones,charging_cases",
    "аксессуары для радиооборудования": "reusable_headphones,charging_cases",
    "аксессуары": "reusable_headphones,charging_cases",
    "комплексные решения": "kits_solutions",
}


def our_slugs_for(site: str, competitor_id: int | None, name: str) -> str | None:
    low_any = name.lower().strip()
    if competitor_id is not None:
        for k, v in COMPETITOR_OVERRIDES.items():
            if low_any == k or low_any.startswith(k + " "):
                return v
    if competitor_id is None:
        low = name.lower().strip()
        if low in M22_OVERRIDES:
            return M22_OVERRIDES[low]
        for k, v in M22_OVERRIDES.items():
            if low.startswith(k) and len(k) >= 6:
                return v
    return classify_category(name)


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
                     (site, competitor_id, m["name"], m.get("url"), m.get("parent"), m.get("level", 1), our_slugs_for(site, competitor_id, m["name"]),
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
    approx_cache: dict[tuple, int] = {}
    for r in rows:
        s = sites.setdefault(r["site"], {"site": r["site"], "competitor_id": r["competitor_id"], "seller": r["seller"] or "M22", "tier": r["tier"] or "M22", "cats": [], "top": 0})
        r = dict(r)
        if not r["product_count"] and r["our_slug"] and r["competitor_id"]:
            # адреса товаров не вложены в адрес раздела (Tilda, Magento) — оцениваем по нашей классификации собранных товаров
            key = (r["competitor_id"], r["our_slug"])
            if key not in approx_cache:
                approx_cache[key] = db.row(conn, "SELECT COUNT(*) n FROM competitor_products WHERE competitor_id=? AND is_active=1 AND category_slug=?", key)["n"]
            r["approx_count"] = approx_cache[key]
        s["cats"].append(r)
        s["top"] += 1 if r["level"] == 1 else 0
    def slugs_of(r) -> list[str]:
        return [x.strip() for x in (r["our_slug"] or "_none").split(",") if x.strip()]
    slugs = [s for s in CATEGORY_NAMES if any(s in slugs_of(r) for r in rows)] + ["_none"]
    matrix = {slug: {site: [r["name"] for r in s["cats"] if slug in slugs_of(r)] for site, s in sites.items()} for slug in slugs}
    return {"sites": list(sites.values()), "slugs": slugs, "matrix": matrix, "names": {**CATEGORY_NAMES, "_none": "Вне нашего контура / не сопоставлено"}}


# ---------------- Разделы конкурентов вне контура радара: темы, пояснения, содержимое страниц ----------------
OUT_THEMES: list[tuple[str, str, str]] = [
    # (ключ-регулярка по названию раздела, тема, что это такое / зачем смотреть)
    (r"кнопк|вызов|пейджер|оповещ|табло|медсестр|официант|order|paging|calling", "Системы вызова персонала и пейджеры",
     "Кнопки вызова, часы‑пейджеры, табло: HoReCa, клиники, склады. У M22 это направление есть на m22.ru, но радар его не отслеживает — здесь видно, кто из конкурентов по радиогидам тоже в нём торгует."),
    (r"конференц|crestron|biamp|dcn|встраиваем|микшер|управлени", "Конференц‑системы и управление залом",
     "Дискуссионные пульты, микрофонные конференц‑системы, управление залом. Соседняя ниша B2B: те же заказчики (администрации, вузы, музеи), другой продукт."),
    (r"проекц|dlp|экран|видеостен|светодиод|видеоконференц|жк", "Проекция, экраны, видео",
     "Проекторы, LED‑экраны, видеостены, ВКС. Интеграторский ассортимент для мероприятий; для M22 скорее аренда/партнёрство, чем собственный склад."),
    (r"звуков|аудиооборуд|вокальн|аудиорешен|радиосистем\w* (?:akg|sennheiser)|акустик|колонк", "Звуковое оборудование",
     "Вокальные радиосистемы, акустика, звук для конференций. Смежно с микрофонами для гидов — те же бренды (Sennheiser, AKG)."),
    (r"кофр|рэков|кейсы и кофры", "Кейсы и кофры для транспортировки",
     "Флайт‑кейсы, рэковые кейсы. Смежно с зарядными кейсами M22, но для перевозки техники, а не для зарядки приёмников."),
    (r"студи|запись|аудиозапис|озвуч|карт маршрут|разработк|контент|услуг|под ключ|аутсорс", "Услуги и контент",
     "Запись аудиогидов, озвучивание, разработка маршрутов. Не товар, а сервис вокруг аудиогидов: возможный апсейл к железу."),
    (r"радиоприемник|радиоприёмник|fm|радио\b", "Бытовые радиоприёмники",
     "FM/AM приёмники (Retekess). Дешёвый смежный товар, к экскурсиям отношения почти не имеет."),
    (r"лазерн|указк", "Лазерные указки и мелочи для гида",
     "Дешёвые сопутствующие товары для экскурсовода: указки, шнурки, ремешки. Хорошо продаются в корзину к радиогиду."),
    (r"приложени|qr|мобильн", "Приложения и QR‑аудиогиды",
     "Аудиогид на смартфоне посетителя вместо устройства. Технологический заменитель железа — риск для аудиогидов и одновременно новая ниша (контент + подписка)."),
    (r"трансляц|стрим|вещан", "Трансляция звука",
     "Передача звука на смартфоны/приёмники в зале, тихая дискотека, трансляция лекций. Новый сценарий применения той же радиотехники."),
    (r"доступн|тифло|слабослыш|индукцион|незряч", "Доступная среда",
     "Тифлокомментирование, индукционные петли, оборудование для слабослышащих. Госзаказ по программе «Доступная среда»: отдельный бюджетный канал."),
    (r"усилител|мегафон|громкогов", "Усилители голоса и мегафоны",
     "Поясные усилители голоса, мегафоны. Дешёвая альтернатива радиогиду для небольших групп; у M22 отдельного раздела нет."),
    (r"решени", "Готовые решения / кейсы применения",
     "Страницы‑сценарии («решения для музеев, заводов»), которые ведут к тем же товарам; интересны формулировками, а не ассортиментом."),
]


def out_theme(name: str) -> tuple[str, str]:
    low = name.lower()
    for rx, theme, why in OUT_THEMES:
        if re.search(rx, low):
            return theme, why
    return "Прочее", "Раздел не удалось отнести к известной теме — откройте страницу и посмотрите содержимое."


def _page_summary(url: str) -> tuple[str, list[str]]:
    """Описание страницы раздела и примеры позиций (заголовки карточек/названия с ценой)."""
    res = http.fetch(url, "menu", save=False, timeout=25)
    soup = BeautifulSoup(res.text, "lxml")
    desc = ""
    for sel in ('meta[name="description"]', 'meta[property="og:description"]'):
        el = soup.select_one(sel)
        if el is not None and (el.get("content") or "").strip() and not re.match(r"^(спасибо|главная)", clean_text(el.get("content")), re.I):
            desc = clean_text(el.get("content"))[:300]
            break
    if not desc:
        h1 = soup.find("h1")
        p = h1.find_next("p") if h1 is not None else None
        if p is not None:
            desc = clean_text(p.get_text(" "))[:300]
    if re.match(r"^(спасибо|главная|записей нет)", desc, re.I):
        desc = ""
    items: list[str] = []
    main = soup.find("main") or soup.body or soup
    for el in main.select("h2, h3, h4, [class*=product-title], [class*=product__title], [class*=item-title], [class*=name], [itemprop=name]"):
        t = clean_text(el.get_text(" "))
        if 5 <= len(t) <= 80 and not re.search(r"корзин|доставк|оплат|контакт|отзыв|похожие|рекоменд|новости|статьи|каталог|меню|подписк|главная|спасибо|записей нет|категори|решение \d|^для |свяжемся|позвон|заявк|вопрос", t, re.I)                 and not SKIP.search(t):
            if t not in items:
                items.append(t)
        if len(items) >= 8:
            break
    return desc, items


def enrich_out_of_scope(conn: sqlite3.Connection, force: bool = False) -> int:
    """Для разделов вне контура (и смежных) подгружает описание страницы и примеры товаров."""
    rows = db.rows(conn, """SELECT id, url FROM site_categories WHERE url IS NOT NULL AND url!='' AND competitor_id IS NOT NULL
                            AND (our_slug IS NULL OR our_slug IN ('adjacent_new','substitutes_apps','voice_amplifier')) """ + ("" if force else "AND enriched_at IS NULL"))
    n = 0
    for r in rows:
        try:
            desc, items = _page_summary(r["url"])
        except Exception as exc:  # noqa: BLE001
            log.info("summary %s: %s", r["url"], exc)
            desc, items = "", []
        conn.execute("UPDATE site_categories SET note=?, sample_items=?, enriched_at=datetime('now') WHERE id=?", (desc, " | ".join(items), r["id"]))
        conn.commit()
        n += 1
    return n


def out_of_scope(conn: sqlite3.Connection) -> dict:
    """Разделы конкурентов, которых нет в матрице M22: сгруппированы по темам, с пояснением, ссылкой, описанием и примерами."""
    m22_slugs: set[str] = set()
    m22_out: list[dict] = []
    for r in db.rows(conn, "SELECT name, url, our_slug FROM site_categories WHERE competitor_id IS NULL"):
        if r["our_slug"]:
            m22_slugs |= {x.strip() for x in r["our_slug"].split(",")}
        else:
            m22_out.append(dict(r))
    m22_themes = {out_theme(r["name"])[0] for r in m22_out}
    rows = db.rows(conn, """SELECT sc.*, COALESCE(c.group_name, c.name) AS seller, c.tier, c.website FROM site_categories sc JOIN competitors c ON c.id=sc.competitor_id
                            ORDER BY c.tier, sc.site, sc.name""")
    themes: dict[str, dict] = {}
    for r in rows:
        slugs = {x.strip() for x in (r["our_slug"] or "").split(",") if x.strip()}
        if slugs and slugs & m22_slugs:
            continue  # у M22 такой раздел есть
        theme, why = out_theme(r["name"])
        t = themes.setdefault(theme, {"theme": theme, "why": why, "cats": [], "sites": set(), "m22_has": theme in m22_themes})
        d = dict(r)
        d["samples"] = [x for x in (r["sample_items"] or "").split(" | ") if x][:6]
        d["in_radar"] = ", ".join(CATEGORY_NAMES.get(s, s) for s in slugs) if slugs else ""
        t["cats"].append(d)
        t["sites"].add(r["site"])
    out = sorted(themes.values(), key=lambda t: (-len(t["sites"]), t["theme"]))
    for t in out:
        t["sites"] = sorted(t["sites"])
    return {"themes": out, "m22_out": m22_out, "total": sum(len(t["cats"]) for t in out)}
