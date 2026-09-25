-- 0004: financial_line_items.first_filed (when a period was first public). The table is
-- rebuilt by the derive step, so no backfill is needed. The TTM view now dates availability
-- by first_filed instead of the latest re-reporting filing.

ALTER TABLE financial_line_items ADD COLUMN first_filed TEXT NOT NULL DEFAULT '';

DROP VIEW IF EXISTS financial_line_items_ttm;
CREATE VIEW financial_line_items_ttm AS
WITH q AS (
  SELECT li.cik, li.line_item, li.period_end, li.value,
         COALESCE(NULLIF(li.first_filed, ''), li.filed) AS public_from, cm.kind
  FROM financial_line_items li
  JOIN (SELECT line_item, MIN(kind) AS kind FROM concept_map GROUP BY line_item) cm
    ON cm.line_item = li.line_item
  WHERE li.period_kind = 'quarter'
),
w AS (
  SELECT cik, line_item, period_end, kind, value, public_from,
         SUM(value)        OVER win AS sum4,
         COUNT(*)          OVER win AS n4,
         MIN(period_end)   OVER win AS first_end,
         MAX(public_from)  OVER win AS public4
  FROM q
  WINDOW win AS (PARTITION BY cik, line_item ORDER BY period_end ROWS BETWEEN 3 PRECEDING AND CURRENT ROW)
)
SELECT cik, line_item, period_end,
       CASE WHEN kind IN ('instant', 'shares') THEN value ELSE sum4 END AS value,
       CASE WHEN kind IN ('instant', 'shares') THEN 1 ELSE n4 END AS n_quarters,
       CASE WHEN kind IN ('instant', 'shares') THEN public_from ELSE public4 END AS available_from
FROM w
WHERE kind IN ('instant', 'shares')
   OR (n4 = 4 AND julianday(period_end) - julianday(first_end) BETWEEN 240 AND 300);
