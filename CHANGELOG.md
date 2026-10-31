# Changelog

## Unreleased

- Valuation engine: each fund (`etf`, `mutual_fund`, `fund`) gets `pe` (trailing P/E), `dividend_yield` and `expense_ratio` from its own `fund_snapshots`: the newest snapshot dated on or before the day (`unsupported` "no fund snapshot yet", or "newest snapshot is from <date>" past 7 days). `pe` and `dividend_yield` get a percentile and band only against snapshots with the newest one's `source` (a proxy's days do not count as the fund's own) and only once those span `min_history_days` (365); until then `not_enough_history` with the value, no band and "<N> days of its own snapshots; a band needs 365", so a young fund is read by its price measures only. `expense_ratio` is a value only. No row names a `valuation_daily` column (only a `*` hold holds them). New `Store.fund_snapshot_rows`, `scores.fund_scores`.
- Valuation engine: ten company fundamentals for each stock with a CIK, without percentile or band: `implied_growth` (the 10-year free-cash-flow growth, then `terminal_growth`, discounted at `discount_rate`, that equals the market cap; bisection on -50%..+100%), `fcf_growth_5y` and `revenue_growth_5y` (compound growth against the fiscal year 5 lower), `fcf_margin_change` and `op_margin_change` (the TTM row in force against the one ending 350-380 days earlier), `net_debt_ebitda`, `buyback_yield` (a missing buybacks line is 0), `last_surprise` (with the 8-K's SEC link), `estimate_change_30d` (this year's EPS estimate against a snapshot 30-45 days older; blank when a report fell between or the newest snapshot is more than 7 days old) and `insider_net_90d` (buys minus sells in the sibling `data/insider.json` feed, read once per run; Form 4 links built from CIK and accession, never the feed's; blank when the feed has no `as_of` or it is more than 7 days old). A value that cannot be known is `unsupported` or `not_enough_history` with why. `implied_growth` names `market_cap`, `fcf_ttm`, `close` as inputs, `buyback_yield` `market_cap`, `close`, `fcf_margin_change` `revenue_ttm`, `fcf_ttm` and `op_margin_change` `revenue_ttm` (so the `figures` check's field holds reach them); the others none, so only a `*` hold holds them; at build time as in the view. New reads `Store.estimate_rows`, `scores.read_insider`.
- Valuation engine: each stock with a CIK is scored against its own history: `pe`, `p_fcf`, `ps`, `ev_ebit` ((market cap + long-term debt - cash - short-term investments) / operating income, from the TTM statements in force each day) and `dividend_yield`, each with `percentile_5y` (mid-rank among its own `ok` daily values of the last 5 years, days a hold covered left out) and a band: under the 25th percentile `cheap`, over the 75th `premium` (mirrored for the yield, where high is cheap), else `usual`; `[engine]` sets the years, the 365 days of history a band needs (`not_enough_history` until then) and both thresholds. The newest valuation row decides the rest: stale or unsupported makes every score `unsupported` with its reason, a value a hold blanked (its own column, any `*`, or the close) is `held` with the hold messages, a value that can't be computed says why. Sources: the newest close, the earliest 10-Q/10-K reporting the TTM's period end (SEC link built from CIK and accession), the history span, the settings. Each row's `inputs` is its own column plus `close` (`,pe,close,`), so the `valuation_scores` view holds it under a `close` hold opened after the build too. New reads `Store.valuation_rows`, `ttm_rows`, `statement_filing`.
- `engine` step (after `check`, the sync's last; migration 0019): builds `valuation_scores_raw`, one row per symbol and metric at the symbol's newest final close, served through the `valuation_scores` view. It runs only once a full, error-free check run has covered the newest derive (else `skipped`, "waiting for a check run to cover derive <id>", and the scores it had stay), stamps every row with that check run's finish time (`checked_at`), and reads the checker's open holds as missing: closes are adjusted over every final bar, then a held close is left out of every window (a held split day still adjusts the closes before it), and one on the newest day holds the price rows. The view applies holds opened after the build in `valuation_latest`'s style (field `score:<metric>`, one of the row's `inputs`, or `*`, which on a price row counts only from a price check). First metrics, the price shape on adjusted closes: `off_high_52w`, `vs_200d`, `week_vs_market` (against `[benchmarks] market`), `week_vs_sector` (against the sector's fund). Scored: securities not parked, not money markets or crypto, with a final close, and not only a benchmark. A repeat run with nothing new rewrites no row; `fdc status` counts `valuation_scores_raw`.
- Inputs for drop causes (migration 0018): `current_reports` keeps every 8-K and 8-K/A SEC's submissions list shows for a company (accession, form, filing date, item numbers as SEC writes them), stored by the `earnings` step from the answer it already asks for (its message gains ", N 8-Ks new", its rows count them); `news_headlines` keeps the sibling `data/news.json` feed's headlines (date, headline, outlet, publish time, and a link only when it is a plain https one; never the article text or tags), stored by the `ingest` step (its message gains "N headlines new", and a note when the feed can't be read; no feed, no message). Both are kept as first stored, so a repeat run changes nothing; `fdc status` and the MCP status count them.
- Engine groundwork: `[engine]` in `config.toml` (history years, band percentiles, discount rate, terminal growth, drop and excess-move thresholds; validated, defaults in `config.example.toml`), `derive/sources.py` (builders of the `sources` lists every score and price move carries; a filing's link is built from its CIK and accession, a feed's link kept only when it is a plain https link) and the shared shapes in `models.py` (`SCORE_METRICS`, `ScoreRow`, `PriceMove`, `CurrentReport`, `Headline`). Nothing runs yet.
- Checker: a new offline check `closes` sees a stopped price feed. For each held symbol and benchmark (money markets and crypto left out), the NYSE trading days after its newest final close and before today: `close_warn_days` (2) of them warn, `close_hold_days` (5) hold every value of the symbol (`[check]` in `config.toml`). A symbol with only a pre-close bar, or no price from any provider ("held, never priced"), warns.
- Sectors: each company's `securities.sector` and `securities.sector_source` (migration 0017). The sibling classifications feed (`<investing content_dir>/data/classifications.json`, Yahoo's sector names mapped to the eleven) names it when it can, else SEC's industry code is mapped through the package's `seeds/sic_sector.csv` (the narrowest range wins); the source column says which, with the feed's date or the code. Funds get none. A feed that cannot be read at all keeps the sectors it gave (the step says "classifications feed unreadable; feed sectors kept"), an entry that cannot be read is skipped alone, and a company the feed no longer names falls back to SIC. The pass runs at the end of the `earnings` step on every run, even when no company was asked, and the step's message and row count include the sectors changed (a sector that changed, not a source rewritten for a new feed date).
- `earnings` step (after `funds`): each stock's earnings reports into `earnings_events` (migration 0016): the day from SEC's 8-K filings with item 2.02 (accession kept) when SEC_USER_AGENT is set, else Yahoo's; EPS expected and reported, the surprise and the time of day from Yahoo; a plain merge keeps old events both lists have dropped and folds a Yahoo-dated event into its 8-K when the filing appears (an 8-K up to six calendar days after Yahoo's day, since four business days is legal); a Yahoo date with no reported EPS that Yahoo has since moved is dropped once no 8-K can still come for it. Yahoo's EPS estimates (`0q`, `+1q`, `0y`, `+1y`) are stored once a day in `estimate_snapshots`, and SEC's industry code in `securities.sic`. One SEC and one Yahoo ask per company per New York day; a failed part is asked again next run (an error yfinance logs asking for estimates counts as failed), and so is a company that gets no estimates after a day it had some (the step names it). Without SEC_USER_AGENT the SEC part is skipped and the step says so. A run with no company to ask is `skipped`, which the exit code leaves out like an unconfigured connections or macro step. `fdc status` and the MCP status count `fund_snapshots`, `earnings_events` and `estimate_snapshots` rows.
- `funds` step (after `sec`): a daily valuation snapshot per fund (ETF, mutual fund) from Yahoo into `fund_snapshots` (trailing and forward P/E, dividend yield, expense ratio as fractions; migration 0015), one Yahoo call per fund per New York day. A fund with no P/E or yield of its own can borrow an ETF's with `[fund_proxies]` in `config.toml`; the row's `source` says so. History starts the day it first runs. Yahoo is reached through a `YahooClient` seam (`collectors/yahoo.py`) that tests replace. yfinance logs a failed request instead of raising (its `hide_exceptions`): an error it logs while asking fails the ask, so the fund is asked again next run, and a fund that comes back with no figures after a day it had a snapshot is not marked either. A yield or expense ratio out of range, or whose two Yahoo readings (`yield` against `dividendYield`, `netExpenseRatio` against `annualReportExpenseRatio`) are 10 times or more apart, is refused with a note. A run with no fund to ask is `skipped`, which the exit code leaves out as it does the earnings step's.
- Benchmarks: a sync collects prices for `SPY` and the eleven sector SPDRs (`[benchmarks]` in `config.toml` changes them), marks them in `securities.benchmark` (migration 0014), keeps them out of `fdc export` unless held or listed, never parks them (one parked before it became a benchmark is unparked), and the checker's `prices2` compares their newest close with a second provider's.
- `fdc check --summary` lists every open finding of the checker, holds first, without running a check; `fdc status` and the console's steps table show when the checker last ran and how many holds and warnings are open; `scripts/schedule_check.ps1` registers a daily 07:00 `fdc check` task as a backstop for a day the sync did not run.
- Known: SEC's companyfacts answer can lag a filing by months. A large-cap's 10-Q filed in July was absent from
  SEC's companyfacts answer three months later (no fact carried its accession number and no period ended after
  March), although SEC's submissions list showed the filing with XBRL. fdc stores every fact companyfacts returns
  (the stored rows matched the answer one for one), so its TTM stopped at the March quarter. Nothing in fdc dropped
  it and no code changed; the checker's filings check (SEC's submissions list against `sec_facts`) holds such a
  company's TTM-based values from the filing date until the facts appear.
- `macro` step (after `prices`): FRED economic numbers into `macro_observations` (CPI, core CPI, the Fed's target range, the 10-year yield, unemployment) with a free `FRED_API_KEY` in `.env`; skipped without it. Migration 0010.
- `fdc console`: a live terminal screen (Textual) with three tabs: each step's last result and staleness plus
  the scheduled task's registration (Windows), every source and output with its last activity, and the history
  of runs with who started them and each step's message. `r` runs the sync, `s` one step, `ctrl+p` the
  palette; `fdc console --once` prints the same content once. The console only reads, over a read-only
  connection (`readonly.open_store`; the universe it counts comes from `universe.universe_symbols`, which writes
  nothing); a run it starts is an `fdc sync` subprocess that carries on after quitting. Migration 0009 indexes
  `sec_facts(filed)` so the Sources tab's newest-filing lookup is instant.
- The console is easier to read and move around in: every tab is a table with a detail strip under it that
  shows the whole highlighted row (full messages, full paths), tables show short text and numbers with
  separators, the status band is colored by state and names the next scheduled run, Sources leads with the
  state and shows short paths, Runs covers the last seven days with the date once per day and dims `unknown`
  origins, the schedule sits in its own panel, `left`/`right` switch tabs, `tab` moves between a table and
  its strip, and `?` opens a help screen.
- `scripts\open_console.ps1` opens the live screen in a PowerShell window of its own and returns, so a shortcut or
  another program can launch the console; the window stays open with the exit code if the console fails.
- A running sync keeps a heartbeat (`data/sync.heartbeat.json`: run, step, progress line, pid, origin) and
  removes it when it ends; a sync that finds another alive waits its turn (up to 30 minutes). Every run records
  its `origin` (migration 0008: `scheduled`, `manual`, `console`; older rows read `unknown`), and
  `scripts/run_sync.ps1` passes `--origin scheduled`.
- `derive` writes `unexplained_daily` (migration 0007): for each account and day, the change in the replay that
  no transaction or split explains, as its cash part, its shares part at the day's close, and their sum. Most often
  this is a deposit the broker's balance shows before its activity feed posts it. A consumer that took money moved
  only from `transactions` read such a deposit as growth. An ordinary day has no row, nor does a change under a
  dollar. Changes that cancel within a week (a value wrong for a day or two, such as a money-market sweep counted
  twice) moved no money and have no row either. When the feed dates a deposit
  after the balance showed it, the replay now pairs it with that earlier cash instead of adding it again as money in
  transit. A split the records carry as a transaction (Fidelity's share distribution) isn't counted twice.
  `Store.splits_by_symbol()` feeds the replay its splits.
- The scheduled sync (`scripts/schedule_sync.ps1`) runs on battery, and a start that arrives while a sync
  is running waits its turn instead of being dropped. A laptop left unplugged never synced, and a sync that
  began just before a feed finished writing left the warehouse a day behind.
- Connections: `fdc connect snaptrade` (brokerages, a personal key) and `fdc connect simplefin` (banks, cards
  and loans, balances only), `fdc connections`, `fdc disconnect`, `fdc accounts` and `fdc accounts set`. Keys
  live in the operating system's key store, or in `.env` on a computer without one. `fdc sync` gains a first
  step, `connections`. Migration 0006 adds the `connections` table, an account's `external_key`, `origin`,
  `kind_confirmed`, `credit_limit`, `rate_pct` and `flows`, and `cash_balances.available`. A connected account
  is known by a hashed key, so a rename keeps its history. Spec: `docs/superpowers/specs/2026-09-29-connections-design.md`.
- Identical transactions in one import are kept as separate rows (three $100 deposits on the same day
  were stored as one). The first keeps its old dedupe key, so re-importing a file still changes nothing.
- The replay no longer decides by the clock alone whether a snapshot already holds its day's rows. A
  statement's own units say which same-day trades it holds, and its cash says which deposits and other cash
  rows have landed; the fetch time only breaks ties. Fixes a buy counted twice when a feed stamped
  start-of-day was live, a deposit dropped until the balance showed it (a cash-only statement has no fetch
  time), and an instant deposit added on top of a morning statement that already held it. A deposit on its
  way counts in the account from its date; one the statements never show stops counting after seven days.
- Fidelity history: IRA deposits ("CASH CONTRIBUTION CURRENT YEAR" / "PRIOR YEAR") are contributions, not
  'other', so they count as money put in; "DIRECT DEBIT" is a withdrawal; a share distribution (a split) moves
  shares and no cash. Migration 0005 repairs rows already stored: retypes them, drops the IRA deposits the
  SnapTrade feed already holds, stores 0 units as NULL, and re-keys each row the way an import keys it now.
  A history file imported before the identical-rows fix can be imported again to recover a collapsed repeat.

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
