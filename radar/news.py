"""«Новости конкурентов»: что изменилось на сайтах конкурентов уровней A и B с прошлой проверки.

Схема: CRAWL → SNAPSHOT → COMPARE → CLASSIFY → SUMMARIZE → DISPLAY.

Две ступени, чтобы не читать сайты моделью целиком:
1. дёшево и технически: обход коммерчески значимых страниц (главная, каталог, разделы, новинки, новости, блог, решения,
   кейсы, бренды, акции, партнёры), нормализация текста (без скриптов, счётчиков, дат, cookie), хеш и снимок, сравнение
   с прошлым снимком построчно и по списку значимых ссылок;
2. только найденные изменения — модели (radar.ai), которая решает, значимо ли это и как назвать; без ключа — эвристика.

Карточки товаров сюда не входят: их ведёт сбор конкурентов (competitor_products, история цен, сигналы), и отсюда берутся
события «новый товар», «товар исчез», «цена изменилась» — так одна новинка не превращается в четыре новости.
"""
from __future__ import annotations

import difflib
import hashlib
import json
import logging
import re
import sqlite3
from datetime import datetime, timedelta
from urllib.parse import urljoin, urlparse

from bs4 import BeautifulSoup

from . import ai, config, db, http, normalize

log = logging.getLogger(__name__)
SOURCE_KEY = "competitor_news"

EVENT_NAMES = {
    "NEW_PRODUCT": "Новый товар", "NEW_CATEGORY": "Новая категория", "NEW_BRAND": "Новый бренд", "PRODUCT_UPDATE": "Обновление товара",
    "PRODUCT_REMOVED": "Товар снят", "PRICE_CHANGE": "Изменение цены", "NEW_SOLUTION": "Новое решение / B2B", "NEW_CASE": "Новый кейс",
    "NEW_PARTNERSHIP": "Партнёрство", "PROMOTION": "Акция", "COMPANY_NEWS": "Новость компании", "SITE_CHANGE": "Изменение сайта", "OTHER": "Другое",
}
STATUS_NAMES = {"NO_CHANGES": "без изменений", "CHANGES_FOUND": "есть изменения", "CRAWL_ERROR": "не удалось проверить", "PARTIAL_CRAWL": "проверена часть страниц", "BASELINE": "первый снимок"}

# какие адреса считаем коммерчески значимыми и какого они вида (порядок = приоритет обхода).
# stems — совпадают как часть слова (novost…, katalog…), words — только как отдельный сегмент пути (case, sale, b2b),
# чтобы «carrying-case» не считалась кейсом, а «wholesale» — распродажей
KIND_RULES: list[tuple[str, list[str], list[str]]] = [
    ("news", ["novost", "novosti", "pressa", "sobytiy"], ["news", "press", "events", "event"]),
    ("promo", ["akci", "aktsi", "skidk", "rasprodazh", "promo", "spec"], ["sale", "sales", "special", "specials", "discount", "discounts"]),
    ("solutions", ["resheni", "solution", "otrasl", "industr", "napravlen", "dlya-biznes", "dlya-muze", "dlya-zavod", "dlya-shkol", "dlya-ekskurs", "dlya-konferen", "dlya-otel", "dlya-gid"], ["b2b", "business", "corporate"]),
    ("cases", ["keis", "kejs", "proekt", "portfolio", "realizovan", "klienty", "otzyv"], ["case", "cases", "project", "projects", "clients", "works"]),
    ("brands", ["brend", "proizvoditel", "manufactur", "vendor"], ["brand", "brands"]),
    ("partners", ["partn", "diler", "distrib"], ["dealer", "dealers", "partners", "partner"]),
    ("blog", ["blog", "stati", "statyi", "article", "obzor", "poleznoe", "baza-znan", "wiki"], ["faq"]),
    ("catalog", ["novink", "new-arriv", "katalog", "catalog", "magazin", "produkt", "product", "tovar", "prais", "price", "arenda", "rent", "uslug", "service", "bestsell", "categor", "razdel"], ["shop", "store", "hits", "hit", "top", "new"]),
]


SKIP_RX = re.compile(r"\.(jpg|jpeg|png|gif|svg|webp|pdf|zip|rar|doc|docx|xls|xlsx|mp4|mp3|css|js|xml|ico)(\?|$)|/cart|/basket|/korzin|/login|/auth|/register|/account|/personal|/search|/poisk|/compare|/wishlist|"
                     r"/privacy|/policy|/oferta|/offer|/agreement|/dostavka|/delivery|/oplata|/payment|/contact|/kontakt|/vacanc|/sitemap|/tag/|/tags/|/feed|/rss|/print|"
                     r"utm_|[?&](sort|order|page|p|limit|view|filter)=|#|mailto:|tel:|javascript:", re.I)
NOISE_LINE_RX = re.compile(r"cookie|куки|©|\(c\)|все права|политик|конфиденциал|корзин|войти|регистрац|подписаться|подписка|^\d+[\s\d.,]*$|товар(ов|а)?:?\s*\d+|"
                           r"просмотр|отзыв(ов|а)?:?\s*\d+|рейтинг|\bсегодня\b|\bвчера\b|^\d{1,2}[.:]\d{2}(:\d{2})?$|^\d{1,2}\s+[а-я]+\s+\d{4}|^\d{2}\.\d{2}\.\d{4}|"
                           r"обновлен[оа]|время работы|мы в соц|яндекс\.метрика|counter|показать ещ|загрузка|loading|наверх|в начало", re.I)
CATEGORY_WORD_RX = re.compile(r"радиогид|аудиогид|синхрон|перевод|рац|наушник|микрофон|гарнитур|кейс|зарядн|вызов|пейджер|табло|конферен|усилител|интерком|"
                              r"переговор|громкоговор|аренд|прокат|комплект|решени|системы|система|оборудован", re.I)
MODEL_RX = re.compile(r"\b[A-Za-z]{1,6}[- ]?\d{2,5}[A-Za-z]{0,3}\b")


# ---------------- CRAWL: какие страницы ----------------
def page_kind(url: str) -> str | None:
    """Вид страницы по адресу; None — коммерчески незначимая."""
    path = (urlparse(url).path or "/").lower()
    if SKIP_RX.search(url):
        return None
    if path in ("", "/"):
        return "home"
    segments = [s for s in path.split("/") if s]
    for kind, stems, words in KIND_RULES:
        if any(st in path for st in stems):
            return kind
        # слово-сегмент: «/cases/», «/case-studies/», но не «/carrying-case» и не «/wholesale»
        if any(seg == w or seg.startswith(w + "-") or seg.startswith(w + "_") for seg in segments for w in words):
            return kind
    return None


def _same_site(url: str, root: str) -> bool:
    a, b = urlparse(url).netloc.lower().replace("www.", ""), urlparse(root).netloc.lower().replace("www.", "")
    return a == b


def discover_links(html: str, base_url: str, root: str) -> list[tuple[str, str, str]]:
    """Внутренние ссылки страницы с их видом: [(url, kind, text)] — только коммерчески значимые, без дублей."""
    soup = BeautifulSoup(html, "lxml")
    out, seen = [], set()
    for a in soup.find_all("a", href=True):
        href = a["href"].strip()
        if not href or href.startswith(("#", "javascript:", "mailto:", "tel:")):
            continue
        u = urljoin(base_url, href).split("#")[0]
        if not _same_site(u, root) or u in seen:
            continue
        kind = page_kind(u)
        if not kind or kind == "home":
            continue
        seen.add(u)
        out.append((u, kind, normalize.clean_text(a.get_text(" "))[:120]))
    return out


def ensure_pages(conn: sqlite3.Connection, comp: dict) -> list[dict]:
    """Список страниц конкурента для проверки: главная + найденные разделы + каталожные страницы из мониторинга. Не более NEWS_MAX_PAGES."""
    root = comp["website"].rstrip("/") + "/"
    if not db.row(conn, "SELECT id FROM news_pages WHERE url=?", (root,)):
        conn.execute("INSERT OR IGNORE INTO news_pages(competitor_id, url, kind, title, discovered_from) VALUES(?,?,?,?,?)", (comp["id"], root, "home", "Главная", "manual"))
    # каталожные страницы, которые уже есть в мониторинге (разделы каталога, не карточки)
    for mp in db.rows(conn, "SELECT url FROM monitored_pages WHERE competitor_id=? AND is_active=1 AND kind IN ('catalog','rent') LIMIT 6", (comp["id"],)):
        conn.execute("INSERT OR IGNORE INTO news_pages(competitor_id, url, kind, title, discovered_from) VALUES(?,?,?,?,?)", (comp["id"], mp["url"], "catalog", None, "monitored_pages"))
    conn.commit()
    return _pick_pages(conn, comp["id"])


PRIORITY = {"home": 0, "news": 1, "promo": 2, "catalog": 3, "solutions": 4, "cases": 5, "brands": 6, "partners": 7, "blog": 8, "category": 9, "other": 10}


def _pick_pages(conn: sqlite3.Connection, cid: int) -> list[dict]:
    pages = db.rows(conn, "SELECT * FROM news_pages WHERE competitor_id=? AND is_active=1", (cid,))
    pages.sort(key=lambda p: (PRIORITY.get(p["kind"], 10), p["fail_count"], p["id"]))
    return pages[: config.NEWS_MAX_PAGES]


def add_discovered(conn: sqlite3.Connection, cid: int, links: list[tuple[str, str, str]], from_url: str) -> int:
    """Новые адреса разделов с главной/каталога — в список страниц (в пределах лимита; по 2–4 на вид, чтобы не расползаться)."""
    existing = db.row(conn, "SELECT COUNT(*) n FROM news_pages WHERE competitor_id=?", (cid,))["n"]
    room = max(0, config.NEWS_MAX_PAGES * 2 - existing)
    per_kind: dict[str, int] = {}
    added = 0
    for u, kind, text in links:
        if added >= room:
            break
        cap = 6 if kind == "catalog" else 3
        if per_kind.get(kind, 0) >= cap:
            continue
        if db.row(conn, "SELECT id FROM news_pages WHERE url=?", (u,)):
            continue
        conn.execute("INSERT OR IGNORE INTO news_pages(competitor_id, url, kind, title, discovered_from) VALUES(?,?,?,?,?)", (cid, u, kind, text or None, from_url))
        per_kind[kind] = per_kind.get(kind, 0) + 1
        added += 1
    return added


# ---------------- SNAPSHOT: нормализация ----------------
def normalize_page(html: str, url: str) -> tuple[str, list[dict], str | None]:
    """(нормализованный текст, значимые ссылки [{t,u}], заголовок). Убираем скрипты, стили, навигацию, шапку/подвал, формы и шумовые строки."""
    soup = BeautifulSoup(html, "lxml")
    title = normalize.clean_text(soup.title.get_text()) if soup.title else None
    for tag in soup(["script", "style", "noscript", "iframe", "svg", "form", "input", "select", "button", "template"]):
        tag.decompose()
    for tag in soup.find_all(["header", "footer", "nav"]):
        tag.decompose()
    for tag in soup.find_all(attrs={"class": re.compile(r"cookie|counter|metrika|breadcrumb|pagination|pager|social|share|popup|modal|widget-cart|basket|footer|header|menu|nav\b", re.I)}):
        tag.decompose()
    main = soup.find("main") or soup.body or soup
    items: list[dict] = []
    seen_u: set[str] = set()
    for a in main.find_all("a", href=True):
        t = normalize.clean_text(a.get_text(" "))
        u = urljoin(url, a["href"]).split("#")[0]
        if 3 <= len(t) <= 120 and _same_site(u, url) and u not in seen_u and not SKIP_RX.search(u) and not NOISE_LINE_RX.search(t):
            seen_u.add(u)
            items.append({"t": t, "u": u})
    raw = main.get_text("\n")
    lines = []
    for ln in raw.split("\n"):
        ln = normalize.clean_text(ln)
        if len(ln) < 4 or NOISE_LINE_RX.search(ln):
            continue
        ln = re.sub(r"\d{1,2}[.:]\d{2}(:\d{2})?", "", ln)                   # время
        ln = re.sub(r"\b\d{2}\.\d{2}\.\d{4}\b|\b\d{4}-\d{2}-\d{2}\b", "", ln)  # даты генерации
        ln = re.sub(r"\s+", " ", ln).strip()
        if len(ln) >= 4:
            lines.append(ln)
    # повторы (меню, дублирующиеся блоки) не должны считаться изменением при перестановке
    uniq = list(dict.fromkeys(lines))
    return "\n".join(uniq), items[:400], title


def content_hash(text: str, items: list[dict]) -> str:
    """Хеш по множеству строк (порядок не важен) и множеству ссылок — перестановка блоков не считается изменением."""
    h = hashlib.sha1()
    for ln in sorted(set(text.split("\n"))):
        h.update(ln.encode("utf-8")); h.update(b"\n")
    for it in sorted({(i["t"], i["u"]) for i in items}):
        h.update(f"{it[0]}|{it[1]}".encode("utf-8"))
    return h.hexdigest()


# ---------------- COMPARE ----------------
def diff_snapshots(old_text: str, new_text: str, old_items: list[dict], new_items: list[dict]) -> dict | None:
    """Что добавилось/исчезло по смыслу. None — различий, стоящих внимания, нет."""
    old_lines, new_lines = set(old_text.split("\n")), set(new_text.split("\n"))
    added = [ln for ln in new_text.split("\n") if ln not in old_lines]
    removed = [ln for ln in old_text.split("\n") if ln not in new_lines]
    # мелкие правки: строка «почти та же» (опечатка, число) — не новость
    def _minor(ln: str, pool: set[str]) -> bool:
        return any(difflib.SequenceMatcher(None, ln, o).ratio() >= 0.9 for o in pool if abs(len(o) - len(ln)) < 30)
    added = [ln for ln in added if not _minor(ln, old_lines)]
    removed = [ln for ln in removed if not _minor(ln, new_lines)]
    old_i = {(i["t"], i["u"]) for i in old_items}
    new_i = {(i["t"], i["u"]) for i in new_items}
    old_u = {u for _, u in old_i}
    items_added = [{"t": t, "u": u} for t, u in new_i - old_i if u not in old_u]  # новая ссылка, а не переименованная
    items_removed = [{"t": t, "u": u} for t, u in old_i - new_i if u not in {u for _, u in new_i}]
    added_chars = sum(len(x) for x in added)
    if added_chars < config.NEWS_MIN_ADDED_CHARS and len(items_added) < 1 and len(items_removed) < 3:
        return None
    return {"added": added[:60], "removed": removed[:60], "items_added": items_added[:60], "items_removed": items_removed[:60], "added_chars": added_chars}


# ---------------- CLASSIFY (эвристика) ----------------
def _looks_like_category(text: str) -> bool:
    return bool(CATEGORY_WORD_RX.search(text)) and not MODEL_RX.search(text) and len(text) <= 60


def _looks_like_product(text: str) -> bool:
    return bool(MODEL_RX.search(text)) or bool(re.search(r"\b(на \d{1,3} (персон|человек|чел)|комплект|система|приёмник|приемник|передатчик|рация|радиостанция|аудиогид|радиогид)\b", text, re.I))


def heuristic_classify(page: dict, d: dict, comp_name: str) -> list[dict]:
    """Без модели: вид страницы + добавленные ссылки/строки → события. Возвращает список событий (может быть пустым — шум)."""
    kind = page["kind"]
    events: list[dict] = []
    url = page["url"]
    # новые ссылки на каталожных/главной страницах: товары и категории
    if kind in ("catalog", "home", "category"):
        prods = [i for i in d["items_added"] if _looks_like_product(i["t"])]
        cats = [i for i in d["items_added"] if _looks_like_category(i["t"]) and i not in prods]
        for i in prods[:8]:
            events.append({"event_type": "NEW_PRODUCT", "title": f"В каталоге появилось: {i['t'][:80]}", "summary": f"На странице «{page.get('title') or kind}» появилась новая позиция «{i['t']}».",
                           "what_changed": "Раньше этой ссылки на странице не было.", "url": i["u"], "product_name": i["t"], "importance": 3})
        for i in cats[:4]:
            events.append({"event_type": "NEW_CATEGORY", "title": f"Новый раздел каталога: {i['t'][:80]}", "summary": f"В каталоге появился раздел «{i['t']}».",
                           "what_changed": "Ранее такого раздела на странице не было.", "url": i["u"], "product_name": None, "importance": 3})
        if not events and d["added_chars"] >= config.NEWS_MIN_ADDED_CHARS * 2:
            events.append({"event_type": "SITE_CHANGE", "title": f"Изменился раздел «{(page.get('title') or kind)[:60]}»", "summary": _snippet(d["added"]),
                           "what_changed": "Добавлен текст, которого раньше не было.", "url": url, "product_name": None, "importance": 1})
        return events
    type_by_kind = {"news": "COMPANY_NEWS", "promo": "PROMOTION", "solutions": "NEW_SOLUTION", "cases": "NEW_CASE", "brands": "NEW_BRAND", "partners": "NEW_PARTNERSHIP", "blog": "COMPANY_NEWS"}
    et = type_by_kind.get(kind, "SITE_CHANGE")
    text_all = " ".join(d["added"]).lower()
    if re.search(r"акци|скидк|спецпредлож|распродаж|подар", text_all):
        et = "PROMOTION"
    elif re.search(r"партн[её]р|дилер|дистриб", text_all) and kind != "news":
        et = "NEW_PARTNERSHIP"
    elif re.search(r"кейс|проект|внедр|поставил|оснастил", text_all) and kind in ("cases", "blog", "home"):
        et = "NEW_CASE"
    # новые ссылки на странице новостей/блога/кейсов — это новые публикации
    links = [i for i in d["items_added"] if len(i["t"]) >= 12][:5]
    if links:
        for i in links:
            events.append({"event_type": et, "title": i["t"][:90], "summary": f"На странице «{page.get('title') or kind}» появилась публикация «{i['t']}».",
                           "what_changed": "Новая публикация, которой не было при прошлой проверке.", "url": i["u"], "product_name": None, "importance": 2 if et != "SITE_CHANGE" else 1})
    elif d["added_chars"] >= config.NEWS_MIN_ADDED_CHARS:
        events.append({"event_type": et, "title": f"{EVENT_NAMES[et]}: {(d['added'][0] if d['added'] else page.get('title') or kind)[:80]}", "summary": _snippet(d["added"]),
                       "what_changed": "Добавлен текст, которого раньше не было.", "url": url, "product_name": None, "importance": 2 if et != "SITE_CHANGE" else 1})
    return events


def _snippet(lines: list[str], limit: int = 280) -> str:
    s = " ".join(lines)
    return (s[:limit] + "…") if len(s) > limit else s


def _norm_key(s: str) -> str:
    return re.sub(r"[^a-zа-яё0-9]+", "", (s or "").lower())[:80]


# ---------------- события из уже собранных данных (товары, цены) ----------------
def events_from_products(conn: sqlite3.Connection, comp: dict, since: str, run_id: int) -> int:
    """Новые/исчезнувшие товары и существенные изменения цен — из сбора конкурентов, без повторного обхода карточек."""
    n = 0
    cid = comp["id"]
    base = db.row(conn, "SELECT MIN(fetched_at) t FROM competitor_products WHERE competitor_id=?", (cid,))
    if not base or not base["t"] or base["t"][:10] >= since[:10]:
        return 0  # первый сбор этого конкурента — всё «новое» лишь потому, что раньше не смотрели
    for cp in db.rows(conn, "SELECT * FROM competitor_products WHERE competitor_id=? AND is_active=1 AND first_seen_at >= ? AND price IS NOT NULL", (cid, since)):
        key = f"{cid}:NEW_PRODUCT:{_norm_key(cp['model_key'] or cp['name'])}"
        cat = normalize.CATEGORY_NAMES.get(cp["category_slug"], cp["category_slug"] or "")
        n += _save_event(conn, cid, run_id, "NEW_PRODUCT", f"В каталог добавлен товар: {cp['name'][:80]}",
                         f"{comp['name']}: в каталоге появился «{cp['name']}» по цене {cp['price']:,.0f} ₽.".replace(",", " ") + (f" Категория: {cat}." if cat else ""),
                         "Раньше этого товара в каталоге не было.", cp["url"], cp["name"], cp["category_slug"], 3, key, "products", cp["first_seen_at"])
    for cp in db.rows(conn, "SELECT * FROM competitor_products WHERE competitor_id=? AND is_active=0 AND last_seen_at >= ? AND last_seen_at < datetime('now','-2 days')", (cid, since)):
        key = f"{cid}:PRODUCT_REMOVED:{_norm_key(cp['model_key'] or cp['name'])}:{cp['last_seen_at'][:7]}"
        n += _save_event(conn, cid, run_id, "PRODUCT_REMOVED", f"Товар исчез из каталога: {cp['name'][:80]}",
                         f"{comp['name']}: «{cp['name']}» больше не находится на сайте (последний раз виден {cp['last_seen_at'][:10]}).",
                         "Позиция пропала со страниц каталога при последних проверках.", cp["url"], cp["name"], cp["category_slug"], 2, key, "products", None)
    for s in db.rows(conn, "SELECT * FROM signals WHERE type='competitor_price_change' AND competitor_id=? AND observed_at >= ? AND severity IN ('medium','high')", (cid, since)):
        key = f"{cid}:PRICE_CHANGE:{s['competitor_product_id']}:{(s['observed_at'] or '')[:10]}"
        n += _save_event(conn, cid, run_id, "PRICE_CHANGE", s["title"][:120], s["what_happened"], f"Было {s['old_value']}, стало {s['new_value']}.", s["source_url"], None, s["category_slug"], 2, key, "prices", s["observed_at"])
    return n


def _save_event(conn, cid, run_id, et, title, summary, what_changed, url, product_name, category_slug, importance, key, source_kind, published_at, ai_used=0) -> int:
    if db.row(conn, "SELECT id FROM news_events WHERE dedupe_key=?", (key,)):
        return 0
    # семантический дубль: тот же товар уже заведён другим путём (страница каталога и сбор карточек) в последние 14 дней
    if product_name:
        pk = _norm_key(product_name)
        for e in db.rows(conn, "SELECT id, product_name FROM news_events WHERE competitor_id=? AND event_type=? AND detected_at >= datetime('now','-14 days') AND product_name IS NOT NULL", (cid, et)):
            ek = _norm_key(e["product_name"])
            if pk and ek and (pk == ek or (len(pk) >= 8 and (pk in ek or ek in pk))):
                return 0
    conn.execute("""INSERT INTO news_events(competitor_id, run_id, event_type, title, summary, what_changed, url, product_name, category_slug, importance, published_at, source_kind, ai_used, dedupe_key)
                    VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)""", (cid, run_id, et, title, summary, what_changed, url, product_name, category_slug, importance, published_at, source_kind, ai_used, key))
    return 1


# ---------------- главный цикл по одному конкуренту ----------------
def check_competitor(conn: sqlite3.Connection, comp: dict, run_id: int) -> dict:
    cur = conn.execute("INSERT INTO news_runs(run_id, competitor_id) VALUES(?,?)", (run_id, comp["id"]))
    nr_id = int(cur.lastrowid)
    conn.commit()
    prev_run = db.row(conn, "SELECT finished_at FROM news_runs WHERE competitor_id=? AND id<? AND status IN ('NO_CHANGES','CHANGES_FOUND','PARTIAL_CRAWL','BASELINE') ORDER BY id DESC LIMIT 1", (comp["id"], nr_id))
    since = (prev_run["finished_at"] if prev_run and prev_run["finished_at"] else (datetime.now() - timedelta(days=1)).strftime("%Y-%m-%d %H:%M:%S"))
    pages = ensure_pages(conn, comp)
    ok = failed = 0
    changes: list[dict] = []
    baseline_pages = 0
    done: set[int] = set()
    queue = list(pages)
    while True:
        if not queue and len(done) < config.NEWS_MAX_PAGES:
            # разделы, найденные на главной и в каталоге, проверяем в этом же прогоне (в пределах лимита страниц)
            queue = [p for p in _pick_pages(conn, comp["id"]) if p["id"] not in done][: config.NEWS_MAX_PAGES - len(done)]
        if not queue:
            break
        page = queue.pop(0)
        if page["id"] in done:
            continue
        done.add(page["id"])
        try:
            res = http.fetch(page["url"], SOURCE_KEY, save=False, timeout=30)
        except http.RobotsDisallowed as exc:
            failed += 1
            conn.execute("UPDATE news_pages SET last_status='robots_disallowed', last_error=?, last_checked_at=datetime('now'), fail_count=fail_count+1, is_active=0 WHERE id=?", (str(exc)[:300], page["id"]))
            conn.commit()
            continue
        except Exception as exc:  # noqa: BLE001
            failed += 1
            conn.execute("UPDATE news_pages SET last_status='error', last_error=?, last_checked_at=datetime('now'), fail_count=fail_count+1 WHERE id=?", (str(exc)[:300], page["id"]))
            if page["fail_count"] + 1 >= 4 and page["kind"] != "home":
                conn.execute("UPDATE news_pages SET is_active=0 WHERE id=?", (page["id"],))
            conn.commit()
            continue
        ok += 1
        text, items, title = normalize_page(res.text, res.final_url or page["url"])
        h = content_hash(text, items)
        if page["kind"] in ("home", "catalog"):
            add_discovered(conn, comp["id"], discover_links(res.text, res.final_url or page["url"], comp["website"]), page["url"])
        prev = db.row(conn, "SELECT * FROM news_snapshots WHERE page_id=? ORDER BY id DESC LIMIT 1", (page["id"],))
        conn.execute("UPDATE news_pages SET last_status='ok', last_error=NULL, last_checked_at=datetime('now'), fail_count=0, title=COALESCE(title, ?) WHERE id=?", (title, page["id"]))
        if prev is None:
            baseline_pages += 1
            conn.execute("INSERT INTO news_snapshots(page_id, run_id, content_hash, text, items_json) VALUES(?,?,?,?,?)", (page["id"], run_id, h, text, db.j(items)))
            conn.commit()
            continue
        if prev["content_hash"] == h:
            conn.execute("UPDATE news_snapshots SET fetched_at=datetime('now'), run_id=? WHERE id=?", (run_id, prev["id"]))
            conn.commit()
            continue
        d = diff_snapshots(prev["text"] or "", text, db.uj(prev["items_json"], []) or [], items)
        conn.execute("INSERT INTO news_snapshots(page_id, run_id, content_hash, text, items_json) VALUES(?,?,?,?,?)", (page["id"], run_id, h, text, db.j(items)))
        conn.execute("DELETE FROM news_snapshots WHERE page_id=? AND id NOT IN (SELECT id FROM news_snapshots WHERE page_id=? ORDER BY id DESC LIMIT 3)", (page["id"], page["id"]))
        if d is None:
            conn.commit()
            continue
        cur = conn.execute("INSERT INTO news_changes(run_id, page_id, competitor_id, change_kind, before_text, after_text, items_json) VALUES(?,?,?,?,?,?,?)",
                           (run_id, page["id"], comp["id"], "items_added" if d["items_added"] else "text", "\n".join(d["removed"])[:4000], "\n".join(d["added"])[:6000], db.j(d["items_added"])))
        changes.append({"id": int(cur.lastrowid), "page": {**dict(page), "title": page["title"] or title}, "diff": d, "url": page["url"], "kind": page["kind"], "change_kind": "items_added" if d["items_added"] else "text",
                        "before": "\n".join(d["removed"])[:1500], "after": "\n".join(d["added"])[:2500], "items": [i["t"] for i in d["items_added"]][:40]})
        conn.commit()
    events = 0
    # CLASSIFY: сначала модель (только по найденным изменениям), при отказе — эвристика
    verdicts = ai.classify_changes(changes, comp["name"]) if changes else None
    by_id = {v["id"]: v for v in verdicts} if verdicts else {}
    for ch in changes:
        v = by_id.get(ch["id"])
        made = 0
        if v is not None:
            if v.get("significant") and v.get("event_type") in EVENT_NAMES:
                key = f"{comp['id']}:{v['event_type']}:{_norm_key(v.get('product_name') or v.get('title') or ch['url'])}"
                made = _save_event(conn, comp["id"], run_id, v["event_type"], (v.get("title") or ch["page"].get("title") or ch["url"])[:120], v.get("summary"), v.get("what_changed"),
                                   ch["url"], v.get("product_name"), None, int(v.get("importance") or 2), key, "ai", None, ai_used=1)
        else:
            for e in heuristic_classify(ch["page"], ch["diff"], comp["name"]):
                key = f"{comp['id']}:{e['event_type']}:{_norm_key(e.get('product_name') or e['title'])}"
                made += _save_event(conn, comp["id"], run_id, e["event_type"], e["title"][:120], e["summary"], e["what_changed"], e["url"], e.get("product_name"), None, e["importance"], key, "page_diff", None)
        events += made
        conn.execute("UPDATE news_changes SET verdict=?, event_id=(SELECT MAX(id) FROM news_events WHERE run_id=? AND competitor_id=?) WHERE id=?",
                     ("event" if made else "noise", run_id, comp["id"], ch["id"]))
    events += events_from_products(conn, comp, since, run_id)
    total = len(done)
    if total == 0 or ok == 0:
        status = "CRAWL_ERROR"
    elif baseline_pages == ok and not events:
        status = "BASELINE"
    elif failed and ok < total:
        status = "PARTIAL_CRAWL"
    else:
        status = "CHANGES_FOUND" if events else "NO_CHANGES"
    if status == "PARTIAL_CRAWL" and events:
        status = "PARTIAL_CRAWL"  # часть страниц не проверена, но события есть — так и показываем
    conn.execute("UPDATE news_runs SET finished_at=datetime('now'), status=?, pages_total=?, pages_ok=?, pages_failed=?, events_found=?, error=? WHERE id=?",
                 (status, total, ok, failed, events, None if ok else "ни одна страница не открылась", nr_id))
    conn.commit()
    return {"competitor": comp["name"], "status": status, "pages": total, "ok": ok, "failed": failed, "events": events}


def run(conn: sqlite3.Connection, tiers: tuple[str, ...] = ("A", "B"), competitor_id: int | None = None) -> dict:
    """Ежедневный прогон по конкурентам уровней A и B (сайты; участники тендеров без сайта пропускаются)."""
    run_id = db.start_run(conn, SOURCE_KEY)
    conn.commit()
    sql = "SELECT * FROM competitors WHERE is_active=1 AND tier IN (%s) AND website LIKE 'http%%'" % ",".join("?" * len(tiers))
    params: list = list(tiers)
    if competitor_id:
        sql += " AND id=?"
        params.append(competitor_id)
    comps = db.rows(conn, sql + " ORDER BY tier, name", params)
    results = []
    for comp in comps:
        try:
            results.append(check_competitor(conn, comp, run_id))
        except Exception as exc:  # noqa: BLE001
            log.exception("news %s", comp["name"])
            db.log_error(conn, SOURCE_KEY, comp["website"], str(exc), comp["name"])
            conn.execute("UPDATE news_runs SET finished_at=datetime('now'), status='CRAWL_ERROR', error=? WHERE competitor_id=? AND run_id=?", (str(exc)[:500], comp["id"], run_id))
            conn.commit()
            results.append({"competitor": comp["name"], "status": "CRAWL_ERROR", "pages": 0, "ok": 0, "failed": 0, "events": 0})
    ev = sum(r["events"] for r in results)
    errs = sum(1 for r in results if r["status"] == "CRAWL_ERROR")
    db.finish_run(conn, run_id, "ok" if not errs else "partial", len(results), ev, errs, f"событий {ev}, конкурентов с ошибкой {errs}")
    digest = build_digest(conn, run_id, results)
    conn.commit()
    return {"competitors": len(results), "events": ev, "errors": errs, "digest": digest[:300]}


# ---------------- DIGEST ----------------
def build_digest(conn: sqlite3.Connection, run_id: int, results: list[dict]) -> str:
    day = datetime.now().strftime("%Y-%m-%d")
    a = sum(1 for r in results if r.get("tier", "A") == "A")
    comps = {c["id"]: c for c in db.rows(conn, "SELECT id, name, tier FROM competitors")}
    runs = db.rows(conn, "SELECT nr.*, c.tier FROM news_runs nr JOIN competitors c ON c.id=nr.competitor_id WHERE nr.run_id=?", (run_id,))
    by_tier = {"A": [r for r in runs if r["tier"] == "A"], "B": [r for r in runs if r["tier"] == "B"]}
    events = db.rows(conn, "SELECT e.*, c.name competitor_name FROM news_events e JOIN competitors c ON c.id=e.competitor_id WHERE e.run_id=? ORDER BY e.importance DESC, e.id", (run_id,))
    lines = [f"Новости конкурентов за {datetime.now().strftime('%d.%m.%Y')}", "",
             f"Проверено: {len(runs)} конкурентов (категория A: {len(by_tier['A'])}, категория B: {len(by_tier['B'])}).",
             f"Не удалось проверить: {sum(1 for r in runs if r['status'] == 'CRAWL_ERROR')}; проверены частично: {sum(1 for r in runs if r['status'] == 'PARTIAL_CRAWL')}.", ""]
    if events:
        lines.append(f"Обнаружено: {len(events)} существенных изменений у {len({e['competitor_id'] for e in events})} конкурентов.")
        lines.append("")
        lines.append("Главное:")
        for i, e in enumerate(events[:10], 1):
            lines.append(f"{i}. {e['competitor_name']} — {EVENT_NAMES.get(e['event_type'], e['event_type']).lower()}: {e['title']}")
        if len(events) > 10:
            lines.append(f"…и ещё {len(events) - 10}.")
        lines.append("")
        lines.append("Остальные конкуренты — без существенных изменений.")
    else:
        lines.append("За последние сутки существенных изменений у конкурентов категорий A и B не обнаружено.")
    text = "\n".join(lines)
    conn.execute("INSERT INTO news_digests(day, text, stats_json) VALUES(?,?,?) ON CONFLICT(day) DO UPDATE SET text=excluded.text, stats_json=excluded.stats_json, created_at=datetime('now')",
                 (day, text, db.j({"checked": len(runs), "events": len(events), "errors": sum(1 for r in runs if r["status"] == "CRAWL_ERROR")})))
    return text


# ---------------- DISPLAY: данные для экрана ----------------
def overview(conn: sqlite3.Connection, f: dict | None = None) -> dict:
    """Сводка для экрана: последняя проверка, за 24 часа, по уровням A и B — конкуренты со статусом и событиями."""
    f = f or {}
    last = db.row(conn, "SELECT MAX(finished_at) t FROM news_runs")
    since24 = (datetime.now() - timedelta(hours=24)).strftime("%Y-%m-%d %H:%M:%S")
    days = int(f.get("days") or 7)
    since = (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d %H:%M:%S")
    where = ["e.detected_at >= ?"]
    params: list = [since]
    if f.get("event_type"):
        where.append("e.event_type=?"); params.append(f["event_type"])
    if f.get("competitor_id"):
        where.append("e.competitor_id=?"); params.append(int(f["competitor_id"]))
    if f.get("category"):
        where.append("e.category_slug=?"); params.append(f["category"])
    if f.get("unread"):
        where.append("e.is_read=0")
    if f.get("products_only"):
        where.append("e.event_type IN ('NEW_PRODUCT','NEW_CATEGORY','NEW_BRAND','PRODUCT_UPDATE')")
    events = db.rows(conn, f"SELECT e.*, c.name competitor_name, c.tier FROM news_events e JOIN competitors c ON c.id=e.competitor_id WHERE {' AND '.join(where)} ORDER BY e.detected_at DESC, e.importance DESC", params)
    views = {v["competitor_id"]: v["viewed_at"] for v in db.rows(conn, "SELECT * FROM news_views")}
    comps = db.rows(conn, "SELECT id, name, tier, website FROM competitors WHERE is_active=1 AND tier IN ('A','B') AND website LIKE 'http%' ORDER BY tier, name")
    last_runs = {r["competitor_id"]: r for r in db.rows(conn, "SELECT nr.* FROM news_runs nr WHERE nr.id IN (SELECT MAX(id) FROM news_runs GROUP BY competitor_id)")}
    last_ok = {r["competitor_id"]: r["finished_at"] for r in db.rows(conn, "SELECT competitor_id, MAX(finished_at) finished_at FROM news_runs WHERE status IN ('NO_CHANGES','CHANGES_FOUND','PARTIAL_CRAWL','BASELINE') GROUP BY competitor_id")}
    tiers = {"A": [], "B": []}
    ev_by_comp: dict[int, list] = {}
    for e in events:
        ev_by_comp.setdefault(e["competitor_id"], []).append(e)
    for c in comps:
        if f.get("tier") and c["tier"] != f["tier"]:
            continue
        viewed = views.get(c["id"], "")
        evs = ev_by_comp.get(c["id"], [])
        new_n = sum(1 for e in evs if e["detected_at"] > viewed and not e["is_read"])
        r = last_runs.get(c["id"])
        row = {"c": c, "events": evs, "new": new_n, "run": r, "last_ok": last_ok.get(c["id"]), "status": r["status"] if r else None}
        if f.get("competitor_id") and int(f["competitor_id"]) != c["id"]:
            continue
        tiers[c["tier"]].append(row)
    for t in tiers.values():
        t.sort(key=lambda x: (-x["new"], -len(x["events"]), x["c"]["name"]))
    ev24 = [e for e in events if e["detected_at"] >= since24]
    digest = db.row(conn, "SELECT * FROM news_digests ORDER BY day DESC LIMIT 1")
    return {"last_check": last["t"] if last else None, "n24": len(ev24), "comp24": len({e["competitor_id"] for e in ev24}), "tiers": tiers, "events": events, "digest": digest,
            "errors": sum(1 for r in last_runs.values() if r["status"] == "CRAWL_ERROR"), "checked": len(last_runs)}


def competitor_feed(conn: sqlite3.Connection, cid: int) -> dict:
    comp = db.row(conn, "SELECT * FROM competitors WHERE id=?", (cid,))
    events = db.rows(conn, "SELECT * FROM news_events WHERE competitor_id=? ORDER BY detected_at DESC, importance DESC", (cid,))
    runs = db.rows(conn, "SELECT * FROM news_runs WHERE competitor_id=? ORDER BY id DESC LIMIT 30", (cid,))
    pages = db.rows(conn, "SELECT * FROM news_pages WHERE competitor_id=? ORDER BY is_active DESC, kind, id", (cid,))
    return {"comp": comp, "events": events, "runs": runs, "pages": pages}


def mark_viewed(conn: sqlite3.Connection, cid: int) -> None:
    conn.execute("INSERT INTO news_views(competitor_id, viewed_at) VALUES(?, datetime('now')) ON CONFLICT(competitor_id) DO UPDATE SET viewed_at=datetime('now')", (cid,))
    conn.execute("UPDATE news_events SET is_read=1 WHERE competitor_id=?", (cid,))
    conn.commit()
