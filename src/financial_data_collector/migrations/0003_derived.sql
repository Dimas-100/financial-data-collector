-- 0003: derived tables (rebuilt by the `derive` sync step) and TTM / history views.

CREATE TABLE holdings_daily (
  as_of_date    TEXT NOT NULL,
  account_id    INTEGER NOT NULL REFERENCES accounts(id),
  symbol        TEXT NOT NULL,
  units         REAL NOT NULL,
  close         REAL,
  market_value  REAL,
  basis         TEXT NOT NULL,            -- snapshot | reconstructed
  PRIMARY KEY (as_of_date, account_id, symbol)
);
CREATE INDEX idx_holdings_daily_symbol ON holdings_daily (symbol, as_of_date);

CREATE TABLE cash_daily (
  as_of_date  TEXT NOT NULL,
  account_id  INTEGER NOT NULL REFERENCES accounts(id),
  amount      REAL NOT NULL,
  basis       TEXT NOT NULL,
  PRIMARY KEY (as_of_date, account_id)
);

CREATE TABLE lots (
  id             INTEGER PRIMARY KEY AUTOINCREMENT,
  account_id     INTEGER NOT NULL REFERENCES accounts(id),
  symbol         TEXT NOT NULL,
  open_date      TEXT NOT NULL,
  units_open     REAL NOT NULL,
  units_left     REAL NOT NULL,
  cost_total     REAL NOT NULL,
  cost_per_unit  REAL NOT NULL,
  cost_known     INTEGER NOT NULL DEFAULT 1,
  source_tx_id   INTEGER
);
CREATE INDEX idx_lots_symbol ON lots (account_id, symbol, open_date);

CREATE TABLE realized_gains (
  id              INTEGER PRIMARY KEY AUTOINCREMENT,
  account_id      INTEGER NOT NULL REFERENCES accounts(id),
  symbol          TEXT NOT NULL,
  sell_date       TEXT NOT NULL,
  units           REAL NOT NULL,
  proceeds        REAL,
  cost_basis      REAL,
  gain            REAL,
  first_lot_date  TEXT,
  holding_days    INTEGER,
  cost_known      INTEGER NOT NULL DEFAULT 1,
  source_tx_id    INTEGER
);
CREATE INDEX idx_realized_symbol ON realized_gains (symbol, sell_date);

CREATE TABLE reconciliation (
  as_of_date       TEXT NOT NULL,
  account_id       INTEGER NOT NULL REFERENCES accounts(id),
  symbol           TEXT NOT NULL,
  projected_units  REAL NOT NULL,
  snapshot_units   REAL NOT NULL,
  diff             REAL NOT NULL,
  PRIMARY KEY (as_of_date, account_id, symbol)
);

CREATE TABLE valuation_daily (
  symbol           TEXT NOT NULL,
  date             TEXT NOT NULL,
  close            REAL NOT NULL,
  shares           REAL,
  market_cap       REAL,
  revenue_ttm      REAL,
  net_income_ttm   REAL,
  ocf_ttm          REAL,
  fcf_ttm          REAL,
  eps_ttm          REAL,
  pe               REAL,
  ps               REAL,
  p_fcf            REAL,
  dividends_12m    REAL,
  dividend_yield   REAL,
  ttm_period_end   TEXT,
  available_from   TEXT,
  PRIMARY KEY (symbol, date)
);

-- Trailing twelve months per line item: flow kinds sum four consecutive quarters,
-- instant kinds pass the current quarter's value through.
CREATE VIEW financial_line_items_ttm AS
WITH q AS (
  SELECT li.cik, li.line_item, li.period_end, li.value, li.filed, cm.kind
  FROM financial_line_items li
  JOIN (SELECT line_item, MIN(kind) AS kind FROM concept_map GROUP BY line_item) cm
    ON cm.line_item = li.line_item
  WHERE li.period_kind = 'quarter'
),
w AS (
  SELECT cik, line_item, period_end, kind, value, filed,
         SUM(value)      OVER win AS sum4,
         COUNT(*)        OVER win AS n4,
         MIN(period_end) OVER win AS first_end,
         MAX(filed)      OVER win AS filed4
  FROM q
  WINDOW win AS (PARTITION BY cik, line_item ORDER BY period_end ROWS BETWEEN 3 PRECEDING AND CURRENT ROW)
)
SELECT cik, line_item, period_end,
       CASE WHEN kind IN ('instant', 'shares') THEN value ELSE sum4 END AS value,
       CASE WHEN kind IN ('instant', 'shares') THEN 1 ELSE n4 END AS n_quarters,
       CASE WHEN kind IN ('instant', 'shares') THEN filed ELSE filed4 END AS available_from
FROM w
WHERE kind IN ('instant', 'shares')
   OR (n4 = 4 AND julianday(period_end) - julianday(first_end) BETWEEN 240 AND 300);

CREATE VIEW valuation_latest AS
SELECT v.* FROM valuation_daily v
WHERE v.date = (SELECT MAX(v2.date) FROM valuation_daily v2 WHERE v2.symbol = v.symbol);

CREATE VIEW portfolio_daily_full AS
WITH h AS (SELECT as_of_date, account_id, SUM(market_value) AS holdings_value, MIN(basis) AS hbasis
           FROM holdings_daily GROUP BY 1, 2),
     c AS (SELECT as_of_date, account_id, amount AS cash, basis AS cbasis FROM cash_daily),
     d AS (SELECT as_of_date, account_id FROM h UNION SELECT as_of_date, account_id FROM c),
     a AS (SELECT d.as_of_date, d.account_id, acc.institution,
                  COALESCE(h.holdings_value, 0) AS hv, COALESCE(c.cash, 0) AS cash,
                  CASE WHEN COALESCE(h.hbasis, 'snapshot') = 'snapshot' AND COALESCE(c.cbasis, 'snapshot') = 'snapshot'
                       THEN 'snapshot' ELSE 'reconstructed' END AS basis
           FROM d
           JOIN accounts acc ON acc.id = d.account_id
           LEFT JOIN h ON h.as_of_date = d.as_of_date AND h.account_id = d.account_id
           LEFT JOIN c ON c.as_of_date = d.as_of_date AND c.account_id = d.account_id)
SELECT as_of_date,
       SUM(hv) AS holdings_value,
       SUM(cash) AS cash,
       SUM(hv + cash) AS total,
       SUM(CASE WHEN institution = 'fidelity' THEN hv + cash ELSE 0 END) AS fidelity_total,
       CASE WHEN MIN(basis) = 'reconstructed' THEN 'reconstructed' ELSE 'snapshot' END AS basis,
       COUNT(*) AS accounts
FROM a
GROUP BY as_of_date;
