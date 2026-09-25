-- 0001: core tables. schema_version itself is created by migrate.py.

CREATE TABLE accounts (
  id            INTEGER PRIMARY KEY AUTOINCREMENT,
  label         TEXT NOT NULL UNIQUE,
  slug          TEXT NOT NULL DEFAULT 'other',
  institution   TEXT NOT NULL DEFAULT 'unknown',
  account_type  TEXT NOT NULL DEFAULT 'other',
  first_seen    TEXT NOT NULL
);

CREATE TABLE securities (
  symbol          TEXT PRIMARY KEY,
  description     TEXT,
  asset_type      TEXT NOT NULL DEFAULT 'unknown',
  cik             TEXT,
  sec_name        TEXT,
  first_seen      TEXT NOT NULL,
  last_sec_fetch  TEXT,
  price_source    TEXT
);

CREATE TABLE position_snapshots (
  as_of_date        TEXT NOT NULL,
  account_id        INTEGER NOT NULL REFERENCES accounts(id),
  symbol            TEXT NOT NULL REFERENCES securities(symbol),
  quantity          REAL NOT NULL,
  price             REAL,
  market_value      REAL,
  cost_basis_total  REAL,
  avg_cost          REAL,
  unrealized_pnl    REAL,
  source            TEXT NOT NULL,
  source_fetched_at TEXT,
  PRIMARY KEY (as_of_date, account_id, symbol)
);
CREATE INDEX idx_positions_symbol ON position_snapshots (symbol, as_of_date);

CREATE TABLE cash_balances (
  as_of_date  TEXT NOT NULL,
  account_id  INTEGER NOT NULL REFERENCES accounts(id),
  currency    TEXT NOT NULL DEFAULT 'USD',
  amount      REAL NOT NULL,
  source      TEXT NOT NULL,
  PRIMARY KEY (as_of_date, account_id, currency)
);

CREATE TABLE transactions (
  id              INTEGER PRIMARY KEY AUTOINCREMENT,
  account_id      INTEGER NOT NULL REFERENCES accounts(id),
  trade_date      TEXT NOT NULL,
  settlement_date TEXT,
  type            TEXT NOT NULL,
  symbol          TEXT,
  units           REAL,
  price           REAL,
  amount          REAL,
  fee             REAL,
  description     TEXT NOT NULL DEFAULT '',
  source          TEXT NOT NULL,
  dedupe_key      TEXT NOT NULL UNIQUE
);
CREATE INDEX idx_transactions_symbol ON transactions (symbol, trade_date);
CREATE INDEX idx_transactions_account ON transactions (account_id, trade_date);

CREATE TABLE prices (
  symbol        TEXT NOT NULL,
  date          TEXT NOT NULL,
  close         REAL NOT NULL,
  adj_close     REAL NOT NULL,
  dividend      REAL NOT NULL DEFAULT 0,
  split_factor  REAL NOT NULL DEFAULT 1,
  source        TEXT NOT NULL,
  PRIMARY KEY (symbol, date)
);

CREATE TABLE sec_facts (
  cik           TEXT NOT NULL,
  taxonomy      TEXT NOT NULL,
  concept       TEXT NOT NULL,
  unit          TEXT NOT NULL,
  period_start  TEXT NOT NULL DEFAULT '',
  period_end    TEXT NOT NULL,
  value         REAL NOT NULL,
  fy            INTEGER,
  fp            TEXT,
  form          TEXT NOT NULL,
  filed         TEXT NOT NULL,
  accn          TEXT NOT NULL,
  frame         TEXT,
  PRIMARY KEY (cik, taxonomy, concept, unit, period_start, period_end, accn)
) WITHOUT ROWID;
CREATE INDEX idx_sec_facts_lookup ON sec_facts (cik, concept, period_end);

CREATE TABLE concept_map (
  line_item  TEXT NOT NULL,
  statement  TEXT NOT NULL,
  kind       TEXT NOT NULL,
  taxonomy   TEXT NOT NULL,
  concept    TEXT NOT NULL,
  priority   INTEGER NOT NULL,
  PRIMARY KEY (line_item, taxonomy, concept)
);

CREATE TABLE financial_line_items (
  cik             TEXT NOT NULL,
  line_item       TEXT NOT NULL,
  period_kind     TEXT NOT NULL,
  period_start    TEXT NOT NULL DEFAULT '',
  period_end      TEXT NOT NULL,
  fiscal_year     INTEGER NOT NULL,
  fiscal_quarter  INTEGER,
  value           REAL NOT NULL,
  concept         TEXT NOT NULL,
  filed           TEXT NOT NULL,
  accn            TEXT NOT NULL,
  is_derived      INTEGER NOT NULL DEFAULT 0,
  PRIMARY KEY (cik, line_item, period_kind, period_end)
);

CREATE TABLE sync_runs (
  id            INTEGER PRIMARY KEY AUTOINCREMENT,
  run_id        TEXT NOT NULL,
  step          TEXT NOT NULL,
  started_at    TEXT NOT NULL,
  finished_at   TEXT NOT NULL,
  status        TEXT NOT NULL,
  rows_written  INTEGER NOT NULL DEFAULT 0,
  message       TEXT NOT NULL DEFAULT ''
);
CREATE INDEX idx_sync_runs_step ON sync_runs (step, id);

CREATE TABLE ingested_files (
  sha256       TEXT PRIMARY KEY,
  path         TEXT NOT NULL,
  kind         TEXT NOT NULL,
  ingested_at  TEXT NOT NULL,
  rows         INTEGER NOT NULL DEFAULT 0
);
