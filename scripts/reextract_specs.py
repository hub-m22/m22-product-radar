"""Повторное извлечение характеристик из сохранённых страниц (data/raw) без обращения к сайтам.

Нужно, когда уточнены правила извлечения (specs_extract / extract_specs): берём последнюю сохранённую копию страницы товара
и заново снимаем параметры. Найденное сливается с уже сохранённым specs_json (новое поверх старого).
Запуск: python scripts/reextract_specs.py A          — все конкуренты уровня A
        python scripts/reextract_specs.py 7,10       — по id
"""
import hashlib
import sys
from pathlib import Path

sys.path.insert(0, ".")
from bs4 import BeautifulSoup

from radar import config, db, specs_extract
from radar.collectors.competitor_generic import extract_specs

arg = sys.argv[1] if len(sys.argv) > 1 else "A"
conn = db.connect()
if arg in ("A", "B", "C"):
    ids = [r["id"] for r in db.rows(conn, "SELECT id FROM competitors WHERE is_active=1 AND tier=? AND website NOT LIKE 'tender:%' ORDER BY id", (arg,))]
else:
    ids = [int(x) for x in arg.split(",")]


def raw_file(cid: int, url: str) -> Path | None:
    name = hashlib.sha1(url.encode("utf-8")).hexdigest()[:16] + ".html"
    folder = config.RAW_DIR / f"competitor_{cid}"
    if not folder.exists():
        return None
    for day in sorted(folder.iterdir(), reverse=True):
        p = day / name
        if p.exists():
            return p
    return None


for cid in ids:
    # только позиции со своей страницей товара: у позиций с адресом каталога/раздела страница содержит чужие характеристики
    rows = db.rows(conn, """SELECT id, url, specs_json FROM competitor_products WHERE competitor_id=? AND is_active=1
                            AND url NOT IN (SELECT url FROM monitored_pages WHERE kind!='product')""", (cid,))
    found = updated = 0
    cache: dict[str, dict] = {}
    for r in rows:
        url = r["url"].split("#")[0]
        if url not in cache:
            p = raw_file(cid, url)
            if not p:
                cache[url] = {}
                continue
            html = p.read_text(encoding="utf-8", errors="replace")
            specs = extract_specs(BeautifulSoup(html, "lxml"))
            for k, v in specs_extract.extract_specs(html).items():
                specs.setdefault(k, v)
            cache[url] = specs
        specs = cache[url]
        if not specs:
            continue
        found += 1
        old = db.uj(r["specs_json"], {}) or {}
        merged = {**old, **specs}
        if merged != old:
            conn.execute("UPDATE competitor_products SET specs_json=? WHERE id=?", (db.j(merged), r["id"]))
            updated += 1
    conn.commit()
    print(f"конкурент {cid}: товаров {len(rows)}, страницы с параметрами {found}, обновлено {updated}", flush=True)
