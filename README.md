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
| `fund_snapshots` | for each fund (ETF, mutual fund) one row per day: trailing and forward P/E, dividend yield, expense ratio (yields and expense ratios as fractions: 0.0121 is 1.21%) and where they came from (`yfinance`, or `proxy: SPY via yfinance` when a fund Yahoo has no P/E or yield for borrows an ETF's; set `[fund_proxies]` in `config.toml`). A yield or expense ratio out of range, or whose two Yahoo readings are 10 times or more apart (a unit Yahoo changed), is left empty and the step says so. History starts the day the `funds` step first runs; Yahoo keeps none |
| `earnings_events` | each company's earnings reports: the day (from SEC's 8-K with item 2.02, "Results of Operations", when SEC lists one; else Yahoo's), when in the day (`before_open`, `during`, `after_close`, New York time, when Yahoo knows it), EPS expected and reported and the surprise in percent (from Yahoo), and which source gave each part. An 8-K filed on Yahoo's day or up to six calendar days later (`MATCH_DAYS` in `collectors/earnings.py`: four business days is legal) is the same report. Reports that SEC's and Yahoo's lists no longer show stay as stored, except a Yahoo date with no reported EPS that Yahoo no longer lists (it moved the report) once no 8-K can still come for it. Without `SEC_USER_AGENT` the days are Yahoo's |
| `estimate_snapshots` | Yahoo's EPS estimates for this quarter, next quarter, this year and next year (`0q`, `+1q`, `0y`, `+1y`: average, low, high, number of analysts), one row per company, day and period. History starts the day the `earnings` step first runs |
| `current_reports` | every 8-K and 8-K/A SEC's submissions list shows for a company: accession, form, filing date and item numbers as SEC writes them (`2.02,9.01`). Kept as first stored. A sync also keeps each company's 8-Ks (from the answer the `earnings` step already gets) and the news feed's headlines, for drop causes |
| `news_headlines` | the sibling news feed's (`<investing content_dir>/data/news.json`) headlines by symbol: date, headline, outlet, publish time and a link when it is a plain https one; never the article text or tags. Kept by the `ingest` step as first seen; no feed is nothing |
| `sec_facts` | every XBRL fact each held company ever filed (all forms, all periods) |
| `financial_line_items` | those facts shaped into income statement, balance sheet and cash-flow line items, annual and quarterly |
| `financials_annual`, `financials_quarterly` | one wide row per company per period, with margins and free cash flow |
| `positions_latest`, `holdings_history`, `account_values_daily`, `portfolio_daily` | the shapes most questions start from |
| `financials_ttm`, `valuation_daily`, `valuation_latest` | trailing-twelve-month statements and daily P/E, P/S, P/FCF, market cap and yield, using only filings available on each date |
| `holdings_daily`, `cash_daily`, `portfolio_daily_full` | the portfolio replayed from your first transaction, re-anchored on every real snapshot (`basis` tells you which) |
| `lots`, `realized_gains`, `reconciliation` | FIFO cost-basis lots, gains per sale, and dates where the replay disagrees with a snapshot (splits, missing rows) |
| `unexplained_daily` | each day's change in an account that no transaction explains, such as a deposit the balance shows before the activity feed posts it: money moved, not growth |
| `check_runs`, `check_findings` | the checker's runs and what it found; open `hold` findings blank the matching fields of `valuation_latest` (see "Checker") |
| `valuation_scores`, `valuation_scores_raw` | the valuation engine's scores: one row per symbol and metric at the symbol's newest final close, with its status, what it was built from (`inputs`, `sources`) and when the checker covered the data (`checked_at`); read the view, which also applies holds opened since (see "Valuation engine") |
| `sync_runs`, `sync_status` | what ran, when, and whether it worked |

Quarterly cash-flow items are computed from year-to-date filings where companies only report
year-to-date; fourth quarters are derived from the full year. A quarterly row whose line items include
any computed value carries `has_derived_items = 1`; `financial_line_items.is_derived` says which ones.
Funds (ETFs, mutual funds) get positions, prices and a daily `fund_snapshots` row; they file no statements.
Stocks (and foreign filers) are asked about once a New York day for their earnings history and estimates; the
`earnings` step also records SEC's industry code in `securities.sic`. yfinance reports a failed request by logging it
and answering empty, so an error it logs while asking counts as a failed ask, asked again at the next sync; a fund that
comes back with no figures, or a company with no estimates, after a day it had some is asked again too, not given up
for the day. A `funds` or `earnings` run with nothing left to ask (all asked today) is `skipped`; `fdc sync` exits 1
only when every network step that ran failed, and a skipped one is left out.

**Sectors.** Each stock and foreign filer gets a `sector` (one of the eleven) and a `sector_source` saying where it came from: the sibling feed's, else the SEC SIC code's (`seeds/sic_sector.csv`). The sector's benchmark is `[benchmarks] sectors`. Funds have no sector. A feed that cannot be read at all (missing, not JSON, no `by_ticker`) leaves every sector that came from it as it is and the step says "classifications feed unreadable; feed sectors kept"; one odd entry is skipped alone; a company the feed no longer names falls back to SIC. The pass reads only stored data and the sibling file, so it runs at the end of the `earnings` step on every sync.

**Benchmarks.** A sync also collects prices for the market fund (`SPY`) and the eleven Select Sector SPDRs (`XLC`, `XLY`,
`XLP`, `XLE`, `XLF`, `XLV`, `XLI`, `XLB`, `XLRE`, `XLK`, `XLU`), so a later measure has something to compare a move against.
They are marked in `securities.benchmark`, collected like any symbol, and never counted as holdings. `fdc export` leaves
them out unless you also hold them or list them, and the checker compares their newest close with a second provider's
(`prices2`). Set `[benchmarks]` in `config.toml` to change the market fund or to replace the sector list; remove a
benchmark there and its mark goes at the next sync. A benchmark is never parked; a symbol parked before it became one
is unparked.

Replayed history before your first snapshot assumes the transaction history starts at account opening; for an
account with a partial history (a broker feed that only goes back a year) treat `reconstructed` cash as approximate.
Stock splits are not applied during replay; `reconciliation` shows where units drift, and every real snapshot
re-anchors the series. A snapshot fetched before the market open (before 13:30 UTC) is the state at the start of
its day, so that day's trades land on top of it; one fetched later (or a CSV, whose time is unknown) already
contains them and anchors after.

See `docs/QUERIES.md` for a cookbook.

## Checker

`fdc check` is an independent second opinion on the numbers `derive` writes into `valuation_daily`. It is code of its
own (`check/`) and imports nothing from the code it checks. The sync runs it after `export`, just before the
`engine` step; a daily task is the backstop (below). Run on its own, `fdc check` first waits for a sync that is running, as a second sync does. The
thresholds are in `[check]` of `config.toml`. What it looks at:

- **fresh**: statements only. Each symbol's newest valuation row (status `ok` or `stale`): the end of its TTM
  statements against the row's price date. More than `fresh_warn_months` (5) apart is a warning, more than
  `fresh_hold_months` (15) a hold on every value (`*`), both from the day the threshold was crossed;
- **impossible**: negative shares, or a company ratio (P/E, P/S, P/FCF, market cap) set on something that is not a
  company (an ETF, fund, mutual fund, money market, crypto or foreign filer), on the newest valuation row; and a
  one-day move in the close of more than `max_daily_move` (40%) with no split that day, within the last 30 final bars;
- **gaps**: prices only. A held symbol with no final close on an NYSE trading day of the last 60 (today left out),
  counted from its first price and up to its newest final bar, so a day no sync has fetched yet is not a gap. A warning;
- **closes**: a stopped price feed. For each held symbol and each benchmark (money markets and crypto left out), the NYSE
  trading days after its newest final close and before today. `close_warn_days` (2) of them is a warning on the close,
  `close_hold_days` (5) a hold on every value. A symbol with only a bar fetched before the close, or with no price from any
  provider at all, is a warning too;
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

## Valuation engine

The sync's last step, `engine`, turns the warehouse into scores: no network, no AI, only stored data. It runs after
the checker on purpose: it builds only from a derive that a full, error-free check run has covered (the newest ok
`derive` and a `check_runs` row with that `derive_run_id` and no error). Until one has, it writes nothing, leaves the
scores it had, and the step is `skipped` with "waiting for a check run to cover derive <id>".

It reads the checker's open holds as missing values. A held close (a `close` hold, or a `*` hold from `prices2`,
`impossible` or `closes`) is left out of every window, after the closes are adjusted over every final bar, so a
held split day still adjusts the closes before it; when one covers the newest day, every price-shape row is `held`
(no value; the hold messages are the reason). A `*` hold from `fresh`, `filings` or `figures` is about statements and
leaves prices alone.

What it scores: every security that is not parked, not a money market or crypto, has a final close, and is not only a
benchmark (a benchmark you also hold is scored). Each gets one row per metric in `valuation_scores_raw` at its newest
final close, starting with the price shape, on split- and dividend-adjusted closes:

- `off_high_52w`: the close against the highest close of the 365 days ending that day (needs a close 300 days back);
- `vs_200d`: the close against the mean of the last 200 closes (needs 200);
- `week_vs_market`: its return over the last 5 trading bars minus the market fund's (`[benchmarks] market`, SPY)
  between the same two dates; `unsupported` ("no SPY close on <date>") when the fund lacks either date;
- `week_vs_sector`: the same against its sector's fund (`[benchmarks] sectors`); `unsupported` ("no sector") when it
  has no sector or the config names no fund for it.

Each stock with an SEC CIK and `valuation_daily` rows also gets five scores against its own history:

- `pe`, `p_fcf`, `ps`: the day's P/E, price / free cash flow and price / sales from `valuation_daily`;
- `ev_ebit`: (market cap + long-term debt - cash - short-term investments) / operating income, each day from the
  TTM statements in force that day (`unsupported` "no debt or cash figure in its filings" when the filings give no
  long-term debt or cash, "operating income not positive" on an operating loss; missing short-term investments
  count 0);
- `dividend_yield`: trailing 12-month dividends / close.

Each carries `percentile_5y`: where today's value sits among the company's own daily values of the last
`history_years` (5) calendar years, as a mid-rank percentile (values below it plus half of those equal to it, over
all of them, today's included). Only `ok` days count; a day a hold covered is left out. A percentile needs history
reaching `min_history_days` (365) back; until then the row is `not_enough_history` ("<N> days of its own history; a
band needs 365") and still shows its value. The `band` reads the percentile: for P/E, P/FCF, P/S and EV/EBIT, where
high means expensive, under `cheap_percentile` (25) is `cheap`, over `premium_percentile` (75) is `premium`, between
them (both included) `usual`; for the dividend yield, where high means cheap, it is mirrored (over the 75th `cheap`,
under the 25th `premium`). A band says how the company is priced against its own past, not against other companies
or the market. All four numbers are `[engine]` settings in `config.toml`, and each row's `sources` lists them with the
newest close, the 10-Q or 10-K the statements came from (with its SEC link) and the span of history it was ranked
in. The newest `valuation_daily` row decides the rest: a `stale` or `unsupported` row makes every score
`unsupported` with its reason; a value a hold blanks (a hold on the metric's own column, a `*` hold from any check,
or a `close` hold, since every one of these is built on the close) makes it `held`; a value that cannot be computed is
`unsupported` with why ("earnings not positive", "free cash flow not positive", "no revenue", "no dividend").

Each stock with an SEC CIK also gets ten fundamentals, with no percentile or band:

- `implied_growth`: the yearly free-cash-flow growth over 10 years, then `terminal_growth` (2.5%) forever, discounted
  at `discount_rate` (9%), that makes the cash flows worth today's market cap (newest `valuation_daily` row; bisection
  between -50% and +100%, 4 places); `unsupported` "free cash flow not positive" or "needs more than 100% or less
  than -50% a year".
- `fcf_growth_5y`, `revenue_growth_5y`: compound yearly growth from the `financials_annual` row whose fiscal year is
  5 lower to the newest (`not_enough_history` without that row; `unsupported` when either is missing or not positive).
- `fcf_margin_change`, `op_margin_change`: free cash flow / revenue and operating income / revenue of the TTM row in
  force, minus the same of the TTM row ending 350-380 days earlier (`not_enough_history` without one).
- `net_debt_ebitda`: (long-term debt - cash - short-term investments) / (operating income + depreciation and
  amortization) of the TTM row in force; `unsupported` when EBITDA is not positive or debt or cash is missing.
- `buyback_yield`: TTM buybacks / the newest market cap; no buybacks line counts 0 (companies report the concept only
  when they bought back).
- `last_surprise`: the surprise (percent) of the newest earnings report on or before the day that has one; its
  sources are the 8-K (when SEC dated the report) and Yahoo's date.
- `estimate_change_30d`: (now - then) / |then| of this year's (`0y`) EPS estimate, now the newest snapshot, then the
  newest 30-45 days before it; `unsupported` "newest estimate snapshot is from <date>" when now is more than 7 days
  before the day; `not_enough_history` ("estimate snapshots start <date>") until a snapshot is 30 days old, or "no
  estimate snapshot 30-45 days before <date>" when the one before is older than 45 days; `unsupported` when an
  earnings report fell between the two (the fiscal year may have rolled) or then is 0.
- `insider_net_90d`: buys minus sells among the sibling `<investing content_dir>/data/insider.json` feed's Form 4
  entries filed in the 90 days ending the day (`mixed` and `other` count 0); each counted filing's SEC link is built
  from the CIK and accession, never taken from the feed; `unsupported` "insider feed not found", "insider feed has no
  date", "insider feed is from <date>" (its `as_of` more than 7 days before the day) or "not in the insider feed".
  The feed is read once per run.

The newest valuation row decides the four built on it (`implied_growth`, the margins, `net_debt_ebitda`,
`buyback_yield`): not on the day, `stale` or `unsupported` makes them `unsupported` with its reason. Inputs:
`implied_growth` names `market_cap`, `fcf_ttm` and `close`, `buyback_yield` `market_cap` and `close`,
`fcf_margin_change` `revenue_ttm` and `fcf_ttm`, `op_margin_change` `revenue_ttm`, so a hold on one of those (the
`figures` check holds `revenue_ttm` and `fcf_ttm` field by field) holds them; the rest name none, so only a `*` hold
holds them. Either way at build time as in the view.

A row's `status` is `ok`, `held`, `unsupported` or `not_enough_history`; `inputs` names the `valuation_daily` columns
it was built from (`,close,` for the price shape; a company history score names its own column and the close it is
built on, `,pe,close,` for P/E, `,market_cap,close,` for EV/EBIT, so a `close` hold opened after the build holds it in
the view as it would have at build time); `sources` is a JSON list of where each number came from (`price`, `filing`,
`feed`, `config`, `derived`); `checked_at` is the finish time of the check run that covered the data. Read the view
`valuation_scores`: it applies, in the way `valuation_latest` does, a hold opened after the build. An open hold on the
symbol covering the row's date whose field is `score:<metric>`, one of the row's inputs, or `*` (on a price row only
from a price check) blanks `value`, `percentile_5y` and `band`, makes an `ok` row `held` with the hold messages as its
reason, and lists the hold in `hold_ids`. The raw table is never changed by a hold.

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
