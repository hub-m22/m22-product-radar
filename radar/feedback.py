"""Обратная связь и самокалибровка.

Каждое решение человека («принять» / «отклонить» по сигналу или рекомендации) записывается как проверка.
Из проверок считается доля подтверждений по типу сигнала (и по типу + категории); она калибрует уверенность
новых сигналов того же типа и закрепляет/снимает сопоставления товаров, на которых сигнал был построен.
Ничего не «дообучается» скрыто: правила калибровки прозрачны и видны в разделе «Настройки».
"""
from __future__ import annotations

import sqlite3

from . import db

CONFIRM = {"accepted", "done", "in_progress", "approved"}
REJECT = {"rejected"}
MIN_CHECKS = 3  # калибровка включается после трёх проверок по типу


def record(conn: sqlite3.Connection, entity_type: str, entity_id: int, status: str, author: str | None = None, note: str | None = None) -> str | None:
    """Записывает проверку по решению человека. Возвращает 'confirmed' | 'rejected' | None."""
    decision = "confirmed" if status in CONFIRM else ("rejected" if status in REJECT else None)
    if decision is None:
        return None
    signals = []
    if entity_type == "signal":
        s = db.row(conn, "SELECT * FROM signals WHERE id=?", (entity_id,))
        signals = [s] if s else []
    elif entity_type == "recommendation":
        r = db.row(conn, "SELECT * FROM recommendations WHERE id=?", (entity_id,))
        ids = db.uj(r["signal_ids_json"], []) if r else []
        if ids:
            signals = db.rows(conn, f"SELECT * FROM signals WHERE id IN ({','.join('?' * len(ids))})", ids)
    elif entity_type == "hypothesis":
        h = db.row(conn, "SELECT * FROM hypotheses WHERE id=?", (entity_id,))
        ids = db.uj(h["signals_json"], []) if h else []
        if ids:
            signals = db.rows(conn, f"SELECT * FROM signals WHERE id IN ({','.join('?' * len(ids))})", ids)
    if not signals:
        conn.execute("INSERT INTO feedback(entity_type, entity_id, decision, author, note) VALUES(?,?,?,?,?)", (entity_type, entity_id, decision, author, note))
        return decision
    for s in signals:
        existing = db.row(conn, "SELECT id FROM feedback WHERE entity_type=? AND entity_id=? AND signal_id=?", (entity_type, entity_id, s["id"]))
        if existing:
            conn.execute("UPDATE feedback SET decision=?, author=?, note=?, created_at=datetime('now') WHERE id=?", (decision, author, note, existing["id"]))
        else:
            conn.execute("INSERT INTO feedback(entity_type, entity_id, signal_id, signal_type, category_slug, decision, author, note, snapshot_json) VALUES(?,?,?,?,?,?,?,?,?)",
                         (entity_type, entity_id, s["id"], s["type"], s["category_slug"], decision, author, note, s["evidence_json"]))
        _propagate_to_matches(conn, s, decision)
    return decision


def _propagate_to_matches(conn: sqlite3.Connection, s: dict, decision: str) -> None:
    """Подтверждённый ценовой сигнал закрепляет сопоставления, на которых он построен; отклонённый — отправляет их на ручную проверку."""
    ev = db.uj(s["evidence_json"], {}) or {}
    comps = ev.get("comparables") or []
    if not comps or not s["m22_product_id"]:
        return
    ids = [c["competitor_product_id"] for c in comps if c.get("competitor_product_id")]
    if not ids:
        return
    q = ",".join("?" * len(ids))
    if decision == "confirmed":
        conn.execute(f"UPDATE product_matches SET review_status='confirmed', method='manual', needs_review=0, reviewer_note=COALESCE(reviewer_note, 'подтверждено через сигнал #{s['id']}'), updated_at=datetime('now') "
                     f"WHERE m22_product_id=? AND competitor_product_id IN ({q}) AND review_status='auto'", [s["m22_product_id"], *ids])
    else:
        conn.execute(f"UPDATE product_matches SET needs_review=1, reviewer_note=COALESCE(reviewer_note, 'сигнал #{s['id']} отклонён — проверить сопоставление'), updated_at=datetime('now') "
                     f"WHERE m22_product_id=? AND competitor_product_id IN ({q}) AND review_status='auto'", [s["m22_product_id"], *ids])


def stats(conn: sqlite3.Connection) -> dict:
    """Доля подтверждений по типам сигналов и по парам тип+категория."""
    by_type: dict[str, dict] = {}
    for r in db.rows(conn, "SELECT signal_type, category_slug, decision, COUNT(*) n FROM feedback WHERE signal_type IS NOT NULL GROUP BY 1,2,3"):
        t = by_type.setdefault(r["signal_type"], {"confirmed": 0, "rejected": 0, "by_category": {}})
        t[r["decision"]] += r["n"]
        c = t["by_category"].setdefault(r["category_slug"] or "—", {"confirmed": 0, "rejected": 0})
        c[r["decision"]] += r["n"]
    for t in by_type.values():
        n = t["confirmed"] + t["rejected"]
        t["checks"] = n
        t["rate"] = round((t["confirmed"] + 1) / (n + 2), 2)  # сглаженная доля подтверждений
        t["factor"] = calibration_factor(t["confirmed"], t["rejected"])
    return by_type


def calibration_factor(confirmed: int, rejected: int) -> float:
    """Множитель уверенности: 1.0 при отсутствии проверок; от 0.6 (всё отклонено) до 1.15 (всё подтверждено) после MIN_CHECKS проверок."""
    n = confirmed + rejected
    if n < MIN_CHECKS:
        return 1.0
    rate = (confirmed + 1) / (n + 2)
    return round(0.6 + 0.55 * rate, 2)


def calibrate(conn: sqlite3.Connection, signal_type: str, category_slug: str | None, confidence: float | None) -> tuple[float | None, str | None]:
    """Возвращает откалиброванную уверенность и пояснение (или исходную, если проверок мало)."""
    if confidence is None:
        return None, None
    r = db.row(conn, "SELECT SUM(decision='confirmed') c, SUM(decision='rejected') r FROM feedback WHERE signal_type=? AND category_slug IS ?", (signal_type, category_slug))
    c, rj = (r["c"] or 0, r["r"] or 0) if r else (0, 0)
    scope = "по типу и категории"
    if c + rj < MIN_CHECKS:
        r = db.row(conn, "SELECT SUM(decision='confirmed') c, SUM(decision='rejected') r FROM feedback WHERE signal_type=?", (signal_type,))
        c, rj = (r["c"] or 0, r["r"] or 0) if r else (0, 0)
        scope = "по типу сигнала"
    f = calibration_factor(c, rj)
    if f == 1.0:
        return confidence, None
    new = round(max(0.2, min(0.98, confidence * f)), 2)
    return new, f"Калибровка {scope}: {c} подтверждений, {rj} отклонений → множитель {f}"
