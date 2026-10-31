-- 0019: the valuation engine's scores. valuation_scores_raw is what the engine step built: one row per symbol and
-- metric at the symbol's newest final close, with the checker's holds that were open at build time already applied
-- (held inputs read as missing). `inputs` names the valuation_daily columns a score was built from as ",pe," (",close,"
-- for a price row), `sources` is a JSON list of {kind, label, date, ref[, period][, url]}, `checked_at` the finish
-- time of the check run that covered the derive it was built from. It is rebuilt by the engine, never a source of truth.
--
-- valuation_scores serves it with the holds open NOW applied, in 0013's style: an open hold (severity 'hold', not
-- cleared) on the same symbol whose dates cover the row's date blanks value, percentile_5y and band, and makes the
-- status 'held' (only when it was 'ok': another status keeps its own, already value-less, status and reason) when
-- its field is 'score:' || metric (the scores check's findings), or one of the row's inputs, or '*' and either the row
-- is not a price row or the hold comes from a price check (prices2, impossible, closes): a '*' hold from fresh,
-- filings or figures is about statements and leaves a price row alone, as valuation_latest always shows `close`.
-- hold_ids lists every such hold (NULL when none). The raw table is never changed by a hold. Columns are only ever
-- added to this view.

CREATE TABLE valuation_scores_raw (
  symbol TEXT NOT NULL,
  date TEXT NOT NULL,
  metric TEXT NOT NULL,
  value REAL,
  percentile_5y REAL,
  band TEXT CHECK (band IN ('cheap', 'usual', 'premium')),
  status TEXT NOT NULL CHECK (status IN ('ok', 'held', 'unsupported', 'not_enough_history')),
  status_reason TEXT,
  inputs TEXT NOT NULL DEFAULT '',
  sources TEXT NOT NULL DEFAULT '[]',
  checked_at TEXT,
  PRIMARY KEY (symbol, metric)
);

CREATE VIEW valuation_scores AS
WITH x AS (
  SELECT r.*,
         (SELECT group_concat(id, ',') FROM
            (SELECT f.id FROM check_findings f
             WHERE f.severity = 'hold' AND f.cleared_at IS NULL AND f.symbol = r.symbol
               AND r.date BETWEEN f.date_from AND COALESCE(f.date_to, '9999-12-31')
               AND (f.field = 'score:' || r.metric
                    OR instr(r.inputs, ',' || f.field || ',') > 0
                    OR (f.field = '*' AND (r.inputs <> ',close,' OR f.check_name IN ('prices2', 'impossible', 'closes'))))
             ORDER BY f.id)) AS hold_ids,
         (SELECT group_concat(message, '; ') FROM
            (SELECT f.message FROM check_findings f
             WHERE f.severity = 'hold' AND f.cleared_at IS NULL AND f.symbol = r.symbol
               AND r.date BETWEEN f.date_from AND COALESCE(f.date_to, '9999-12-31')
               AND (f.field = 'score:' || r.metric
                    OR instr(r.inputs, ',' || f.field || ',') > 0
                    OR (f.field = '*' AND (r.inputs <> ',close,' OR f.check_name IN ('prices2', 'impossible', 'closes'))))
             ORDER BY f.id)) AS hold_messages
  FROM valuation_scores_raw r
)
SELECT x.symbol, x.date, x.metric,
       CASE WHEN x.hold_ids IS NULL THEN x.value END AS value,
       CASE WHEN x.hold_ids IS NULL THEN x.percentile_5y END AS percentile_5y,
       CASE WHEN x.hold_ids IS NULL THEN x.band END AS band,
       CASE WHEN x.status = 'ok' AND x.hold_ids IS NOT NULL THEN 'held' ELSE x.status END AS status,
       CASE WHEN x.status = 'ok' AND x.hold_ids IS NOT NULL THEN x.hold_messages ELSE x.status_reason END AS status_reason,
       x.inputs, x.sources, x.checked_at, x.hold_ids
FROM x;
