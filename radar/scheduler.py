"""Планировщик регулярных задач (APScheduler, в процессе веб-приложения).

- каталог M22 и RadioSync — ежедневно;
- конкуренты — ежедневно (страницы мониторинга, ≥2 с между запросами к домену);
- Яндекс Подсказки и Google Trends — еженедельно;
- анализ (сопоставление, сигналы, рекомендации, гипотезы) — после каждого сбора;
- еженедельный отчёт и резервная копия — раз в неделю;
- повторный запуск после ошибки — через 2 часа (до 2 повторов).
"""
from __future__ import annotations

import logging
import threading
from datetime import datetime, timedelta

from . import config, db, discovery, matching, recommendations, reports, signals

log = logging.getLogger(__name__)
_lock = threading.Lock()
_scheduler = None
JOB_STATE: dict[str, dict] = {}


def _run_job(name: str, fn, retry: int = 0):
    if not _lock.acquire(blocking=False):
        log.info("job %s пропущен: другой сбор ещё идёт", name)
        return
    JOB_STATE[name] = {"status": "running", "started_at": db.now_iso()}
    try:
        with db.session() as conn:
            result = fn(conn)
        JOB_STATE[name] = {"status": "ok", "finished_at": db.now_iso(), "result": str(result)[:500]}
        log.info("job %s: %s", name, result)
    except Exception as exc:  # noqa: BLE001
        log.exception("job %s failed", name)
        JOB_STATE[name] = {"status": "error", "finished_at": db.now_iso(), "error": str(exc)[:500]}
        with db.session() as conn:
            db.log_error(conn, name, None, f"Задача планировщика упала: {exc}")
        if retry < 2 and _scheduler is not None:
            _scheduler.add_job(_run_job, "date", run_date=datetime.now() + timedelta(hours=2), args=[name, fn, retry + 1], id=f"{name}-retry-{retry + 1}", replace_existing=True)
    finally:
        _lock.release()


def analyze(conn):
    r = {"matching": matching.run_matching(conn), "signals": signals.run_all(conn), "recommendations": recommendations.generate(conn), "hypotheses": discovery.generate(conn)}
    db.set_setting(conn, "last_analysis_at", db.now_iso())
    return r


def collect_m22(conn):
    from .collectors import m22, radiosync

    r = {"m22": m22.run(conn), "radiosync": radiosync.run(conn)}
    r["analysis"] = analyze(conn)
    return r


def collect_competitors(conn):
    from .collectors import competitor_generic

    r = {"competitors": competitor_generic.run(conn)}
    r["analysis"] = analyze(conn)
    return r


def collect_demand(conn):
    from .collectors import trends, yandex_suggest

    r = {"suggest": yandex_suggest.run(conn), "trends": trends.run(conn)}
    r["analysis"] = analyze(conn)
    return r


def weekly_report(conn):
    rep = reports.build_weekly(conn, days=7)
    rid = reports.save(conn, rep)
    path = db.backup(conn, "weekly")
    return {"report_id": rid, "backup": str(path)}


def start():
    global _scheduler
    if not config.SCHEDULE_ENABLED or _scheduler is not None:
        return None
    from apscheduler.schedulers.background import BackgroundScheduler

    _scheduler = BackgroundScheduler(timezone="Europe/Moscow")
    _scheduler.add_job(_run_job, "cron", hour=config.M22_CRON_HOUR, minute=0, args=["m22", collect_m22], id="m22", replace_existing=True)
    _scheduler.add_job(_run_job, "cron", hour=config.COMPETITORS_CRON_HOUR, minute=0, args=["competitors", collect_competitors], id="competitors", replace_existing=True)
    _scheduler.add_job(_run_job, "cron", day_of_week=config.TRENDS_CRON_DOW, hour=7, minute=30, args=["demand", collect_demand], id="demand", replace_existing=True)
    _scheduler.add_job(_run_job, "cron", day_of_week=config.REPORT_CRON_DOW, hour=8, minute=30, args=["weekly_report", weekly_report], id="weekly_report", replace_existing=True)
    _scheduler.start()
    log.info("scheduler started")
    return _scheduler


def run_now(name: str) -> bool:
    """Запуск задачи из интерфейса в фоне."""
    fn = {"m22": collect_m22, "competitors": collect_competitors, "demand": collect_demand, "analyze": analyze, "weekly_report": weekly_report}.get(name)
    if fn is None:
        return False
    threading.Thread(target=_run_job, args=[name, fn], daemon=True).start()
    return True


def jobs_info() -> list[dict]:
    out = []
    if _scheduler is not None:
        for j in _scheduler.get_jobs():
            out.append({"id": j.id, "next_run": j.next_run_time.strftime("%Y-%m-%d %H:%M") if j.next_run_time else None, "state": JOB_STATE.get(j.id)})
    for k, v in JOB_STATE.items():
        if not any(o["id"] == k for o in out):
            out.append({"id": k, "next_run": None, "state": v})
    return out
