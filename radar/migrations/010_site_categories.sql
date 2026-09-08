-- Категории, задекларированные на сайтах (меню/хлебные крошки), и раздел сайта у товаров конкурентов
ALTER TABLE competitor_products ADD COLUMN site_category_path TEXT;
CREATE TABLE IF NOT EXISTS site_categories (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    site TEXT NOT NULL,                 -- домен: m22.ru, radiosync.ru, retekess.com.ru ...
    competitor_id INTEGER,              -- NULL для сайтов M22
    name TEXT NOT NULL,                 -- название категории как на сайте
    url TEXT,
    parent TEXT,                        -- родительская категория (если меню многоуровневое)
    level INTEGER NOT NULL DEFAULT 1,
    our_slug TEXT,                      -- на какую нашу категорию похоже по названию
    product_count INTEGER,              -- сколько собранных товаров отнесено к категории
    source TEXT,                        -- menu | breadcrumbs | manual
    checked_at TEXT,
    UNIQUE(site, name, parent)
);
