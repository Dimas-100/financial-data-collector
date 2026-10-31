-- 0012: the checker's record. check_runs is one row per run of `fdc check` (or the sync's check step);
-- check_findings is what the checks found, one row per (check, symbol, field, first day). A finding that
-- stops being found is cleared (cleared_at), never deleted; one that comes back is reopened.
-- `field` is a valuation_daily column name, or '*' for every value column.

CREATE TABLE check_runs (
    run_id TEXT PRIMARY KEY,
    started_at TEXT NOT NULL,
    finished_at TEXT NOT NULL,
    derive_run_id TEXT,
    checks_run INTEGER NOT NULL DEFAULT 0,
    holds INTEGER NOT NULL DEFAULT 0,
    warns INTEGER NOT NULL DEFAULT 0,
    error TEXT
);

CREATE TABLE check_findings (
    id INTEGER PRIMARY KEY,
    check_name TEXT NOT NULL,
    symbol TEXT NOT NULL,
    field TEXT NOT NULL,
    date_from TEXT NOT NULL,
    date_to TEXT,
    severity TEXT NOT NULL CHECK (severity IN ('hold', 'warn', 'info')),
    message TEXT NOT NULL,
    evidence TEXT,
    first_seen TEXT NOT NULL,
    last_seen TEXT NOT NULL,
    cleared_at TEXT,
    UNIQUE (check_name, symbol, field, date_from)
);

CREATE INDEX idx_check_findings_open ON check_findings (check_name) WHERE cleared_at IS NULL;
