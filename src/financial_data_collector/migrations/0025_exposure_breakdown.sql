-- 0025: the breakdown of the owner's two Fidelity accounts that Mr. Buffett's book page draws. Read over the
-- read-only SQLite connection by claude-os, which cannot import this repo's Python; the names here are its contract
-- (claude_os/buffett_book.py, and the design claude-os docs/superpowers/specs/2026-11-08-mr-buffett-page-design.md).
--
-- exposure_breakdown_raw is DERIVED, rebuilt from scratch by the derive step beside exposure_figures_raw, one row
-- per (dimension, base, bucket):
--   dimension  asset (stocks / bonds / cash), sector, country (stocks only), company (direct + through funds),
--              crash (a published market fall applied to an allocation like his), fee (the weighted expense ratio)
--   base       'fidelity_rolled_up' or 'brokerage' (only sector rows use the brokerage). The Webull book is in NO
--              row: the owner's rule (2026-08-26) keeps it out of every percentage.
--   value      dollars (a fee row: the yearly dollars); NULL when it could not be computed
--   pct        the share of the base, in percent (a crash row: the published fall; a fee row: the weighted ratio)
--   line_pct   the plan's sector weight, on the brokerage's sector rows only; NULL elsewhere
--   direct     company rows: the dollars held directly (value - direct reaches it through funds)
--   look_through_complete / coverage_pct - 0 with the measured coverage on anything built from top-ten fund
--              holdings (the company rows): those values are FLOORS. Sector and country use the funds' complete
--              published maps, so they are complete.

CREATE TABLE exposure_breakdown_raw (
  dimension TEXT NOT NULL CHECK (dimension IN ('asset', 'sector', 'country', 'company', 'crash', 'fee')),
  base TEXT NOT NULL CHECK (base IN ('brokerage', 'fidelity_rolled_up')),
  bucket TEXT NOT NULL,
  value REAL,
  pct REAL,
  line_pct REAL,
  direct REAL,
  look_through_complete INTEGER NOT NULL DEFAULT 1 CHECK (look_through_complete IN (0, 1)),
  coverage_pct REAL,
  note TEXT,
  date TEXT,
  sources TEXT NOT NULL DEFAULT '[]',
  PRIMARY KEY (dimension, base, bucket)
);

CREATE VIEW exposure_breakdown AS
SELECT dimension, base, bucket, value, pct, line_pct, direct, look_through_complete, coverage_pct, note, date, sources
FROM exposure_breakdown_raw;
