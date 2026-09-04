"""Google Trends через pytrends (неофициальная библиотека). Относительный индекс интереса 0–100, RU.

Ограничения: жёсткие лимиты частоты (HTTP 429) — запрашиваем только seed-запросы, группами до 5, с паузами;
при 429 фиксируем ошибку источника и продолжаем со следующей группы через паузу. Абсолютных объёмов нет.
"""
from __future__ import annotations

import logging
import sqlite3
import time

from .. import db

log = logging.getLogger(__name__)
SOURCE_KEY = "google_trends"


def _groups(items: list, size: int = 5) -> list[list]:
    return [items[i:i + size] for i in range(0, len(items), size)]


def run(conn: sqlite3.Connection, timeframe: str = "today 5-y", max_groups: int | None = None, only_seed: bool = True, pause: float = 70.0) -> dict:
    run_id = db.start_run(conn, SOURCE_KEY)
    conn.commit()
    seen = errors = 0
    try:
        from pytrends.request import TrendReq
    except ImportError as exc:
        db.finish_run(conn, run_id, "error", 0, 0, 1, f"pytrends не установлен: {exc}")
        conn.commit()
        return {"seen": 0, "errors": 1}
    sql = "SELECT id, query FROM search_queries WHERE is_active=1" + (" AND is_seed=1" if only_seed else "") + " ORDER BY is_seed DESC, id"
    queries = db.rows(conn, sql)
    groups = _groups(queries, 5)
    if max_groups:
        groups = groups[:max_groups]
    try:
        pt = TrendReq(hl="ru-RU", tz=180, timeout=(10, 30))
    except Exception as exc:  # noqa: BLE001
        db.finish_run(conn, run_id, "error", 0, 0, 1, f"TrendReq: {exc}")
        conn.commit()
        return {"seen": 0, "errors": 1}
    last_error = ""
    for gi, g in enumerate(groups):
        kw = [q["query"] for q in g]
        try:
            pt.build_payload(kw, geo="RU", timeframe=timeframe)
            df = pt.interest_over_time()
            if df is None or df.empty:
                errors += 1
                last_error = f"пустой ответ для {kw}"
                db.log_error(conn, SOURCE_KEY, None, last_error)
            else:
                for q in g:
                    if q["query"] not in df.columns:
                        continue
                    for idx, val in df[q["query"]].items():
                        d = idx.strftime("%Y-%m-%d")
                        conn.execute("""INSERT INTO demand_observations(query_id, source, period_start, period_end, value, unit, geo, meta_json) VALUES(?,?,?,?,?,?,?,?)
                                        ON CONFLICT(query_id, source, period_start, period_end, geo) DO UPDATE SET value=excluded.value, fetched_at=datetime('now')""",
                                     (q["id"], SOURCE_KEY, d, d, float(val), "index", "RU", db.j({"timeframe": timeframe, "group": kw})))
                        seen += 1
                conn.commit()
            # связанные запросы — расширение семантики (только для первой группы, чтобы не ловить 429)
            if gi == 0:
                try:
                    rel = pt.related_queries()
                    for q in g:
                        r = rel.get(q["query"]) or {}
                        for kind in ("top", "rising"):
                            frame = r.get(kind)
                            if frame is None or frame.empty:
                                continue
                            for _, row in frame.head(10).iterrows():
                                text = str(row["query"]).strip().lower()
                                if text and not db.row(conn, "SELECT id FROM search_queries WHERE query=?", (text,)):
                                    conn.execute("INSERT INTO search_queries(query, category_slug, intent, notes, added_by) VALUES(?,?,?,?,?)",
                                                 (text, q.get("category_slug"), "commercial", f"google trends related ({kind}) к «{q['query']}»", "google_trends"))
                    conn.commit()
                except Exception as exc:  # noqa: BLE001
                    log.info("related_queries недоступны: %s", exc)
        except Exception as exc:  # noqa: BLE001
            errors += 1
            last_error = f"{type(exc).__name__}: {exc}"[:300]
            db.log_error(conn, SOURCE_KEY, None, last_error, str(kw))
            conn.commit()
            if "429" in str(exc) or "TooManyRequests" in type(exc).__name__:
                log.warning("Google Trends 429 — пауза 60 с")
                time.sleep(60)
        time.sleep(pause)
    status = "ok" if errors == 0 else ("partial" if seen else "error")
    db.finish_run(conn, run_id, status, seen, seen, errors, f"{len(groups)} групп, {seen} наблюдений, {errors} ошибок. {last_error}")
    conn.commit()
    return {"groups": len(groups), "seen": seen, "errors": errors, "last_error": last_error}
