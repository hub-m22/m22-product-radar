"""Командная строка: python -m radar <команда>

  init            — создать базу и применить миграции, заполнить справочники
  collect [src]   — сбор данных: all | m22 | radiosync | competitors | trends | suggest
  analyze         — сопоставление, сигналы, рекомендации, гипотезы
  report          — сформировать еженедельный отчёт
  serve           — запустить веб-интерфейс (с планировщиком)
  backup          — резервная копия базы
  import-competitors <json> | import-semantic <json>
"""
from __future__ import annotations

import argparse
import logging
import sys

from . import config, db, discovery, matching, recommendations, seed, signals
from .logging_setup import setup_logging

log = logging.getLogger("radar.cli")


def cmd_init(_):
    db.init_db()
    with db.session() as conn:
        seed.seed(conn)
    print(f"База инициализирована: {config.DB_PATH}")


def cmd_collect(args):
    from .collectors import competitor_generic, m22, radiosync, trends, yandex_suggest

    what = args.source
    results = {}
    with db.session() as conn:
        if what in ("all", "m22"):
            results["m22.ru"] = m22.run(conn)
        if what in ("all", "radiosync"):
            results["radiosync.ru"] = radiosync.run(conn)
        if what in ("all", "competitors"):
            results["competitors"] = competitor_generic.run(conn)
        if what in ("all", "suggest"):
            results["yandex_suggest"] = yandex_suggest.run(conn)
        if what in ("all", "trends"):
            results["google_trends"] = trends.run(conn, max_groups=args.max_groups)
    print(results)


def cmd_analyze(_):
    with db.session() as conn:
        r1 = matching.run_matching(conn)
        r2 = signals.run_all(conn)
        r3 = recommendations.generate(conn)
        r4 = discovery.generate(conn)
        db.set_setting(conn, "last_analysis_at", db.now_iso())
    print({"matching": r1, "signals": r2, "recommendations": r3, "hypotheses": r4})


def cmd_report(args):
    from . import reports

    with db.session() as conn:
        rep = reports.build_weekly(conn, days=args.days)
        rid = reports.save(conn, rep)
    print(f"Отчёт #{rid} за {rep['period_start']} — {rep['period_end']} сформирован")


def cmd_serve(args):
    import uvicorn

    uvicorn.run("radar.web.app:app", host=config.HOST, port=args.port or config.PORT, reload=False, log_level="info")


def cmd_backup(_):
    with db.session() as conn:
        path = db.backup(conn, "manual")
    print(f"Резервная копия: {path}")


def cmd_import(args):
    from . import importers

    with db.session() as conn:
        if args.kind == "competitors":
            print(importers.import_competitors_json(conn, args.path))
        else:
            print(importers.import_semantic_map_json(conn, args.path))


def cmd_pipeline(args):
    """Полный цикл: сбор → анализ → отчёт."""
    cmd_collect(argparse.Namespace(source=args.source, max_groups=None))
    cmd_analyze(args)
    cmd_report(argparse.Namespace(days=7))


def main(argv=None):
    setup_logging()
    p = argparse.ArgumentParser(prog="radar", description="M22 Product Radar")
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("init").set_defaults(fn=cmd_init)
    c = sub.add_parser("collect")
    c.add_argument("source", nargs="?", default="all", choices=["all", "m22", "radiosync", "competitors", "trends", "suggest"])
    c.add_argument("--max-groups", type=int, default=None)
    c.set_defaults(fn=cmd_collect)
    sub.add_parser("analyze").set_defaults(fn=cmd_analyze)
    r = sub.add_parser("report")
    r.add_argument("--days", type=int, default=7)
    r.set_defaults(fn=cmd_report)
    s = sub.add_parser("serve")
    s.add_argument("--port", type=int, default=None)
    s.set_defaults(fn=cmd_serve)
    sub.add_parser("backup").set_defaults(fn=cmd_backup)
    i = sub.add_parser("import")
    i.add_argument("kind", choices=["competitors", "semantic"])
    i.add_argument("path")
    i.set_defaults(fn=cmd_import)
    pl = sub.add_parser("pipeline")
    pl.add_argument("source", nargs="?", default="all")
    pl.set_defaults(fn=cmd_pipeline)
    args = p.parse_args(argv)
    args.fn(args)


if __name__ == "__main__":
    main(sys.argv[1:])
