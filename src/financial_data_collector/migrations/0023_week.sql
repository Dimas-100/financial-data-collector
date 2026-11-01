-- 0023: what the window's "This week" page reads, all calculated here.
--
-- allocation_proposals_raw gains the week score of each proposal (spec addendum 2026-10-31): account_type (the
-- warehouse's accounts.account_type of the configured account), score (0-10), verdict ('go' | 'half' | 'hold'),
-- fraction (1, 0.5 or 0: the share of the cash the lines place), parts (JSON list of {sign, code, symbol, text}),
-- weeks_waited, forced ('waited' | 'under_and_cheap' | NULL) and waiting_since (the cash date the held-back money has
-- waited from). Proposals written before this migration have them NULL. The view gains the same columns, after the
-- ones it had (columns are only ever added to it); allocation_lines is recreated unchanged because it reads the view.
--
-- holding_bands_raw and allocation_targets_raw are DERIVED, rebuilt from scratch by the engine step under the split's
-- gate: one band per symbol held in a configured account, and one row per targets-file entry per configured account
-- with plan_split's own gap and top. holding_bands applies holds opened since: an open hold on a score row that
-- decided the band (the valuation_scores view says 'held' for it) blanks the band and lists hold_ids.

ALTER TABLE allocation_proposals_raw ADD COLUMN account_type TEXT;
ALTER TABLE allocation_proposals_raw ADD COLUMN score INTEGER;
ALTER TABLE allocation_proposals_raw ADD COLUMN verdict TEXT CHECK (verdict IN ('go', 'half', 'hold'));
ALTER TABLE allocation_proposals_raw ADD COLUMN fraction REAL;
ALTER TABLE allocation_proposals_raw ADD COLUMN parts TEXT NOT NULL DEFAULT '[]';
ALTER TABLE allocation_proposals_raw ADD COLUMN weeks_waited INTEGER;
ALTER TABLE allocation_proposals_raw ADD COLUMN forced TEXT CHECK (forced IN ('waited', 'under_and_cheap'));
ALTER TABLE allocation_proposals_raw ADD COLUMN waiting_since TEXT;

DROP VIEW allocation_lines;
DROP VIEW allocation_proposals;

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
       x.closed_at, x.closed_cash, x.notes, x.sources, x.checked_at, x.hold_ids,
       x.account_type, x.score, x.verdict, x.fraction, x.parts, x.weeks_waited, x.forced, x.waiting_since
FROM x;

CREATE VIEW allocation_lines AS
SELECT l.proposal_id, l.symbol,
       CASE WHEN p.status = 'held' THEN NULL ELSE l.dollars END AS dollars,
       l.target_gap, l.band, l.tilt, l.reasons, l.sources,
       CASE WHEN p.status = 'held' THEN 1 ELSE 0 END AS held
FROM allocation_lines_raw l
JOIN allocation_proposals p ON p.id = l.proposal_id;

CREATE TABLE holding_bands_raw (
  symbol TEXT PRIMARY KEY,
  date TEXT,                         -- the symbol's newest score date (NULL when it has no score row)
  band TEXT NOT NULL CHECK (band IN ('cheap', 'usual', 'premium')),
  reasons TEXT NOT NULL DEFAULT '',  -- holding_band's codes, comma-joined
  deciding TEXT NOT NULL DEFAULT '', -- the metrics of the deciding score rows, comma-joined
  sources TEXT NOT NULL DEFAULT '[]',
  checked_at TEXT
);

CREATE VIEW holding_bands AS
WITH x AS (
  SELECT b.*,
         (SELECT group_concat(s.hold_ids, ',') FROM valuation_scores s
          WHERE s.symbol = b.symbol AND s.date = b.date AND s.status = 'held' AND s.hold_ids IS NOT NULL
            AND instr(',' || b.deciding || ',', ',' || s.metric || ',') > 0) AS hold_ids
  FROM holding_bands_raw b
)
SELECT x.symbol, x.date,
       CASE WHEN x.hold_ids IS NULL THEN x.band END AS band,
       x.reasons, x.sources, x.checked_at, x.hold_ids
FROM x;

CREATE TABLE allocation_targets_raw (
  account_key TEXT NOT NULL,         -- the targets file's name for the account, never an fdc label
  account_type TEXT,
  symbol TEXT NOT NULL,
  rule TEXT NOT NULL,                -- hold | contributions | trades | unknown
  kind TEXT NOT NULL,                -- pct_of_account | pct_of_brokerage | direct_passive_ratio
  target REAL NOT NULL,              -- percent (15.0 = 15%), or a plain ratio for direct_passive_ratio
  held_value REAL NOT NULL,          -- dollars held (a ratio entry: held directly, household-wide)
  gap REAL CHECK (gap >= 0),         -- dollars to the target, either way; NULL when it cannot be known (note)
  side TEXT CHECK (side IN ('short', 'over', 'on')),
  top REAL,                          -- dollars to the band top; negative when past it
  date TEXT,                         -- the account's newest positions date (its cash date when it has none)
  sources TEXT NOT NULL DEFAULT '[]',
  note TEXT,                         -- why gap is NULL: no_look_through
  PRIMARY KEY (account_key, symbol)
);

CREATE VIEW allocation_targets AS
SELECT account_key, account_type, symbol, rule, kind, target, held_value, gap, side, top, date, sources, note
FROM allocation_targets_raw;
