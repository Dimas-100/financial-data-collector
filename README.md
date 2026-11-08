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
| `price_moves` | drops of a final close in the last year (a day down 4%, a week down 8%, or a day 2.5 of its own standard deviations below the market) with their causes, each from a sourced fact (earnings, an 8-K, the market, the sector, headlines) or "no cause found" (see "Drops and their causes") |
| `allocation_proposals`, `allocation_lines` (views over `allocation_proposals_raw`, `allocation_lines_raw`) | the weekly split's proposals, a record kept as written: how the cash waiting in one account was split across its holdings, in whole dollars, with each line's band, reasons and sources, and the week's score, verdict and the parts that moved it (see "The weekly split"); the views apply the checker's holds. Never an order |
| `holding_bands`, `allocation_targets` (views over `holding_bands_raw`, `allocation_targets_raw`) | rebuilt by each split run: one overall band per holding of the configured accounts (its reasons and the score rows that decided it; blank while a hold covers one of those), and each targets-file entry's gap and band top in dollars, worked out as the split works them out |
| `exposure_figures` (view over `exposure_figures_raw`) | rebuilt by every `derive`: five exposure figures for the two Fidelity accounts - the three largest holdings, the sector gap to plan, the largest stacked company, the AI-capex share and what a published crash would cost - each with the `base` it is measured on (`brokerage` or `fidelity_rolled_up`), the plan's own `line_pct` (NULL where the rulebook draws none), whether its fund look-through was complete and a `note` in words (see "Exposure figures") |
| `trading_days` (view over `trading_days_raw`) | NYSE trading days by rule that also have a final price bar, with how many: the day axis a consumer should read instead of guessing one from a price file, and the answer to "is this bar the day's close?" (see "Exposure figures") |
| `sync_runs`, `sync_status` | what ran, when, and whether it worked |

Quarterly cash-flow items are computed from year-to-date filings where companies only report
year-to-date; fourth quarters are derived from the full year. A quarterly row whose line items include
any computed value carries `has_derived_items = 1`; `financial_line_items.is_derived` says which ones.
Funds (ETFs, mutual funds) get positions, prices and a daily `fund_snapshots` row; they file no statements.
A broker file does not say what a symbol is, so the `sec` step types it: a symbol of unknown type that SEC's company
ticker map does not list but its fund ticker list (`company_tickers_mf.json`, cached a week) does is an `etf`, or a
`mutual_fund` when it is five letters ending in X. Most ETFs are a series of a trust and are only on the fund list.
`[classify]` in `config.toml` overrides any of this.
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
  `close_hold_days` (5) a hold on every value. Both are dated from the newest final close itself, open-ended, so the hold
  covers the stale row in `valuation_latest`, `valuation_scores` and the engine's build. A symbol with only a bar fetched
  before the close, or with no price from any provider at all, is a warning too;
- **recompute**: P/E, P/S, P/FCF, yield and market cap worked out again from the row's own inputs (rows with status
  `ok`). A stored value more than `recompute_tolerance` (1%) away is held, and so is one stored when its own inputs
  cannot give one (a P/E with no EPS);
- **filings**: a filing SEC lists for a company that the warehouse does not have (without `SEC_USER_AGENT`, SEC is
  not asked: one note, and the open holds stay);
- **prices2** and **figures**: a second price source and outside statement figures (Yahoo, Tiingo), compared with ours
  (free cash flow against the four quarters of Yahoo's cash flow statement that end on our TTM's period end, operating
  cash flow less purchases of property and equipment as ours is; on Yahoo's wider "Capital Expenditure" line only a
  warning; Yahoo's own `freeCashflow` figure is levered and never compared);
- **scores**: the valuation engine's scores (`valuation_scores_raw`). One hold per score that is `ok` with no `sources` (or
  unreadable ones), has a source link that was not built (every link `https://`; a filing's exactly
  `https://www.sec.gov/Archives/edgar/data/<CIK>/<accession without dashes>/<accession>-index.htm`), a `percentile_5y`
  outside 0..100, or a band its percentile does not give by the checker's own rule and the `[engine]` thresholds (P/E, P/FCF,
  P/S, EV/EBIT: high is premium; yield: high is cheap; every other metric, and any row that is not `ok`, has no band). Its
  findings name the field `score:<metric>`, which the `valuation_scores` view blanks. A row it cannot read is a finding,
  never an error, and a row whose metric the engine does not know is one warning on `*` (it would otherwise be an error that keeps the engine from building). The `engine` step runs this check again alone right after it writes (its message ends "; checks: N holds,
  N warnings, N errors"); that partial run never stamps `checked_at`, and `fdc status` and the console show the last full run, not that one.
- **split**: an open weekly-split proposal (`allocation_proposals_raw`) that breaks a rule is held: its lines add up to more
  than the cash, a line is not a whole number of dollars of at least 1, a line goes to a name your targets file marks
  `hold` in that account (or whose `rebalance` is not `trades`, `contributions` or `hold`) or that the account has no
  entry for, the cash is not a number or is not the account's own USD cash row on the proposal's `cash_date` (half a cent
  apart; the account found through `[allocation.accounts]`), or it has no sources. One finding per open proposal with
  every fault in its message, field `proposal:<id>`, symbol the account's targets-file name; a closed proposal gets none.
  A missing or unreadable targets file is a warning ("cannot check hold names") while a proposal is open, and so is a
  proposal whose account has no cash row that day or is no longer in `[allocation.accounts]`. It reads the targets file
  with its own code; a number too large for a float or JSON nested too deeply never makes it fail. The `engine` step runs it alone right after the split writes ("; split checks: N holds, N
  warnings, N errors").

It records into two tables. `check_runs` has one row per run (when, how many checks, how many holds and warnings it
found, and any check that raised). `check_findings` has one row per problem: `check_name`, `symbol`, `field` (a
`valuation_daily` column, `*` for every value column, `score:<metric>` for one valuation score, or `proposal:<id>` for one weekly-split proposal), the days it covers (`date_from`, `date_to`), `severity`
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

The sync's last step, `engine`, turns the warehouse into scores and then proposes the weekly split (below): no network, no AI, only stored data. It runs after
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
`unsupported` with why ("earnings not positive", "free cash flow not positive", "no revenue"). A dividend yield of
zero is not ranked: today's is `unsupported` "no dividend in 12 months", and only the days with a positive yield make
up the history it is ranked in.

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
  from the CIK and accession, never taken from the feed; `unsupported` "insider feed not found" (no file), "insider feed
  unreadable" (there, but not JSON of the feed's shape), "insider feed has no date", "insider feed is from <date>"
  (its `as_of` more than 7 days before the day) or "not in the insider feed". The feed is read once per run.

The newest valuation row decides the four built on it (`implied_growth`, the margins, `net_debt_ebitda`,
`buyback_yield`): not on the day, `stale` or `unsupported` makes them `unsupported` with its reason. Inputs:
`implied_growth` names `market_cap`, `fcf_ttm` and `close`, `buyback_yield` `market_cap` and `close`,
`fcf_margin_change` `revenue_ttm` and `fcf_ttm`, `op_margin_change` `revenue_ttm`, so a hold on one of those (the
`figures` check holds `revenue_ttm` and `fcf_ttm` field by field) holds them; the rest name none, so only a `*` hold
holds them. Either way at build time as in the view.

Each fund (ETF, mutual fund) also gets three scores from its own daily `fund_snapshots` rows, since it files no statements:

- `pe` (Yahoo's trailing P/E) and `dividend_yield`: where the newest snapshot (dated on or before the day) sits among
  the fund's own snapshots of the last `history_years`, as a mid-rank percentile, with the same bands as a company's.
  Only snapshots with the same `source` as the newest count, so a fund that borrows an ETF's P/E through
  `[fund_proxies]` ranks the proxy's days apart from its own. History starts the day the `funds` step first ran, so a
  fund has **no band** until its own snapshots span `min_history_days` (365): the row is `not_enough_history`, shows
  its value, and says "<N> days of its own snapshots; a band needs 365". It also needs at least 200 of its own
  snapshots (`not_enough_history`, "<N> snapshots of its own; a band needs 200"; short on both, the days reason is
  the one said). Until then a fund is read by its price
  measures only (off the 52-week high, against the 200-day average, the week against the market).
- `expense_ratio`: the newest snapshot's figure as a fraction (0.0004 = 0.04%); a value only.

A fund with no snapshot is `unsupported` "no fund snapshot yet"; one whose newest snapshot is more than 7 days before
the day, "newest snapshot is from <date>"; a figure the snapshot lacks says so; a yield of zero is `unsupported` "no
dividend in 12 months", and only positive yields are ranked. No fund row names a `valuation_daily`
column, so only a `*` hold (or one on `score:<metric>`, through the view) holds it. Sources: the snapshot
(`fund_snapshots: <source>`), the history span and the settings.

A row's `status` is `ok`, `held`, `unsupported` or `not_enough_history`; `inputs` names the `valuation_daily` columns
it was built from (`,close,` for the price shape; a company history score names its own column and the close it is
built on, `,pe,close,` for P/E, `,market_cap,close,` for EV/EBIT, so a `close` hold opened after the build holds it in
the view as it would have at build time); `sources` is a JSON list of where each number came from (`price`, `filing`,
`feed`, `config`, `derived`); `checked_at` is the finish time of the check run that covered the data. Read the view
`valuation_scores`: it applies, in the way `valuation_latest` does, a hold opened after the build. An open hold on the
symbol covering the row's date whose field is `score:<metric>`, one of the row's inputs, or `*` (on a price row only
from a price check) blanks `value`, `percentile_5y` and `band`, makes an `ok` row `held` with the hold messages as its
reason, and lists the hold in `hold_ids`. The raw table is never changed by a hold.

### Drops and their causes

The same step writes `price_moves`: every drop of each scored symbol (stocks and funds alike) dated in the last
`moves_days` (365) calendar days before its newest final close, on the same adjusted closes with held closes left out
(so a split or an ex-dividend day is never a drop). A row is one of:

- `1d`, trigger `drop_day`: the close fell `drop_day` (4%) or more from the previous held close;
- `1d`, trigger `excess`: the close fell (a stock that rose while the market rose more is not a drop) and the day's
  move minus the market fund's (`[benchmarks] market`, SPY) was `excess_sd` (2.5) or more of the symbol's own standard
  deviations of such daily excess moves below zero, over the 252 bars before (needs 120 of them with the market fund's
  closes on both dates). `excess_sd` is stored on every `1d` row it can compute;
- `5d`, trigger `drop_week`: the close fell `drop_week` (8%) or more from 5 held bars back, recorded only on the first
  day it crosses.

Each row carries the market fund's and the sector fund's move between the same two dates (`market_move`,
`sector_move`; NULL when the fund lacks either close) and its `causes`, a JSON list of `{kind, text, sources}`. A cause
is stated only from a sourced fact, in this order:

1. `earnings`: a report dated after the start close and up to the day, or on the start day after the close or at an
   unknown time ("reported earnings on 2026-10-28 after the close: EPS 2.02 vs 1.89 expected (+6.9%)"); a report on
   the day itself after the close came out after the close the move ends on, so it explains the next move, not this one;
2. `filing`: an 8-K filed from the start day to the day with an item other than 2.02 (results) and 9.01 (exhibits),
   its items named ("filed an 8-K on 2026-10-28: a director or officer change"), with its SEC link;
3. `market`: the market fund fell at least two thirds as much ("the market fell 5.0% (SPY)");
4. `sector`: the sector fund did ("its sector fell 4.1% (XLK, Technology)"); a symbol with no sector has none;
5. `news`: up to three stored headlines dated from the start day to the day, newest first ("<title> (<outlet>,
   <date>)"); a headline stays stored after the feed drops it, so an old drop keeps its news;
6. none of these: `{"kind": "none", "text": "no cause found", "sources": []}`, never a guess.

`sources` lists the closes used (the symbol's and the funds') and the thresholds that fired. Rows are rebuilt each
engine run; one already as it should be is not rewritten. `price_moves` has no view that applies holds: the build
leaves held closes out, but a hold the checker finds after the build reaches the moves only at the next sync's engine
run, so until then a move may rest on a close that is now held.

### The weekly split (proposals, never an order)

After the scores and their re-check, the same step proposes how to split cash waiting in an account (its money market,
which the adapters turn into cash) across that account's holdings, from your own targets file. It is a proposal written
to the database, never an order: nothing here can trade, sell, or move money between accounts, and it never proposes
more than the waiting cash. It runs only under the engine's gate, so a sync whose check failed (or whose score re-check
had errors) opens, replaces and closes nothing; the step's message says why (`; split: score re-check failed`,
`; split: no targets file`; if the split itself fails, the step is an `error` and its re-check still runs). The checker
looks at what it writes at once (the `split` check, see "Checker"): a proposal whose lines add up to more than the cash,
are not whole dollars, go to a name your targets file marks `hold` or has no entry for in that account, whose cash is
not the account's own cash row of that day, or that carry no sources is held, and `allocation_proposals` then shows it `held` with its dollars
blank until the finding clears. Set it up in `config.toml`:

```toml
[allocation]
targets = "../investing/config/sizing-targets.json"   # no file, no proposals (the step says "split: no targets file")
fund_holdings = "../investing/fund_holdings"          # optional <FUND>.csv look-through weights
min_new = 25                       # dollars of new cash that make a proposal
min_line = 10                      # a line under this is folded into the largest line
household_days = 7                 # caps count investment accounts whose newest positions are this close to the newest
tilt = { cheap = 1.5, usual = 1.0, premium = 0.5 }

[allocation.accounts]              # targets-file account name = fdc account label (`fdc accounts` lists them)
brokerage = "Sample Brokerage"
roth = "Sample Roth"
```

Neither path is set in code: without a `targets` line there is no split, so a database never reads a file it was not
pointed at (a `config.toml` made before the split needs the `[allocation]` `targets` / `fund_holdings` lines and the
`[allocation.accounts]` table added by hand before any proposal is made). A proposal's `account_key` is the
targets-file name (`brokerage`), never the label.

**The targets file** is one JSON object. Each key that holds a list (other than `sleeves`, `caps`, `planned` and keys
starting with `_`) is an account, named as in `[allocation.accounts]`, and each entry is `{ticker, target, type, min,
max, rebalance}`; `type` is `pct_of_account` (the default), `pct_of_brokerage` (measured on, and paid to, the account
mapped as `brokerage`, wherever it is filed) or `direct_passive_ratio` (below). For the two percent kinds `target`,
`min` and `max` are percent as written (15 is 15%); for `direct_passive_ratio` they are plain ratios (1.5 is one and a
half times). With no `min`/`max` the band is 0.75x to 1.25x of the target; `rebalance` is `trades` (the default),
`contributions` or `hold`, and a name with any `hold` entry in an account is held there, even when another of its
entries says otherwise. `sleeves` (`{account, label, members, min, max, rebalance}`) and
`caps` (`{ticker, max}`, a ceiling in percent of the household) limit; `last_revised` is quoted in the sources. What the
reader cannot use (an entry with no ticker or target, or a number too large to be one) is skipped and said in the
proposal's notes. The optional `fund_holdings` folder holds one `<FUND>.csv` per fund (columns `ticker`, `name`,
`weight_pct`), which lets a fund count toward the names it holds. A symbol with a file there is a fund even when the
warehouse does not know its type.

For each account named in `[allocation.accounts]`, the waiting cash is the account's newest USD cash row, and:

- the first proposal is made once the cash is at least `min_new`;
- with a proposal open, cash `min_new` or more above its cash makes a new proposal for all of the cash, which
  replaces it (`replaced`); cash at least $1 below it means you placed it, in full or in part (`spent`); anything
  between changes nothing;
- a `buy` of a holding (not the money market) in that account dated after the proposal's cash date, and on or before
  the date of the newest cash row (so that row shows it), also means you placed it (`spent`), even when a deposit since
  then hides it from the cash; a new proposal for all of the cash then opens at once if it is at least `min_new`. A buy
  on the proposal's own cash date is not counted (it may already be in the cash the proposal was made from; a same-day
  placement after the sync is caught by the $1 rule). When the transactions feed lags behind the balances, the cash
  rules above still apply, and a late buy is caught a sync late;
- after a spent proposal, the next one needs the cash `min_new` above the baseline, the lower of the cash it closed with
  and the lowest balance since its closing day (that day included), and covers all of the cash, so money you left
  waiting joins the next deposit;
- an account with no entries in the targets file ("no targets for <name>"), with no cash row, with a cash row older
  than its newest positions (it may not show what you just placed; an account with no positions counts any row), or
  named twice on one account gets no proposal; one it already has stays open, as does one whose name left
  `[allocation.accounts]`, and the step's notes say so by the targets-file name (the notes are part of the step's
  message in `sync_runs`: `split: 1 new, 0 replaced, 0 spent (notes: ...)`).

**How a holding's band is decided.** Each holding gets one band, `cheap`, `usual` or `premium`, from its own primary
valuation scores (the `valuation_scores` view, so a hold opened since still counts): for a company `pe`, `p_fcf`,
`ev_ebit` and `ps`, for a fund `pe` and `dividend_yield`, each counted only when it is `ok` with a band. The lean is
careful: with fewer than two counted it is `usual` (a line says `one_metric` or `no_metrics`); more than half `premium`
makes it `premium`; `cheap` needs no `premium` at all and more than half `cheap`, and a company whose free-cash-flow and
operating margin changes are both known and both falling is held at `usual` (`quality_falling`; when either is not
known `cheap` stands and says `quality_unknown`); anything else is `usual` (`disagree`). The tilt then weights the
share: `cheap` 1.5, `usual` 1.0, `premium` 0.5. A line's `reasons` lists the primary metrics that did not count
(`held:`, `unsupported:`, `no_history:`, `missing:`) and the verdict (`cheap_by:pe+p_fcf`), comma-joined.

**How the cash is shared.** A percent target is measured on the account's value with the waiting cash in it (positions
plus cash): a holding's gap is its target% of that, less what it holds. Money is shared by gap times tilt, and a line can
grow past its target up to its band's top once the others are full, never past it. It never passes a sleeve's `max` (its
members in that account) or a cap, which is measured on the household: every investment account (one with positions)
whose newest positions are within `household_days` of the newest of any account, with its cash when its cash row is that
fresh too; a bank or card account with only cash is not part of it. With a look-through file a fund counts toward the
names it holds, both in a cap and in the `direct_passive_ratio` target (the gap is the target times the household's
dollars held through funds, less its direct dollars; a name no fund on file holds gets nothing, "no look-through"); a
fund with no file is counted at the largest weight of the name in any file, so missing look-through never makes room
(`look_through_assumed`); a symbol whose type the warehouse does not know and that has no file is counted the same
way, as a fund without a file (`type_unknown`). Caps hold across accounts: when a proposal is made, the lines of every
other account's open proposal count as holdings already (a proposal being replaced does not count its own old lines),
so two deposits in one week cannot together pass a cap. A sleeve's `min` is never pushed toward: the split does not sell and does not force money in.
A `hold` name or one with a misspelled rule gets nothing, and neither does a holding at or over its target. `trades` and
`contributions` are treated alike, since the split only ever adds (a line says `trades_as_contributions`). Shares are
floored to whole dollars, lines under `min_line` are folded into the largest line as far as its room allows; when no
line reaches `min_line`, all of it is folded into the largest share as far as its room allows, and waits if that is
still under `min_line`. What fits nowhere waits for next week (`placed` is the dollars of the lines, `waits` the rest:
`cash` = `placed` + `waits` to the cent).

**What a proposal says.** Each line lists its sources: the targets entry (the file's name only, never its path, its
`last_revised`, and the account the entry is filed under), the scores that decided its band, and the tilt; the proposal
lists the cash row and the targets file. A line's `reasons`, in order: the band's (`held:<metric>`,
`unsupported:<metric>`, `no_history:<metric>`, `missing:<metric>`, then `one_metric`, `no_metrics`, `premium_by:`,
`cheap_by:`, `quality_falling`, `quality_unknown` or `disagree`), then the split's: `trades_as_contributions`,
`no_scores` (no score row at all: `usual`), `type_unknown`, `look_through_assumed`, what stopped it (`sleeve_cut:<label>`,
`cap_cut:<symbol>`, `band_top`) and `folded`. Its notes say why an entry got no line (`duplicate`, `hold`, `unknown_rule`, `no_look_through`,
`at_or_over_target`, `no_cash`, `under_min`, `sleeve_cut:`, `cap_cut:`, `band_top`), what the targets file could not be
used for, and how many accounts were left out of the household for being older than `household_days`, or had a cash row
too old to count (counts, never a name). The proposals are a record, like the checker's findings: a run only adds one or
closes one, an open proposal's lines are never rewritten (new scores alone do not change it; only new cash makes a new
one), and a re-run with the same cash writes nothing. Read them in `allocation_proposals` and `allocation_lines`
(`docs/QUERIES.md` has the queries).

**The week score.** Each proposal says whether this is the week the cash goes in: a `score` from 0 to 10 (never a
probability), from where the money would go (the entries short of target the split could give money to, each weighted
by its share of the gap). A usual week starts at 7; cheap destinations add up to 3 and premium ones take up to 6, by
their share; a destination of 10% or more of the gap that dropped in the 14 days to the cash date with "no cause found"
takes 1 (at most 2); "not known" counts as usual and is said. `verdict` is `go` from `go_from` (7), `half` from
`half_from` (4), else `hold`, and the split is made for that `fraction` of the cash (1, 0.5, 0); the rest waits and
joins the next proposal. Two guards make it `go` whatever the score (`forced`): money held back for `max_wait_weeks`
(4) weeks (`waited`), and the largest-gap destinations that make up half the gap all cheap (`under_and_cheap`). `parts`
lists what moved the score in plain words; `weeks_waited` counts whole weeks since the held-back money started
waiting. A proposal that held money back is scored again once a week even when the cash has not changed. The `split`
check also holds lines over cash x fraction, or a fraction its verdict does not give.

```toml
[allocation]
go_from = 7                        # a week score this high or higher: all the waiting cash
half_from = 4                      # ... this high: half, the rest waits; under it, it all waits
max_wait_weeks = 4                 # held-back money that has waited this long goes in whatever the score
```

The same run rebuilds `holding_bands` (each held symbol's overall band, as the split reads it) and `allocation_targets`
(each entry's `gap` in dollars either way with `side` `short` / `over` / `on`, and `top`, the dollars to its band top),
even when no proposal opens; no targets file, no target rows.

## Exposure figures

Every `derive` run also publishes what a portfolio page can show without doing its own arithmetic. Two views,
both read-only to everyone else, both rebuilt from scratch each run:

`exposure_figures` holds five figures for the two Fidelity accounts, from `[allocation.accounts]`, the targets
file and the funds' published weights:

| `figure` | `base` | what it is | `line_pct` |
|---|---|---|---|
| `top_three` | `brokerage` | percent of the brokerage in its three largest holdings | the plan's three largest targets |
| `sector_spread` | `brokerage` | the share of the brokerage that would have to move to match the plan's sectors | 0: the gap is the line |
| `stacked` | `fidelity_rolled_up` | the largest single company across both accounts, held directly and through the funds | none |
| `ai_capex` | `brokerage` | MSFT + NVDA + AMZN + GOOGL, direct and through funds, as a share of the brokerage | their combined plan target |
| `crash_cost` | `fidelity_rolled_up` | dollars a published market fall would take off this allocation, in `usd` | none |

Five things about them are deliberate:

- **One base, never mixed.** The same holdings answer differently on each: a name that is a large share of the
  brokerage is a much smaller share of the two accounts rolled up, and the rolled-up book's own largest names
  can be different names. A figure read against the wrong base is a wrong number, so each row carries its own.
  `base` says which book a figure is, and a consumer must compare it only against that base's line.
- **Only the accounts at this institution.** Every figure is built from the configured accounts the warehouse
  records as Fidelity, so another broker's account added to `[allocation.accounts]` cannot reach a figure. The
  owner's rule: a household total may include a trading account, concentration math may not.
- **A floor says it is one.** `stacked` and `ai_capex` read fund constituent weights, so they publish the measured
  `coverage_pct` and set `look_through_complete = 0` while the holdings on file cover only part of the funds - the
  value is then a floor. The other three read no constituent weight, so they are complete and carry no coverage.
  A holdings file that sums OVER 100% is the opposite case - it counts something twice, so the figure it makes is
  too big, not too small - and those two then publish NO value, only the measured coverage and a `note` saying
  why. A consumer that draws an incomplete figure as "at least x" would otherwise assert the wrong direction.
- **Every note says where the figure came from.** `positions_latest` is per-account newest, so one account's feed
  can stop while another stays current; the `note` ends with the positions date the figure rests on and names any
  account behind it. A run that cannot rebuild a figure keeps the one already published - a read that found
  nothing is not a book that holds nothing - and writes that into the note too, so a figure from an earlier day
  is never read as today's.
- **No invented line.** `line_pct` comes from the targets file, so it moves when the plan is revised. It is NULL
  where the plan draws no line, and a consumer should say so rather than show a threshold nobody set.

`trading_days` answers the price question a consumer in another repo cannot answer for itself: `day` is an NYSE
trading day by rule (`check/calendar.py` writes the raw table, so no day the calendar rejects can get in) that
also has at least one `final = 1` bar, with `final_bars` counting them. `prices.final = 0` marks a bar fetched
before its close, and the newest date is exactly where one lives - so **read closes from `trading_days`, or filter
`prices` on `final = 1`**, and a partial bar can never be read as a close.

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
.\scripts\schedule_sync.ps1          # at logon (+20 min) and daily at 08:30, 17:00 and 20:30
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
