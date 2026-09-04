"""Яндекс Подсказки: расширение семантики и обнаружение новых формулировок (без объёмов)."""
from __future__ import annotations

import json
import logging
import sqlite3

from .. import db, http, normalize

log = logging.getLogger(__name__)
SOURCE_KEY = "yandex_suggest"
ENDPOINT = "https://suggest.yandex.ru/suggest-ff.cgi?part={q}&uil=ru&v=3"


def fetch_suggestions(term: str) -> list[str]:
    from urllib.parse import quote

    res = http.fetch(ENDPOINT.format(q=quote(term)), SOURCE_KEY, respect_robots=False, save=False, delay=1.0, extra_headers={"Accept": "*/*"})
    data = json.loads(res.text)
    items = data[1] if isinstance(data, list) and len(data) > 1 else []
    return [normalize.clean_text(str(x)).lower() for x in items if isinstance(x, str)]


def run(conn: sqlite3.Connection, limit: int | None = None) -> dict:
    run_id = db.start_run(conn, SOURCE_KEY)
    conn.commit()
    seeds = db.rows(conn, "SELECT id, query, category_slug FROM search_queries WHERE is_active=1 AND is_seed=1 ORDER BY id")
    if limit:
        seeds = seeds[:limit]
    seen = new = errors = 0
    for s in seeds:
        try:
            sugg = fetch_suggestions(s["query"])
            seen += len(sugg)
            for i, text in enumerate(sugg):
                if not text or text == s["query"]:
                    continue
                if not db.row(conn, "SELECT id FROM search_queries WHERE query=?", (text,)):
                    conn.execute("INSERT INTO search_queries(query, category_slug, intent, notes, added_by) VALUES(?,?,?,?,?)",
                                 (text, normalize.classify_category(text) or s["category_slug"], "commercial", f"yandex suggest к «{s['query']}» (позиция {i + 1})", "yandex_suggest"))
                    new += 1
                qid = db.row(conn, "SELECT id FROM search_queries WHERE query=?", (text,))["id"]
                # ранг подсказки как наблюдение (unit=rank): позволяет отслеживать появление/исчезновение формулировок
                today = db.now_iso()[:10]
                conn.execute("""INSERT INTO demand_observations(query_id, source, period_start, period_end, value, unit, geo, meta_json) VALUES(?,?,?,?,?,?,?,?)
                                ON CONFLICT(query_id, source, period_start, period_end, geo) DO UPDATE SET value=excluded.value""",
                             (qid, SOURCE_KEY, today, today, float(i + 1), "rank", "RU", db.j({"seed": s["query"]})))
            conn.commit()
        except Exception as exc:  # noqa: BLE001
            errors += 1
            db.log_error(conn, SOURCE_KEY, None, str(exc)[:300], s["query"])
            conn.commit()
    status = "ok" if errors == 0 else ("partial" if seen else "error")
    db.finish_run(conn, run_id, status, seen, new, errors, f"{len(seeds)} seed-запросов, {seen} подсказок, {new} новых формулировок")
    conn.commit()
    return {"seeds": len(seeds), "suggestions": seen, "new_queries": new, "errors": errors}
