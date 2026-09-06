-- Журнал ручных проверок: решения человека по сигналам, рекомендациям и гипотезам
CREATE TABLE IF NOT EXISTS feedback (
    id INTEGER PRIMARY KEY,
    entity_type TEXT NOT NULL,           -- signal | recommendation | hypothesis
    entity_id INTEGER NOT NULL,
    signal_id INTEGER,
    signal_type TEXT,
    category_slug TEXT,
    decision TEXT NOT NULL,              -- confirmed | rejected
    author TEXT,
    note TEXT,
    snapshot_json TEXT,                  -- доказательства сигнала на момент решения
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_feedback_type ON feedback(signal_type, category_slug);
