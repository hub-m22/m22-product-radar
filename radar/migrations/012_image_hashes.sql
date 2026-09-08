-- Перцептивные хеши фотографий товаров и совпадения по фото
CREATE TABLE IF NOT EXISTS image_hashes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    owner TEXT NOT NULL,              -- m22 | comp
    product_id INTEGER NOT NULL,
    url TEXT NOT NULL,
    phash TEXT,                       -- 64 бит, hex
    dhash TEXT,                       -- 64 бит, hex
    width INTEGER, height INTEGER,
    status TEXT,                      -- ok | error
    error TEXT,
    fetched_at TEXT,
    UNIQUE(owner, product_id)
);
CREATE INDEX IF NOT EXISTS ix_image_hashes_url ON image_hashes(url);
CREATE TABLE IF NOT EXISTS image_matches (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    m22_product_id INTEGER NOT NULL,
    competitor_product_id INTEGER NOT NULL,
    phash_dist INTEGER NOT NULL,
    dhash_dist INTEGER NOT NULL,
    verdict TEXT NOT NULL,            -- same | similar
    computed_at TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE(m22_product_id, competitor_product_id)
);
