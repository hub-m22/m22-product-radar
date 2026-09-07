"""Полный пересбор товаров конкурентов: сбор всех страниц, дозагрузка характеристик, пересчёт нормализации.
Запуск: python scripts/rebuild_competitors.py A   (или B, или список id через запятую)"""
import sys, time, json, logging
sys.path.insert(0, ".")
from radar import db, specs_extract, logging_setup
from radar.collectors import competitor_generic
from radar.collectors.common import renormalize

logging_setup.setup_logging("INFO")
log = logging.getLogger("rebuild")
arg = sys.argv[1] if len(sys.argv) > 1 else "A"
conn = db.connect()
if arg in ("A", "B", "C"):
    ids = [r["id"] for r in db.rows(conn, "SELECT id FROM competitors WHERE is_active=1 AND tier=? AND website NOT LIKE 'tender:%' ORDER BY id", (arg,))]
else:
    ids = [int(x) for x in arg.split(",")]
summary = {}
for cid in ids:
    name = db.row(conn, "SELECT name FROM competitors WHERE id=?", (cid,))["name"]
    t0 = time.time()
    try:
        r = competitor_generic.run(conn, competitor_id=cid)
    except Exception as exc:
        r = {"error": str(exc)}
    try:
        e = specs_extract.enrich_competitor(conn, cid, only_missing=True)
    except Exception as exc:
        e = {"error": str(exc)}
    summary[cid] = {"name": name, "collect": r, "enrich": e, "sec": int(time.time() - t0)}
    print(json.dumps({cid: summary[cid]}, ensure_ascii=False), flush=True)
if arg in ("A", "B", "C"):
    print("renormalize", renormalize(conn), flush=True)
print("DONE", json.dumps(summary, ensure_ascii=False), flush=True)
