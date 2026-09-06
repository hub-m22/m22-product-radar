"""Веб-интерфейс M22 Product Radar (FastAPI + Jinja2, серверный рендеринг, русский язык)."""
from __future__ import annotations

import csv
import io
import json
import logging
import sqlite3
from contextlib import asynccontextmanager
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import HTMLResponse, RedirectResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from .. import config, db, discovery, importers, matching, recommendations, reports, scheduler, seed, signals
from .. import specs as specmod
from ..importers import TYPE_NAMES
from ..logging_setup import setup_logging
from ..normalize import CATEGORY_NAMES, classify_category
from ..signals import SIGNAL_TYPES

log = logging.getLogger(__name__)
HERE = Path(__file__).parent

STATUS_NAMES = {"new": "Новый", "in_research": "На исследовании", "accepted": "Принято", "rejected": "Отклонено", "done": "Выполнено", "in_progress": "В работе",
                "research": "Исследуется", "approved": "Одобрено", "parked": "Отложено"}
SEV_NAMES = {"high": "Высокая", "medium": "Средняя", "low": "Низкая"}
FACT_NAMES = {"fact": "Подтверждённый факт", "inference": "Аналитический вывод", "hypothesis": "Гипотеза"}
MATCH_NAMES = {"exact_model": "Точное совпадение модели", "direct_analog": "Прямой аналог", "functional": "Функционально похожий", "kit": "Комплект", "accessory": "Аксессуар",
               "substitute": "Заменитель", "adjacent": "Смежный товар", "new_category": "Новая категория"}
SOURCE_STATUS = {"ok": "Работает", "error": "Ошибка", "needs_auth": "Требует подключения", "blocked": "Недоступен", "paid": "Платный", "manual_import": "Ручной импорт",
                 "disabled": "Отключён", "unknown": "Не проверялся"}
MVP_NAMES = {"ready": "Готов к использованию", "manual_import": "Ручной импорт", "needs_key": "Нужен ключ/токен", "blocked": "Заблокирован для автоматики", "paid": "Платный доступ"}


@asynccontextmanager
async def lifespan(app: FastAPI):
    setup_logging()
    db.init_db()
    with db.session() as conn:
        seed.seed(conn)
    scheduler.start()
    yield


app = FastAPI(title="M22 Product Radar", lifespan=lifespan)
app.mount("/static", StaticFiles(directory=str(HERE / "static")), name="static")
templates = Jinja2Templates(directory=str(HERE / "templates"))


def fmt_money(v):
    if v is None or v == "":
        return "—"
    try:
        return f"{float(v):,.0f} ₽".replace(",", " ")
    except (TypeError, ValueError):
        return str(v)


def fmt_pct(v):
    return "—" if v is None else f"{float(v):.0%}"


def fmt_dt(v):
    if not v:
        return "—"
    s = str(v).replace("T", " ").replace("Z", "")
    return s[:16]


templates.env.filters.update({"money": fmt_money, "pct": fmt_pct, "dt": fmt_dt, "uj": lambda t, d=None: db.uj(t, d)})
templates.env.globals.update({"CATEGORY_NAMES": CATEGORY_NAMES, "SIGNAL_TYPES": SIGNAL_TYPES, "STATUS_NAMES": STATUS_NAMES, "SEV_NAMES": SEV_NAMES, "FACT_NAMES": FACT_NAMES,
                              "MATCH_NAMES": MATCH_NAMES, "SOURCE_STATUS": SOURCE_STATUS, "MVP_NAMES": MVP_NAMES, "TYPE_NAMES": TYPE_NAMES, "app_version": "0.1.0", "asset_version": str(int(__import__("time").time()))})


def render(request: Request, name: str, **ctx) -> HTMLResponse:
    with db.session() as conn:
        last_collect = db.row(conn, "SELECT MAX(finished_at) AS t FROM source_runs WHERE status IN ('ok','partial')")
        errors = db.row(conn, "SELECT COUNT(*) AS n FROM sources WHERE status='error'")["n"]
        owners = (db.get_setting(conn, "owners") or "").split(";")
    ctx.update({"request": request, "last_update": last_collect["t"] if last_collect else None, "source_errors": errors, "owners": [o for o in owners if o],
                "now": datetime.now().strftime("%d.%m.%Y %H:%M"), "path": request.url.path})
    return templates.TemplateResponse(request, name, ctx)


def _filters(request: Request) -> dict:
    q = request.query_params
    return {"period": q.get("period", ""), "category": q.get("category", ""), "product": q.get("product", ""), "competitor": q.get("competitor", ""),
            "type": q.get("type", ""), "severity": q.get("severity", ""), "confidence": q.get("confidence", ""), "status": q.get("status", ""), "q": q.get("q", "")}


def _signal_where(f: dict) -> tuple[str, list]:
    where, params = ["1=1"], []
    if f["period"]:
        days = int(f["period"])
        where.append("substr(s.created_at,1,10) >= ?")
        params.append((date.today() - timedelta(days=days)).isoformat())
    if f["category"]:
        where.append("s.category_slug=?")
        params.append(f["category"])
    if f["product"]:
        where.append("s.m22_product_id=?")
        params.append(int(f["product"]))
    if f["competitor"]:
        where.append("s.competitor_id=?")
        params.append(int(f["competitor"]))
    if f["type"]:
        where.append("s.type=?")
        params.append(f["type"])
    if f["severity"]:
        where.append("s.severity=?")
        params.append(f["severity"])
    if f["confidence"]:
        where.append("s.confidence >= ?")
        params.append(float(f["confidence"]))
    if f["status"]:
        where.append("s.status=?")
        params.append(f["status"])
    if f["q"]:
        where.append("(s.title LIKE ? OR s.what_happened LIKE ?)")
        params += [f"%{f['q']}%", f"%{f['q']}%"]
    return " AND ".join(where), params


SIGNAL_SQL = """SELECT s.*, m.name AS m22_name, m.url AS m22_url, c.name AS competitor_name, q.query AS query_text FROM signals s
                LEFT JOIN m22_products m ON m.id=s.m22_product_id LEFT JOIN competitors c ON c.id=s.competitor_id LEFT JOIN search_queries q ON q.id=s.query_id"""


def _lists(conn) -> dict:
    return {"categories": db.rows(conn, "SELECT slug, name_ru FROM categories ORDER BY sort_order"),
            "competitors": db.rows(conn, "SELECT id, name FROM competitors WHERE is_active=1 ORDER BY name"),
            "products": db.rows(conn, "SELECT id, name, site FROM m22_products WHERE is_active=1 AND in_scope=1 AND parent_url IS NULL ORDER BY name")}


# ---------------- Главная ----------------
@app.get("/", response_class=HTMLResponse)
def index(request: Request):
    with db.session() as conn:
        sev = "CASE severity WHEN 'high' THEN 0 WHEN 'medium' THEN 1 ELSE 2 END"
        top = db.rows(conn, SIGNAL_SQL + f" WHERE s.status IN ('new','in_research') AND s.type!='source_error' ORDER BY {sev.replace('severity', 's.severity')}, s.confidence DESC, s.created_at DESC LIMIT 8")
        recs = db.rows(conn, "SELECT * FROM recommendations WHERE status IN ('new','accepted','in_progress') ORDER BY priority, confidence DESC LIMIT 6")
        d7, d30 = (date.today() - timedelta(days=7)).isoformat(), (date.today() - timedelta(days=30)).isoformat()
        counts = {
            "signals_7": db.row(conn, "SELECT COUNT(*) n FROM signals WHERE substr(created_at,1,10)>=? AND type!='source_error'", (d7,))["n"],
            "signals_30": db.row(conn, "SELECT COUNT(*) n FROM signals WHERE substr(created_at,1,10)>=? AND type!='source_error'", (d30,))["n"],
            "price_7": db.row(conn, "SELECT COUNT(*) n FROM signals WHERE type='competitor_price_change' AND substr(created_at,1,10)>=?", (d7,))["n"],
            "new_products_7": db.row(conn, "SELECT COUNT(*) n FROM signals WHERE type IN ('product_appeared','new_kit_solution') AND substr(created_at,1,10)>=?", (d7,))["n"],
            "new_products_30": db.row(conn, "SELECT COUNT(*) n FROM signals WHERE type IN ('product_appeared','new_kit_solution') AND substr(created_at,1,10)>=?", (d30,))["n"],
            "gaps": db.row(conn, "SELECT COUNT(*) n FROM signals WHERE type IN ('new_category','multi_competitor_product','category_growth_gap') AND status!='rejected' AND (title LIKE '%нет у M22%' OR title LIKE '%у M22 её нет%' OR title LIKE '%отсутствует%')")["n"],
            "hyps": db.row(conn, "SELECT COUNT(*) n FROM hypotheses WHERE decision_status IN ('new','research')")["n"],
            "recs": db.row(conn, "SELECT COUNT(*) n FROM recommendations WHERE status='new'")["n"],
            "m22": db.row(conn, "SELECT COUNT(*) n FROM m22_products WHERE is_active=1 AND in_scope=1")["n"],
            "comp": db.row(conn, "SELECT COUNT(*) n FROM competitors WHERE is_active=1")["n"],
            "cp": db.row(conn, "SELECT COUNT(*) n FROM competitor_products WHERE is_active=1 AND price IS NOT NULL")["n"],
        }
        cats = db.rows(conn, """SELECT s.category_slug, SUM(CASE WHEN s.type IN ('demand_change','category_growth_existing','category_growth_gap') AND s.new_value LIKE '+%' THEN 1 ELSE 0 END) AS up,
                                SUM(CASE WHEN s.type IN ('demand_change','category_growth_existing') AND s.new_value LIKE '-%' THEN 1 ELSE 0 END) AS down, COUNT(*) AS n
                                FROM signals s WHERE s.category_slug IS NOT NULL AND s.status!='rejected' GROUP BY s.category_slug ORDER BY n DESC LIMIT 12""")
        cat_demand = db.rows(conn, """SELECT q.category_slug, COUNT(DISTINCT q.id) AS queries, COUNT(d.id) AS obs FROM search_queries q LEFT JOIN demand_observations d ON d.query_id=q.id AND d.source='google_trends'
                                      WHERE q.category_slug IS NOT NULL GROUP BY q.category_slug""")
        price_signals = db.rows(conn, SIGNAL_SQL + " WHERE s.type IN ('m22_price_above_market','m22_price_below_market','competitor_price_change','cross_site_discrepancy') AND s.status!='rejected' ORDER BY s.severity='high' DESC, s.confidence DESC LIMIT 6")
        new_products = db.rows(conn, SIGNAL_SQL + " WHERE s.type IN ('product_appeared','new_kit_solution','multi_competitor_product') AND s.status!='rejected' ORDER BY s.created_at DESC LIMIT 6")
        gaps = db.rows(conn, SIGNAL_SQL + " WHERE s.type IN ('new_category','category_growth_gap') AND s.status!='rejected' ORDER BY s.severity='high' DESC LIMIT 6")
        hyps = db.rows(conn, "SELECT * FROM hypotheses WHERE decision_status IN ('new','research') ORDER BY created_at DESC LIMIT 5")
        bad_sources = db.rows(conn, "SELECT * FROM sources WHERE status='error' OR consecutive_failures>0 ORDER BY consecutive_failures DESC")
        bad_pages = db.rows(conn, "SELECT mp.*, c.name AS cname FROM monitored_pages mp JOIN competitors c ON c.id=mp.competitor_id WHERE mp.fail_count>=1 AND mp.is_active=1 ORDER BY mp.fail_count DESC LIMIT 8")
        runs = db.rows(conn, "SELECT * FROM source_runs ORDER BY id DESC LIMIT 6")
        limits = reports._data_limits(conn)
        top_rec = None
        if top:
            for r in db.rows(conn, "SELECT * FROM recommendations WHERE status!='rejected' ORDER BY priority"):
                if top[0]["id"] in (db.uj(r["signal_ids_json"], []) or []):
                    top_rec = r
                    break
    return render(request, "index.html", top=top, recs=recs, top_rec=top_rec, counts=counts, cats=cats, cat_demand={c["category_slug"]: c for c in cat_demand}, price_signals=price_signals,
                  new_products=new_products, gaps=gaps, hyps=hyps, bad_sources=bad_sources, bad_pages=bad_pages, runs=runs, limits=limits)


# ---------------- Рекомендации ----------------
@app.get("/actions", response_class=HTMLResponse)
def actions(request: Request):
    f = _filters(request)
    where, params = ["1=1"], []
    if f["status"]:
        where.append("r.status=?")
        params.append(f["status"])
    if f["category"]:
        where.append("r.category_slug=?")
        params.append(f["category"])
    if f["severity"]:
        where.append("r.priority=?")
        params.append(f["severity"])
    if f["confidence"]:
        where.append("r.confidence>=?")
        params.append(float(f["confidence"]))
    if f["product"]:
        where.append("r.m22_product_id=?")
        params.append(int(f["product"]))
    if f["period"]:
        where.append("substr(r.created_at,1,10)>=?")
        params.append((date.today() - timedelta(days=int(f["period"]))).isoformat())
    if f["q"]:
        where.append("(r.title LIKE ? OR r.action LIKE ?)")
        params += [f"%{f['q']}%"] * 2
    with db.session() as conn:
        recs = db.rows(conn, f"SELECT r.*, m.name AS m22_name FROM recommendations r LEFT JOIN m22_products m ON m.id=r.m22_product_id WHERE {' AND '.join(where)} ORDER BY r.status='new' DESC, r.priority, r.confidence DESC", params)
        lists = _lists(conn)
    return render(request, "actions.html", recs=recs, f=f, **lists)


@app.get("/actions/{rid}", response_class=HTMLResponse)
def action_detail(request: Request, rid: int):
    with db.session() as conn:
        r = db.row(conn, "SELECT r.*, m.name AS m22_name, m.url AS m22_url FROM recommendations r LEFT JOIN m22_products m ON m.id=r.m22_product_id WHERE r.id=?", (rid,))
        if not r:
            raise HTTPException(404)
        sig_ids = db.uj(r["signal_ids_json"], []) or []
        sigs = db.rows(conn, SIGNAL_SQL + f" WHERE s.id IN ({','.join('?' * len(sig_ids))})", sig_ids) if sig_ids else []
        comments = db.rows(conn, "SELECT * FROM comments WHERE entity_type='recommendation' AND entity_id=? ORDER BY created_at DESC", (rid,))
    return render(request, "action_detail.html", r=r, sigs=sigs, comments=comments, sources=db.uj(r["sources_json"], []) or [])


@app.post("/actions/{rid}/update")
def action_update(rid: int, status: str = Form(None), owner: str = Form(None), comment: str = Form(None), due_date: str = Form(None), author: str = Form("")):
    with db.session() as conn:
        if status:
            conn.execute("UPDATE recommendations SET status=?, updated_at=datetime('now') WHERE id=?", (status, rid))
        if owner is not None and owner != "":
            conn.execute("UPDATE recommendations SET owner=?, updated_at=datetime('now') WHERE id=?", (owner, rid))
        if due_date:
            conn.execute("UPDATE recommendations SET due_date=?, updated_at=datetime('now') WHERE id=?", (due_date, rid))
        if comment and comment.strip():
            conn.execute("INSERT INTO comments(entity_type, entity_id, author, text) VALUES('recommendation',?,?,?)", (rid, author or "пользователь", comment.strip()))
            conn.execute("UPDATE recommendations SET comment=?, updated_at=datetime('now') WHERE id=?", (comment.strip(), rid))
    return RedirectResponse(f"/actions/{rid}", status_code=303)


# ---------------- Сигналы ----------------
@app.get("/signals", response_class=HTMLResponse)
def signals_page(request: Request):
    f = _filters(request)
    where, params = _signal_where(f)
    with db.session() as conn:
        rows = db.rows(conn, SIGNAL_SQL + f" WHERE {where} ORDER BY s.status='new' DESC, CASE s.severity WHEN 'high' THEN 0 WHEN 'medium' THEN 1 ELSE 2 END, s.confidence DESC, s.created_at DESC LIMIT 500", params)
        lists = _lists(conn)
    return render(request, "signals.html", sigs=rows, f=f, **lists)


@app.get("/signals/{sid}", response_class=HTMLResponse)
def signal_detail(request: Request, sid: int):
    with db.session() as conn:
        s = db.row(conn, SIGNAL_SQL + " WHERE s.id=?", (sid,))
        if not s:
            raise HTTPException(404)
        comments = db.rows(conn, "SELECT * FROM comments WHERE entity_type='signal' AND entity_id=? ORDER BY created_at DESC", (sid,))
        cp = db.row(conn, "SELECT * FROM competitor_products WHERE id=?", (s["competitor_product_id"],)) if s["competitor_product_id"] else None
        recs = db.rows(conn, "SELECT * FROM recommendations WHERE signal_ids_json LIKE ?", (f"%{sid}%",))
        recs = [r for r in recs if sid in (db.uj(r["signal_ids_json"], []) or [])]
        series = db.rows(conn, "SELECT period_start, value FROM demand_observations WHERE query_id=? AND source='google_trends' ORDER BY period_start", (s["query_id"],)) if s["query_id"] else []
    return render(request, "signal_detail.html", s=s, comments=comments, evidence=db.uj(s["evidence_json"], {}), cp=cp, recs=recs, series=series)


@app.post("/signals/{sid}/update")
def signal_update(sid: int, status: str = Form(None), owner: str = Form(None), comment: str = Form(None), author: str = Form("")):
    with db.session() as conn:
        if status:
            conn.execute("UPDATE signals SET status=?, updated_at=datetime('now') WHERE id=?", (status, sid))
        if owner:
            conn.execute("UPDATE signals SET owner=?, updated_at=datetime('now') WHERE id=?", (owner, sid))
        if comment and comment.strip():
            conn.execute("INSERT INTO comments(entity_type, entity_id, author, text) VALUES('signal',?,?,?)", (sid, author or "пользователь", comment.strip()))
            conn.execute("UPDATE signals SET comment=?, updated_at=datetime('now') WHERE id=?", (comment.strip(), sid))
        if status == "in_research":
            s = db.row(conn, "SELECT * FROM signals WHERE id=?", (sid,))
            key = f"hyp:signal:{sid}"
            if s and not db.row(conn, "SELECT id FROM hypotheses WHERE dedupe_key=?", (key,)):
                conn.execute("""INSERT INTO hypotheses(title, description, discovered_via, signals_json, m22_link, next_step, owner, category_slug, decision_status, dedupe_key)
                                VALUES(?,?,?,?,?,?,?,?,'research',?)""",
                             (f"Исследовать: {s['title'][:90]}", s["what_happened"], f"сигнал #{sid} ({SIGNAL_TYPES.get(s['type'], s['type'])})", db.j([sid]), s["why_matters"],
                              s["recommended_action"], owner or "Продуктовая команда", s["category_slug"], key))
    return RedirectResponse(f"/signals/{sid}", status_code=303)


# ---------------- Конкуренты ----------------
@app.get("/competitors", response_class=HTMLResponse)
def competitors_page(request: Request):
    f = _filters(request)
    with db.session() as conn:
        comps = db.rows(conn, """SELECT c.*, (SELECT COUNT(*) FROM competitor_products cp WHERE cp.competitor_id=c.id AND cp.is_active=1) AS products,
                                 (SELECT COUNT(*) FROM competitor_products cp WHERE cp.competitor_id=c.id AND cp.is_active=1 AND cp.price IS NOT NULL) AS priced,
                                 (SELECT COUNT(*) FROM monitored_pages mp WHERE mp.competitor_id=c.id AND mp.is_active=1) AS pages,
                                 (SELECT MAX(last_checked_at) FROM monitored_pages mp WHERE mp.competitor_id=c.id) AS last_checked,
                                 (SELECT COUNT(*) FROM monitored_pages mp WHERE mp.competitor_id=c.id AND mp.fail_count>0) AS failing
                                 FROM competitors c WHERE c.is_active=1 ORDER BY priced DESC, products DESC, c.name""")
        if f["type"]:
            comps = [c for c in comps if f["type"] in (db.uj(c["types_json"], []) or [])]
        if f["category"]:
            ids = {r["competitor_id"] for r in db.rows(conn, "SELECT DISTINCT competitor_id FROM competitor_products WHERE category_slug=?", (f["category"],))}
            comps = [c for c in comps if c["id"] in ids]
        if f["q"]:
            comps = [c for c in comps if f["q"].lower() in (c["name"] + " " + (c["website"] or "")).lower()]
        lists = _lists(conn)
        review = db.row(conn, "SELECT COUNT(*) n FROM product_matches WHERE needs_review=1 AND review_status='auto'")["n"]
    return render(request, "competitors.html", comps=comps, f=f, review=review, **lists)


@app.post("/competitors/add")
def competitor_add(name: str = Form(...), website: str = Form(...), types: list[str] = Form([]), geography: str = Form(""), notes: str = Form(""), page_url: str = Form(""), page_kind: str = Form("product")):
    website = importers._root(website if website.startswith("http") else "https://" + website)
    with db.session() as conn:
        if not db.row(conn, "SELECT id FROM competitors WHERE website=?", (website,)):
            conn.execute("INSERT INTO competitors(name, website, types_json, geography, notes, added_by, checked_at) VALUES(?,?,?,?,?,'manual',date('now'))",
                         (name.strip(), website, db.j(types or ["direct_seller"]), geography, notes))
        cid = db.row(conn, "SELECT id FROM competitors WHERE website=?", (website,))["id"]
        if page_url.strip() and not db.row(conn, "SELECT id FROM monitored_pages WHERE url=?", (page_url.strip(),)):
            conn.execute("INSERT INTO monitored_pages(competitor_id, url, kind, name) VALUES(?,?,?,?)", (cid, page_url.strip(), page_kind, name))
    return RedirectResponse(f"/competitors/{cid}", status_code=303)


@app.get("/competitors/{cid}", response_class=HTMLResponse)
def competitor_detail(request: Request, cid: int):
    with db.session() as conn:
        c = db.row(conn, "SELECT * FROM competitors WHERE id=?", (cid,))
        if not c:
            raise HTTPException(404)
        pages = db.rows(conn, "SELECT * FROM monitored_pages WHERE competitor_id=? ORDER BY is_active DESC, id", (cid,))
        products = db.rows(conn, """SELECT cp.*, (SELECT COUNT(*) FROM competitor_price_history h WHERE h.competitor_product_id=cp.id) AS obs,
                                    (SELECT GROUP_CONCAT(m.name || ' [' || pm.match_type || ' ' || CAST(ROUND(pm.confidence*100) AS INT) || '%]', ' | ') FROM product_matches pm JOIN m22_products m ON m.id=pm.m22_product_id WHERE pm.competitor_product_id=cp.id AND pm.review_status!='rejected') AS matches
                                    FROM competitor_products cp WHERE cp.competitor_id=? ORDER BY cp.is_active DESC, cp.category_slug, cp.price""", (cid,))
        sigs = db.rows(conn, SIGNAL_SQL + " WHERE s.competitor_id=? ORDER BY s.created_at DESC LIMIT 30", (cid,))
        comments = db.rows(conn, "SELECT * FROM comments WHERE entity_type='competitor' AND entity_id=? ORDER BY created_at DESC", (cid,))
    return render(request, "competitor_detail.html", c=c, pages=pages, products=products, sigs=sigs, comments=comments,
                  types=db.uj(c["types_json"], []) or [], brands=db.uj(c["brands_json"], []) or [], cats=db.uj(c["categories_json"], []) or [], src=db.uj(c["source_urls_json"], []) or [])


@app.post("/competitors/{cid}/update")
def competitor_update(cid: int, comment: str = Form(None), author: str = Form(""), is_active: str = Form(None), notes: str = Form(None)):
    with db.session() as conn:
        if comment and comment.strip():
            conn.execute("INSERT INTO comments(entity_type, entity_id, author, text) VALUES('competitor',?,?,?)", (cid, author or "пользователь", comment.strip()))
        if is_active is not None:
            conn.execute("UPDATE competitors SET is_active=?, updated_at=datetime('now') WHERE id=?", (1 if is_active == "1" else 0, cid))
        if notes is not None:
            conn.execute("UPDATE competitors SET notes=?, updated_at=datetime('now') WHERE id=?", (notes, cid))
    return RedirectResponse(f"/competitors/{cid}", status_code=303)


@app.post("/competitors/{cid}/pages/add")
def page_add(cid: int, url: str = Form(...), kind: str = Form("product"), name: str = Form(""), category_slug: str = Form(""), parser_config: str = Form("")):
    with db.session() as conn:
        if not db.row(conn, "SELECT id FROM monitored_pages WHERE url=?", (url.strip(),)):
            cfg = None
            if parser_config.strip():
                try:
                    cfg = json.dumps(json.loads(parser_config), ensure_ascii=False)
                except ValueError:
                    cfg = None
            conn.execute("INSERT INTO monitored_pages(competitor_id, url, kind, name, category_slug, parser, parser_config_json) VALUES(?,?,?,?,?,?,?)",
                         (cid, url.strip(), kind, name or None, category_slug or None, "css" if cfg else "auto", cfg))
    return RedirectResponse(f"/competitors/{cid}", status_code=303)


@app.post("/pages/{pid}/toggle")
def page_toggle(pid: int):
    with db.session() as conn:
        p = db.row(conn, "SELECT competitor_id, is_active FROM monitored_pages WHERE id=?", (pid,))
        conn.execute("UPDATE monitored_pages SET is_active=?, fail_count=0 WHERE id=?", (0 if p["is_active"] else 1, pid))
    return RedirectResponse(f"/competitors/{p['competitor_id']}", status_code=303)


@app.post("/pages/{pid}/check")
def page_check(pid: int):
    from ..collectors import competitor_generic

    with db.session() as conn:
        p = db.row(conn, "SELECT competitor_id FROM monitored_pages WHERE id=?", (pid,))
        competitor_generic.run(conn, page_id=pid)
        matching.run_matching(conn)
    return RedirectResponse(f"/competitors/{p['competitor_id']}", status_code=303)


@app.get("/matches", response_class=HTMLResponse)
def matches_page(request: Request):
    only_review = request.query_params.get("review", "1") == "1"
    with db.session() as conn:
        rows = db.rows(conn, f"""SELECT pm.*, cp.name AS cp_name, cp.price AS cp_price, cp.url AS cp_url, cp.category_slug AS cp_cat, c.name AS competitor_name, m.name AS m22_name, m.price AS m22_price, m.url AS m22_url
                                 FROM product_matches pm JOIN competitor_products cp ON cp.id=pm.competitor_product_id JOIN competitors c ON c.id=cp.competitor_id
                                 LEFT JOIN m22_products m ON m.id=pm.m22_product_id WHERE {'pm.needs_review=1 AND pm.review_status=\'auto\'' if only_review else '1=1'} ORDER BY pm.confidence DESC LIMIT 400""")
    return render(request, "matches.html", rows=rows, only_review=only_review)


@app.post("/matches/{mid}/review")
def match_review(mid: int, decision: str = Form(...), note: str = Form(""), match_type: str = Form("")):
    with db.session() as conn:
        conn.execute("UPDATE product_matches SET review_status=?, reviewer_note=?, needs_review=0, method='manual', match_type=COALESCE(NULLIF(?, ''), match_type), updated_at=datetime('now') WHERE id=?",
                     ("confirmed" if decision == "confirm" else "rejected", note, match_type, mid))
    return RedirectResponse("/matches", status_code=303)


# ---------------- Сравнение характеристик и цен ----------------
def _norm_rows(rows: list[dict], side: str) -> list[dict]:
    out, seen = [], set()
    for r in rows:
        key = (r.get("model_key"), r.get("kind"), r.get("price"), r.get("capacity")) if side == "m22" else r["id"]
        if key in seen:
            continue  # один и тот же товар M22 в двух цветах / на двух сайтах
        seen.add(key)
        img = r.get("image_url") or (db.uj(r.get("images_json"), []) or [None])[0]
        if img and img.startswith("/"):
            img = f"https://{r.get('site') or 'm22.ru'}{img}"
        out.append({"side": side, "id": r["id"], "name": r["name"], "url": r["url"], "price": r["price"], "seller": r.get("seller") or r.get("site") or "M22",
                    "image": img, "fetched_at": r.get("fetched_at"),
                    "norm": specmod.normalize(r["name"], r.get("description"), r.get("specs_json"), r["price"], r.get("capacity")),
                    "specs": specmod.flatten_specs(r.get("specs_json"))})
    return out


@app.get("/compare", response_class=HTMLResponse)
def compare(request: Request, signal: Optional[int] = None, product: Optional[int] = None, cp: Optional[str] = None, m22: Optional[str] = None):
    """Сравнение: предложения конкурентов (по сигналу, по товару M22 или по списку id) против ближайших моделей M22."""
    with db.session() as conn:
        title, comp_rows, m22_rows = "Сравнение", [], []
        if signal:
            s = db.row(conn, "SELECT * FROM signals WHERE id=?", (signal,))
            if not s:
                raise HTTPException(404)
            ev = db.uj(s["evidence_json"], {}) or {}
            ids = [it["id"] for it in ev.get("items", []) if it.get("id")]
            if ids:
                comp_rows = db.rows(conn, f"SELECT cp.*, COALESCE(c.group_name, c.name) AS seller FROM competitor_products cp JOIN competitors c ON c.id=cp.competitor_id WHERE cp.id IN ({','.join('?' * len(ids))})", ids)
            elif ev.get("comparables"):
                cids = [c["competitor_product_id"] for c in ev["comparables"]]
                comp_rows = db.rows(conn, f"SELECT cp.*, COALESCE(c.group_name, c.name) AS seller FROM competitor_products cp JOIN competitors c ON c.id=cp.competitor_id WHERE cp.id IN ({','.join('?' * len(cids))})", cids)
            elif s["competitor_product_id"]:
                comp_rows = db.rows(conn, "SELECT cp.*, COALESCE(c.group_name, c.name) AS seller FROM competitor_products cp JOIN competitors c ON c.id=cp.competitor_id WHERE cp.id=?", (s["competitor_product_id"],))
            if s["m22_product_id"]:
                m22_rows = db.rows(conn, "SELECT * FROM m22_products WHERE id=?", (s["m22_product_id"],))
            elif comp_rows:
                kinds = {r["kind"] for r in comp_rows}
                m22_rows = db.rows(conn, f"SELECT * FROM m22_products WHERE is_active=1 AND in_scope=1 AND parent_url IS NULL AND price IS NOT NULL AND site='m22.ru' AND category_slug=? AND kind IN ({','.join('?' * len(kinds))}) ORDER BY price", [s["category_slug"], *kinds])
                # показываем до 4 ближайших по цене к минимальной цене конкурентов
                ref = min((r["price"] for r in comp_rows if r["price"]), default=None)
                if ref and len(m22_rows) > 4:
                    m22_rows = sorted(m22_rows, key=lambda r: abs(r["price"] - ref))[:4]
                    m22_rows.sort(key=lambda r: r["price"])
            title = s["title"]
        elif product:
            p = db.row(conn, "SELECT * FROM m22_products WHERE id=?", (product,))
            if not p:
                raise HTTPException(404)
            m22_rows = [p]
            comp_rows = db.rows(conn, """SELECT cp.*, COALESCE(c.group_name, c.name) AS seller FROM product_matches pm JOIN competitor_products cp ON cp.id=pm.competitor_product_id
                                        JOIN competitors c ON c.id=cp.competitor_id WHERE pm.m22_product_id=? AND pm.review_status!='rejected' AND pm.confidence>=0.55 AND cp.is_active=1 ORDER BY pm.confidence DESC, cp.price""", (product,))
            title = f"«{p['name']}» и сопоставимые предложения конкурентов"
        else:
            cids = [int(x) for x in (cp or "").split(",") if x.strip().isdigit()]
            mids = [int(x) for x in (m22 or "").split(",") if x.strip().isdigit()]
            if cids:
                comp_rows = db.rows(conn, f"SELECT cp.*, COALESCE(c.group_name, c.name) AS seller FROM competitor_products cp JOIN competitors c ON c.id=cp.competitor_id WHERE cp.id IN ({','.join('?' * len(cids))})", cids)
            if mids:
                m22_rows = db.rows(conn, f"SELECT * FROM m22_products WHERE id IN ({','.join('?' * len(mids))})", mids)
        cols = _norm_rows(m22_rows, "m22") + _norm_rows(comp_rows, "competitor")
        category_in_m22 = bool(m22_rows)
        just = specmod.justify([c for c in cols if c["side"] == "competitor"], [c for c in cols if c["side"] == "m22"], category_in_m22)
        # лучшие значения по строкам для подсветки
        best = {}
        for field, _ in specmod.FIELDS:
            vals = [(c["norm"].get(field), i) for i, c in enumerate(cols) if c["norm"].get(field) is not None and isinstance(c["norm"].get(field), (int, float)) and not isinstance(c["norm"].get(field), bool)]
            if vals:
                best[field] = (min if field in specmod.BETTER_LOW else max)(vals)[0]
        raw_keys = []
        for c in cols:
            for k in c["specs"]:
                if k not in raw_keys:
                    raw_keys.append(k)
    return render(request, "compare.html", title=title, cols=cols, fields=specmod.FIELDS, best=best, just=just, raw_keys=raw_keys[:40], signal_id=signal)


# ---------------- Матрица M22 ----------------
@app.get("/matrix", response_class=HTMLResponse)
def matrix(request: Request):
    f = _filters(request)
    where, params = ["p.is_active=1", "p.parent_url IS NULL"], []
    if f["category"]:
        where.append("p.category_slug=?")
        params.append(f["category"])
    if f["q"]:
        where.append("(p.name LIKE ? OR p.sku LIKE ? OR p.model_key LIKE ?)")
        params += [f"%{f['q']}%"] * 3
    site = request.query_params.get("site", "")
    if site:
        where.append("p.site=?")
        params.append(site)
    scope = request.query_params.get("scope", "1")
    if scope == "1":
        where.append("p.in_scope=1")
    with db.session() as conn:
        prods = db.rows(conn, f"""SELECT p.*, (SELECT COUNT(*) FROM product_matches pm WHERE pm.m22_product_id=p.id AND pm.review_status!='rejected') AS matches,
                                  (SELECT COUNT(*) FROM signals s WHERE s.m22_product_id=p.id AND s.status!='rejected') AS signals,
                                  (SELECT COUNT(*) FROM m22_price_history h WHERE h.product_id=p.id) AS obs
                                  FROM m22_products p WHERE {' AND '.join(where)} ORDER BY p.category_slug, p.model_key, p.capacity, p.site""", params)
        lists = _lists(conn)
        cat_stats = db.rows(conn, "SELECT category_slug, COUNT(*) n, MIN(price) pmin, MAX(price) pmax FROM m22_products WHERE is_active=1 AND in_scope=1 GROUP BY 1")
    return render(request, "matrix.html", prods=prods, f=f, site=site, scope=scope, cat_stats=cat_stats, **lists)


@app.get("/matrix/{pid}", response_class=HTMLResponse)
def product_detail(request: Request, pid: int):
    with db.session() as conn:
        p = db.row(conn, "SELECT * FROM m22_products WHERE id=?", (pid,))
        if not p:
            raise HTTPException(404)
        hist = db.rows(conn, "SELECT * FROM m22_price_history WHERE product_id=? ORDER BY observed_at", (pid,))
        comps = matching.comparables_for(conn, pid, min_conf=0.0)
        all_matches = db.rows(conn, """SELECT pm.*, cp.name AS cp_name, cp.price AS cp_price, cp.url AS cp_url, cp.capacity AS cp_capacity, c.name AS competitor_name FROM product_matches pm
                                       JOIN competitor_products cp ON cp.id=pm.competitor_product_id JOIN competitors c ON c.id=cp.competitor_id WHERE pm.m22_product_id=? ORDER BY pm.confidence DESC""", (pid,))
        twin = db.rows(conn, "SELECT * FROM m22_products WHERE id!=? AND ((sku IS NOT NULL AND sku!='' AND sku=?) OR (model_key=? AND capacity IS ? AND kind=?)) ORDER BY site", (pid, p["sku"], p["model_key"], p["capacity"], p["kind"]))
        variants = db.rows(conn, "SELECT * FROM m22_products WHERE parent_url=? ORDER BY price", (p["url"],))
        sigs = db.rows(conn, SIGNAL_SQL + " WHERE s.m22_product_id=? ORDER BY s.created_at DESC", (pid,))
        prices = [m["cp_price"] for m in all_matches if m["cp_price"] and m["confidence"] >= 0.6 and m["match_type"] in ("exact_model", "direct_analog", "kit")]
        median = sorted(prices)[len(prices) // 2] if prices else None
    return render(request, "product_detail.html", p=p, hist=hist, matches=all_matches, twin=twin, variants=variants, sigs=sigs, median=median, n_comp=len(prices),
                  specs=db.uj(p["specs_json"], {}) or {}, kit=db.uj(p["kit_json"], {}) or {}, images=db.uj(p["images_json"], []) or [])


# ---------------- Поисковый спрос ----------------
@app.get("/demand", response_class=HTMLResponse)
def demand(request: Request):
    f = _filters(request)
    where, params = ["q.is_active=1"], []
    if f["category"]:
        where.append("q.category_slug=?")
        params.append(f["category"])
    if f["type"]:
        where.append("q.intent=?")
        params.append(f["type"])
    if f["q"]:
        where.append("q.query LIKE ?")
        params.append(f"%{f['q']}%")
    with db.session() as conn:
        qs = db.rows(conn, f"""SELECT q.*, (SELECT COUNT(*) FROM demand_observations d WHERE d.query_id=q.id AND d.source='google_trends') AS trends_n,
                               (SELECT COUNT(*) FROM demand_observations d WHERE d.query_id=q.id AND d.source IN ('wordstat','wordstat_import')) AS ws_n,
                               (SELECT value FROM demand_observations d WHERE d.query_id=q.id AND d.source IN ('wordstat','wordstat_import') ORDER BY period_start DESC LIMIT 1) AS ws_last,
                               (SELECT AVG(value) FROM (SELECT value FROM demand_observations d WHERE d.query_id=q.id AND d.source='google_trends' ORDER BY period_start DESC LIMIT 4)) AS tr_last4,
                               (SELECT AVG(value) FROM (SELECT value FROM demand_observations d WHERE d.query_id=q.id AND d.source='google_trends' ORDER BY period_start DESC LIMIT 4 OFFSET 4)) AS tr_prev4
                               FROM search_queries q WHERE {' AND '.join(where)} ORDER BY q.is_seed DESC, trends_n DESC, ws_n DESC, q.category_slug, q.query LIMIT 1500""", params)
        srcs = db.rows(conn, "SELECT * FROM sources WHERE kind IN ('demand','import') ORDER BY key")
        by_cat = db.rows(conn, "SELECT category_slug, COUNT(*) n, SUM(is_brand) brands FROM search_queries WHERE is_active=1 GROUP BY 1 ORDER BY n DESC")
        intents = db.rows(conn, "SELECT intent, COUNT(*) n FROM search_queries WHERE is_active=1 GROUP BY 1 ORDER BY n DESC")
        imports_ = db.rows(conn, "SELECT * FROM imports ORDER BY id DESC LIMIT 10")
        lists = _lists(conn)
        # серии для seed-запросов (для графика)
        series = {}
        for q in [x for x in qs if x["trends_n"]][:12]:
            series[q["query"]] = [(r["period_start"], r["value"]) for r in db.rows(conn, "SELECT period_start, value FROM demand_observations WHERE query_id=? AND source='google_trends' ORDER BY period_start", (q["id"],))]
    return render(request, "demand.html", qs=qs, f=f, srcs=srcs, by_cat=by_cat, intents=intents, imports=imports_, series=series, **lists)


@app.post("/demand/add")
def demand_add(query: str = Form(...), category_slug: str = Form(""), intent: str = Form("commercial"), is_seed: str = Form("0")):
    text = query.strip().lower()
    with db.session() as conn:
        if text and not db.row(conn, "SELECT id FROM search_queries WHERE query=?", (text,)):
            conn.execute("INSERT INTO search_queries(query, category_slug, intent, is_seed, added_by) VALUES(?,?,?,?,'manual')", (text, category_slug or classify_category(text), intent, 1 if is_seed == "1" else 0))
    return RedirectResponse("/demand", status_code=303)


@app.post("/import")
async def import_file(kind: str = Form(...), file: UploadFile = File(...), period: str = Form("")):
    content = await file.read()
    with db.session() as conn:
        try:
            if kind == "wordstat":
                res = importers.import_wordstat_table(conn, file.filename, content, period or None)
                back = "/demand"
            elif kind == "trends":
                res = importers.import_trends_csv(conn, file.filename, content)
                back = "/demand"
            elif kind == "queries":
                res = importers.import_queries_table(conn, file.filename, content)
                back = "/demand"
            elif kind == "competitors":
                res = importers.import_competitors_table(conn, file.filename, content)
                back = "/competitors"
            elif kind == "competitor_prices":
                res = importers.import_competitor_prices_table(conn, file.filename, content)
                back = "/competitors"
            else:
                raise HTTPException(400, "Неизвестный тип импорта")
            db.set_setting(conn, "last_import_result", json.dumps({"kind": kind, "file": file.filename, "result": res}, ensure_ascii=False))
        except Exception as exc:  # noqa: BLE001
            conn.execute("INSERT INTO imports(kind, filename, rows, status, message) VALUES(?,?,0,'error',?)", (kind, file.filename, str(exc)[:500]))
            db.set_setting(conn, "last_import_result", json.dumps({"kind": kind, "file": file.filename, "error": str(exc)[:300]}, ensure_ascii=False))
            back = "/sources"
    return RedirectResponse(back, status_code=303)


# ---------------- Новые возможности ----------------
@app.get("/opportunities", response_class=HTMLResponse)
def opportunities(request: Request):
    f = _filters(request)
    where, params = ["1=1"], []
    if f["status"]:
        where.append("decision_status=?")
        params.append(f["status"])
    if f["category"]:
        where.append("category_slug=?")
        params.append(f["category"])
    with db.session() as conn:
        hyps = db.rows(conn, f"SELECT * FROM hypotheses WHERE {' AND '.join(where)} ORDER BY decision_status='new' DESC, created_at DESC", params)
        obs = db.rows(conn, "SELECT * FROM market_observations ORDER BY kind, observed_date DESC")
        lists = _lists(conn)
    return render(request, "opportunities.html", hyps=hyps, f=f, obs=obs, **lists)


@app.post("/opportunities/add")
def hypothesis_add(title: str = Form(...), description: str = Form(""), category_slug: str = Form(""), next_step: str = Form(""), owner: str = Form("")):
    with db.session() as conn:
        conn.execute("INSERT INTO hypotheses(title, description, discovered_via, category_slug, next_step, owner, dedupe_key) VALUES(?,?,'вручную',?,?,?,?)",
                     (title.strip(), description, category_slug or None, next_step, owner, f"hyp:manual:{title.strip().lower()[:80]}"))
        hid = db.row(conn, "SELECT last_insert_rowid() AS id")["id"]
    return RedirectResponse(f"/opportunities/{hid}", status_code=303)


@app.get("/opportunities/{hid}", response_class=HTMLResponse)
def hypothesis_detail(request: Request, hid: int):
    with db.session() as conn:
        h = db.row(conn, "SELECT * FROM hypotheses WHERE id=?", (hid,))
        if not h:
            raise HTTPException(404)
        sig_ids = db.uj(h["signals_json"], []) or []
        sigs = db.rows(conn, SIGNAL_SQL + f" WHERE s.id IN ({','.join('?' * len(sig_ids))})", sig_ids) if sig_ids else []
        comments = db.rows(conn, "SELECT * FROM comments WHERE entity_type='hypothesis' AND entity_id=? ORDER BY created_at DESC", (hid,))
    return render(request, "hypothesis_detail.html", h=h, sigs=sigs, comments=comments, comps=db.uj(h["competitors_json"], []) or [])


@app.post("/opportunities/{hid}/update")
def hypothesis_update(hid: int, decision_status: str = Form(None), owner: str = Form(None), comment: str = Form(None), author: str = Form(""), next_step: str = Form(None), missing_data: str = Form(None)):
    with db.session() as conn:
        if decision_status:
            conn.execute("UPDATE hypotheses SET decision_status=?, updated_at=datetime('now') WHERE id=?", (decision_status, hid))
        if owner:
            conn.execute("UPDATE hypotheses SET owner=?, updated_at=datetime('now') WHERE id=?", (owner, hid))
        if next_step is not None and next_step.strip():
            conn.execute("UPDATE hypotheses SET next_step=?, updated_at=datetime('now') WHERE id=?", (next_step.strip(), hid))
        if missing_data is not None and missing_data.strip():
            conn.execute("UPDATE hypotheses SET missing_data=?, updated_at=datetime('now') WHERE id=?", (missing_data.strip(), hid))
        if comment and comment.strip():
            conn.execute("INSERT INTO comments(entity_type, entity_id, author, text) VALUES('hypothesis',?,?,?)", (hid, author or "пользователь", comment.strip()))
    return RedirectResponse(f"/opportunities/{hid}", status_code=303)


# ---------------- Источники ----------------
@app.get("/sources", response_class=HTMLResponse)
def sources_page(request: Request):
    with db.session() as conn:
        srcs = db.rows(conn, "SELECT * FROM sources ORDER BY CASE status WHEN 'ok' THEN 0 WHEN 'error' THEN 1 ELSE 2 END, kind, name")
        runs = db.rows(conn, "SELECT * FROM source_runs ORDER BY id DESC LIMIT 40")
        errors = db.rows(conn, "SELECT * FROM error_log ORDER BY id DESC LIMIT 60")
        pages = db.rows(conn, "SELECT mp.*, c.name AS cname FROM monitored_pages mp JOIN competitors c ON c.id=mp.competitor_id WHERE mp.last_status NOT IN ('ok') OR mp.last_status IS NULL ORDER BY mp.fail_count DESC, mp.id LIMIT 60")
        pages_stats = db.row(conn, "SELECT COUNT(*) total, SUM(last_status='ok') ok, SUM(fail_count>0) failing, SUM(is_active=0) disabled FROM monitored_pages")
        last_import = db.get_setting(conn, "last_import_result")
    return render(request, "sources.html", srcs=srcs, runs=runs, errors=errors, pages=pages, pages_stats=pages_stats, jobs=scheduler.jobs_info(), last_import=last_import,
                  has_wordstat_token=bool(config.YANDEX_WORDSTAT_TOKEN), has_ai_key=bool(config.ANTHROPIC_API_KEY))


@app.post("/sources/run/{name}")
def sources_run(name: str):
    if not scheduler.run_now(name):
        raise HTTPException(400, "Неизвестная задача")
    return RedirectResponse("/sources", status_code=303)


@app.post("/sources/{key}/toggle")
def source_toggle(key: str):
    with db.session() as conn:
        s = db.row(conn, "SELECT status FROM sources WHERE key=?", (key,))
        conn.execute("UPDATE sources SET status=? WHERE key=?", ("unknown" if s and s["status"] == "disabled" else "disabled", key))
    return RedirectResponse("/sources", status_code=303)


# ---------------- Отчёты ----------------
@app.get("/reports", response_class=HTMLResponse)
def reports_page(request: Request):
    with db.session() as conn:
        reps = db.rows(conn, "SELECT id, kind, period_start, period_end, generated_at FROM reports ORDER BY id DESC")
    return render(request, "reports.html", reps=reps)


@app.post("/reports/generate")
def reports_generate(days: int = Form(7)):
    with db.session() as conn:
        rep = reports.build_weekly(conn, days=days)
        rid = reports.save(conn, rep)
    return RedirectResponse(f"/reports/{rid}", status_code=303)


@app.get("/reports/{rid}", response_class=HTMLResponse)
def report_detail(request: Request, rid: int):
    with db.session() as conn:
        r = db.row(conn, "SELECT * FROM reports WHERE id=?", (rid,))
        if not r:
            raise HTTPException(404)
    return render(request, "report_detail.html", r=r, rep=db.uj(r["content_json"], {}), sections=reports.SECTIONS)


@app.get("/reports/{rid}/export.{fmt}")
def report_export(rid: int, fmt: str):
    with db.session() as conn:
        r = db.row(conn, "SELECT * FROM reports WHERE id=?", (rid,))
        if not r:
            raise HTTPException(404)
    rep = db.uj(r["content_json"], {})
    name = f"m22-radar-report-{rep['period_start']}_{rep['period_end']}"
    if fmt == "pdf":
        return Response(reports.to_pdf(rep), media_type="application/pdf", headers={"Content-Disposition": f'attachment; filename="{name}.pdf"'})
    if fmt == "xlsx":
        return Response(reports.to_xlsx(rep), media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", headers={"Content-Disposition": f'attachment; filename="{name}.xlsx"'})
    if fmt == "csv":
        return Response(reports.to_csv(rep), media_type="text/csv; charset=utf-8", headers={"Content-Disposition": f'attachment; filename="{name}.csv"'})
    if fmt == "json":
        return Response(json.dumps(rep, ensure_ascii=False, indent=1), media_type="application/json")
    raise HTTPException(404)


# ---------------- Экспорт данных ----------------
def _csv_response(rows: list[dict], name: str) -> Response:
    buf = io.StringIO()
    if rows:
        w = csv.DictWriter(buf, fieldnames=list(rows[0].keys()), delimiter=";")
        w.writeheader()
        for r in rows:
            w.writerow(r)
    return Response(("﻿" + buf.getvalue()).encode("utf-8"), media_type="text/csv; charset=utf-8", headers={"Content-Disposition": f'attachment; filename="{name}.csv"'})


def _xlsx_response(rows: list[dict], name: str) -> Response:
    import openpyxl

    wb = openpyxl.Workbook()
    ws = wb.active
    if rows:
        ws.append(list(rows[0].keys()))
        for r in rows:
            ws.append([("" if v is None else (json.dumps(v, ensure_ascii=False) if isinstance(v, (dict, list)) else v)) for v in r.values()])
    out = io.BytesIO()
    wb.save(out)
    return Response(out.getvalue(), media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", headers={"Content-Disposition": f'attachment; filename="{name}.xlsx"'})


@app.get("/export/backup.sqlite3")
def export_backup():
    with db.session() as conn:
        path = db.backup(conn, "export")
    return Response(path.read_bytes(), media_type="application/octet-stream", headers={"Content-Disposition": f'attachment; filename="{path.name}"'})



EXPORTS = {
    "signals": SIGNAL_SQL + " ORDER BY s.created_at DESC",
    "recommendations": "SELECT r.*, m.name AS m22_name FROM recommendations r LEFT JOIN m22_products m ON m.id=r.m22_product_id ORDER BY r.priority",
    "matrix": "SELECT * FROM m22_products WHERE is_active=1 ORDER BY site, category_slug, name",
    "competitors": "SELECT * FROM competitors ORDER BY name",
    "competitor_products": "SELECT cp.*, c.name AS competitor_name FROM competitor_products cp JOIN competitors c ON c.id=cp.competitor_id ORDER BY c.name, cp.name",
    "matches": "SELECT pm.*, cp.name AS competitor_product, c.name AS competitor_name, m.name AS m22_name FROM product_matches pm JOIN competitor_products cp ON cp.id=pm.competitor_product_id JOIN competitors c ON c.id=cp.competitor_id LEFT JOIN m22_products m ON m.id=pm.m22_product_id",
    "queries": "SELECT * FROM search_queries ORDER BY category_slug, query",
    "demand": "SELECT q.query, q.category_slug, d.source, d.period_start, d.period_end, d.value, d.unit, d.geo, d.fetched_at FROM demand_observations d JOIN search_queries q ON q.id=d.query_id ORDER BY q.query, d.period_start",
    "hypotheses": "SELECT * FROM hypotheses ORDER BY created_at DESC",
    "price_history": "SELECT cp.name, c.name AS competitor_name, h.price, h.availability, h.observed_at FROM competitor_price_history h JOIN competitor_products cp ON cp.id=h.competitor_product_id JOIN competitors c ON c.id=cp.competitor_id ORDER BY h.observed_at",
}


@app.get("/export/{what}.{fmt}")
def export(what: str, fmt: str):
    if what == "backup":
        return export_backup()
    if what not in EXPORTS:
        raise HTTPException(404)
    with db.session() as conn:
        rows = db.rows(conn, EXPORTS[what])
    return _xlsx_response(rows, f"m22-radar-{what}") if fmt == "xlsx" else _csv_response(rows, f"m22-radar-{what}")


# ---------------- Настройки ----------------
@app.get("/settings", response_class=HTMLResponse)
def settings_page(request: Request):
    with db.session() as conn:
        settings = {r["key"]: r["value"] for r in db.rows(conn, "SELECT key, value FROM settings")}
        cats = db.rows(conn, "SELECT * FROM categories ORDER BY sort_order")
        counts = {"backups": len(list(config.BACKUP_DIR.glob("*.sqlite3")))}
    thresholds = {"Порог изменения цены конкурента, %": config.PRICE_CHANGE_THRESHOLD_PCT, "Порог отклонения от медианы рынка, %": config.MARKET_GAP_THRESHOLD_PCT,
                  "Минимум сопоставимых предложений": config.MIN_COMPARABLES, "Порог изменения спроса, %": config.DEMAND_CHANGE_THRESHOLD_PCT, "Z-порог аномалии": config.ANOMALY_Z,
                  "Задержка между запросами к домену, с": config.REQUEST_DELAY_SEC}
    return render(request, "settings.html", settings=settings, cats=cats, thresholds=thresholds, counts=counts, db_path=str(config.DB_PATH), backup_dir=str(config.BACKUP_DIR),
                  schedule={"M22 (час)": config.M22_CRON_HOUR, "Конкуренты (час)": config.COMPETITORS_CRON_HOUR, "Спрос (день недели)": config.TRENDS_CRON_DOW, "Отчёт (день недели)": config.REPORT_CRON_DOW, "Включён": config.SCHEDULE_ENABLED})


@app.post("/settings/update")
def settings_update(owners: str = Form(None), owner_default: str = Form(None)):
    with db.session() as conn:
        if owners is not None:
            db.set_setting(conn, "owners", ";".join(o.strip() for o in owners.split(";") if o.strip()))
        if owner_default:
            db.set_setting(conn, "owner_default", owner_default.strip())
    return RedirectResponse("/settings", status_code=303)


@app.post("/settings/category/{slug}/toggle")
def category_toggle(slug: str):
    with db.session() as conn:
        conn.execute("UPDATE categories SET in_scope = 1 - in_scope WHERE slug=?", (slug,))
    return RedirectResponse("/settings", status_code=303)


@app.post("/analyze")
def analyze_now():
    with db.session() as conn:
        matching.run_matching(conn)
        signals.run_all(conn)
        recommendations.generate(conn)
        discovery.generate(conn)
        db.set_setting(conn, "last_analysis_at", db.now_iso())
    return RedirectResponse("/", status_code=303)


@app.get("/health")
def health():
    with db.session() as conn:
        n = db.row(conn, "SELECT COUNT(*) n FROM m22_products")["n"]
    return {"status": "ok", "m22_products": n, "version": "0.1.0"}
