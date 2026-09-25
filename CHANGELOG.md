# Changelog

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
