-- 0015: fund snapshots. One row per fund per New York day with the valuation figures Yahoo gives for a fund: trailing
-- and forward P/E, dividend yield and expense ratio, the last two as fractions (a yield of 1.21% is 0.0121). History
-- starts the day the funds step first runs; Yahoo keeps none. `source` says where the numbers came from: 'yfinance',
-- or 'proxy: SPY via yfinance' when the fund has no P/E or yield of its own and an ETF tracking the same index stands
-- in ([fund_proxies] in config.toml). securities.last_fund_fetch is the New York day the fund was last asked about.

CREATE TABLE fund_snapshots (
    symbol         TEXT NOT NULL,
    date           TEXT NOT NULL,
    trailing_pe    REAL,
    forward_pe     REAL,
    dividend_yield REAL,
    expense_ratio  REAL,
    source         TEXT NOT NULL,
    fetched_at     TEXT NOT NULL,
    PRIMARY KEY (symbol, date)
);

ALTER TABLE securities ADD COLUMN last_fund_fetch TEXT;
