# Changelog

## Unreleased

- Identical transactions in one import are kept as separate rows (three $100 deposits on the same day
  were stored as one). The first keeps its old dedupe key, so re-importing a file still changes nothing.
- The replay no longer decides by the clock alone whether a snapshot already holds its day's rows. A
  statement's own units say which same-day trades it holds, and its cash says which deposits and other cash
  rows have landed; the fetch time only breaks ties. Fixes a buy counted twice when a feed stamped
  start-of-day was live, a deposit dropped until the balance showed it (a cash-only statement has no fetch
  time), and an instant deposit added on top of a morning statement that already held it. A deposit on its
  way counts in the account from its date; one the statements never show stops counting after seven days.

## 0.3.0 - 2026-09-25

- Polished terminal output (rich): a live progress line during `fdc sync`, aligned and
  formatted tables for `status` and `query`, colored per-file results for `import`, a
  "Next steps" panel after `init`, and one-line errors with a hint instead of tracebacks.
  Output is plain text when not attached to a terminal (logs, pipes) and honours NO_COLOR.
- Collectors and sync steps accept an optional progress callback.

## 0.2.0 - 2026-09-25

- `derive` sync step: trailing-twelve-month statements (`financials_ttm`), point-in-time valuation
  ratios per price date (`valuation_daily`, `valuation_latest`), replayed daily history from the first
  transaction (`holdings_daily`, `cash_daily`, `portfolio_daily_full`), FIFO `lots`, `realized_gains`
  and a `reconciliation` table for replay drift.
- `export` sync step and `fdc export`: writes `prices.json`, `dividends.json` and `fundamentals.json`
  for the investing cockpit in its own shapes; investing stops fetching those itself.
- Universe: `[sources.investing]` adds theses, watchlist and basket tickers.

## 0.1.0 - 2026-09-25

First release: SQLite warehouse, Fidelity positions/history CSV adapters, SnapTrade JSON adapter,
Tiingo/yfinance prices, SEC EDGAR company facts with derived statements, `fdc` CLI, read-only MCP
server for Claude Desktop, Windows scheduler scripts.
