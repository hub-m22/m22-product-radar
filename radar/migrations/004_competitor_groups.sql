-- Группа компаний: несколько сайтов одного продавца считаются одним продавцом
ALTER TABLE competitors ADD COLUMN group_name TEXT;
