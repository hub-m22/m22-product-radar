"""Исходные данные (сиды): выгрузка в data/seed/*.json и загрузка на новой машине.

Что считается исходными данными (введено руками или отобрано вручную и не восстанавливается сбором):
- competitors — реестр конкурентов с уровнями, группами, юрлицом и финансами;
- monitored_pages — страницы мониторинга (адрес, вид, категория, парсер и его настройки, включена/выключена);
- search_queries — поисковые запросы и seed-термины (кроме подсказок Яндекса — они собираются заново);
- settings — ответственные, профиль M22, таблица идентичных моделей (кроме служебных отметок времени);
- site_categories — категории сайтов конкурентов с сопоставлением к нашим категориям;
- sources — реестр источников (описания и статус, без отметок последних запусков);
- price_decisions — решения по ценовым вердиктам (привязка к товару M22 по адресу страницы).

Производные таблицы (товары конкурентов, история цен, сопоставления, сигналы, действия, гипотезы) в сиды не входят:
они восстанавливаются командами collect и analyze.

Связи хранятся по естественным ключам (сайт конкурента, адрес страницы, текст запроса, ключ настройки), а не по id,
поэтому загрузка идемпотентна: повторный запуск обновляет строки, а не дублирует их.
"""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from . import config, db

SEED_DIR = config.BASE_DIR / "data" / "seed"

COMPETITOR_SKIP = {"id"}
PAGE_FIELDS = ["url", "kind", "name", "category_slug", "parser", "parser_config_json", "is_active", "created_at"]
QUERY_SKIP = {"id"}
SETTINGS_RUNTIME = {"last_analysis_at", "last_import_result"}
SOURCE_SKIP = {"id", "last_run_at", "last_success_at", "last_error", "consecutive_failures", "created_at"}
SITECAT_SKIP = {"id", "competitor_id"}
JSON_COLS_SUFFIX = "_json"


def _cols(conn: sqlite3.Connection, table: str) -> list[str]:
    return [r[1] for r in conn.execute(f"PRAGMA table_info({table})")]


def _row_out(row: sqlite3.Row | dict, skip: set[str]) -> dict:
    """Строка таблицы -> словарь для JSON; колонки *_json раскрываются в объекты."""
    out = {}
    for k in row.keys():
        if k in skip:
            continue
        v = row[k]
        if k.endswith(JSON_COLS_SUFFIX) and isinstance(v, str) and v.strip():
            try:
                v = json.loads(v)
            except ValueError:
                pass
        out[k] = v
    return out


def _row_in(d: dict) -> dict:
    """Словарь из JSON -> значения для базы; объекты в колонках *_json сворачиваются обратно в строку."""
    out = {}
    for k, v in d.items():
        if k.endswith(JSON_COLS_SUFFIX) and not isinstance(v, (str, type(None))):
            v = json.dumps(v, ensure_ascii=False)
        out[k] = v
    return out


def _write(path: Path, data) -> None:
    path.write_text(json.dumps(data, ensure_ascii=False, indent=1, sort_keys=False) + "\n", encoding="utf-8")


def _read(path: Path):
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else None


# ---------------- выгрузка ----------------
def dump(conn: sqlite3.Connection, out_dir: Path | str | None = None) -> dict:
    out = Path(out_dir) if out_dir else SEED_DIR
    out.mkdir(parents=True, exist_ok=True)
    counts: dict[str, int] = {}

    comps = db.rows(conn, "SELECT * FROM competitors ORDER BY id")
    site_of = {c["id"]: c["website"] for c in comps}
    _write(out / "competitors.json", [_row_out(c, COMPETITOR_SKIP) for c in comps])
    counts["competitors"] = len(comps)

    pages = db.rows(conn, "SELECT * FROM monitored_pages ORDER BY competitor_id, id")
    items = []
    for p in pages:
        d = {"competitor_website": site_of.get(p["competitor_id"])}
        d.update(_row_out({k: p[k] for k in PAGE_FIELDS}, set()))
        if not p["is_active"] and p["last_error"]:
            d["disabled_reason"] = p["last_error"]  # почему выключена (вручную или автоматически) — чтобы не включать заново вслепую
        items.append(d)
    _write(out / "monitored_pages.json", items)
    counts["monitored_pages"] = len(items)

    qs = db.rows(conn, "SELECT * FROM search_queries WHERE COALESCE(added_by,'') != 'yandex_suggest' ORDER BY id")
    _write(out / "search_queries.json", [_row_out(q, QUERY_SKIP) for q in qs])
    counts["search_queries"] = len(qs)

    st = [s for s in db.rows(conn, "SELECT key, value FROM settings ORDER BY key") if s["key"] not in SETTINGS_RUNTIME]
    settings = {}
    for s in st:
        v = s["value"]
        try:
            v = json.loads(v) if v and v.strip()[:1] in "{[" else v
        except ValueError:
            pass
        settings[s["key"]] = v
    _write(out / "settings.json", settings)
    counts["settings"] = len(settings)

    cats = db.rows(conn, "SELECT * FROM site_categories ORDER BY id")
    sc = []
    for c in cats:
        d = {"competitor_website": site_of.get(c["competitor_id"])}
        d.update(_row_out(c, SITECAT_SKIP))
        sc.append(d)
    _write(out / "site_categories.json", sc)
    counts["site_categories"] = len(sc)

    srcs = db.rows(conn, "SELECT * FROM sources ORDER BY id")
    _write(out / "sources.json", [_row_out(s, SOURCE_SKIP) for s in srcs])
    counts["sources"] = len(srcs)

    dec = db.rows(conn, """SELECT d.*, m.site AS m22_site, m.url AS m22_url, m.name AS m22_name FROM price_decisions d
                           JOIN m22_products m ON m.id=d.m22_product_id ORDER BY d.id""")
    _write(out / "price_decisions.json", [_row_out(d, {"id", "m22_product_id"}) for d in dec])
    counts["price_decisions"] = len(dec)

    from . import get_version
    _write(out / "meta.json", {"dumped_at": db.now_iso(), "radar_version": get_version(), "counts": counts,
                               "load": "python -m radar seed load", "note": "исходные данные; производные таблицы восстанавливаются collect + analyze"})
    return counts


# ---------------- загрузка ----------------
def _upsert(conn: sqlite3.Connection, table: str, key_cols: list[str], fields: dict) -> tuple[int, bool]:
    """Вставка или обновление по естественному ключу. Возвращает (id, создано?)."""
    where = " AND ".join(f"{k}=?" for k in key_cols)
    existing = db.row(conn, f"SELECT id FROM {table} WHERE {where}", tuple(fields[k] for k in key_cols))
    if existing:
        upd = {k: v for k, v in fields.items() if k not in key_cols}
        if upd:
            conn.execute(f"UPDATE {table} SET {', '.join(f'{k}=?' for k in upd)} WHERE id=?", (*upd.values(), existing["id"]))
        return existing["id"], False
    conn.execute(f"INSERT INTO {table}({', '.join(fields)}) VALUES({', '.join('?' for _ in fields)})", tuple(fields.values()))
    return db.row(conn, f"SELECT id FROM {table} WHERE {where}", tuple(fields[k] for k in key_cols))["id"], True


def load(conn: sqlite3.Connection, in_dir: Path | str | None = None) -> dict:
    src = Path(in_dir) if in_dir else SEED_DIR
    if not src.exists():
        raise FileNotFoundError(f"папка сидов не найдена: {src}")
    res: dict[str, dict] = {}

    # конкуренты — по сайту
    known = set(_cols(conn, "competitors"))
    created = updated = 0
    id_by_site: dict[str, int] = {}
    for c in _read(src / "competitors.json") or []:
        fields = {k: v for k, v in _row_in(c).items() if k in known}
        if not fields.get("website"):
            continue
        cid, new = _upsert(conn, "competitors", ["website"], fields)
        id_by_site[fields["website"]] = cid
        created += new
        updated += not new
    res["competitors"] = {"created": created, "updated": updated}

    # страницы мониторинга — по адресу
    known = set(_cols(conn, "monitored_pages"))
    created = updated = skipped = 0
    for p in _read(src / "monitored_pages.json") or []:
        cid = id_by_site.get(p.get("competitor_website"))
        if cid is None:
            skipped += 1
            continue
        fields = {k: v for k, v in _row_in(p).items() if k in known and k != "competitor_id"}
        fields["competitor_id"] = cid
        if not p.get("is_active") and p.get("disabled_reason"):
            fields["last_error"] = p["disabled_reason"]
        _, new = _upsert(conn, "monitored_pages", ["url"], fields)
        created += new
        updated += not new
    res["monitored_pages"] = {"created": created, "updated": updated, "skipped_no_competitor": skipped}

    # поисковые запросы — по тексту
    known = set(_cols(conn, "search_queries"))
    created = updated = 0
    for q in _read(src / "search_queries.json") or []:
        fields = {k: v for k, v in _row_in(q).items() if k in known}
        if not fields.get("query"):
            continue
        _, new = _upsert(conn, "search_queries", ["query"], fields)
        created += new
        updated += not new
    res["search_queries"] = {"created": created, "updated": updated}

    # настройки — по ключу
    n = 0
    for k, v in (_read(src / "settings.json") or {}).items():
        if k in SETTINGS_RUNTIME:
            continue
        db.set_setting(conn, k, v if isinstance(v, str) else json.dumps(v, ensure_ascii=False))
        n += 1
    res["settings"] = {"set": n}

    # категории сайтов — по сайту конкурента, адресу и названию
    known = set(_cols(conn, "site_categories"))
    created = updated = 0
    for c in _read(src / "site_categories.json") or []:
        fields = {k: v for k, v in _row_in(c).items() if k in known and k != "competitor_id"}
        fields["competitor_id"] = id_by_site.get(c.get("competitor_website"))
        keys = ["site", "name"] + (["url"] if fields.get("url") else []) + (["parent"] if fields.get("parent") else [])
        _, new = _upsert(conn, "site_categories", keys, fields)
        created += new
        updated += not new
    res["site_categories"] = {"created": created, "updated": updated}

    # источники — по ключу (описания и статус; отметки запусков остаются как есть)
    known = set(_cols(conn, "sources"))
    created = updated = 0
    for s in _read(src / "sources.json") or []:
        fields = {k: v for k, v in _row_in(s).items() if k in known}
        if not fields.get("key"):
            continue
        _, new = _upsert(conn, "sources", ["key"], fields)
        created += new
        updated += not new
    res["sources"] = {"created": created, "updated": updated}

    # решения по ценам — по адресу товара M22 (товары появляются после сбора; без них решение пропускается)
    known = set(_cols(conn, "price_decisions"))
    created = updated = skipped = 0
    for d in _read(src / "price_decisions.json") or []:
        m = db.row(conn, "SELECT id FROM m22_products WHERE url=?", (d.get("m22_url"),))
        if not m:
            skipped += 1
            continue
        fields = {k: v for k, v in _row_in(d).items() if k in known}
        fields["m22_product_id"] = m["id"]
        _, new = _upsert(conn, "price_decisions", ["m22_product_id"], fields)
        created += new
        updated += not new
    res["price_decisions"] = {"created": created, "updated": updated, "skipped_no_product": skipped}

    conn.commit()
    return res
