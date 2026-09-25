# Query cookbook

Run any of these with `fdc query "<sql>"`, in Datasette, in pandas (`pd.read_sql`), or through the
Claude Desktop `query` tool. Dates are ISO text, so string comparison works.

## Portfolio

```sql
-- household value by day (and Fidelity-only)
SELECT as_of_date, total, fidelity_total FROM portfolio_daily ORDER BY as_of_date;

-- what I hold right now, by account
SELECT account, symbol, quantity, market_value, unrealized_pnl FROM positions_latest ORDER BY account, market_value DESC;

-- one symbol's exposure over time, all accounts combined
SELECT as_of_date, quantity, market_value FROM holdings_history WHERE symbol = 'AAPL' ORDER BY as_of_date;

-- month-end exposure per symbol
SELECT substr(as_of_date, 1, 7) AS month, symbol, market_value
FROM holdings_history h
WHERE as_of_date = (SELECT MAX(as_of_date) FROM holdings_history h2 WHERE substr(h2.as_of_date,1,7) = substr(h.as_of_date,1,7))
ORDER BY month, market_value DESC;
```

## Cash flows

```sql
-- contributions per month
SELECT substr(trade_date, 1, 7) AS month, SUM(amount) AS contributed
FROM transactions WHERE type = 'contribution' GROUP BY 1 ORDER BY 1;

-- dividends received per symbol per year
SELECT substr(trade_date, 1, 4) AS year, symbol, SUM(amount) AS dividends
FROM transactions WHERE type = 'dividend' GROUP BY 1, 2 ORDER BY 1, 3 DESC;
```

## Prices

```sql
-- total return over the stored history (adjusted close)
SELECT symbol, MIN(date) AS first, MAX(date) AS last,
       ROUND((MAX(CASE WHEN date = (SELECT MAX(date) FROM prices p2 WHERE p2.symbol = p.symbol) THEN adj_close END)
             / MAX(CASE WHEN date = (SELECT MIN(date) FROM prices p2 WHERE p2.symbol = p.symbol) THEN adj_close END) - 1) * 100, 1) AS pct
FROM prices p GROUP BY symbol ORDER BY pct DESC;
```

## Financial statements

```sql
-- annual income statement summary
SELECT symbol, fiscal_year, revenue, gross_margin, operating_margin, net_income, eps_diluted, fcf
FROM financials_annual WHERE symbol = 'AAPL' ORDER BY fiscal_year;

-- quarterly revenue and cash flow, marking computed fourth quarters
SELECT symbol, period_end, fiscal_quarter, revenue, ocf, is_derived_q4
FROM financials_quarterly WHERE symbol = 'AAPL' ORDER BY period_end;

-- what a company reported for a period at the time, and how it was restated later
SELECT accn, filed, form, value FROM sec_facts
WHERE cik = (SELECT cik FROM securities WHERE symbol = 'AAPL')
  AND concept = 'Revenues' AND period_start = '2024-01-01' AND period_end = '2024-12-31'
ORDER BY filed;

-- my exposure next to the company's quarterly revenue
SELECT q.period_end, q.revenue, h.market_value
FROM financials_quarterly q
JOIN holdings_history h ON h.symbol = q.symbol
 AND h.as_of_date = (SELECT MAX(as_of_date) FROM holdings_history h2 WHERE h2.symbol = q.symbol AND h2.as_of_date <= q.period_end)
WHERE q.symbol = 'AAPL' ORDER BY q.period_end;
```

## Health

```sql
SELECT * FROM sync_status;
SELECT * FROM sync_runs ORDER BY id DESC LIMIT 20;
```
