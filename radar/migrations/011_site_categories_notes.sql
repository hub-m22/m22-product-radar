-- Пояснения к разделам сайтов: описание со страницы и примеры товаров (для блока «вне нашего контура»)
ALTER TABLE site_categories ADD COLUMN note TEXT;
ALTER TABLE site_categories ADD COLUMN sample_items TEXT;
ALTER TABLE site_categories ADD COLUMN enriched_at TEXT;
