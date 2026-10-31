-- 0016: earnings history and estimate snapshots. One earnings_events row per company per report: the day comes from
-- SEC (the 8-K with item 2.02, "Results of Operations", whose accession is kept) when SEC lists one, else from Yahoo;
-- EPS expected, reported and the surprise come from Yahoo (the surprise in percent, as Yahoo gives it: 6.74 = +6.74%).
-- time_of_day is New York time: before_open, during, after_close, or NULL when only the day is known. The *_source
-- columns say who gave each part. estimate_snapshots keeps Yahoo's EPS estimates (this quarter, next quarter, this
-- year, next year) once per New York day; Yahoo keeps no history of them, so it starts the day the step first runs.
-- securities.last_earnings_fetch is the New York day the company was last asked about; securities.sic is SEC's
-- industry code from the same submissions list.

CREATE TABLE earnings_events (
    symbol       TEXT NOT NULL,
    report_date  TEXT NOT NULL,
    time_of_day  TEXT CHECK (time_of_day IN ('before_open', 'during', 'after_close')),
    accession    TEXT,                       -- the 8-K (item 2.02) that dates it, as SEC writes it
    eps_expected REAL,
    eps_reported REAL,
    surprise_pct REAL,                       -- surprise in percent, as Yahoo gives it (6.74 = +6.74%)
    date_source  TEXT NOT NULL CHECK (date_source IN ('sec', 'yahoo')),
    time_source  TEXT,
    eps_source   TEXT,
    PRIMARY KEY (symbol, report_date)
);

CREATE TABLE estimate_snapshots (
    symbol     TEXT NOT NULL,
    date       TEXT NOT NULL,
    period     TEXT NOT NULL CHECK (period IN ('0q', '+1q', '0y', '+1y')),
    eps_avg    REAL,
    eps_low    REAL,
    eps_high   REAL,
    analysts   INTEGER,
    source     TEXT NOT NULL,
    fetched_at TEXT NOT NULL,
    PRIMARY KEY (symbol, date, period)
);

ALTER TABLE securities ADD COLUMN last_earnings_fetch TEXT;
ALTER TABLE securities ADD COLUMN sic INTEGER;
