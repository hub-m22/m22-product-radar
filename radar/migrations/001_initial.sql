-- M22 Product Radar — начальная схема
PRAGMA journal_mode = WAL;

CREATE TABLE IF NOT EXISTS schema_migrations (
    version TEXT PRIMARY KEY,
    applied_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS settings (
    key TEXT PRIMARY KEY,
    value TEXT,
    updated_at TEXT NOT NULL DEFAULT (datetime('now'))
);

-- Реестр источников данных
CREATE TABLE IF NOT EXISTS sources (
    id INTEGER PRIMARY KEY,
    key TEXT UNIQUE NOT NULL,
    name TEXT NOT NULL,
    kind TEXT NOT NULL,                  -- own_site | competitor | marketplace | demand | tenders | customs | manufacturer | serp | import
    url TEXT,
    status TEXT NOT NULL DEFAULT 'unknown', -- ok | error | needs_auth | blocked | paid | manual_import | disabled
    mvp_suitability TEXT,                -- ready | manual_import | needs_key | blocked | paid
    data_available TEXT,
    official_api TEXT,
    needs_parsing INTEGER DEFAULT 1,
    needs_auth INTEGER DEFAULT 0,
    cost TEXT,
    limits TEXT,
    update_frequency TEXT,
    stability TEXT,
    risks TEXT,
    how_to_connect TEXT,
    notes TEXT,
    last_run_at TEXT,
    last_success_at TEXT,
    last_error TEXT,
    consecutive_failures INTEGER DEFAULT 0,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS source_runs (
    id INTEGER PRIMARY KEY,
    source_key TEXT NOT NULL,
    started_at TEXT NOT NULL DEFAULT (datetime('now')),
    finished_at TEXT,
    status TEXT NOT NULL DEFAULT 'running', -- running | ok | partial | error
    items_seen INTEGER DEFAULT 0,
    items_changed INTEGER DEFAULT 0,
    errors INTEGER DEFAULT 0,
    message TEXT
);

-- Категории (контур исследования)
CREATE TABLE IF NOT EXISTS categories (
    slug TEXT PRIMARY KEY,
    name_ru TEXT NOT NULL,
    parent_slug TEXT,
    in_scope INTEGER NOT NULL DEFAULT 1,
    sort_order INTEGER DEFAULT 100,
    description TEXT,
    keywords TEXT                        -- JSON-массив ключевых слов для авто-классификации
);

-- Товарная матрица M22 (оба сайта)
CREATE TABLE IF NOT EXISTS m22_products (
    id INTEGER PRIMARY KEY,
    site TEXT NOT NULL,                  -- m22.ru | radiosync.ru
    url TEXT UNIQUE NOT NULL,
    external_id TEXT,
    sku TEXT,
    name TEXT NOT NULL,
    brand TEXT,
    category_slug TEXT,                  -- наша категория (radar)
    site_category_path TEXT,             -- категория на сайте
    model_key TEXT,                      -- нормализованный ключ модели (SGTR02 и т.п.)
    kind TEXT,                           -- system | transmitter | receiver | accessory | headphones | microphone | case | kit | ...
    capacity INTEGER,                    -- вместимость комплекта (число приёмников), если есть
    price REAL,
    old_price REAL,
    currency TEXT DEFAULT 'RUB',
    availability TEXT,
    description TEXT,
    specs_json TEXT,                     -- {"группа":{"параметр":"значение"}}
    kit_json TEXT,                       -- комплектация
    applications TEXT,                   -- сферы применения (текст/JSON)
    images_json TEXT,
    parent_url TEXT,                     -- если это вариант (модификация) другого товара
    content_hash TEXT,
    in_scope INTEGER NOT NULL DEFAULT 1,
    is_active INTEGER NOT NULL DEFAULT 1,
    first_seen_at TEXT NOT NULL DEFAULT (datetime('now')),
    last_seen_at TEXT NOT NULL DEFAULT (datetime('now')),
    fetched_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_m22_products_model ON m22_products(model_key);
CREATE INDEX IF NOT EXISTS idx_m22_products_cat ON m22_products(category_slug);

CREATE TABLE IF NOT EXISTS m22_price_history (
    id INTEGER PRIMARY KEY,
    product_id INTEGER NOT NULL REFERENCES m22_products(id),
    price REAL,
    old_price REAL,
    availability TEXT,
    observed_at TEXT NOT NULL DEFAULT (datetime('now')),
    run_id INTEGER
);
CREATE INDEX IF NOT EXISTS idx_m22_price_hist ON m22_price_history(product_id, observed_at);

-- Реестр конкурентов
CREATE TABLE IF NOT EXISTS competitors (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL,
    website TEXT UNIQUE NOT NULL,
    types_json TEXT,                     -- ["direct_seller","brand_owner",...]
    geography TEXT,
    categories_json TEXT,
    brands_json TEXT,
    rental_available TEXT,               -- yes | no | unknown
    advantages TEXT,
    target_segments TEXT,
    new_products TEXT,
    notes TEXT,
    source_urls_json TEXT,
    scrapable TEXT,                      -- static | js | blocked | unknown
    is_active INTEGER NOT NULL DEFAULT 1,
    added_by TEXT DEFAULT 'research',
    checked_at TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at TEXT NOT NULL DEFAULT (datetime('now'))
);

-- Страницы мониторинга (конкурентов и других источников)
CREATE TABLE IF NOT EXISTS monitored_pages (
    id INTEGER PRIMARY KEY,
    competitor_id INTEGER REFERENCES competitors(id),
    url TEXT UNIQUE NOT NULL,
    kind TEXT NOT NULL DEFAULT 'product', -- product | catalog | listing | rent | info
    name TEXT,
    category_slug TEXT,
    parser TEXT NOT NULL DEFAULT 'auto', -- auto | jsonld | microdata | css | wb_search | tilda
    parser_config_json TEXT,            -- {"name":"css","price":"css","items":"css"}
    is_active INTEGER NOT NULL DEFAULT 1,
    last_checked_at TEXT,
    last_status TEXT,
    last_error TEXT,
    fail_count INTEGER DEFAULT 0,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

-- Товары конкурентов
CREATE TABLE IF NOT EXISTS competitor_products (
    id INTEGER PRIMARY KEY,
    competitor_id INTEGER NOT NULL REFERENCES competitors(id),
    page_id INTEGER REFERENCES monitored_pages(id),
    url TEXT NOT NULL,
    name TEXT NOT NULL,
    brand TEXT,
    model_key TEXT,
    category_slug TEXT,
    kind TEXT,
    capacity INTEGER,
    price REAL,
    currency TEXT DEFAULT 'RUB',
    availability TEXT,
    description TEXT,
    specs_json TEXT,
    content_hash TEXT,
    is_active INTEGER NOT NULL DEFAULT 1,
    first_seen_at TEXT NOT NULL DEFAULT (datetime('now')),
    last_seen_at TEXT NOT NULL DEFAULT (datetime('now')),
    fetched_at TEXT,
    UNIQUE(competitor_id, url, name)
);
CREATE INDEX IF NOT EXISTS idx_cp_model ON competitor_products(model_key);
CREATE INDEX IF NOT EXISTS idx_cp_cat ON competitor_products(category_slug);

CREATE TABLE IF NOT EXISTS competitor_price_history (
    id INTEGER PRIMARY KEY,
    competitor_product_id INTEGER NOT NULL REFERENCES competitor_products(id),
    price REAL,
    availability TEXT,
    observed_at TEXT NOT NULL DEFAULT (datetime('now')),
    run_id INTEGER
);
CREATE INDEX IF NOT EXISTS idx_cp_price_hist ON competitor_price_history(competitor_product_id, observed_at);

-- Сопоставление товаров
CREATE TABLE IF NOT EXISTS product_matches (
    id INTEGER PRIMARY KEY,
    m22_product_id INTEGER REFERENCES m22_products(id),
    competitor_product_id INTEGER NOT NULL REFERENCES competitor_products(id),
    match_type TEXT NOT NULL,            -- exact_model | direct_analog | functional | kit | accessory | substitute | adjacent | new_category
    confidence REAL NOT NULL,            -- 0..1
    method TEXT,                         -- rule | manual | ai
    reasons_json TEXT,
    needs_review INTEGER NOT NULL DEFAULT 0,
    review_status TEXT DEFAULT 'auto',   -- auto | confirmed | rejected
    reviewer_note TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE(m22_product_id, competitor_product_id)
);

-- Семантическая карта: поисковые запросы
CREATE TABLE IF NOT EXISTS search_queries (
    id INTEGER PRIMARY KEY,
    query TEXT UNIQUE NOT NULL,
    category_slug TEXT,
    intent TEXT,                         -- commercial | informational | b2b | rental | purchase | application | brand | navigational
    lang TEXT DEFAULT 'ru',
    is_brand INTEGER DEFAULT 0,
    brand_or_model TEXT,
    notes TEXT,
    is_seed INTEGER DEFAULT 0,
    is_active INTEGER NOT NULL DEFAULT 1,
    added_by TEXT DEFAULT 'research',
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

-- Наблюдения спроса (Google Trends индекс, Wordstat показы, импорт)
CREATE TABLE IF NOT EXISTS demand_observations (
    id INTEGER PRIMARY KEY,
    query_id INTEGER NOT NULL REFERENCES search_queries(id),
    source TEXT NOT NULL,                -- google_trends | wordstat_import | yandex_suggest | manual
    period_start TEXT NOT NULL,
    period_end TEXT NOT NULL,
    value REAL,
    unit TEXT NOT NULL,                  -- index | impressions | count
    geo TEXT DEFAULT 'RU',
    fetched_at TEXT NOT NULL DEFAULT (datetime('now')),
    meta_json TEXT,
    UNIQUE(query_id, source, period_start, period_end, geo)
);

-- Рыночные сигналы
CREATE TABLE IF NOT EXISTS signals (
    id INTEGER PRIMARY KEY,
    type TEXT NOT NULL,
    severity TEXT NOT NULL,              -- high | medium | low
    fact_kind TEXT NOT NULL DEFAULT 'fact', -- fact | inference | hypothesis
    category_slug TEXT,
    m22_product_id INTEGER,
    competitor_id INTEGER,
    competitor_product_id INTEGER,
    query_id INTEGER,
    title TEXT NOT NULL,
    what_happened TEXT,
    old_value TEXT,
    new_value TEXT,
    observed_at TEXT NOT NULL DEFAULT (datetime('now')),
    period TEXT,
    source TEXT,
    source_url TEXT,
    evidence_json TEXT,
    confidence REAL,
    why_matters TEXT,
    recommended_action TEXT,
    status TEXT NOT NULL DEFAULT 'new',  -- new | in_research | accepted | rejected | done
    comment TEXT,
    owner TEXT,
    dedupe_key TEXT UNIQUE,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_signals_type ON signals(type);
CREATE INDEX IF NOT EXISTS idx_signals_created ON signals(created_at);

-- Рекомендуемые действия
CREATE TABLE IF NOT EXISTS recommendations (
    id INTEGER PRIMARY KEY,
    title TEXT NOT NULL,
    action TEXT NOT NULL,
    priority TEXT NOT NULL,              -- P1 | P2 | P3
    basis TEXT,                          -- фактическое основание
    expected_effect TEXT,
    confidence REAL,
    owner TEXT,
    due_date TEXT,
    status TEXT NOT NULL DEFAULT 'new',  -- new | accepted | rejected | in_progress | done
    sources_json TEXT,
    signal_ids_json TEXT,
    category_slug TEXT,
    m22_product_id INTEGER,
    comment TEXT,
    dedupe_key TEXT UNIQUE,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at TEXT NOT NULL DEFAULT (datetime('now'))
);

-- Product Discovery: карточки гипотез
CREATE TABLE IF NOT EXISTS hypotheses (
    id INTEGER PRIMARY KEY,
    title TEXT NOT NULL,
    description TEXT,
    discovered_via TEXT,
    signals_json TEXT,
    demand_evidence TEXT,
    competitors_json TEXT,
    market_prices TEXT,
    m22_link TEXT,
    target_segment TEXT,
    use_case TEXT,
    pros TEXT,
    cons TEXT,
    risks TEXT,
    missing_data TEXT,
    next_step TEXT,
    owner TEXT,
    decision_status TEXT NOT NULL DEFAULT 'new', -- new | research | approved | rejected | parked
    category_slug TEXT,
    dedupe_key TEXT UNIQUE,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS comments (
    id INTEGER PRIMARY KEY,
    entity_type TEXT NOT NULL,           -- signal | recommendation | hypothesis | competitor | product
    entity_id INTEGER NOT NULL,
    author TEXT,
    text TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_comments_entity ON comments(entity_type, entity_id);

CREATE TABLE IF NOT EXISTS error_log (
    id INTEGER PRIMARY KEY,
    ts TEXT NOT NULL DEFAULT (datetime('now')),
    source_key TEXT,
    url TEXT,
    error TEXT NOT NULL,
    context TEXT
);

CREATE TABLE IF NOT EXISTS reports (
    id INTEGER PRIMARY KEY,
    kind TEXT NOT NULL DEFAULT 'weekly',
    period_start TEXT NOT NULL,
    period_end TEXT NOT NULL,
    generated_at TEXT NOT NULL DEFAULT (datetime('now')),
    content_json TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS imports (
    id INTEGER PRIMARY KEY,
    kind TEXT NOT NULL,                  -- wordstat | trends_csv | competitors | queries
    filename TEXT,
    rows INTEGER,
    status TEXT,
    message TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);
