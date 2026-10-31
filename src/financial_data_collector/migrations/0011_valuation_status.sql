-- 0011: final closes, and the columns that let a valuation say why it is not known.
-- prices.final = 0 marks a bar fetched before its close; the next run's trailing refresh rewrites it.
-- Existing rows are final, except those on the newest date (their fetch time is unknown). Task columns
-- below (securities.parked_reason / miss_runs, valuation_daily.status / status_reason) are filled by later code.

ALTER TABLE prices ADD COLUMN final INTEGER NOT NULL DEFAULT 1;
ALTER TABLE prices ADD COLUMN fetched_at TEXT;
UPDATE prices SET final = 0 WHERE date = (SELECT MAX(date) FROM prices);

ALTER TABLE securities ADD COLUMN parked_reason TEXT;
ALTER TABLE securities ADD COLUMN miss_runs INTEGER NOT NULL DEFAULT 0;

ALTER TABLE valuation_daily ADD COLUMN status TEXT NOT NULL DEFAULT 'ok';
ALTER TABLE valuation_daily ADD COLUMN status_reason TEXT;
