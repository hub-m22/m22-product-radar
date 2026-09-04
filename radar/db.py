"""SQLite: подключение, миграции, утилиты."""
from __future__ import annotations

import json
import logging
import shutil
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

from . import config

log = logging.getLogger(__name__)
MIGRATIONS_DIR = Path(__file__).parent / "migrations"


def now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def connect(db_path: Path | None = None) -> sqlite3.Connection:
    path = db_path or config.DB_PATH
    conn = sqlite3.connect(str(path), timeout=30, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA busy_timeout = 30000")
    return conn


@contextmanager
def session(db_path: Path | None = None) -> Iterator[sqlite3.Connection]:
    conn = connect(db_path)
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def migrate(conn: sqlite3.Connection) -> list[str]:
    """Применяет все SQL-миграции из radar/migrations по порядку. Идемпотентно."""
    conn.execute(
        "CREATE TABLE IF NOT EXISTS schema_migrations (version TEXT PRIMARY KEY, applied_at TEXT NOT NULL DEFAULT (datetime('now')))"
    )
    applied = {r[0] for r in conn.execute("SELECT version FROM schema_migrations")}
    done: list[str] = []
    for file in sorted(MIGRATIONS_DIR.glob("*.sql")):
        version = file.stem
        if version in applied:
            continue
        sql = file.read_text(encoding="utf-8")
        conn.executescript(sql)
        conn.execute("INSERT OR IGNORE INTO schema_migrations(version) VALUES (?)", (version,))
        conn.commit()
        done.append(version)
        log.info("migration applied: %s", version)
    return done


def init_db(db_path: Path | None = None) -> None:
    with session(db_path) as conn:
        migrate(conn)


def backup(conn: sqlite3.Connection, tag: str = "") -> Path:
    """Резервная копия базы через sqlite backup API."""
    config.BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    dest = config.BACKUP_DIR / f"radar_{stamp}{('_' + tag) if tag else ''}.sqlite3"
    target = sqlite3.connect(str(dest))
    with target:
        conn.backup(target)
    target.close()
    return dest


def j(value: Any) -> str | None:
    return None if value is None else json.dumps(value, ensure_ascii=False)


def uj(text: str | None, default: Any = None) -> Any:
    if not text:
        return default
    try:
        return json.loads(text)
    except (ValueError, TypeError):
        return default


def rows(conn: sqlite3.Connection, sql: str, params: tuple | list = ()) -> list[dict]:
    return [dict(r) for r in conn.execute(sql, params).fetchall()]


def row(conn: sqlite3.Connection, sql: str, params: tuple | list = ()) -> dict | None:
    r = conn.execute(sql, params).fetchone()
    return dict(r) if r else None


def get_setting(conn: sqlite3.Connection, key: str, default: str | None = None) -> str | None:
    r = conn.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
    return r[0] if r else default


def set_setting(conn: sqlite3.Connection, key: str, value: str) -> None:
    conn.execute(
        "INSERT INTO settings(key,value,updated_at) VALUES(?,?,datetime('now')) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=datetime('now')",
        (key, value),
    )


def log_error(conn: sqlite3.Connection, source_key: str | None, url: str | None, error: str, context: str | None = None) -> None:
    conn.execute(
        "INSERT INTO error_log(source_key,url,error,context) VALUES(?,?,?,?)",
        (source_key, url, error[:2000], context),
    )


def start_run(conn: sqlite3.Connection, source_key: str) -> int:
    cur = conn.execute("INSERT INTO source_runs(source_key) VALUES(?)", (source_key,))
    conn.execute("UPDATE sources SET last_run_at=datetime('now') WHERE key=?", (source_key,))
    return int(cur.lastrowid)


def finish_run(conn: sqlite3.Connection, run_id: int, status: str, seen: int, changed: int, errors: int, message: str = "") -> None:
    conn.execute(
        "UPDATE source_runs SET finished_at=datetime('now'), status=?, items_seen=?, items_changed=?, errors=?, message=? WHERE id=?",
        (status, seen, changed, errors, message[:2000], run_id),
    )
    r = conn.execute("SELECT source_key FROM source_runs WHERE id=?", (run_id,)).fetchone()
    if r:
        if status in ("ok", "partial"):
            conn.execute(
                "UPDATE sources SET status='ok', last_success_at=datetime('now'), last_error=NULL, consecutive_failures=0 WHERE key=?",
                (r[0],),
            )
        else:
            conn.execute(
                "UPDATE sources SET status='error', last_error=?, consecutive_failures=consecutive_failures+1 WHERE key=?",
                (message[:1000], r[0]),
            )


def upsert_source(conn: sqlite3.Connection, key: str, **fields: Any) -> None:
    existing = conn.execute("SELECT id FROM sources WHERE key=?", (key,)).fetchone()
    if existing:
        sets = ", ".join(f"{k}=?" for k in fields)
        conn.execute(f"UPDATE sources SET {sets} WHERE key=?", (*fields.values(), key))
    else:
        cols = ", ".join(["key", *fields.keys()])
        qs = ", ".join("?" for _ in range(len(fields) + 1))
        conn.execute(f"INSERT INTO sources({cols}) VALUES({qs})", (key, *fields.values()))


def vacuum_into_export(conn: sqlite3.Connection, dest: Path) -> Path:
    shutil.copyfile(config.DB_PATH, dest)
    return dest
