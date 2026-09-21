-- «Новости конкурентов»: страницы для отслеживания изменений, снимки, найденные изменения, события, прогоны, просмотры, дайджесты
CREATE TABLE IF NOT EXISTS news_pages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    competitor_id INTEGER NOT NULL,
    url TEXT NOT NULL UNIQUE,
    kind TEXT NOT NULL,               -- home | catalog | category | news | blog | promo | solutions | cases | brands | partners | other
    title TEXT,
    discovered_from TEXT,             -- откуда взяли адрес: home | catalog | monitored_pages | manual
    is_active INTEGER NOT NULL DEFAULT 1,
    last_status TEXT,                 -- ok | error | robots_disallowed
    last_error TEXT,
    last_checked_at TEXT,
    fail_count INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS ix_news_pages_comp ON news_pages(competitor_id);

CREATE TABLE IF NOT EXISTS news_snapshots (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    page_id INTEGER NOT NULL,
    run_id INTEGER,
    fetched_at TEXT NOT NULL DEFAULT (datetime('now')),
    content_hash TEXT NOT NULL,       -- хеш нормализованного текста (без счётчиков, дат, скриптов)
    text TEXT,                        -- нормализованный текст страницы
    items_json TEXT                   -- значимые ссылки страницы: [{"t": название, "u": адрес}]
);
CREATE INDEX IF NOT EXISTS ix_news_snapshots_page ON news_snapshots(page_id, id);

CREATE TABLE IF NOT EXISTS news_changes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id INTEGER,
    page_id INTEGER NOT NULL,
    competitor_id INTEGER NOT NULL,
    change_kind TEXT NOT NULL,        -- text | items_added | items_removed | page_new | page_gone
    before_text TEXT,
    after_text TEXT,
    items_json TEXT,
    event_id INTEGER,                 -- событие, в которое вошло изменение (NULL — признано шумом)
    verdict TEXT,                     -- event | noise
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS ix_news_changes_run ON news_changes(run_id);

CREATE TABLE IF NOT EXISTS news_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    competitor_id INTEGER NOT NULL,
    run_id INTEGER,
    event_type TEXT NOT NULL,         -- NEW_PRODUCT | NEW_CATEGORY | NEW_BRAND | PRODUCT_UPDATE | PRODUCT_REMOVED | PRICE_CHANGE | NEW_SOLUTION | NEW_CASE | NEW_PARTNERSHIP | PROMOTION | COMPANY_NEWS | SITE_CHANGE | OTHER
    title TEXT NOT NULL,
    summary TEXT,
    what_changed TEXT,
    url TEXT,
    product_name TEXT,
    category_slug TEXT,
    importance INTEGER NOT NULL DEFAULT 2,   -- 3 важно, 2 обычное, 1 второстепенное
    detected_at TEXT NOT NULL DEFAULT (datetime('now')),
    published_at TEXT,                -- дата публикации с сайта, если она есть; иначе NULL («обнаружено …»)
    source_kind TEXT,                 -- page_diff | products | prices | ai
    ai_used INTEGER NOT NULL DEFAULT 0,
    dedupe_key TEXT NOT NULL UNIQUE,
    is_read INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS ix_news_events_comp ON news_events(competitor_id, detected_at);

CREATE TABLE IF NOT EXISTS news_runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id INTEGER,                   -- source_runs.id общего прогона
    competitor_id INTEGER NOT NULL,
    started_at TEXT NOT NULL DEFAULT (datetime('now')),
    finished_at TEXT,
    status TEXT,                      -- NO_CHANGES | CHANGES_FOUND | CRAWL_ERROR | PARTIAL_CRAWL | BASELINE
    pages_total INTEGER NOT NULL DEFAULT 0,
    pages_ok INTEGER NOT NULL DEFAULT 0,
    pages_failed INTEGER NOT NULL DEFAULT 0,
    events_found INTEGER NOT NULL DEFAULT 0,
    error TEXT
);
CREATE INDEX IF NOT EXISTS ix_news_runs_comp ON news_runs(competitor_id, id);

CREATE TABLE IF NOT EXISTS news_views (
    competitor_id INTEGER PRIMARY KEY,
    viewed_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS news_digests (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    day TEXT NOT NULL UNIQUE,
    text TEXT NOT NULL,
    stats_json TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);
