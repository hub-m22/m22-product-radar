"""Сервисный профиль конкурента: гарантия, сервисный центр, подменный фонд, аренда, бесплатная доставка, УТП.

Извлекается из текста страниц (главная, доставка, гарантия, сервис, о компании) и из уже сохранённых страниц товаров.
Каждое значение хранится с фрагментом текста и адресом страницы, откуда оно взято, чтобы его можно было проверить.
"""
from __future__ import annotations

import glob
import json
import logging
import re
import sqlite3
from urllib.parse import urljoin, urlparse

from bs4 import BeautifulSoup

from . import config, db, http, normalize

log = logging.getLogger(__name__)

GUESS_PATHS = ["/", "/delivery", "/dostavka", "/dostavka-i-oplata", "/garantiya", "/garantii", "/warranty", "/service", "/servis", "/about", "/o-kompanii", "/rent", "/arenda", "/support", "/faq",
               "/uslugi", "/services", "/o-nas", "/company", "/kontakty", "/contacts", "/test-drive", "/testdrive", "/montazh", "/oplata", "/payment", "/tender", "/tendery", "/lizing", "/clients", "/klienty", "/otzyvy"]
FIELDS = [("warranty_years", "Гарантия, лет"), ("service_center", "Сервисный центр"), ("replacement_fund", "Подменный фонд"), ("rental", "Аренда"), ("free_delivery", "Бесплатная доставка"), ("usp", "УТП")]
# расширенные признаки для сравнительной таблицы (yes / число / текст); хранятся в profile_json
EXTRA_FIELDS = [("test_drive", "Бесплатный тест-драйв / демо"), ("installation", "Монтаж / настройка на объекте"), ("event_support", "Техническое сопровождение мероприятий"),
                ("warehouse", "Собственный склад / наличие"), ("production", "Собственное производство"), ("branding", "Брендирование / кастомизация"), ("training", "Обучение персонала"),
                ("tender", "Работа по 44-ФЗ / 223-ФЗ"), ("leasing", "Лизинг / рассрочка"), ("showroom", "Шоурум / демозал"), ("delivery_russia", "Доставка по всей России"),
                ("support_247", "Поддержка 24/7"), ("years_on_market", "Лет на рынке"), ("clients_count", "Клиентов / проектов (заявлено)"), ("multilang", "Многоязычные / переводческие решения")]
EXTRA_RX = {
    "test_drive": re.compile(r"(тест-?драйв|бесплатн\w+\s+(?:тест|проб\w+|демонстрац\w+)|попробуйте\s+бесплатно|демо-?комплект|на\s+пробу)", re.I),
    "installation": re.compile(r"((?<!видео)(?<!для\s)монтаж\s+(?:и\s+настройк\w+|оборудовани\w+|радиосистем\w*|системы?\s+на\s+объекте|на\s+объекте|рассчитыва\w+)|осуществ\w+\s+(?:монтаж|инсталляц)\w*|инсталляц\w+\s+(?:оборудовани|систем)\w*|пуско-?наладк\w+|настройк\w+\s+на\s+объекте|выезд\s+(?:инженера|специалиста)\s+на\s+объект)", re.I),
    "event_support": re.compile(r"(техническ\w+\s+сопровожден\w+\s+мероприят\w+|сопровожден\w+\s+(?:на\s+)?мероприят\w+|инженер\w*\s+на\s+мероприят\w+|инженер\w*\s+на\s+площадк\w+|техник\w*\s+на\s+площадк\w+|техническ\w+\s+обеспечени\w+\s+мероприят\w+|обеспечени\w+\s+мероприят\w+\s+под\s+ключ|мероприят\w+\s+под\s+ключ)", re.I),
    "warehouse": re.compile(r"(собственн\w+\s+склад\w*|со\s+склада\s+в\s+\w+|в\s+наличии\s+на\s+складе|отгрузка\s+со\s+склада)", re.I),
    "production": re.compile(r"(собственн\w+\s+производств\w+|мы\s+производим\s+(?!дезинф)|производим\s+(?:радиогид|аудиогид|оборудован|наушник|приемник|приёмник)\w*|производител\w+\s+(?:радиогид|аудиогид|оборудован)\w*|разработчик\w*\s+и\s+производител\w*|производится\s+в\s+(?:санкт-петербурге|москве|россии))", re.I),
    "branding": re.compile(r"(брендирован\w+|нанесен\w+\s+логотип\w*|под\s+ваш\w*\s+бренд\w*|кастомизац\w+|индивидуальн\w+\s+дизайн)", re.I),
    "training": re.compile(r"(проводим\s+(?:вводный\s+)?(?:инструктаж|обучени\w+)|провед[её]т\s+(?:вводный\s+)?инструктаж|вводн\w+\s+инструктаж|обучаем\s+(?:ваш\w*\s+)?(?:персонал|сотрудник|гид|экскурсовод)\w*|обучени\w+\s+персонала\s+(?:эксплуатации|работе)|бесплатн\w+\s+обучени\w+|инструктаж\s+(?:персонала|сотрудников|гидов))", re.I),
    "tender": re.compile(r"(44-?фз|223-?фз|госзакупк\w+|тендер\w*|для\s+бюджетных\s+организаций|по\s+госконтракт\w+)", re.I),
    "leasing": re.compile(r"(лизинг\w*|рассрочк\w+|оплата\s+частями|отсрочк\w+\s+платеж\w+)", re.I),
    "showroom": re.compile(r"(шоу-?рум\w*|демо-?зал\w*|демонстрационн\w+\s+зал|можно\s+посмотреть\s+в\s+(?:нашем\s+)?офисе|приезжайте\s+в\s+(?:наш\s+)?офис|посмотреть\s+вживую)", re.I),
    "delivery_russia": re.compile(r"(доставк\w+\s+по\s+(?:всей\s+)?россии|доставляем\s+по\s+(?:всей\s+)?россии|во\s+все\s+регионы|в\s+любой\s+город)", re.I),
    "support_247": re.compile(r"(24\s*/\s*7|круглосуточн\w+\s+поддержк\w+|поддержка\s+24\s+часа)", re.I),
    "multilang": re.compile(r"(многоязычн\w+|мультиязычн\w+|на\s+\d+\s+языках)", re.I),
}
RX_YEARS = re.compile(r"(\d{1,2})\+?\s+лет\s+на\s+рынке|(?:с|c)\s+((?:19|20)\d\d)\s+года\s+(?:на\s+рынке|работаем|производим|поставляем)", re.I)
RX_CLIENTS = re.compile(r"(\d[\d\s]{1,7})\+?\s+(?:клиентов|организаций|компаний|проектов|мероприятий|заказчиков|музеев)", re.I)

RX = {
    "warranty": re.compile(r"гарант\w*\D{0,40}?(\d{1,2})\s*(лет|года?|год|мес\w*|months?|years?)", re.I),
    "service_center": re.compile(r"(собственн\w{0,3}\s+сервис\w*|сервисн\w*\s+центр|сервис-центр|ремонт\w*\s+(?:оборудования|радиогид|приёмник|приемник)|техническ\w+\s+поддержк\w+|служба\s+поддержки|техподдержк\w+)", re.I),
    "replacement": re.compile(r"(подменн\w+\s+(?:фонд|оборудован\w+|устройств\w*)|подмен\w*\s+на\s+время\s+ремонта|бесплатн\w+\s+подмен\w+)", re.I),
    "free_delivery": re.compile(r"(бесплатн\w+\s+доставк\w+|доставк\w+\s+бесплатн\w+)", re.I),
    "rental": re.compile(r"(аренд\w+\s+(?:радиогид|оборудован|приёмник|приемник|аудиогид)|радиогид\w*\s+в\s+аренду|прокат\s+(?:радиогид|оборудован))", re.I),
    "usp": re.compile(r"((?:с|c)\s+(?:19|20)\d\d\s+года|\d{1,2}\+?\s+лет\s+на\s+рынке|\d[\d\s]{2,7}\+?\s+(?:клиентов|организаций|мероприятий|устройств)|24\s*/\s*7|тест-драйв|бесплатн\w+\s+тест|доставка\s+по\s+(?:всей\s+)?россии|отправка\s+в\s+день\s+заказа|производитель|собственное\s+производство|официальный\s+дистрибьютор|лизинг|рассрочк\w+|работаем\s+по\s+44-фз|44-фз|223-фз)", re.I),
}


def _text(html: str) -> str:
    soup = BeautifulSoup(html, "lxml")
    for t in soup(["script", "style", "noscript"]):
        t.decompose()
    return normalize.clean_text(soup.get_text(" "))


def _snippet(text: str, m: re.Match, width: int = 90) -> str:
    a, b = max(0, m.start() - width), min(len(text), m.end() + width)
    return ("…" if a else "") + text[a:b] + ("…" if b < len(text) else "")


def extract(text: str, url: str) -> dict:
    """Возвращает найденные признаки с доказательствами: {field: {"value":..., "snippet":..., "url":...}}."""
    out: dict = {}
    best_w = None
    for m in RX["warranty"].finditer(text):
        n, unit = int(m.group(1)), m.group(2).lower()
        years = n / 12 if unit.startswith(("мес", "month")) else n
        if 0 < years <= 10 and (best_w is None or years > best_w[0]):
            best_w = (years, _snippet(text, m), url)
    if best_w:
        out["warranty_years"] = {"value": round(best_w[0], 2), "snippet": best_w[1], "url": best_w[2]}
    for key, field in (("service_center", "service_center"), ("replacement", "replacement_fund"), ("free_delivery", "free_delivery"), ("rental", "rental")):
        m = RX[key].search(text)
        if m:
            out[field] = {"value": "yes", "snippet": _snippet(text, m), "url": url}
    for field, rx in EXTRA_RX.items():
        m = rx.search(text)
        if m:
            out[field] = {"value": "yes", "snippet": _snippet(text, m), "url": url}
    m = RX_YEARS.search(text)
    if m:
        years = int(m.group(1)) if m.group(1) else (2026 - int(m.group(2)))
        if 0 < years <= 60:
            out["years_on_market"] = {"value": years, "snippet": _snippet(text, m), "url": url}
    best_c = None
    for m in RX_CLIENTS.finditer(text):
        try:
            n = int(m.group(1).replace(" ", ""))
        except ValueError:
            continue
        if 10 <= n <= 1_000_000 and (best_c is None or n > best_c[0]):
            best_c = (n, _snippet(text, m), url)
    if best_c:
        out["clients_count"] = {"value": best_c[0], "snippet": best_c[1], "url": best_c[2]}
    usps = []
    for m in RX["usp"].finditer(text):
        v = normalize.clean_text(m.group(1)).lower()
        if v not in usps:
            usps.append(v)
        if len(usps) >= 8:
            break
    if usps:
        out["usp"] = {"value": "; ".join(usps), "snippet": "", "url": url}
    return out


def _merge(acc: dict, found: dict) -> None:
    for k, v in found.items():
        if k == "usp":
            cur = set(x.strip() for x in (acc.get("usp", {}).get("value") or "").split(";") if x.strip())
            new = [x.strip() for x in v["value"].split(";") if x.strip() and x.strip() not in cur]
            if new:
                acc["usp"] = {"value": "; ".join(sorted(cur | set(new))), "snippet": "", "url": v["url"]}
        elif k in ("warranty_years", "years_on_market", "clients_count"):
            if k not in acc or (v["value"] or 0) > (acc[k]["value"] or 0):
                acc[k] = v
        elif k not in acc:
            acc[k] = v


def _domain(url: str) -> str:
    return urlparse(url).netloc.lower().removeprefix("www.")


def scan_site(base_url: str, source_key: str, cached_dir: str | None = None, fetch: bool = True, max_pages: int = 24) -> dict:
    acc: dict = {}
    checked: list[str] = []
    # 1. уже сохранённые страницы товаров этого конкурента (без сети)
    if cached_dir:
        files = sorted(glob.glob(cached_dir + "/*/*.html"))[-40:]
        for f in files:
            try:
                html = open(f, encoding="utf-8", errors="replace").read()
            except OSError:
                continue
            m = re.search(r'<link rel="canonical" href="([^"]+)"|property="og:url" content="([^"]+)"', html)
            url = (m.group(1) or m.group(2)) if m else base_url
            if _domain(url) != _domain(base_url):
                continue  # страница другого домена (площадка, реселлер) — не доказательство для этого конкурента
            _merge(acc, extract(_text(html)[:60000], url))
    # 2. главная и типовые страницы (сеть, с задержкой и robots)
    if fetch:
        dom = urlparse(base_url).netloc
        for path in GUESS_PATHS[:max_pages]:
            url = urljoin(base_url, path)
            try:
                res = http.fetch(url, source_key, save=False, timeout=20)
            except Exception:  # noqa: BLE001
                continue
            if _domain(res.final_url) != _domain(base_url):
                continue
            checked.append(url)
            _merge(acc, extract(_text(res.text)[:80000], url))
    acc["_checked"] = checked
    return acc


def scan_competitor(conn: sqlite3.Connection, cid: int, fetch: bool = True) -> dict:
    c = db.row(conn, "SELECT * FROM competitors WHERE id=?", (cid,))
    if not c or not c["website"].startswith("http"):
        return {}
    cached = str(config.RAW_DIR / f"competitor_{cid}")
    prof = scan_site(c["website"], f"competitor_{cid}", cached, fetch=fetch)
    old_manual = (db.uj(c["profile_json"], {}) or {}).get("_manual") or {}
    if old_manual:
        prof["_manual"] = old_manual
    # аренда — также из типов и товаров
    if "rental" not in prof and (c["rental_available"] == "yes" or "rental" in (c["types_json"] or "")):
        prof["rental"] = {"value": "yes", "snippet": "по типу конкурента / позициям аренды", "url": c["website"]}
    conn.execute("""UPDATE competitors SET profile_json=?, profile_checked_at=datetime('now'),
                    warranty_years=COALESCE(warranty_years_manual, ?), service_center=COALESCE(service_center_manual, ?), replacement_fund=COALESCE(replacement_fund_manual, ?),
                    free_delivery=COALESCE(free_delivery_manual, ?), usp=COALESCE(usp_manual, ?),
                    rental_available=CASE WHEN rental_available='yes' THEN 'yes' WHEN ? IS NOT NULL THEN 'yes' ELSE rental_available END WHERE id=?""",
                 (db.j(prof), prof.get("warranty_years", {}).get("value"), prof.get("service_center", {}).get("value"), prof.get("replacement_fund", {}).get("value"),
                  prof.get("free_delivery", {}).get("value"), prof.get("usp", {}).get("value"), prof.get("rental", {}).get("value"), cid))
    conn.commit()
    return prof


def scan_m22(conn: sqlite3.Connection, fetch: bool = True) -> dict:
    prof = scan_site("https://m22.ru", "m22.ru", str(config.RAW_DIR / "m22.ru"), fetch=fetch, max_pages=20)
    prof2 = scan_site("https://radiosync.ru", "radiosync.ru", str(config.RAW_DIR / "radiosync.ru"), fetch=fetch, max_pages=12)
    _merge(prof, {k: v for k, v in prof2.items() if not k.startswith("_")})
    prof["_checked"] = prof.get("_checked", []) + prof2.get("_checked", [])
    # гарантия M22 известна из характеристик товаров (Гарантийный срок 2 года)
    w = db.row(conn, "SELECT COUNT(*) n FROM m22_products WHERE specs_json LIKE '%Гарантийный срок%2 года%'")
    if w and w["n"] and (prof.get("warranty_years", {}).get("value") or 0) < 2:
        prof["warranty_years"] = {"value": 2, "snippet": "Характеристики товаров m22.ru: «Гарантийный срок — 2 года»", "url": "https://m22.ru"}
    db.set_setting(conn, "m22_profile", db.j(prof))
    return prof


def m22_profile(conn: sqlite3.Connection) -> dict:
    p = db.uj(db.get_setting(conn, "m22_profile"), {}) or {}
    manual = db.uj(db.get_setting(conn, "m22_profile_manual"), {}) or {}
    out = {k: (p.get(k) or {}).get("value") for k, _ in FIELDS}
    out["rental"] = out.get("rental") or "yes"
    for k, v in manual.items():
        if v not in (None, ""):
            out[k] = v
    return out


OWN_BRANDS = {"crystalsound": "CrystalSound", "crystal sound": "CrystalSound", "reinvox": "Reinvox", "retekess": "Retekess", "radiosync": "Radiosync (M22)", "kromix": "Kromix (M22)",
              "radioguide": "Radioguide (ООО «Радио Гид»)", "conferencepro": "ConferencePro", "conference pro": "ConferencePro", "touraudio": "TourAudio", "sennheiser": "Sennheiser",
              "bosch": "Bosch", "beyerdynamic": "Beyerdynamic", "okayo": "OKAYO", "williams": "Williams AV", "disaudio": "DisAudio", "spbaudio": "SPBAUDIO"}


def seller_roles(conn: sqlite3.Connection) -> dict[int, str]:
    """Роль продавца: производитель/владелец бренда, реселлер чужих брендов, прямой продавец, аренда, маркетплейс, заменитель."""
    roles: dict[int, str] = {}
    comps = db.rows(conn, "SELECT id, name, website, types_json, brands_json, group_name FROM competitors WHERE is_active=1")
    group_brands: dict[str, str] = {}
    for c in comps:
        if c["group_name"]:
            group_brands[c["group_name"]] = group_brands.get(c["group_name"], "") + " " + " ".join(db.uj(c["brands_json"], []) or []).lower() + " " + c["name"].lower() + " " + c["website"].lower()
    for c in comps:
        types = db.uj(c["types_json"], []) or []
        own_brands = " ".join(db.uj(c["brands_json"], []) or []).lower() + " " + c["name"].lower() + " " + c["website"].lower() + " " + group_brands.get(c["group_name"] or "", "")
        is_aggregator = any(w in c["website"].lower() for w in ("wildberries", "ozon.ru", "market.yandex", "avito", "etpgpb"))
        prods = db.rows(conn, "SELECT brand, name FROM competitor_products WHERE competitor_id=? AND is_active=1", (c["id"],))
        found: dict[str, int] = {}
        for p in prods:
            text = ((p["brand"] or "") + " " + p["name"]).lower()
            for key, label in OWN_BRANDS.items():
                if key in text and key not in own_brands:
                    found[label] = found.get(label, 0) + 1
                    break
        foreign_share = (sum(found.values()) / len(prods)) if prods else 0
        top = sorted(found.items(), key=lambda x: -x[1])[:3]
        if "tender_supplier" in types and not prods:
            roles[c["id"]] = "участник госзакупок (без сайта)"
        elif "substitute" in types:
            roles[c["id"]] = "технологический заменитель"
        elif "brand_owner" in types and foreign_share < 0.5:
            roles[c["id"]] = "производитель / владелец бренда" + (f" (также перепродаёт: {', '.join(k for k, _ in top)})" if top else "")
        elif is_aggregator:
            roles[c["id"]] = "продавцы на площадке"
        elif prods and foreign_share >= 0.5:
            roles[c["id"]] = "реселлер: " + ", ".join(k for k, _ in top)
        elif "rental" in types and "direct_seller" not in types:
            roles[c["id"]] = "аренда"
        elif "direct_seller" in types or prods:
            roles[c["id"]] = "прямой продавец" + (f" (в т.ч. {', '.join(k for k, _ in top)})" if top else "")
        else:
            roles[c["id"]] = TYPE_LABELS_SHORT.get(types[0], types[0]) if types else "—"
    return roles


TYPE_LABELS_SHORT = {"tender_supplier": "участник госзакупок", "integrator": "интегратор", "b2b_solutions": "B2B-решения", "museum_supplier": "оборудование для музеев", "sync_translation_supplier": "синхронный перевод",
                     "events_supplier": "оборудование для мероприятий", "foreign": "зарубежный бренд", "indirect": "косвенный", "rental": "аренда"}


def comparison_table(conn: sqlite3.Connection, tiers: tuple = ("A",)) -> dict:
    """Сравнительная таблица: признаки × продавцы (M22 первым). Значения: yes / число / None (нет данных)."""
    comps = [c for c in db.rows(conn, f"SELECT * FROM competitors WHERE is_active=1 AND tier IN ({','.join('?' * len(tiers))}) ORDER BY tier, name", list(tiers)) if not is_tender_participant(c)]
    m22p = m22_profile(conn)
    m22_auto = db.uj(db.get_setting(conn, "m22_profile"), {}) or {}
    manual = db.uj(db.get_setting(conn, "m22_profile_manual"), {}) or {}
    rows_def = [("warranty_years", "Гарантия, лет", "num"), ("service_center", "Сервисный центр", "yn"), ("replacement_fund", "Подменный фонд", "yn"), ("rental", "Аренда", "yn"),
                ("free_delivery", "Бесплатная доставка", "yn")] + [(k, n, "num" if k in ("years_on_market", "clients_count") else "yn") for k, n in EXTRA_FIELDS]
    stats = {r["competitor_id"]: r for r in db.rows(conn, "SELECT competitor_id, COUNT(*) n, SUM(price IS NOT NULL) priced, SUM(availability='InStock') a, SUM(availability IN ('OutOfStock','PreOrder','SoldOut')) b FROM competitor_products WHERE is_active=1 GROUP BY competitor_id")}
    cats = {r["competitor_id"]: r["n"] for r in db.rows(conn, "SELECT competitor_id, COUNT(DISTINCT category_slug) n FROM competitor_products WHERE is_active=1 GROUP BY competitor_id")}
    columns = [{"id": 0, "name": "M22", "is_m22": True, "vals": {}, "evidence": {}, "products": db.row(conn, "SELECT COUNT(*) n FROM m22_products WHERE is_active=1 AND in_scope=1 AND parent_url IS NULL")["n"],
                "categories": db.row(conn, "SELECT COUNT(DISTINCT category_slug) n FROM m22_products WHERE is_active=1 AND in_scope=1")["n"], "stock": None, "role": "производитель / владелец бренда (Radiosync, Kromix)", "tier": "—",
                "legal": db.uj(db.get_setting(conn, "m22_legal"), {}) or {}}]
    columns[0]["usp"] = manual.get("usp") or ((m22_auto.get("usp") or {}).get("value") if isinstance(m22_auto.get("usp"), dict) else None)
    for k, _, _t in rows_def:
        v = manual.get(k) if manual.get(k) not in (None, "") else (m22_auto.get(k) or {}).get("value") if isinstance(m22_auto.get(k), dict) else None
        if k == "rental" and v is None:
            v = "yes"
        columns[0]["vals"][k] = v
        columns[0]["evidence"][k] = (m22_auto.get(k) or {}) if isinstance(m22_auto.get(k), dict) else {}
    roles = seller_roles(conn)
    for c in comps:
        prof = db.uj(c["profile_json"], {}) or {}
        man = prof.get("_manual") or {}
        vals, ev = {}, {}
        for k, _, _t in rows_def:
            if k in ("warranty_years", "service_center", "replacement_fund", "free_delivery"):
                vals[k] = c[k]
            elif k == "rental":
                vals[k] = "yes" if c["rental_available"] == "yes" else None
            elif man.get(k) not in (None, ""):
                vals[k] = man[k]
            else:
                vals[k] = (prof.get(k) or {}).get("value") if isinstance(prof.get(k), dict) else None
            ev[k] = prof.get(k) if isinstance(prof.get(k), dict) else ({"snippet": "задано вручную", "url": ""} if man.get(k) not in (None, "") else {})
        st = stats.get(c["id"])
        known = ((st["a"] or 0) + (st["b"] or 0)) if st else 0
        columns.append({"id": c["id"], "name": c["name"], "is_m22": False, "vals": vals, "evidence": ev, "products": st["n"] if st else 0, "priced": st["priced"] if st else 0,
                        "legal": {k: c[k] for k in ("inn", "legal_name", "legal_status", "reg_date", "employees", "revenue_year", "revenue_rub", "revenue_growth_pct", "tenders_count", "tenders_sum_rub", "legal_source_url", "legal_confidence")},
                        "categories": cats.get(c["id"], 0), "stock": round((st["a"] or 0) / known * 100) if known else None, "role": roles.get(c["id"], "—"), "tier": c["tier"] or "C",
                        "website": c["website"], "usp": c["usp"], "checked": c["profile_checked_at"]})
    # флаги «лучше M22» по строкам
    for col in columns[1:]:
        col["better"] = {}
        for k, _, t in rows_def:
            cv, mv = col["vals"].get(k), columns[0]["vals"].get(k)
            if t == "num":
                col["better"][k] = None if cv is None or mv is None else (cv > mv)
            else:
                col["better"][k] = None if cv is None else (cv == "yes" and mv != "yes")
    return {"rows": rows_def, "columns": columns}


# короткие заголовки колонок реестра
EXTRA_SHORT = {"test_drive": "Тест-драйв", "installation": "Монтаж", "event_support": "Сопровожд.", "warehouse": "Склад", "production": "Пр-во", "branding": "Бренд.", "training": "Обучение",
               "tender": "44-ФЗ", "leasing": "Лизинг", "showroom": "Шоурум", "delivery_russia": "По России", "support_247": "24/7", "years_on_market": "Лет", "clients_count": "Клиентов", "multilang": "Многояз."}


def extra_values(comp: dict) -> dict:
    """Значения расширенных признаков конкурента: ручные поверх найденных на сайте."""
    prof = db.uj(comp.get("profile_json"), {}) or {}
    man = prof.get("_manual") or {}
    out = {}
    for k, _n in EXTRA_FIELDS:
        if man.get(k) not in (None, ""):
            out[k] = man[k]
        else:
            out[k] = (prof.get(k) or {}).get("value") if isinstance(prof.get(k), dict) else None
    return out


def m22_extra(conn: sqlite3.Connection) -> dict:
    auto = db.uj(db.get_setting(conn, "m22_profile"), {}) or {}
    manual = db.uj(db.get_setting(conn, "m22_profile_manual"), {}) or {}
    out = {}
    for k, _n in EXTRA_FIELDS:
        out[k] = manual.get(k) if manual.get(k) not in (None, "") else ((auto.get(k) or {}).get("value") if isinstance(auto.get(k), dict) else None)
    return out


LEGAL_COLS = ("inn", "ogrn", "legal_name", "legal_status", "legal_region", "okved", "reg_date", "employees", "revenue_year", "revenue_rub", "revenue_growth_pct", "profit_rub",
              "tenders_count", "tenders_sum_rub", "tenders_top_customers", "legal_source_url", "legal_confidence", "legal_note", "arbitration_count", "arbitration_sum_rub", "arbitration_note",
              "enforcement_count", "enforcement_note", "taxes_rub", "contributions_rub", "fin_state", "trademarks", "risk_flags", "employees_history")
CONF_RANK = {"подтверждено (ИНН на сайте)": 0, "высокая (адрес/товарный знак)": 1, "средняя (по названию)": 2, "низкая": 3}


def is_tender_participant(c: dict) -> bool:
    return "tender_supplier" in (db.uj(c.get("types_json"), []) or []) or (c.get("website") or "").startswith("tender:")


def tender_segment(c: dict) -> tuple[str, str]:
    """Сегмент участника тендеров по форме и масштабу: (название, пояснение)."""
    inn = c.get("inn") or ""
    rev = c.get("revenue_rub") or 0
    if c.get("risk_flags") and "НЕДОБРОСОВЕСТН" in c["risk_flags"].upper():
        return ("В реестре недобросовестных", "контракты могут расторгаться; заказчики вправе отклонять")
    if len(inn) == 12 or (c.get("legal_name") or "").startswith("ИП"):
        return ("ИП-тендерщик", "без штата и сайта, берёт разные категории, конкурирует ценой")
    if rev >= 1e9:
        return ("Крупный ИТ-интегратор", "миллиардные контракты с ДИТ/транспортом; наушники — побочная позиция")
    if rev >= 100e6:
        return ("Средний поставщик", "оборот 100 млн – 1 млрд, участие в закупках регулярное")
    if rev > 0:
        return ("Малый поставщик", "оборот до 100 млн, 1–5 сотрудников")
    return ("Без данных о масштабе", "юрлицо не подтверждено или отчётность отсутствует")


def tender_participants(conn: sqlite3.Connection) -> dict:
    comps = [c for c in db.rows(conn, "SELECT * FROM competitors WHERE is_active=1 ORDER BY name") if is_tender_participant(c)]
    from .normalize import CATEGORY_NAMES
    rows = []
    for c in comps:
        seg, hint = tender_segment(c)
        rows.append({**c, "segment": seg, "segment_hint": hint, "is_ip": len(c.get("inn") or "") == 12 or (c.get("legal_name") or "").startswith("ИП"),
                     "categories": [CATEGORY_NAMES.get(s, s) for s in (db.uj(c.get("categories_json"), []) or [])]})
    rows.sort(key=lambda r: -(r.get("tenders_sum_rub") or 0))
    segs: dict[str, dict] = {}
    for r in rows:
        s = segs.setdefault(r["segment"], {"name": r["segment"], "hint": r["segment_hint"], "count": 0, "sum": 0.0, "names": []})
        s["count"] += 1
        s["sum"] += r.get("tenders_sum_rub") or 0
        s["names"].append((r.get("legal_name") or r["name"])[:28])
    return {"rows": rows, "total": len(rows), "ip_count": sum(1 for r in rows if r["is_ip"]),
            "tenders_sum": sum(r.get("tenders_sum_rub") or 0 for r in rows), "tenders_count": sum(r.get("tenders_count") or 0 for r in rows),
            "revenue_sum": sum(r.get("revenue_rub") or 0 for r in rows), "with_revenue": sum(1 for r in rows if r.get("revenue_rub")),
            "rnp": sum(1 for r in rows if r.get("risk_flags") and "НЕДОБРОСОВЕСТН" in r["risk_flags"].upper()),
            "arbitration": sum(r.get("arbitration_count") or 0 for r in rows),
            "segments": sorted(segs.values(), key=lambda s: -s["sum"]),
            "bars": [{"label": (r.get("legal_name") or r["name"]), "value": r.get("tenders_sum_rub") or 0, "count": r.get("tenders_count") or 0, "rnp": bool(r.get("risk_flags") and "НЕДОБРОСОВЕСТН" in r["risk_flags"].upper()),
                      "title": r.get("tenders_top_customers") or ""} for r in rows if r.get("tenders_sum_rub")]}


def dashboard_data(conn: sqlite3.Connection, tiers: tuple = ("A",), include_tenders: bool = False) -> dict:
    """Показатели по юрлицам: сайты одной группы (один ИНН) объединяются в одну строку. Участники тендеров без сайтов — отдельно."""
    comps = db.rows(conn, f"SELECT * FROM competitors WHERE is_active=1 AND tier IN ({','.join('?' * len(tiers))}) ORDER BY tier, name", list(tiers))
    if not include_tenders:
        comps = [c for c in comps if not is_tender_participant(c)]
    entities: dict[str, dict] = {}
    for c in comps:
        key = c["inn"] or f"_none_{c['id']}"
        e = entities.get(key)
        if e is None:
            e = {k: c[k] for k in LEGAL_COLS}
            e["sites"] = []
            e["is_ip"] = (c["legal_status"] or "").startswith("Действующий ИП") or (c["inn"] or "").__len__() == 12
            entities[key] = e
        else:
            # берём самое уверенное сопоставление
            if CONF_RANK.get(c["legal_confidence"] or "низкая", 3) < CONF_RANK.get(e["legal_confidence"] or "низкая", 3):
                e["legal_confidence"] = c["legal_confidence"]
        e["sites"].append({"id": c["id"], "name": c["name"], "tier": c["tier"]})
    ents = list(entities.values())
    ents.sort(key=lambda e: (-(e["revenue_rub"] or 0), -(e["tenders_sum_rub"] or 0), e["legal_name"] or "я"))
    with_rev = [e for e in ents if e["revenue_rub"]]
    with_t = [e for e in ents if e["tenders_count"]]
    years = [e["revenue_year"] for e in with_rev if e["revenue_year"]]
    d = {
        "total": len(ents), "with_inn": sum(1 for e in ents if e["inn"]), "entities": ents,
        "revenue_sum": sum(e["revenue_rub"] for e in with_rev), "entities_with_revenue": len(with_rev), "revenue_year": max(years) if years else None,
        "tenders_count": sum(e["tenders_count"] or 0 for e in with_t), "tenders_sum": sum(e["tenders_sum_rub"] or 0 for e in with_t), "entities_with_tenders": len(with_t),
        "arbitration_count": sum(e["arbitration_count"] or 0 for e in ents), "arbitration_sum": sum(e["arbitration_sum_rub"] or 0 for e in ents),
        "employees_sum": sum(e["employees"] or 0 for e in ents),
        "revenue_bars": [{"label": e["legal_name"] or e["sites"][0]["name"], "value": e["revenue_rub"], "growth": e["revenue_growth_pct"], "uncertain": (e["legal_confidence"] or "").startswith("средн") or (e["legal_confidence"] or "").startswith("низк"),
                          "title": f"{e['legal_name']}: {e['revenue_rub'] / 1e6:.1f} млн ₽ за {e['revenue_year']}; сопоставление: {e['legal_confidence']}"} for e in with_rev],
        "tender_bars": sorted([{"label": e["legal_name"] or e["sites"][0]["name"], "value": e["tenders_sum_rub"] or 0, "count": e["tenders_count"], "title": e["tenders_top_customers"] or ""} for e in with_t], key=lambda b: -b["value"]),
        "no_revenue": [f"{e['legal_name']} ({'ИП, отчётность не публикуется' if e['is_ip'] else 'нет данных'})" for e in ents if e["inn"] and not e["revenue_rub"]],
        "arb_notes": [{"name": e["legal_name"], "text": "; ".join(x for x in (e["arbitration_note"], ("исполнительные производства: " + e["enforcement_note"]) if e["enforcement_note"] and e["enforcement_count"] else None) if x)} for e in ents if (e["arbitration_count"] or e["enforcement_count"])],
    }
    return d


# что именно написать на сайте, если признак решено добавить
SITE_ACTIONS = {
    "warranty_years": "Указать срок гарантии на каждой карточке товара и на отдельной странице «Гарантия»; если можно дать больше 2 лет на ключевые модели — это прямой аргумент против {names}.",
    "service_center": "Страница «Сервисный центр»: адрес, сроки ремонта, что делаем по гарантии и после неё.",
    "replacement_fund": "Написать про подменный фонд: «на время ремонта выдаём подменное оборудование» — ни один конкурент это не заявляет, будет уникальным УТП.",
    "free_delivery": "Условия доставки на видном месте: от какой суммы бесплатно, сроки по регионам.",
    "test_drive": "Кнопка «Взять на бесплатный тест-драйв» в каталоге и на главной; форма заявки с выбором комплекта и дат.",
    "installation": "Услуга «Монтаж и настройка на объекте» с ценой или «от … ₽» и примерами объектов.",
    "event_support": "Услуга «Техническое сопровождение мероприятия»: инженер на площадке, что входит, стоимость за смену.",
    "warehouse": "Блок «Собственный склад в …, отгрузка в день оплаты» + остатки «в наличии» в карточках.",
    "production": "Блок «Собственное производство / разработка»: фото, что делаем сами, срок изготовления партии.",
    "branding": "Услуга «Брендирование»: логотип на приёмниках и чехлах, минимальный тираж, сроки.",
    "training": "«Обучаем персонал»: формат (онлайн/на объекте), длительность, бесплатно при покупке комплекта от N устройств.",
    "tender": "Страница «Для госзаказчиков»: работа по 44-ФЗ/223-ФЗ, реестр контрактов, документы для закупки.",
    "leasing": "Блок «Лизинг и рассрочка»: партнёры, условия, калькулятор ежемесячного платежа.",
    "showroom": "«Шоурум / демозал»: адрес, часы, запись на демонстрацию.",
    "delivery_russia": "«Доставка по всей России»: сроки и стоимость по федеральным округам, ТК-партнёры.",
    "support_247": "Круглосуточная поддержка: телефон/чат на видном месте, обещание времени ответа.",
    "multilang": "Раздел «Многоязычные решения / синхронный перевод»: сценарии, комплекты.",
    "years_on_market": "Цифра «N лет на рынке» в шапке и в блоке «О компании» (у {names}: {values}).",
    "clients_count": "Цифра «N клиентов / проектов» + логотипы заказчиков (у {names}: {values}).",
}


def site_recommendations(conn: sqlite3.Connection, tiers: tuple = ("A",), only_competitor: int | None = None) -> list[dict]:
    """Что есть у конкурентов и не заявлено на m22.ru / radiosync.ru. Отсортировано по числу конкурентов (рычаг)."""
    table = comparison_table(conn, tiers)
    m22 = table["columns"][0]
    labels = {k: n for k, n, _t in table["rows"]}
    kinds = {k: t for k, _n, t in table["rows"]}
    out = []
    for key, label in labels.items():
        mv = m22["vals"].get(key)
        claimers = []
        for col in table["columns"][1:]:
            if only_competitor is not None and col["id"] != only_competitor:
                continue
            cv = col["vals"].get(key)
            if cv is None:
                continue
            if kinds[key] == "num":
                if mv is None or cv > mv:
                    claimers.append((col["name"], cv))
            elif cv == "yes" and mv != "yes":
                claimers.append((col["name"], cv))
        if not claimers:
            continue
        names = ", ".join(n.split(" (")[0].split(" — ")[0][:26] for n, _ in claimers[:4]) + (f" и ещё {len(claimers) - 4}" if len(claimers) > 4 else "")
        values = ", ".join(str(v) for _, v in claimers[:4]) if kinds[key] == "num" else ""
        if kinds[key] == "num":
            m22_status = f"на сайтах M22: {mv}" if mv is not None else "на сайтах M22 не указано"
        else:
            m22_status = "на сайтах M22 не заявлено" if mv is None else "на сайтах M22: нет"
        out.append({"key": key, "label": label, "count": len(claimers), "names": names, "vals_txt": values, "m22_status": m22_status,
                    "action": SITE_ACTIONS.get(key, "Заявить на сайте, если есть; если нет — оценить, стоит ли добавить.").format(names=names, values=values or "—"),
                    "unique": key == "replacement_fund"})
    # особые выводы из юрлиц: тендеры и масштаб
    if only_competitor is None:
        t = db.rows(conn, f"SELECT legal_name, tenders_count, tenders_sum_rub FROM competitors WHERE is_active=1 AND tier IN ({','.join('?' * len(tiers))}) AND tenders_count > 0 GROUP BY inn ORDER BY tenders_sum_rub DESC", list(tiers))
        if t and (m22["vals"].get("tender") != "yes"):
            out.append({"key": "tender_proof", "label": "Госзакупки как канал", "count": len(t), "names": ", ".join((r["legal_name"] or "")[:26] for r in t[:3]), "vals_txt": "", "m22_status": "на сайтах M22 не заявлено",
                        "action": "Страница для госзаказчиков и участие в закупках: у конкурентов это заметный канал (" + "; ".join(f"{r['legal_name']}: {r['tenders_count']} закупок на {(r['tenders_sum_rub'] or 0) / 1e6:.0f} млн ₽" for r in t[:3]) + ").", "unique": False})
    out.sort(key=lambda r: (-r["count"], r["label"]))
    if only_competitor is None and m22["vals"].get("replacement_fund") != "yes" and not any(r["key"] == "replacement_fund" for r in out):
        out.append({"key": "replacement_fund", "label": "Подменный фонд", "count": 0, "names": "никто из конкурентов не заявляет", "vals_txt": "", "m22_status": "на сайтах M22 не заявлено",
                    "action": SITE_ACTIONS["replacement_fund"], "unique": True})
    return out


def compare_flags(comp: dict, m22: dict) -> dict:
    """Где конкурент сильнее M22 (True) / слабее (False) / нет данных (None) по каждому признаку."""
    flags = {}
    cw, mw = comp.get("warranty_years"), m22.get("warranty_years")
    flags["warranty_years"] = None if cw is None or mw is None else (cw > mw)
    for k in ("service_center", "replacement_fund", "free_delivery"):
        cv, mv = comp.get(k), m22.get(k)
        flags[k] = None if cv is None else (cv == "yes" and mv != "yes")
    flags["rental"] = (comp.get("rental_available") == "yes" and m22.get("rental") != "yes")
    return flags
