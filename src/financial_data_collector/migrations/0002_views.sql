-- 0002: analysis-friendly views over the raw tables.

CREATE VIEW positions_latest AS
SELECT p.as_of_date, a.label AS account, a.institution, a.account_type,
       p.symbol, s.description, s.asset_type,
       p.quantity, p.price, p.market_value, p.cost_basis_total, p.avg_cost, p.unrealized_pnl, p.source
FROM position_snapshots p
JOIN accounts a ON a.id = p.account_id
JOIN securities s ON s.symbol = p.symbol
WHERE p.as_of_date = (SELECT MAX(p2.as_of_date) FROM position_snapshots p2 WHERE p2.account_id = p.account_id);

CREATE VIEW holdings_history AS
SELECT as_of_date, symbol,
       SUM(quantity) AS quantity,
       SUM(market_value) AS market_value,
       SUM(cost_basis_total) AS cost_basis_total,
       COUNT(*) AS accounts
FROM position_snapshots
GROUP BY as_of_date, symbol;

CREATE VIEW account_values_daily AS
WITH h AS (SELECT as_of_date, account_id, SUM(market_value) AS holdings_value FROM position_snapshots GROUP BY 1, 2),
     c AS (SELECT as_of_date, account_id, SUM(amount) AS cash FROM cash_balances GROUP BY 1, 2),
     d AS (SELECT as_of_date, account_id FROM h UNION SELECT as_of_date, account_id FROM c)
SELECT d.as_of_date, a.label AS account, a.institution, a.account_type,
       COALESCE(h.holdings_value, 0) AS holdings_value,
       COALESCE(c.cash, 0) AS cash,
       COALESCE(h.holdings_value, 0) + COALESCE(c.cash, 0) AS total
FROM d
JOIN accounts a ON a.id = d.account_id
LEFT JOIN h ON h.as_of_date = d.as_of_date AND h.account_id = d.account_id
LEFT JOIN c ON c.as_of_date = d.as_of_date AND c.account_id = d.account_id;

CREATE VIEW portfolio_daily AS
SELECT as_of_date,
       SUM(total) AS total,
       SUM(CASE WHEN institution = 'fidelity' THEN total ELSE 0 END) AS fidelity_total,
       SUM(holdings_value) AS holdings_value,
       SUM(cash) AS cash,
       COUNT(*) AS accounts
FROM account_values_daily
GROUP BY as_of_date;

CREATE VIEW sync_status AS
SELECT r.step, r.status, r.rows_written, r.message, r.started_at, r.finished_at,
       ROUND((julianday('now') - julianday(r.finished_at)) * 24, 1) AS age_hours
FROM sync_runs r
WHERE r.id = (SELECT MAX(r2.id) FROM sync_runs r2 WHERE r2.step = r.step);
