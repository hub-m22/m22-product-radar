-- Решения по ценовым вердиктам: кто согласовал / принял в исполнение / отклонил
CREATE TABLE IF NOT EXISTS price_decisions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    m22_product_id INTEGER NOT NULL UNIQUE,
    status TEXT NOT NULL,             -- approved | accepted | rejected | deferred
    author TEXT,                      -- участник, принявший решение (из списка ответственных в настройках)
    comment TEXT,
    verdict TEXT,                     -- вердикт радара на момент решения (lower | raise | keep | no_data)
    price REAL,                       -- цена M22 на момент решения
    median REAL,                      -- медиана рынка на момент решения
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at TEXT NOT NULL DEFAULT (datetime('now'))
);
