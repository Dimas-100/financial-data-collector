# financial-data-collector

A free, local SQLite warehouse for your brokerage positions. One command collects what you hold
each day, what it is worth, every transaction, daily prices, and the full as-reported financial
statements of every company you own, so you can analyze it with SQL, pandas, or Claude.

- **Free.** SQLite (in Python), SEC EDGAR (no key), yfinance (no key) or Tiingo (free key).
- **Local.** One file, `data/warehouse.db`. Your positions never leave your machine; the only
  outbound calls are price and filing lookups for your symbols (and SEC sees the contact line you set).
- **Yours.** Drop a Fidelity "Portfolio Positions" export in `inbox/` and run `fdc sync`.
- **History that accumulates.** Positions are stored per day; SEC facts keep every filing's value, so
  you can see what a company reported at the time and how it was restated.
- **Claude-ready.** A read-only MCP server lets Claude Desktop query the database directly.

## Quick start

```bash
git clone https://github.com/Dimas-100/financial-data-collector.git
cd financial-data-collector
python -m venv .venv
.venv\Scripts\activate            # macOS/Linux: source .venv/bin/activate
pip install -e ".[dev]"
fdc init
```

Then:

1. Open `.env` and set `SEC_USER_AGENT=Your Name you@example.com` (SEC requires it). Optionally
   add a free Tiingo token; without one, prices come from yfinance.
2. On fidelity.com: Accounts, Positions, Download. Save the CSV into `inbox/`.
   Optional: Accounts, Activity and Orders, Download, for transaction history. If that file has no
   `Account` column, import it explicitly: `fdc import path\to\History.csv --account "My Brokerage"`.
3. Run `fdc sync`. The first run pulls five years of prices and every SEC filing for your companies.
   Later runs are incremental. Run it whenever you like, or schedule it.

```bash
fdc status                                            # last run per step, staleness, row counts
fdc query "select * from portfolio_daily order by 1 desc limit 10"
fdc query --csv "select * from financials_annual where symbol='AAPL'" > aapl.csv
```

## What gets stored

| Table / view | What it holds |
|---|---|
| `position_snapshots`, `cash_balances` | one row per day, account and symbol; cash separately |
| `transactions` | buys, sells, dividends, reinvestments, contributions, withdrawals, fees; de-duplicated across re-imports |
| `prices` | daily close, adjusted close, dividends and splits per symbol |
| `sec_facts` | every XBRL fact each held company ever filed (all forms, all periods) |
| `financial_line_items` | those facts shaped into income statement, balance sheet and cash-flow line items, annual and quarterly |
| `financials_annual`, `financials_quarterly` | one wide row per company per period, with margins and free cash flow |
| `positions_latest`, `holdings_history`, `account_values_daily`, `portfolio_daily` | the shapes most questions start from |
| `financials_ttm`, `valuation_daily`, `valuation_latest` | trailing-twelve-month statements and daily P/E, P/S, P/FCF, market cap and yield, using only filings available on each date |
| `holdings_daily`, `cash_daily`, `portfolio_daily_full` | the portfolio replayed from your first transaction, re-anchored on every real snapshot (`basis` tells you which) |
| `lots`, `realized_gains`, `reconciliation` | FIFO cost-basis lots, gains per sale, and dates where the replay disagrees with a snapshot (splits, missing rows) |
| `sync_runs`, `sync_status` | what ran, when, and whether it worked |

Quarterly cash-flow items are computed from year-to-date filings where companies only report
year-to-date; fourth quarters are derived from the full year. A quarterly row whose line items include
any computed value carries `has_derived_items = 1`; `financial_line_items.is_derived` says which ones.
Funds (ETFs, mutual funds) get positions and prices only; they file no statements.

Replayed history before your first snapshot assumes the transaction history starts at account opening; for an
account with a partial history (a broker feed that only goes back a year) treat `reconstructed` cash as approximate.
Stock splits are not applied during replay; `reconciliation` shows where units drift, and every real snapshot
re-anchors the series. A snapshot fetched before the market open (before 13:30 UTC) is the state at the start of
its day, so that day's trades land on top of it; one fetched later (or a CSV, whose time is unknown) already
contains them and anchors after.

See `docs/QUERIES.md` for a cookbook.

## Claude Desktop

```bash
fdc mcp --print-config
```

Paste the printed block into `claude_desktop_config.json` (Claude Desktop: Settings, Developer,
Edit Config), restart Claude Desktop, and ask it anything about your portfolio. The server is
read-only: it opens the database in read-only mode and refuses anything but SELECT.

Claude Code needs no setup: the database is a file, and `fdc query` works from the terminal.
`AGENTS.md` tells coding agents where everything is.

## Scheduling (Windows)

```powershell
.\scripts\schedule_sync.ps1          # at logon (+20 min) and every 12 hours
.\scripts\schedule_sync.ps1 -Remove
```

Logs go to `%LOCALAPPDATA%\financial-data-collector\sync.log`. On macOS/Linux use cron or launchd
to run `fdc --root /path/to/repo sync`.

## Feeding another app

`fdc export --dir <folder>` writes `prices.json`, `dividends.json` and `fundamentals.json` (the shapes the
investing cockpit in this warehouse reads). Set `[export.cockpit]` in `config.toml` to do it on every sync.

## Other brokers and sources

The universal input is Fidelity's positions CSV. A second adapter ingests the JSON produced by a
SnapTrade-based export (see `config.example.toml`, section `sources.snaptrade`); it is skipped when
the folder does not exist. Adding a broker means writing one adapter that returns
`models.Snapshot`; see `src/financial_data_collector/adapters/`.

## Privacy

`data/`, `inbox/`, `config.toml`, `.env` and `watchlist.txt` are gitignored. The test suite fails if
git ever tracks a database, a CSV outside the synthetic fixtures, or an env file. Account numbers in
Fidelity exports are discarded on import and never stored.

Output is colored and tabular in a terminal and plain text in logs and pipes; set `NO_COLOR=1` to force plain.

## Development

```bash
.venv\Scripts\python -m pytest       # fast, offline
```

MIT licensed. Built for one person's Fidelity book first; issues and adapters for other brokers are welcome.
