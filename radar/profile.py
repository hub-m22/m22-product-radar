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
    "installation": re.compile(r"(монтаж\w*|установк\w+\s+(?:и\s+)?настройк\w+|пуско-?наладк\w+|настройк\w+\s+на\s+объекте|инсталляц\w+)", re.I),
    "event_support": re.compile(r"(техническ\w+\s+сопровожден\w+\s+мероприят\w+|сопровожден\w+\s+(?:на\s+)?мероприят\w+|инженер\w*\s+на\s+мероприят\w+|техник\w*\s+на\s+площадк\w+|под\s+ключ)", re.I),
    "warehouse": re.compile(r"(собственн\w+\s+склад\w*|со\s+склада\s+в\s+\w+|в\s+наличии\s+на\s+складе|отгрузка\s+со\s+склада)", re.I),
    "production": re.compile(r"(собственн\w+\s+производств\w+|мы\s+производим|производител\w+\s+(?:радиогид|аудиогид|оборудован))", re.I),
    "branding": re.compile(r"(брендирован\w+|нанесен\w+\s+логотип\w*|под\s+ваш\w*\s+бренд\w*|кастомизац\w+|индивидуальн\w+\s+дизайн)", re.I),
    "training": re.compile(r"(обучени\w+\s+(?:персонала|сотрудников|гидов|экскурсоводов)|проводим\s+обучени\w+|инструктаж)", re.I),
    "tender": re.compile(r"(44-?фз|223-?фз|госзакупк\w+|тендер\w*|для\s+бюджетных\s+организаций|по\s+госконтракт\w+)", re.I),
    "leasing": re.compile(r"(лизинг\w*|рассрочк\w+|оплата\s+частями|отсрочк\w+\s+платеж\w+)", re.I),
    "showroom": re.compile(r"(шоу-?рум\w*|демо-?зал\w*|выставочн\w+\s+зал|можно\s+посмотреть\s+в\s+офисе|приезжайте\s+в\s+офис)", re.I),
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
            if urlparse(res.final_url).netloc != dom and path != "/":
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
        if "substitute" in types:
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


TYPE_LABELS_SHORT = {"integrator": "интегратор", "b2b_solutions": "B2B-решения", "museum_supplier": "оборудование для музеев", "sync_translation_supplier": "синхронный перевод",
                     "events_supplier": "оборудование для мероприятий", "foreign": "зарубежный бренд", "indirect": "косвенный", "rental": "аренда"}


def comparison_table(conn: sqlite3.Connection, tiers: tuple = ("A",)) -> dict:
    """Сравнительная таблица: признаки × продавцы (M22 первым). Значения: yes / число / None (нет данных)."""
    comps = db.rows(conn, f"SELECT * FROM competitors WHERE is_active=1 AND tier IN ({','.join('?' * len(tiers))}) ORDER BY tier, name", list(tiers))
    m22p = m22_profile(conn)
    m22_auto = db.uj(db.get_setting(conn, "m22_profile"), {}) or {}
    manual = db.uj(db.get_setting(conn, "m22_profile_manual"), {}) or {}
    rows_def = [("warranty_years", "Гарантия, лет", "num"), ("service_center", "Сервисный центр", "yn"), ("replacement_fund", "Подменный фонд", "yn"), ("rental", "Аренда", "yn"),
                ("free_delivery", "Бесплатная доставка", "yn")] + [(k, n, "num" if k in ("years_on_market", "clients_count") else "yn") for k, n in EXTRA_FIELDS]
    stats = {r["competitor_id"]: r for r in db.rows(conn, "SELECT competitor_id, COUNT(*) n, SUM(price IS NOT NULL) priced, SUM(availability='InStock') a, SUM(availability IN ('OutOfStock','PreOrder','SoldOut')) b FROM competitor_products WHERE is_active=1 GROUP BY competitor_id")}
    cats = {r["competitor_id"]: r["n"] for r in db.rows(conn, "SELECT competitor_id, COUNT(DISTINCT category_slug) n FROM competitor_products WHERE is_active=1 GROUP BY competitor_id")}
    columns = [{"id": 0, "name": "M22", "is_m22": True, "vals": {}, "evidence": {}, "products": db.row(conn, "SELECT COUNT(*) n FROM m22_products WHERE is_active=1 AND in_scope=1 AND parent_url IS NULL")["n"],
                "categories": db.row(conn, "SELECT COUNT(DISTINCT category_slug) n FROM m22_products WHERE is_active=1 AND in_scope=1")["n"], "stock": None, "role": "производитель / владелец бренда (Radiosync, Kromix)", "tier": "—"}]
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
