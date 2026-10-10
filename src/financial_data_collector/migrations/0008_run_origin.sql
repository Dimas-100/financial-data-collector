-- 0008: who started each run: scheduled (the task), manual (fdc sync by hand), console (fdc console).
-- Rows logged before this migration read 'unknown'.

ALTER TABLE sync_runs ADD COLUMN origin TEXT NOT NULL DEFAULT 'unknown';
