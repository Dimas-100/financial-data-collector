-- 0022: the weekly split's proposals. allocation_proposals_raw is one row per proposal: how the cash waiting in one
-- account (account_key is the targets file's name for the account, never an fdc label) was split on a given day.
-- allocation_lines_raw holds its lines: whole dollars toward one holding, with the gap, band and tilt behind them,
-- the reason codes (comma-joined) and a JSON list of sources {kind, label, date, ref}. `notes` and `sources` of a
-- proposal are JSON lists. status is 'open' (the one proposal the account is waiting on), 'replaced' (new cash made
-- a bigger one) or 'spent' (the cash was placed); closed_at / closed_cash say when and with how much cash left.
--
-- These tables are a RECORD, kept like check_findings, not derived: a proposal cannot be rebuilt (the scores it was
-- made from are kept only for the newest day), so a run only adds a proposal or closes one, and an open proposal's
-- lines are never rewritten. At most one proposal per account is open (the partial unique index).
--
-- allocation_proposals and allocation_lines serve them with the checker's holds applied, as valuation_scores does:
-- an open hold (check 'split', severity 'hold', not cleared) whose field is 'proposal:' || id makes an open
-- proposal's status 'held' and blanks its lines' dollars (held = 1). hold_ids lists the findings (NULL when none).
-- A proposal already closed keeps its status. The raw tables are never changed by a hold. Columns are only ever
-- added to these views.

CREATE TABLE allocation_proposals_raw (
  id INTEGER PRIMARY KEY,
  account_key TEXT NOT NULL,
  created_at TEXT NOT NULL,
  cash_date TEXT NOT NULL,
  cash REAL NOT NULL,
  placed INTEGER NOT NULL,
  waits REAL NOT NULL,
  status TEXT NOT NULL CHECK (status IN ('open', 'replaced', 'spent')),
  closed_at TEXT,
  closed_cash REAL,
  notes TEXT NOT NULL DEFAULT '[]',
  sources TEXT NOT NULL DEFAULT '[]',
  checked_at TEXT
);

CREATE UNIQUE INDEX idx_allocation_one_open ON allocation_proposals_raw (account_key) WHERE status = 'open';

CREATE TABLE allocation_lines_raw (
  proposal_id INTEGER NOT NULL REFERENCES allocation_proposals_raw (id),
  symbol TEXT NOT NULL,
  dollars INTEGER NOT NULL CHECK (dollars > 0),
  target_gap REAL NOT NULL,
  band TEXT NOT NULL CHECK (band IN ('cheap', 'usual', 'premium')),
  tilt REAL NOT NULL,
  reasons TEXT NOT NULL DEFAULT '',
  sources TEXT NOT NULL DEFAULT '[]',
  PRIMARY KEY (proposal_id, symbol)
);

CREATE VIEW allocation_proposals AS
WITH x AS (
  SELECT r.*,
         (SELECT group_concat(id, ',') FROM
            (SELECT f.id FROM check_findings f
             WHERE f.check_name = 'split' AND f.severity = 'hold' AND f.cleared_at IS NULL
               AND f.field = 'proposal:' || r.id
             ORDER BY f.id)) AS hold_ids
  FROM allocation_proposals_raw r
)
SELECT x.id, x.account_key, x.created_at, x.cash_date, x.cash, x.placed, x.waits,
       CASE WHEN x.status = 'open' AND x.hold_ids IS NOT NULL THEN 'held' ELSE x.status END AS status,
       x.closed_at, x.closed_cash, x.notes, x.sources, x.checked_at, x.hold_ids
FROM x;

CREATE VIEW allocation_lines AS
SELECT l.proposal_id, l.symbol,
       CASE WHEN p.status = 'held' THEN NULL ELSE l.dollars END AS dollars,
       l.target_gap, l.band, l.tilt, l.reasons, l.sources,
       CASE WHEN p.status = 'held' THEN 1 ELSE 0 END AS held
FROM allocation_lines_raw l
JOIN allocation_proposals p ON p.id = l.proposal_id;
