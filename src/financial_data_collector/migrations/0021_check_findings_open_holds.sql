-- 0021: open holds answered from an index. The valuation_latest and valuation_scores views look up the open holds of a
-- row's symbol (severity 'hold', not cleared) for every row and column they show; without an index each lookup scanned
-- every finding the checker ever recorded. The index holds only open holds, so it stays small as findings clear.

CREATE INDEX idx_check_findings_open_holds ON check_findings (symbol) WHERE severity = 'hold' AND cleared_at IS NULL;
