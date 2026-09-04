"""ЕИС zakupki.gov.ru — расширенный поиск закупок по ключевым словам (HTML, серверный рендер).

Ограничения: robots.txt задаёт Crawl-delay 60 — не чаще одного запроса в минуту; сертификат сайта выпущен
российским УЦ (Минцифры) и может отсутствовать в системном хранилище — тогда используется соединение без проверки
сертификата (только чтение публичных данных). Результат: номер, предмет, заказчик, НМЦК, дата — в market_observations.
"""
from __future__ import annotations

import logging
import re
import sqlite3
import time
from urllib.parse import quote

import requests
from bs4 import BeautifulSoup

from .. import config, db, normalize

log = logging.getLogger(__name__)
SOURCE_KEY = "zakupki"
URL = ("https://zakupki.gov.ru/epz/order/extendedsearch/results.html?searchString={q}&morphology=on&search-filter=Дате+размещения"
       "&pageNumber=1&sortDirection=false&recordsPerPage=_20&showLotsInfoHidden=false&sortBy=UPDATE_DATE&fz44=on&fz223=on&af=on&ca=on&pc=on&pa=on&currencyIdGeneral=-1")
QUERIES = ["радиогид", "аудиогид", "система синхронного перевода", "оборудование для экскурсий наушники"]


def _get(url: str) -> str:
    headers = {"User-Agent": config.USER_AGENT, "Accept": "text/html", "Accept-Language": "ru-RU,ru;q=0.9"}
    try:
        r = requests.get(url, headers=headers, timeout=40)
    except requests.exceptions.SSLError:
        log.warning("zakupki.gov.ru: сертификат не распознан системой, повтор без проверки (только чтение)")
        import urllib3

        urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
        r = requests.get(url, headers=headers, timeout=40, verify=False)  # noqa: S501
    r.raise_for_status()
    r.encoding = "utf-8"
    return r.text


def parse_results(html: str) -> list[dict]:
    soup = BeautifulSoup(html, "lxml")
    out = []
    for card in soup.select(".search-registry-entry-block, .registry-entry__form"):
        num_el = card.select_one(".registry-entry__header-mid__number a, .registry-entry__header-mid__number")
        number = normalize.clean_text(num_el.get_text()).replace("№", "").strip() if num_el else None
        link = num_el.get("href") if num_el and num_el.name == "a" else (num_el.find("a")["href"] if num_el and num_el.find("a") else None)
        subj_el = card.select_one(".registry-entry__body-value")
        subject = normalize.clean_text(subj_el.get_text()) if subj_el else None
        cust_el = card.select_one(".registry-entry__body-href a, .registry-entry__body-href")
        customer = normalize.clean_text(cust_el.get_text()) if cust_el else None
        price_el = card.select_one(".price-block__value")
        price = normalize.parse_price(price_el.get_text()) if price_el else None
        date_el = None
        for blk in card.select(".data-block__title"):
            if "Размещено" in blk.get_text():
                date_el = blk.find_next_sibling(class_="data-block__value")
                break
        date_txt = normalize.clean_text(date_el.get_text()) if date_el else None
        d = None
        if date_txt:
            m = re.search(r"(\d{2})\.(\d{2})\.(\d{4})", date_txt)
            if m:
                d = f"{m.group(3)}-{m.group(2)}-{m.group(1)}"
        status_el = card.select_one(".registry-entry__header-mid__title")
        status = normalize.clean_text(status_el.get_text()) if status_el else None
        if number and subject:
            out.append({"number": number, "subject": subject, "customer": customer, "price": price, "date": d, "status": status,
                        "url": ("https://zakupki.gov.ru" + link) if link and link.startswith("/") else link})
    return out


def run(conn: sqlite3.Connection, queries: list[str] | None = None) -> dict:
    run_id = db.start_run(conn, SOURCE_KEY)
    conn.commit()
    seen = new = errors = 0
    last_error = ""
    for i, q in enumerate(queries or QUERIES):
        if i:
            time.sleep(61)  # Crawl-delay: 60
        try:
            html = _get(URL.format(q=quote(q)))
            items = parse_results(html)
            if not items and "captcha" in html.lower():
                raise RuntimeError("ЕИС вернул проверку (captcha) — сбор остановлен, повтор позже")
            for it in items:
                key = f"tender:{it['number']}"
                seen += 1
                if db.row(conn, "SELECT id FROM market_observations WHERE dedupe_key=?", (key,)):
                    conn.execute("UPDATE market_observations SET price=COALESCE(?, price), note=?, fetched_at=datetime('now') WHERE dedupe_key=?", (it["price"], it.get("status"), key))
                    continue
                conn.execute("INSERT INTO market_observations(kind,title,party,price,observed_date,url,source,note,category_slug,dedupe_key) VALUES('tender',?,?,?,?,?,?,?,?,?)",
                             (it["subject"][:300], it["customer"], it["price"], it["date"], it["url"], f"zakupki.gov.ru («{q}»)", it.get("status"), normalize.classify_category(it["subject"]), key))
                new += 1
            conn.commit()
            log.info("zakupki «%s»: %s записей", q, len(items))
        except Exception as exc:  # noqa: BLE001
            errors += 1
            last_error = f"{type(exc).__name__}: {exc}"[:300]
            db.log_error(conn, SOURCE_KEY, q, last_error)
            conn.commit()
    status = "ok" if errors == 0 else ("partial" if seen else "error")
    db.finish_run(conn, run_id, status, seen, new, errors, f"{seen} закупок, {new} новых, {errors} ошибок. {last_error}")
    conn.commit()
    return {"seen": seen, "new": new, "errors": errors}
