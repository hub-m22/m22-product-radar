-- Сервисный профиль конкурента (автоматически извлечённые и ручные значения)
ALTER TABLE competitors ADD COLUMN warranty_years REAL;
ALTER TABLE competitors ADD COLUMN service_center TEXT;
ALTER TABLE competitors ADD COLUMN replacement_fund TEXT;
ALTER TABLE competitors ADD COLUMN free_delivery TEXT;
ALTER TABLE competitors ADD COLUMN usp TEXT;
ALTER TABLE competitors ADD COLUMN warranty_years_manual REAL;
ALTER TABLE competitors ADD COLUMN service_center_manual TEXT;
ALTER TABLE competitors ADD COLUMN replacement_fund_manual TEXT;
ALTER TABLE competitors ADD COLUMN free_delivery_manual TEXT;
ALTER TABLE competitors ADD COLUMN usp_manual TEXT;
ALTER TABLE competitors ADD COLUMN profile_json TEXT;
ALTER TABLE competitors ADD COLUMN profile_checked_at TEXT;
