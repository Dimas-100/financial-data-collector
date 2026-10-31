-- 0020: drops of a final close and their causes, rebuilt by the engine step (derive/moves.py). A row is a day down
-- [engine] drop_day (span '1d', trigger drop_day), 5 held bars down drop_week (span '5d', recorded on the first day it
-- crosses), or a day excess_sd of the symbol's own 1-year standard deviations of daily excess moves over the market
-- fund below it (trigger excess), on closes adjusted over every final bar with held closes then left out. `causes` is
-- a JSON list of {kind, text, sources} in the order earnings, filing, market, sector, news, or the one
-- {"kind": "none", "text": "no cause found"}; `sources` a JSON list of {kind, label, date, ref[, url]}; `checked_at`
-- the finish time of the check run that covered the derive it was built from. Never a source of truth.

CREATE TABLE price_moves (
  symbol TEXT NOT NULL,
  date TEXT NOT NULL,
  span TEXT NOT NULL CHECK (span IN ('1d', '5d')),
  start_date TEXT NOT NULL,               -- the close the move is measured from
  move REAL NOT NULL,                     -- -0.052 = down 5.2%
  market_symbol TEXT,
  market_move REAL,
  sector_symbol TEXT,
  sector_move REAL,
  excess_sd REAL,                         -- (move - market_move) / the symbol's 1-year sd of daily excess moves (1d only)
  triggers TEXT NOT NULL,                 -- "drop_day", "drop_week", "excess", comma-joined
  causes TEXT NOT NULL,                   -- JSON list of {kind, text, sources}, in the spec's order
  sources TEXT NOT NULL,
  checked_at TEXT,
  PRIMARY KEY (symbol, date, span)
);
