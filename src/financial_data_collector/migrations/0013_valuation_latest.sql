-- 0013: valuation_latest applies the checker's open holds. A hold (severity 'hold', not cleared) whose
-- dates cover a row and whose field is a value column (or '*') makes that column NULL here, and the row's
-- status 'held' (only when it was 'ok': a stale or unsupported row keeps its own, already-NULL, status).
-- hold_ids lists every open hold covering the row; checked_at is when the checker last covered the newest
-- derive. valuation_daily itself is never changed by a hold. Columns are only ever added to this view.

DROP VIEW IF EXISTS valuation_latest;

CREATE VIEW valuation_latest AS
WITH x AS (
  SELECT v.*,
         (SELECT group_concat(id, ',') FROM
            (SELECT f.id FROM check_findings f
             WHERE f.severity = 'hold' AND f.cleared_at IS NULL AND f.symbol = v.symbol AND v.date BETWEEN f.date_from AND COALESCE(f.date_to, '9999-12-31') ORDER BY f.id)) AS hold_ids,
         (SELECT group_concat(message, '; ') FROM
            (SELECT f.message FROM check_findings f
             WHERE f.severity = 'hold' AND f.cleared_at IS NULL AND f.symbol = v.symbol AND v.date BETWEEN f.date_from AND COALESCE(f.date_to, '9999-12-31') ORDER BY f.id)) AS hold_messages
  FROM valuation_daily v
  WHERE v.date = (SELECT MAX(v2.date) FROM valuation_daily v2 WHERE v2.symbol = v.symbol)
)
SELECT x.symbol, x.date, x.close,
       CASE WHEN EXISTS (SELECT 1 FROM check_findings f WHERE f.severity = 'hold' AND f.cleared_at IS NULL AND f.symbol = x.symbol AND x.date BETWEEN f.date_from AND COALESCE(f.date_to, '9999-12-31') AND f.field IN ('shares', '*')) THEN NULL ELSE x.shares END AS shares,
       CASE WHEN EXISTS (SELECT 1 FROM check_findings f WHERE f.severity = 'hold' AND f.cleared_at IS NULL AND f.symbol = x.symbol AND x.date BETWEEN f.date_from AND COALESCE(f.date_to, '9999-12-31') AND f.field IN ('market_cap', '*')) THEN NULL ELSE x.market_cap END AS market_cap,
       CASE WHEN EXISTS (SELECT 1 FROM check_findings f WHERE f.severity = 'hold' AND f.cleared_at IS NULL AND f.symbol = x.symbol AND x.date BETWEEN f.date_from AND COALESCE(f.date_to, '9999-12-31') AND f.field IN ('revenue_ttm', '*')) THEN NULL ELSE x.revenue_ttm END AS revenue_ttm,
       CASE WHEN EXISTS (SELECT 1 FROM check_findings f WHERE f.severity = 'hold' AND f.cleared_at IS NULL AND f.symbol = x.symbol AND x.date BETWEEN f.date_from AND COALESCE(f.date_to, '9999-12-31') AND f.field IN ('net_income_ttm', '*')) THEN NULL ELSE x.net_income_ttm END AS net_income_ttm,
       CASE WHEN EXISTS (SELECT 1 FROM check_findings f WHERE f.severity = 'hold' AND f.cleared_at IS NULL AND f.symbol = x.symbol AND x.date BETWEEN f.date_from AND COALESCE(f.date_to, '9999-12-31') AND f.field IN ('ocf_ttm', '*')) THEN NULL ELSE x.ocf_ttm END AS ocf_ttm,
       CASE WHEN EXISTS (SELECT 1 FROM check_findings f WHERE f.severity = 'hold' AND f.cleared_at IS NULL AND f.symbol = x.symbol AND x.date BETWEEN f.date_from AND COALESCE(f.date_to, '9999-12-31') AND f.field IN ('fcf_ttm', '*')) THEN NULL ELSE x.fcf_ttm END AS fcf_ttm,
       CASE WHEN EXISTS (SELECT 1 FROM check_findings f WHERE f.severity = 'hold' AND f.cleared_at IS NULL AND f.symbol = x.symbol AND x.date BETWEEN f.date_from AND COALESCE(f.date_to, '9999-12-31') AND f.field IN ('eps_ttm', '*')) THEN NULL ELSE x.eps_ttm END AS eps_ttm,
       CASE WHEN EXISTS (SELECT 1 FROM check_findings f WHERE f.severity = 'hold' AND f.cleared_at IS NULL AND f.symbol = x.symbol AND x.date BETWEEN f.date_from AND COALESCE(f.date_to, '9999-12-31') AND f.field IN ('pe', '*')) THEN NULL ELSE x.pe END AS pe,
       CASE WHEN EXISTS (SELECT 1 FROM check_findings f WHERE f.severity = 'hold' AND f.cleared_at IS NULL AND f.symbol = x.symbol AND x.date BETWEEN f.date_from AND COALESCE(f.date_to, '9999-12-31') AND f.field IN ('ps', '*')) THEN NULL ELSE x.ps END AS ps,
       CASE WHEN EXISTS (SELECT 1 FROM check_findings f WHERE f.severity = 'hold' AND f.cleared_at IS NULL AND f.symbol = x.symbol AND x.date BETWEEN f.date_from AND COALESCE(f.date_to, '9999-12-31') AND f.field IN ('p_fcf', '*')) THEN NULL ELSE x.p_fcf END AS p_fcf,
       CASE WHEN EXISTS (SELECT 1 FROM check_findings f WHERE f.severity = 'hold' AND f.cleared_at IS NULL AND f.symbol = x.symbol AND x.date BETWEEN f.date_from AND COALESCE(f.date_to, '9999-12-31') AND f.field IN ('dividends_12m', '*')) THEN NULL ELSE x.dividends_12m END AS dividends_12m,
       CASE WHEN EXISTS (SELECT 1 FROM check_findings f WHERE f.severity = 'hold' AND f.cleared_at IS NULL AND f.symbol = x.symbol AND x.date BETWEEN f.date_from AND COALESCE(f.date_to, '9999-12-31') AND f.field IN ('dividend_yield', '*')) THEN NULL ELSE x.dividend_yield END AS dividend_yield,
       x.ttm_period_end, x.available_from,
       CASE WHEN x.status = 'ok' AND x.hold_ids IS NOT NULL THEN 'held' ELSE x.status END AS status,
       CASE WHEN x.status = 'ok' AND x.hold_ids IS NOT NULL THEN x.hold_messages ELSE x.status_reason END AS status_reason,
       x.hold_ids,
       (SELECT cr.finished_at FROM check_runs cr
        WHERE cr.derive_run_id = (SELECT s.run_id FROM sync_runs s WHERE s.step = 'derive' AND s.status = 'ok'
                                  ORDER BY s.id DESC LIMIT 1)
        ORDER BY cr.finished_at DESC, cr.run_id DESC LIMIT 1) AS checked_at
FROM x;
