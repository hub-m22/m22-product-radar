-- Наблюдения рынка вне каталогов: тендеры, листинги маркетплейсов, заметки о производителях
CREATE TABLE IF NOT EXISTS market_observations (
    id INTEGER PRIMARY KEY,
    kind TEXT NOT NULL,                  -- tender | marketplace_listing | manufacturer_note | serp
    title TEXT NOT NULL,
    party TEXT,                          -- заказчик / продавец / производитель
    region TEXT,
    price REAL,
    currency TEXT DEFAULT 'RUB',
    observed_date TEXT,
    url TEXT,
    source TEXT,
    note TEXT,
    category_slug TEXT,
    competitor_id INTEGER,
    fetched_at TEXT NOT NULL DEFAULT (datetime('now')),
    dedupe_key TEXT UNIQUE
);
