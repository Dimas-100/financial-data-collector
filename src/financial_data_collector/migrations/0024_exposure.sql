-- 0024: the exposure figures Mr. Buffett's instrument panel reads, and the price contract a consumer in another
-- repo can reach. Both are read over the read-only SQLite connection by claude-os, which cannot import this
-- repo's Python; the names here are its contract (claude_os/bull_panel.py, claude_os/bull_portfolio.py).
--
-- exposure_figures_raw is DERIVED (rebuilt from scratch by the derive step, a row already right not rewritten),
-- one row per figure of the investing spec's section 5. Two things travel with every figure because a number
-- without them is read against the wrong line:
--   base   - 'brokerage' or 'fidelity_rolled_up'. The same holdings read 58% of the brokerage and about 65%
--            rolled up across both Fidelity accounts. The Webull book is in NEITHER: the owner's standing rule
--            (2026-08-26) lets the household TOTAL include it and keeps concentration math out of it.
--   look_through_complete / coverage_pct - 0 with the measured coverage means the value is a FLOOR, because the
--            fund holdings behind it account for only that share of the funds. Only a figure that actually reads
--            constituent weights may say 0: a figure that reads none is complete, and must not borrow a coverage
--            number it did not use.
-- line_pct is the owner's own plan line, computed from the targets file so it moves as the plan is revised. It is
-- NULL where the rulebook draws no line (stacked exposure, the crash cost): inventing one there would create
-- philosophy, which the spec forbids, and the panel drops a line sent for those two.
--
-- trading_days_raw + trading_days answer the other half: NYSE trading days by rule (check/calendar.py writes the
-- raw table, so no day the calendar rejects can enter) that also HAVE A FINAL BAR. prices.final = 0 marks a bar
-- fetched before its close (migration 0011), and the newest date is exactly where a partial bar lives, so a
-- calendar that did not filter on it would hand a consumer the partial bar this view exists to eliminate. A day
-- with no bar at all is left out too: the calendar is a day axis for closes, and a day fdc has not fetched yet
-- would send a consumer looking for one.

CREATE TABLE exposure_figures_raw (
  figure TEXT PRIMARY KEY CHECK (figure IN ('top_three', 'sector_spread', 'stacked', 'ai_capex', 'crash_cost')),
  base TEXT NOT NULL CHECK (base IN ('brokerage', 'fidelity_rolled_up')),
  value REAL,                            -- percent for a pct figure, dollars for a usd one; NULL: not computable
  line_pct REAL,                         -- the plan's line; NULL where the rulebook draws none, or no plan was read
  unit TEXT NOT NULL CHECK (unit IN ('pct', 'usd')),
  look_through_complete INTEGER NOT NULL DEFAULT 1 CHECK (look_through_complete IN (0, 1)),
  coverage_pct REAL,                     -- the measured share of the funds behind the figure; NULL when none read
  note TEXT,                             -- what the number means and what it does not; shown beside the meter
  date TEXT,                             -- the positions date the figure was computed from
  sources TEXT NOT NULL DEFAULT '[]'
);

CREATE VIEW exposure_figures AS
SELECT figure, base, value, line_pct, unit, look_through_complete, coverage_pct, note, date, sources
FROM exposure_figures_raw;

CREATE TABLE trading_days_raw (
  day TEXT PRIMARY KEY                   -- an NYSE trading day by rule, within the span of the prices on record
);

-- prices is keyed (symbol, date), so a date-only lookup was a full scan; the view joins on exactly that.
CREATE INDEX idx_prices_date_final ON prices (date, final);

CREATE VIEW trading_days AS
SELECT d.day AS day, COUNT(*) AS final_bars
FROM trading_days_raw d JOIN prices p ON p.date = d.day AND p.final = 1
GROUP BY d.day;
