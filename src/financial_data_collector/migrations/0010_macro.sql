-- 0010: economic numbers from FRED (inflation, the Fed's range, the 10-year yield, unemployment).
-- One row per series and observation date, in FRED's own units. A revision overwrites the row.
-- Claude OS reads this table read-only: keep the names and columns.

CREATE TABLE macro_observations (
    series_id  TEXT NOT NULL,
    obs_date   TEXT NOT NULL,
    value      REAL NOT NULL,
    fetched_at TEXT NOT NULL,
    PRIMARY KEY (series_id, obs_date)
);
