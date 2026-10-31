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
   Economic numbers: get a free key at fred.stlouisfed.org (My Account, API Keys), put
   `FRED_API_KEY=` in `.env`. Without it the `macro` step is skipped.
2. Connect a brokerage (`fdc connect snaptrade`) or a bank (`fdc connect simplefin`) with your own key:
   see [docs/connections.md](docs/connections.md). Or, on fidelity.com: Accounts, Positions, Download, and
   save the CSV into `inbox/`. Optional: Accounts, Activity and Orders, Download, for transaction history;
   if that file has no `Account` column, import it explicitly:
   `fdc import path\to\History.csv --account "My Brokerage"`.
3. Run `fdc sync`. The first run pulls five years of prices and every SEC filing for your companies.
   Later runs are incremental. Run it whenever you like, or schedule it.

```bash
fdc status                                            # last run per step, staleness, row counts
fdc query "select * from portfolio_daily order by 1 desc limit 10"
fdc query --csv "select * from financials_annual where symbol='AAPL'" > aapl.csv
```

## Watch it run

```bash
fdc console            # a live screen: what runs, where its data comes from, when it ran
fdc console --once     # the same, printed once (what a pipe or a log gets)
```

`scripts\open_console.ps1` opens the live screen in a window of its own and returns at once, so a shortcut,
another program or an assistant can launch it for you.

Three tabs: **Overview** (each step's last result, how stale it is, what is running right now, and the
scheduled task's triggers, last and next run on Windows), **Sources** (the inbox, the broker export folder, each
connection, the price provider, SEC EDGAR, the watchlist, the research tickers folder and the cockpit feed
files, each with its state and last activity) and **Runs** (the last seven days of runs with who started
each, how long it took and each step's message). Every tab is a table with a detail strip under it: `up` and
`down` move the highlight and the strip shows the whole row, so long messages and full paths live there. The
band at the top is green when idle, cyan while a sync runs, yellow when your own run is queued and red when a
run did not finish, and it names the next scheduled run. Keys: `1` `2` `3` or `left` `right` switch tabs,
`tab` moves between a table and its strip, `r` runs the whole sync, `s` runs the step under the cursor,
`?` shows every key, `ctrl+p` opens the command palette, `q` quits. A run started from the console carries
on after you quit and is logged like any other. While a sync is alive, the keys wait; a sync started elsewhere
waits its turn too. Older history: `fdc query "select * from sync_runs order by id desc"`.

## What gets stored

| Table / view | What it holds |
|---|---|
| `position_snapshots`, `cash_balances` | one row per day, account and symbol; cash separately |
| `transactions` | buys, sells, dividends, reinvestments, contributions, withdrawals, fees; de-duplicated across re-imports |
| `prices` | daily close, adjusted close, dividends and splits per symbol, for what you hold, your watchlist and the benchmarks (see "Benchmarks") |
| `macro_observations` | FRED economic numbers in FRED's own units: `CPIAUCSL`, `CPILFESL` (inflation indexes), `DFEDTARL`, `DFEDTARU` (the Fed's target range), `DGS10` (10-year yield), `UNRATE`; one row per series and date, a revision overwrites its row |
| `sec_facts` | every XBRL fact each held company ever filed (all forms, all periods) |
| `financial_line_items` | those facts shaped into income statement, balance sheet and cash-flow line items, annual and quarterly |
| `financials_annual`, `financials_quarterly` | one wide row per company per period, with margins and free cash flow |
| `positions_latest`, `holdings_history`, `account_values_daily`, `portfolio_daily` | the shapes most questions start from |
| `financials_ttm`, `valuation_daily`, `valuation_latest` | trailing-twelve-month statements and daily P/E, P/S, P/FCF, market cap and yield, using only filings available on each date |
| `holdings_daily`, `cash_daily`, `portfolio_daily_full` | the portfolio replayed from your first transaction, re-anchored on every real snapshot (`basis` tells you which) |
| `lots`, `realized_gains`, `reconciliation` | FIFO cost-basis lots, gains per sale, and dates where the replay disagrees with a snapshot (splits, missing rows) |
| `unexplained_daily` | each day's change in an account that no transaction explains, such as a deposit the balance shows before the activity feed posts it: money moved, not growth |
| `check_runs`, `check_findings` | the checker's runs and what it found; open `hold` findings blank the matching fields of `valuation_latest` (see "Checker") |
| `sync_runs`, `sync_status` | what ran, when, and whether it worked |

Quarterly cash-flow items are computed from year-to-date filings where companies only report
year-to-date; fourth quarters are derived from the full year. A quarterly row whose line items include
any computed value carries `has_derived_items = 1`; `financial_line_items.is_derived` says which ones.
Funds (ETFs, mutual funds) get positions and prices only; they file no statements.

**Benchmarks.** A sync also collects prices for the market fund (`SPY`) and the eleven Select Sector SPDRs (`XLC`, `XLY`,
`XLP`, `XLE`, `XLF`, `XLV`, `XLI`, `XLB`, `XLRE`, `XLK`, `XLU`), so a later measure has something to compare a move against.
They are marked in `securities.benchmark`, collected like any symbol, and never counted as holdings. `fdc export` leaves
them out unless you also hold them or list them, and the checker compares their newest close with a second provider's
(`prices2`). Set `[benchmarks]` in `config.toml` to change the market fund or to replace the sector list; remove a
benchmark there and its mark goes at the next sync.

Replayed history before your first snapshot assumes the transaction history starts at account opening; for an
account with a partial history (a broker feed that only goes back a year) treat `reconstructed` cash as approximate.
Stock splits are not applied during replay; `reconciliation` shows where units drift, and every real snapshot
re-anchors the series. A snapshot fetched before the market open (before 13:30 UTC) is the state at the start of
its day, so that day's trades land on top of it; one fetched later (or a CSV, whose time is unknown) already
contains them and anchors after.

See `docs/QUERIES.md` for a cookbook.

## Checker

`fdc check` is an independent second opinion on the numbers `derive` writes into `valuation_daily`. It is code of its
own (`check/`) and imports nothing from the code it checks. The sync runs it as its last step; a daily task is the
backstop (below). Run on its own, `fdc check` first waits for a sync that is running, as a second sync does. The
thresholds are in `[check]` of `config.toml`. What it looks at:

- **fresh**: statements only. Each symbol's newest valuation row (status `ok` or `stale`): the end of its TTM
  statements against the row's price date. More than `fresh_warn_months` (5) apart is a warning, more than
  `fresh_hold_months` (15) a hold on every value (`*`), both from the day the threshold was crossed;
- **impossible**: negative shares, or a company ratio (P/E, P/S, P/FCF, market cap) set on something that is not a
  company (an ETF, fund, mutual fund, money market, crypto or foreign filer), on the newest valuation row; and a
  one-day move in the close of more than `max_daily_move` (40%) with no split that day, within the last 30 final bars;
- **gaps**: prices only. A held symbol with no final close on an NYSE trading day of the last 60 (today left out),
  counted from its first price and up to its newest final bar, so a day no sync has fetched yet is not a gap. A warning;
- **recompute**: P/E, P/S, P/FCF, yield and market cap worked out again from the row's own inputs (rows with status
  `ok`). A stored value more than `recompute_tolerance` (1%) away is held, and so is one stored when its own inputs
  cannot give one (a P/E with no EPS);
- **filings**: a filing SEC lists for a company that the warehouse does not have (without `SEC_USER_AGENT`, SEC is
  not asked: one note, and the open holds stay);
- **prices2** and **figures**: a second price source and outside statement figures (Yahoo, Tiingo), compared with ours.

It records into two tables. `check_runs` has one row per run (when, how many checks, how many holds and warnings it
found, and any check that raised). `check_findings` has one row per problem: `check_name`, `symbol`, `field` (a
`valuation_daily` column, or `*` for every value column), the days it covers (`date_from`, `date_to`), `severity`
(`hold`, `warn` or `info`), a plain-words `message`, `evidence` as JSON, and `first_seen` / `last_seen` / `cleared_at`.
A finding the checks stop finding is cleared (never deleted); one that returns is reopened.

The open hold count (`fdc status`, the console, the sync's check line) counts every hold not yet cleared, including
holds on past days: an `impossible` one-day move holds only that day (`date_to` is the day), so it blanks nothing
once a newer row is the latest, but it stays open, and counted, while that bar is among the last 30 final bars.

`fdc check --summary` prints every open finding, holds first (`HOLD  KO  pe  from 2026-10-01  recompute: stored 25.0,
recomputed 20.0`) without running any check; `fdc status` shows the last run's age and the open hold and warning counts. The daily task logs to `%LOCALAPPDATA%\financial-data-collector\check.log`
(beside `sync.log`), so a run that crashed before it could record itself still leaves a line.

**What a hold means for a consumer.** Read `valuation_latest`, not `valuation_daily`. A hold is the checker saying a
number is probably wrong and should not be used until someone looks. In `valuation_latest` a hold blanks only what it
names: the held fields (and the ratios built on them) are `NULL`; the other values on the row are still the stored
ones, so a `NULL` is "not known", never zero. Its `status` is `held` when an open hold covers the row (the row was
otherwise `ok`); a row that was already `stale` or `unsupported` keeps that status. `hold_ids` lists the covering
`check_findings` ids, `status_reason` gives the messages, and `checked_at` is when the checker last covered the newest
derive: a run of every check with none failing (a run of `--only` some checks, or one where a check raised, is recorded
with no `derive_run_id` and leaves `checked_at` where it was). `valuation_daily` itself is never changed by a hold: it
stays the raw record, so a cleared hold brings the values straight back. Warnings and notes change nothing; they are
for a person to look at.

A hold and the row's own status can both apply, and the hold is the stricter of the two. A `fresh` hold (statements
more than 15 months older than the price) names `*`, so it blanks every value column but the close, the dividend
yield and trailing dividends included. A row that `derive` itself marked `stale` keeps its close, dividends and dividend yield (only the
statement figures are `NULL`) and keeps the status `stale`; with the `fresh` hold over it, `valuation_latest` shows
those as `NULL` too.

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

The task runs on battery too. A start that arrives while a sync is running waits for it to finish, so a
job that writes files the collector reads can end with `Start-ScheduledTask "FinancialDataCollector Sync"`
and know a sync will read them.

The checker has a backstop task of its own, for a day the sync did not run:

```powershell
.\scripts\schedule_check.ps1          # daily at 07:00: fdc check
.\scripts\schedule_check.ps1 -Remove
```

Logs go to `%LOCALAPPDATA%\financial-data-collector\sync.log`. On macOS/Linux use cron or launchd
to run `fdc --root /path/to/repo sync`.

## Feeding another app

`fdc export --dir <folder>` writes `prices.json`, `dividends.json` and `fundamentals.json` (the shapes the
investing cockpit in this warehouse reads). Set `[export.cockpit]` in `config.toml` to do it on every sync.

## Other brokers and sources

`fdc connect snaptrade` covers 35+ brokerages and `fdc connect simplefin` covers banks, cards and loans, each
with a key you hold yourself ([docs/connections.md](docs/connections.md)). Fidelity's positions CSV is the
file route. A second adapter ingests the JSON produced by a SnapTrade-based export (see `config.example.toml`,
section `sources.snaptrade`); it is skipped when the folder does not exist. Adding another file format means
writing one adapter that returns `models.Snapshot`; see `src/financial_data_collector/adapters/`.

## Privacy

`data/`, `inbox/`, `config.toml`, `.env` and `watchlist.txt` are gitignored. The test suite fails if
git ever tracks a database, a CSV outside the synthetic fixtures, or an env file. Account numbers in
Fidelity exports are discarded on import and never stored. Connection keys live in your computer's key
store, never in the repo; account numbers and service ids from a connection are never stored, and bank
transactions are never requested.

Output is colored and tabular in a terminal and plain text in logs and pipes; set `NO_COLOR=1` to force plain.

## Development

```bash
.venv\Scripts\python -m pytest       # fast, offline
```

MIT licensed. Built for one person's Fidelity book first; issues and adapters for other brokers are welcome.
