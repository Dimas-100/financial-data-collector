# Valuation foundation: correct data, an independent checker, a sourced valuation engine

Date: 2026-10-30. Status: approved by the owner 2026-10-30, with the builder check's answers folded in.

## Intent

The owner adds money to a brokerage account every week. It waits in the account's money market fund
until he decides where it goes, and today he decides by eyeballing prices. He wants a system that, for
the holdings in those accounts:

1. **prices each holding**: is it cheap or expensive against its own history, and on the measures that
   help judge whether now is a good time to add;
2. **says why a holding dropped** (the example he gave: Google fell on its earnings);
3. **proposes how to split the money that is waiting**, in dollars, per account, with an opinion; and
4. **shows where every number came from**: the filing, the price feed, the date, the inputs.

His rule for the whole warehouse: a project that does heavy work (collecting, automating, researching)
stays its own project; displaying it happens in one window (his personal agent OS, a sibling project that
reads this database read-only). So **all the calculating lives here**, in plain code with no AI, and the
window only displays results and explains them. An assistant in the window may write an opinion on top
of a proposal, but it never computes a number.

He also asked for a strong foundation first: the numbers feed everything after them, so the data has to
be right, and **a second, separate system must keep checking it**.

This spec covers everything inside this repo. The window's pages get their own design later, with
mockups on real data.

## Who it is for

Anyone running the collector, with the owner as one instance. Personal inputs (the allocation targets,
which accounts take new money) are owner-only files named in `config.toml`, ignored when missing, the
way `[sources.investing]` already works. Nothing personal goes into code, tests, docs or commits
(`tests/test_privacy.py` stays the gate). Examples below use widely held symbols.

## Goals

- Every valuation number is correct or is explicitly "not known", never quietly wrong.
- An independent checker runs on its own schedule, finds bad or stale values, and holds them back from
  every consumer until they pass.
- Funds, which have no company financials, still get a valuation view.
- A drop is matched to its cause by code, from sourced facts.
- A weekly dollar split per account, built from targets, valuation and the waiting cash.
- Every derived value carries its sources, so a reader can follow it back to a filing or a price bar.

## Non-goals

- No order placement, ever, and no link to anything that can trade. The owner places every trade.
- No AI call anywhere in this repo. Explanations and opinions are the window's job.
- No forecasts presented as fact: "what growth the price implies" is shown as an implied number, not a
  prediction.
- Full IFRS / foreign-currency statement support (20-F filers) is out for now (see Part 1).
- Intraday data. Everything works on daily closes.

## Anti-goals (a failure even if it technically works)

- A number on screen that is wrong and looks right (the 2012-based P/E found on 2026-10-30, below).
- The word "buy" as a verdict. The engine says "cheaper than usual", "premium", "dropped on earnings";
  the proposal is a split of money already set aside, which he accepts, edits or ignores.
- A split that breaks his own targets' rules (a "hold" name getting new money, a cap exceeded).
- The checker sharing code with what it checks, so one bug fools both.
- A source shown that the value did not actually come from.

## What was found on 2026-10-30 (read-only audit)

Healthy: every sync in 14 days finished (about 4 a day); prices for all held symbols on every trading day
for 60 days and 5 years back; macro series current; positions dated the same day; recomputing P/E from
its stored inputs agrees with the stored P/E.

Wrong or missing, in order of harm:

| # | Finding | Effect |
|---|---|---|
| 1 | A foreign filer moved from US-GAAP to IFRS years ago; its last US-GAAP TTM is from 2012 and is used as current | a P/E of 6.7 shown as today's, on 13-year-old earnings |
| 2 | Other 20-F filers (IFRS-only) get no income; one is classified `etf` because it has "no operating facts" | missing values and a wrong type |
| 3 | A large-cap's latest 10-Q (filed 2026-07-29, confirmed on SEC's site) is absent from `sec_facts`; its TTM stops at the March quarter | six-month-old numbers; cause unknown |
| 4 | A company with two share classes whose filings state shares in the other class's terms gets `shares` = NULL | no market cap, P/E, P/S or P/FCF |
| 5 | A company reports capex only under `PaymentsToAcquireOtherPropertyPlantAndEquipment`, which `concept_map` lacks | no FCF, no P/FCF |
| 6 | A bitcoin trust files 10-Qs and is typed `stock` | a meaningless P/E |
| 7 | A daytime sync stores yfinance's intraday price as that day's close; `REFRESH_DAYS = 5` overwrites it on a later run, but `valuation_latest` uses it meanwhile | brief wrong closes |
| 8 | A research tag that is not a listed ticker (a private company) joins the universe, fails every run and spends the Tiingo request limit | noise and a 429 |
| 9 | Tiingo failures (429, DNS) silently fall back to yfinance per symbol | mixed sources, unrecorded per bar beyond `prices.source` |

Missing for this feature:

- **Funds** (index mutual funds, ETFs): no P/E, yield history or any valuation; about half the owner's
  money is in funds.
- **Benchmarks**: no S&P 500 ETF and no sector ETFs, so "the stock fell" can't be told from "the market fell".
- **Earnings history**: past report dates, reported vs expected, and estimate revisions are not stored
  (a sibling feed keeps only the next date).
- **Price shape** (52-week high, 200-day average): computable from `prices`; nothing computes it yet.

## The parts and their order

Each part ships on its own and is useful on its own. Parts 1 and 2 are the foundation and come first.

1. **Fix the data** (findings 1-9).
2. **The checker**: an independent system with holds.
3. **Collect what's missing**: benchmarks, fund valuation, earnings history.
4. **The valuation engine**: scores, price shape, drop causes, all sourced.
5. **The weekly split**.

The window reads the results from `warehouse.db` (`mode=ro`), as it already does for positions and macro.

## Part 1: fix the data

- **Foreign filers.** A company whose newest filings are 20-F / 6-K, or whose operating facts are only in
  `ifrs-full`, is typed `foreign_filer` (a new `asset_type`). `derive` builds no valuation for it, and its
  `valuation_*` rows carry `status = 'unsupported'` with the reason, so a consumer shows "not known". A TTM
  row is never used when its `period_end` is more than 15 months before the price date (any company:
  that is stale, not current). Full IFRS + currency conversion is a later spec.
- **The missing 10-Q.** Find the cause (companyfacts API lag, a fetch or parse path, a dedupe key) before
  changing code, and add a test that reproduces it. Whatever the cause, the checker's filing check
  (Part 2) catches the class of problem from now on.
- **Share classes.** A `[share_classes]` table in `config.toml` gives a symbol's conversion to the class
  its filings use (e.g. `"XYZ.B" = { per = "XYZ.A", ratio = 1500 }`, meaning 1500 of `.B` = one `.A`),
  so shares and EPS are restated into the traded class's terms. The ratio is a published fact; the
  config line names its source in a comment. Where neither class's share count is in the filings, the
  value stays NULL and `status` says why.
- **Concept map.** Add the missing capex concept at a lower priority than the existing two. Any concept
  added for one company must keep every other company's TTM unchanged (test over the stored fixtures).
- **Fund-like filers.** A symbol whose SEC name or description marks it a trust or fund (a bitcoin
  trust, a commodity trust) is typed `fund` and gets fund treatment (Part 3), not company ratios.
  `[classify]` keeps working as the override.
- **Final closes.** `prices` gains `final` (0/1). A bar fetched before 16:15 New York on its own date is
  `final = 0`; a later fetch of that date sets 1. Valuation, scores and drops use final bars only; the
  latest non-final bar may be shown as "intraday" by a consumer.
- **Non-tickers.** A universe symbol with no CIK, no price from any provider and no position, after three
  runs, is parked (`securities.parked_reason`) and skipped by the price and SEC steps; the console lists
  parked symbols. Nothing in the sibling project is edited.
- **Price source per bar.** Keep `prices.source`; add `fetched_at`. The checker (Part 2) compares sources;
  no blending of two providers' closes into one series without a mark.

## Part 2: the checker

A separate system with its own entry point and its own schedule. It reads the warehouse, compares it
against outside sources and its own recomputation, and records what it finds. **It never changes data.**

- **Entry point:** `fdc check` (new module `check/`, with no import from `derive/`, `statements.py` or the
  collectors except `http`). Its reads are its own methods in `store.py` (AGENTS.md: only `store.py`
  executes SQL), and those methods read raw tables (`prices`, `sec_facts`, `positions_latest`) and the
  outputs under test, never the shaped TTM views. It may read `seeds/concept_map.csv` for nothing: its
  outside comparisons (below) are what catch a mapping gap.
- **When:** as the last step of every sync (`check`, after `export`), so nothing new is served unchecked
  for long, and on its own scheduled task (`scripts/schedule_check.ps1`, daily) as a backstop. It is a
  separate step with separate code; a failing check run never fails the sync.
- **Unchecked values.** Every served output carries `checked_at` (the last check run that covered it).
  Existing outputs (`valuation_latest`, the `fundamentals.json` export) keep showing values between a
  derive and the check that follows, stamped with `checked_at`. The new outputs that drive money (Part 4's
  bands, Part 5's split) are built only from values a check run has covered; a proposal waits for the next
  check rather than using unchecked numbers.
- **Tables (migration):** `check_runs` (run id, started, finished, counts) and `check_findings`
  (`id`, `check`, `symbol`, `field`, `date_from`, `date_to`, `severity` = `hold` | `warn` | `info`,
  `message`, `evidence` JSON, `first_seen`, `last_seen`, `cleared_at`). A finding is keyed by
  (`check`, `symbol`, `field`, `date_from`) so a repeat updates `last_seen` instead of piling up.
- **Holds.** A `hold` finding withholds the named field over its dates from every served output, the
  existing ones included, so Part 2 protects consumers on the day it ships:
  - `valuation_daily` stays raw (what `derive` computed); the checker compares against it.
  - `valuation_latest` (a view today) applies open holds: a held value is NULL, and new columns `status`
    (`ok` | `held` | `unsupported` | `stale`), `hold_ids` and `checked_at` say why. Columns are only added,
    none removed or renamed. The `fundamentals.json` export applies the same holds (a held figure is
    `null`, which its consumer already handles for missing data) and keeps its shape.
  - Parts 4 and 5 read the held values, never raw ones.
  Raw tables (`prices`, `sec_facts`, `valuation_daily`) are never changed by a hold, so a hold that
  clears restores the value with no refetch, and the recompute can still see the raw value it held.
  Known consumer: the owner's agent OS reads `valuation_latest`'s `pe`, `market_cap` and the like and
  already treats NULL as missing; it learns to show `status` in its own later spec.
- **Checks (first set):**
  - *Filings caught up:* SEC's own submissions list (`data.sec.gov/submissions/CIK##########.json`)
    against `sec_facts`. A 10-Q / 10-K / 20-F filed more than 3 days ago whose accession number has no
    facts here → `hold` on that company's TTM-based values from the filing date on (finding 3's class).
  - *Fresh enough:* a TTM `period_end` older than 5 months for a quarterly filer → `warn`; older than
    15 months → `hold`.
  - *Recompute:* P/E, P/S, P/FCF, market cap and dividend yield recomputed by the checker's own small
    code from the TTM inputs `valuation_daily` states it used plus `prices`; more than 1% apart →
    `hold` with both values. This catches arithmetic, split and share-class errors. It does not rebuild
    TTM from raw XBRL (that would be a second `statements.py`); the outside comparison below covers the
    shaping.
  - *Outside figures:* for each company, yfinance's trailing EPS, market cap, revenue and free cash flow
    against ours. A figure we have as NULL where the outside source has one → `warn` (a mapping gap,
    finding 5's class); more than 15% apart → `warn`, more than 40% → `hold`. Different providers define
    TTM differently, so the loose tolerances are on purpose; the evidence JSON keeps both numbers.
  - *Second price source:* the latest final close of each held symbol and each benchmark fetched from the
    other provider; more than 0.5% apart (after splits and dividends on the date) → `warn`; more than 3%
    → `hold`.
  - *Impossible values:* a ratio on a fund, negative shares, a market cap off by a share-class factor
    (10x or more from the other provider's market cap when it has one), a one-day close move over 40%
    with no split → `hold`.
  - *Gaps:* a held symbol missing a trading day (NYSE calendar) → `warn`.
  - *Engine sanity (once Parts 4-5 exist):* a split that sums to more than the waiting cash, money to a
    `hold` name, a score with no sources → `hold` on that output.
- **Outside calls** are read-only GETs to SEC and the price providers, with the same user agent, pacing
  and key handling as the collectors (keys through `connections/keys.py`, messages through `redact.py`).
- **Reporting:** `fdc check --summary` prints open findings; `fdc status` and the console show the
  checker's last run and its open `hold` count. The window reads `check_findings` directly.

## Part 3: collect what's missing

- **Benchmarks.** A `[benchmarks]` list in config, defaulting to SPY and the 11 Select Sector SPDRs
  (XLK, XLV, XLF, XLY, XLC, XLI, XLP, XLE, XLU, XLRE, XLB). Their prices are collected like any symbol
  (5 years); they are marked `benchmark` and never counted as holdings. Each company's sector comes from
  the sibling feed's classifications when present, else SEC SIC code mapped in `seeds/sic_sector.csv`.
- **Fund valuation.** For each fund and ETF in the universe, a daily snapshot table
  `fund_snapshots` (symbol, date, trailing P/E, forward P/E, yield, expense ratio, source, fetched_at)
  from yfinance's fund info. A mutual fund without its own figures may name a proxy ETF tracking the
  same index in `[fund_proxies]` (e.g. an S&P 500 index fund → SPY); the row then says
  `source = "proxy: SPY via yfinance"`. History starts the day this ships; until a fund has a year of
  snapshots, its "against its own history" view uses price measures only (Part 4) and says so.
- **Earnings history**, table `earnings_events` (symbol, report date, time of day if known, EPS
  reported, EPS expected, surprise %, source per field):
  - report dates from SEC submissions (8-K with item 2.02), sourced by accession number;
  - reported and expected EPS from yfinance's earnings dates, sourced as Yahoo;
  - `estimate_snapshots` (symbol, date, current-quarter / next-year EPS estimate, number of analysts,
    source) taken daily, so estimate revisions build up from now on.
- **News for drop causes** stays in the sibling project's feed (`news.json`); the engine reads it by the
  configured sibling path, records the headline, outlet, date and link as the source, and never copies
  article text.

## Part 4: the valuation engine

Built by `derive` from the tables above, no network, no AI. Outputs are rebuilt each run like
`valuation_daily`.

- **`valuation_scores`** (symbol, date, one row per metric): `metric`, `value`, `percentile_5y` (where
  history allows), `band` (`cheap` / `usual` / `premium`, or NULL), `status` (`ok` | `held` |
  `unsupported` | `not_enough_history`), `sources` JSON. Metrics, companies:
  - P/E, P/FCF, P/S and EV/EBIT, each against the symbol's own 5-year daily history (percentile);
  - implied growth: the yearly FCF growth for 10 years that the current market cap requires at a fixed
    discount rate, shown next to the company's actual 5-year FCF and revenue growth (rate and method in
    config, shown with the number);
  - quality, so cheap isn't a trap: FCF and operating margin trend (4 quarters vs a year earlier),
    net debt / EBITDA;
  - earnings: last surprise, the 30-day direction of estimates (once snapshots exist);
  - yield: dividend yield against its own 5-year range; buyback yield;
  - insider net buying over 90 days, from the sibling feed if present.
  Funds: price measures (below), P/E and yield from `fund_snapshots` against their own (growing) history,
  expense ratio. Bands come from percentiles: `cheap` under the 25th, `premium` over the 75th. Thresholds
  are config, shown with the band.
- **Price shape** (every symbol): distance from the 52-week high, distance from the 200-day average,
  this week's move against SPY's and its sector ETF's.
- **`price_moves`** (drops and their causes): a move is recorded when a final close is down 4% or more in
  a day, or 8% or more over 5 trading days, or when the move minus SPY's is beyond 2.5 standard deviations
  of the symbol's own 1-year daily excess moves (thresholds in config). Each move gets causes, in this
  order, each with its source:
  1. earnings: an `earnings_events` date on the day or the trading day before, with the surprise;
  2. a filing: an 8-K (other items) in the window, by accession number and items;
  3. market: SPY moved at least two thirds as much the same way → "the market fell";
  4. sector: the sector ETF did → "its sector fell";
  5. news: headlines for the symbol in the window, with outlet and link;
  6. none found → "no cause found", never a guess.
  A move can have several causes; the order says which is shown first.
- **Sources, everywhere.** Every row's `sources` JSON is a list of `{kind, label, date, ref, url?}`:
  `price` (provider, bar date, fetched_at), `filing` (form, accession number, filed date, period, and the
  sec.gov URL built from CIK + accession), `feed` (sibling file and its `as_of`), `config` (the setting and
  its value), `derived` (the ids of the rows it was computed from). A reader can always walk from a number
  to its inputs. A test asserts every `ok` row has at least one source and every URL is built, not stored
  from outside text.

## Part 5: the weekly split

- **Inputs:** the cash in each configured account's money market fund (from `positions_latest`, the money
  market symbols), the owner's targets file (`[allocation] targets = "../investing/config/sizing-targets.json"`,
  owner-only, its documented shape: per-symbol targets per account, bands, caps, and a rule per entry of
  `trades` / `contributions` / `hold`), and Part 4's scores.
- **When:** the derive pass after a sync that sees an account's money market balance rise by at least
  `[allocation] min_new` (default $25) writes a new proposal; one open proposal per account at a time,
  replaced when the cash changes again.
- **How (code):**
  1. the target gap per eligible holding in that account (`hold` entries get nothing; `trades` entries
     are treated like `contributions` here, since this never sells). Every constraint the targets file
     states is honoured, read the way its existing consumer (`investing/kestrel_feed/targets.py`) reads
     it: per-ticker targets and bands (the ±25% default band when an entry gives none),
     `pct_of_brokerage` entries, sleeve maxima, and caps. A cap is a ceiling on the whole household
     (direct holdings plus the look-through slice held via funds, from the sibling project's
     `fund_holdings/` CSVs by configured path); a line that would cross a cap or a sleeve max is cut to
     stay under it. Where look-through data is missing for a fund, the cut is computed as if that fund
     held the capped name at its largest weight in any fund on file (conservative), and the line says so.
     This is now the one implementation of the rule for proposals; a test pins it to the targets file's
     documented examples;
  2. a valuation tilt: an eligible holding's share of the money is multiplied by a factor from its band
     (`cheap` 1.5, `usual` 1.0, `premium` 0.5; config), then renormalized. The holding's band leans
     careful (the owner's choice): its primary metrics are P/E, P/FCF, EV/EBIT and P/S for a company,
     P/E and yield for a fund, each counted only when its status is `ok`.
     - `cheap` only when at least two primary metrics are available, most of them say `cheap`, none says
       `premium`, and the quality check isn't falling (FCF and operating margin not both down on a year);
     - `premium` when most available primary metrics say `premium`;
     - `usual` otherwise, including any disagreement, a single available metric, and every fund until it
       has a year of its own valuation history.
     The line's reason codes say which metrics decided it;
  3. a held or unsupported score counts as `usual` and the row says so;
  4. amounts rounded to whole dollars; anything under a per-order minimum (config, default $10) is
     folded into the largest line; the remainder, if any, is "waits for next week".
- **Lifecycle:** a proposal is `open`, `replaced` (the waiting cash rose by `min_new` again: a new
  proposal covers all of it) or `spent` (the cash fell below `min_new`: he placed money). Partly placed
  money closes it as `spent`; whatever is left waits and joins the next deposit's proposal. Nothing here
  records whether he followed it; acceptance and edits, if wanted, live in the window's own data.
- **Output:** `allocation_proposals` (id, account key, created, cash, status) and `allocation_lines`
  (proposal id, symbol, dollars, target gap, band, reason codes, `sources` JSON naming the cash row, the
  targets entry and the score rows). The account is stored as its key, never its label or number.
- **Opinion:** the window may ask its assistant to write an opinion on a proposal, quoting the lines and
  their sources. The engine's numbers are what is shown; the opinion is text beside them.
- **Never:** an order, a sale, money across accounts, more than the waiting cash, a `hold` name.

## Testing

- No network in tests (collectors take `fetch` / `yf` as today; the checker too).
- Fixture companies for each finding in the audit (a 2012-only US-GAAP filer, an IFRS-only filer, a
  missing 10-Q, a two-class company, the extra capex concept, a trust, an intraday bar, a non-ticker).
- The checker's recompute is tested against hand-computed values, not against `derive`'s output.
- Split tests: hold rules, caps, the tilt, rounding, the remainder, a held score, zero cash.
- `test_privacy.py` covers new fixtures and docs.

## Decided in conversation (the owner's)

- All calculation here, no AI; the window only displays and explains.
- Data fixes and the checker first, then the rest.
- A separate checking system.
- Dollar amounts and an opinion on the split. (His targets file says "no dollar prescriptions are derived,
  just deltas"; this spec changes that on his word, for proposals he accepts or ignores.)
- Scope: the holdings in the accounts he adds money to weekly; the money to place is what waits in each
  account's money market fund.
- Every number shows where it came from.
- The split's tilt leans careful: a holding gets more money only when its measures mostly agree it is
  cheap; disagreement, thin data and funds without history stay at their target share.

## The builder's calls (not discussed; change freely)

- Foreign filers marked `unsupported` rather than converted; 15 months as the hard staleness cut.
- Thresholds: 4% / 8% / 2.5 sd for drops; 25th / 75th percentiles for bands; 1.5 / 1.0 / 0.5 tilt;
  $25 new-money trigger; $10 minimum line; 0.5% / 3% price-source tolerances; 15% / 40% outside-figure
  tolerances.
- The checker runs as the last sync step (its own code and command) plus a daily task of its own as a
  backstop; existing outputs show values stamped `checked_at` until a check covers them, and only the
  money-driving outputs (bands, the split) wait for a check.
- Holds reach the existing `valuation_latest` and `fundamentals.json` by NULLing held values and adding
  columns, never by removing or renaming; `valuation_daily` stays raw.
- The checker recomputes ratios from stated inputs and compares headline figures with an outside provider,
  rather than rebuilding TTM from raw XBRL.
- The split honours every constraint in the targets file the way its existing reader does, caps measured
  on the household with fund look-through (conservative when a fund's holdings aren't on file).
- No accepted/edited state here: a proposal is open, replaced or spent.
- yfinance for fund snapshots, earnings estimates and surprises (sourced as Yahoo); SEC for report dates.
- Sector ETFs as the sector benchmark.
