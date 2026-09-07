-- Дополнительные показатели юрлица: арбитраж, исполнительные производства, налоги, финансовое состояние, товарные знаки
ALTER TABLE competitors ADD COLUMN arbitration_count INTEGER;
ALTER TABLE competitors ADD COLUMN arbitration_sum_rub REAL;
ALTER TABLE competitors ADD COLUMN arbitration_note TEXT;
ALTER TABLE competitors ADD COLUMN enforcement_count INTEGER;
ALTER TABLE competitors ADD COLUMN enforcement_note TEXT;
ALTER TABLE competitors ADD COLUMN taxes_rub REAL;
ALTER TABLE competitors ADD COLUMN contributions_rub REAL;
ALTER TABLE competitors ADD COLUMN fin_state TEXT;
ALTER TABLE competitors ADD COLUMN trademarks TEXT;
ALTER TABLE competitors ADD COLUMN risk_flags TEXT;
ALTER TABLE competitors ADD COLUMN employees_history TEXT;
