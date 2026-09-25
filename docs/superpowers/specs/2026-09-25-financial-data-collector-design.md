# financial-data-collector — design spec

**Date:** 2026-09-25
**Status:** approved in conversation (owner), written for implementation planning
**Owner:** Dimas Diaz

## 1. Purpose

A single, free, local SQL store for everything about the owner's brokerage
positions: what was held each day, what it was worth, what it cost, every
transaction, daily prices, and the full as-reported financial statements of every
company held. The store exists so that other projects in the warehouse (the
`investing` research repo and its cockpit first, anything else later) can focus on
analysis and reasoning instead of collecting and storing data.

Two audiences, in this order:

1. **The owner**, on this PC. Explores the data with SQL, pandas or notebooks, and
   lets Claude (Claude Code in the terminal, Claude Desktop through a read-only
   MCP server) query it directly.
2. **Anyone else** who clones the public GitHub repo, drops in their own Fidelity
   "Portfolio Positions" export, and gets the same database for their own book.

Out of scope for this version: a hosted database, a web UI, migrating the
`investing` pipeline's existing fetchers into this project, brokers other than
Fidelity (and, for the owner, the SnapTrade export that also covers Webull).

## 2. Context and prior art (what already exists)

- `investing/pipeline/` (sibling project) already fetches positions via SnapTrade
  twice a day into `investing/fidelity/dashboard-data/live-positions.json`, keeps
  dated copies in `snapshots/` (104 daily files since 2026-05-30), and refreshes
  market data (Tiingo/yfinance prices, SEC EDGAR fundamentals, filings, earnings,
  insider, dividends) into `investing/data/*.json`. Every file is overwritten on
  refresh; history exists only where the source provides it.
- `investing/fidelity/positions/*.csv` and `investing/fidelity/transactions/*.csv`
  are a frozen archive of hand-downloaded Fidelity exports (positions May 2026;
  history 2024–2026 per account).
- `code/personal/finance-tracker` is an earlier SQLite + Streamlit attempt fed by
  the same CSV exports. Exports stopped in May 2026; it has snapshotted stale
  holdings at live prices daily since. **This project replaces it.** Retiring it
  (unregistering `FinanceTracker Daily Sync`) is a follow-up once this project's
  sync has run for a week.

Ownership rule from day one: **new data feeds are built in this project, never in
`investing`.** Moving investing's existing fetchers here is a later, separate spec,
undertaken only after this database has proven itself.

## 3. Decisions (with reasons)

| Decision | Choice | Why |
|---|---|---|
| Store | SQLite, one file `data/warehouse.db`, WAL mode | Free, in Python's stdlib, every SQL tool and MCP server speaks it, single file to back up. Data volume is a few hundred MB at most. DuckDB can attach it later if analysis outgrows it. |
| Hosting | This PC only | Owner's call 2026-09-25. A Supabase mirror for claude.ai is a possible later addition; nothing in the schema precludes it. |
| Location | Own project `financial-data-collector`, own git repo, own venv | It is the warehouse's data layer; consumers stay pure. Reads investing's SnapTrade output by sibling path, the same wiring pattern the warehouse already uses (finance → investing). |
| Universal input | Fidelity "Portfolio Positions" CSV via an `inbox/` folder | The one thing every Fidelity customer can produce. Makes the repo useful to strangers. |
| Owner input | SnapTrade JSON adapter reading investing's live file + snapshots | Backfills 4 months of daily history on first run; keeps flowing without manual exports. Enabled only when the configured path exists. |
| Prices | Own collector, Tiingo when a token is set, else yfinance | A clone has no access to investing's prices file. Tiingo's daily endpoint returns close, adjClose, divCash, splitFactor in one call; yfinance gives the same four for free without a key. |
| Financial statements | SEC EDGAR company-facts XBRL, **all facts stored raw** | Free, no daily cap, primary source, every filing's restatement of every period is preserved. Statement views are derived from a data-driven concept map. investing's `_sec.py` retry/UA behavior and its annual/quarterly extraction rules are proven and are reused. |
| Statement shaping | Python computes a `financial_line_items` table after each SEC fetch; thin SQL views pivot it | The duration/Q4/latest-filed rules are easier to test in Python than in SQL, and materializing keeps Claude's queries instant. (Revision from the in-chat design, which had pure views.) |
| Public repo | `Dimas-100/financial-data-collector`, MIT | Owner's request 2026-09-25. Created once a sync runs end to end on fixtures with the privacy guard in place. |
| Privacy | `data/`, `inbox/`, `config.toml`, `.env` gitignored; automated test; fixtures synthetic | Same rule trading-rails lives by: no private symbols, sizes, labels or credentials ever reach the repo. |

## 4. Architecture and data flow

```
inputs (local, gitignored)        collectors (network)            store                  consumers
──────────────────────────        ────────────────────            ─────                  ─────────
Fidelity Positions CSV  ─┐                                                               sqlite3 / pandas / Datasette
  (anyone, via inbox/)   ├─ adapters ─► positions ──┐                                    Claude Code (reads the file)
SnapTrade live JSON     ─┘            snapshot rows │                                    Claude Desktop (read-only MCP)
  (owner, from investing)                           ├──► data/warehouse.db ──────────►   investing / other projects (later)
Fidelity History CSV ─────────────► transactions ───┤            ▲
                                                    │            │
tickers ever held ───► prices  (Tiingo, yfinance) ──┤       sync_runs log
                 ───► SEC EDGAR company facts ──────┘       (as_of per feed)
                       └─► financial_line_items (derived in Python)
```

`fdc sync` runs these steps in order; each is independent and idempotent:

1. **migrate** — apply any pending `migrations/*.sql`; regenerate the wide
   financial views from `concept_map`.
2. **ingest** — every adapter that is enabled: inbox CSVs (positions, history),
   then the SnapTrade live file and any snapshot files not yet ingested.
3. **universe** — the set of symbols ever seen in `position_snapshots` or
   `transactions`, plus lines in `watchlist.txt`, minus money-market symbols.
4. **prices** — for each symbol in the universe, fetch from the day after its last
   stored date (first run: `lookback_years`, default 5).
5. **sec** — for each security with a CIK not fetched in the last `sec_max_age_hours`
   (default 24): fetch company facts, upsert `sec_facts`, recompute that company's
   `financial_line_items`.
6. **log** — one `sync_runs` row per step with status, rows written, message.

Re-running with nothing new changes no data (only `sync_runs` grows).

## 5. Schema

Managed by numbered SQL files in `migrations/` (`0001_init.sql`, …) applied in
order by `store.migrate()`, tracked in `schema_version`. Before applying a pending
migration the store copies the database to `data/backups/warehouse-<UTC stamp>.db`
using SQLite's backup API.

Conventions: dates are ISO `YYYY-MM-DD` text; timestamps are ISO-8601 UTC text;
money is REAL in the account's currency; symbols are stored in **canonical form**
(uppercase, class shares with a dot: `BRK.B`).

### 5.1 Tables

**`accounts`** — one row per account.
`id INTEGER PK`, `label TEXT UNIQUE NOT NULL` (e.g. `ROTH IRA`), `slug TEXT`
(`brokerage`/`roth`/`other`, from SnapTrade or inferred from the label),
`institution TEXT` (`fidelity`/`webull`/`unknown`), `account_type TEXT`
(`brokerage`/`roth_ira`/`traditional_ira`/`crypto`/`other`), `first_seen TEXT`.
Identity is the label, because SnapTrade's slugs collide (two `brokerage`
accounts). **Account numbers are never stored.**

**`securities`** — one row per canonical symbol.
`symbol TEXT PK`, `description TEXT`, `asset_type TEXT`
(`stock`/`etf`/`mutual_fund`/`money_market`/`crypto`/`unknown`), `cik TEXT NULL`,
`sec_name TEXT NULL`, `first_seen TEXT`, `last_sec_fetch TEXT NULL`,
`price_source TEXT NULL`.

**`position_snapshots`** — grain: date × account × symbol.
`as_of_date TEXT`, `account_id INTEGER`, `symbol TEXT`, `quantity REAL`,
`price REAL`, `market_value REAL`, `cost_basis_total REAL NULL`,
`avg_cost REAL NULL`, `unrealized_pnl REAL NULL`, `source TEXT`
(`fidelity_csv`/`snaptrade`), `source_fetched_at TEXT NULL`.
`PRIMARY KEY (as_of_date, account_id, symbol)`; upsert on conflict.

**`cash_balances`** — grain: date × account × currency.
`as_of_date TEXT`, `account_id INTEGER`, `currency TEXT`, `amount REAL`,
`source TEXT`. `PRIMARY KEY (as_of_date, account_id, currency)`.
Fidelity's core position (SPAXX and other money markets) is routed here, not to
`position_snapshots`; SnapTrade's duplicate SPAXX holding is dropped in favour of
its `cash` array.

**`transactions`** — one row per activity.
`id INTEGER PK`, `account_id INTEGER`, `trade_date TEXT`, `settlement_date TEXT NULL`,
`type TEXT` (`buy`/`sell`/`dividend`/`reinvest`/`contribution`/`withdrawal`/
`interest`/`fee`/`transfer`/`other`), `symbol TEXT NULL`, `units REAL NULL`,
`price REAL NULL`, `amount REAL NULL` (signed: inflow positive), `fee REAL NULL`,
`description TEXT` (raw source action text), `source TEXT`,
`dedupe_key TEXT UNIQUE` = sha256 of `account label|trade_date|type|symbol|units|amount`
rounded to 4 dp. Re-importing overlapping exports is a no-op.

**`prices`** — grain: symbol × date.
`symbol TEXT`, `date TEXT`, `close REAL`, `adj_close REAL`, `dividend REAL`
(cash dividend paid that date, 0 otherwise), `split_factor REAL` (1 otherwise),
`source TEXT` (`tiingo`/`yfinance`). `PRIMARY KEY (symbol, date)`.

**`sec_facts`** — one row per reported XBRL fact.
`cik TEXT`, `taxonomy TEXT` (`us-gaap`/`dei`/`ifrs-full`/…), `concept TEXT`,
`unit TEXT`, `period_start TEXT` (empty string for instants, never NULL, so the
primary key stays unique), `period_end TEXT`, `value REAL`, `fy INTEGER NULL`,
`fp TEXT NULL`, `form TEXT`, `filed TEXT`, `accn TEXT`, `frame TEXT NULL`.
`PRIMARY KEY (cik, taxonomy, concept, unit, period_start, period_end, accn)`.
Index on `(cik, concept, period_end)`.

**`concept_map`** — seeded from `seeds/concept_map.csv` on every migrate (replace).
`line_item TEXT`, `statement TEXT` (`income`/`balance`/`cashflow`),
`kind TEXT` (`duration`/`instant`/`per_share`/`shares`), `taxonomy TEXT`,
`concept TEXT`, `priority INTEGER`. `PRIMARY KEY (line_item, taxonomy, concept)`.
Seed contents are listed in §5.3.

**`financial_line_items`** — derived, rebuilt per company after each SEC fetch.
`cik TEXT`, `line_item TEXT`, `period_kind TEXT` (`annual`/`quarter`),
`period_start TEXT` (empty for instants), `period_end TEXT`, `fiscal_year INTEGER`,
`fiscal_quarter INTEGER NULL`, `value REAL`, `concept TEXT`, `filed TEXT`,
`accn TEXT`, `is_derived INTEGER` (1 for a Q4 computed as FY minus three quarters).
`PRIMARY KEY (cik, line_item, period_kind, period_end)`.

**`sync_runs`** — one row per step per run.
`id INTEGER PK`, `run_id TEXT`, `step TEXT`, `started_at TEXT`, `finished_at TEXT`,
`status TEXT` (`ok`/`skipped`/`error`), `rows_written INTEGER`, `message TEXT`.

**`ingested_files`** — `sha256 TEXT PK`, `path TEXT`, `kind TEXT`, `ingested_at TEXT`,
`rows INTEGER`. Prevents reprocessing an identical export.

**`schema_version`** — `version INTEGER PK`, `applied_at TEXT`.

### 5.2 Views

Static SQL in migrations:

- `positions_latest` — every row of `position_snapshots` for the max `as_of_date`
  per account, joined to `accounts` and `securities`.
- `holdings_history` — per `as_of_date` × `symbol`: total quantity, total market
  value, total cost basis across accounts.
- `account_values_daily` — per `as_of_date` × account: holdings value, cash, total.
- `portfolio_daily` — per `as_of_date`: household total (all accounts), plus the
  same restricted to accounts whose `institution` is `fidelity`, so the owner's
  "Fidelity-scope" rule is one column away.
- `sync_status` — latest `sync_runs` row per step with its age in hours.

Regenerated by `store.rebuild_views()` on every migrate, from the distinct line
items in `concept_map` (so a new line item needs a seed edit, not a migration):

- `financials_annual` — one wide row per `cik` × `fiscal_year` (annual periods
  only), one column per line item, plus `company` (from `securities.sec_name`) and
  the primary symbol.
- `financials_quarterly` — one wide row per `cik` × `period_end` for quarters,
  including derived Q4 rows, with an `is_derived_q4` column.
- Derived columns in both: `fcf = ocf − capex`, `gross_margin`,
  `operating_margin`, `net_margin`, `fcf_margin` (NULL when inputs are NULL or
  revenue is 0).

### 5.3 Concept map seed (initial)

Priority order left to right; first concept with a value wins for a period.

Income (duration): `revenue` [RevenueFromContractWithCustomerExcludingAssessedTax,
Revenues, SalesRevenueNet, SalesRevenueGoodsNet]; `cost_of_revenue` [CostOfRevenue,
CostOfGoodsAndServicesSold, CostOfGoodsSold]; `gross_profit` [GrossProfit];
`research_and_development` [ResearchAndDevelopmentExpense]; `sga`
[SellingGeneralAndAdministrativeExpense]; `operating_expenses` [OperatingExpenses,
CostsAndExpenses]; `operating_income` [OperatingIncomeLoss]; `interest_expense`
[InterestExpense, InterestExpenseNonoperating]; `pretax_income`
[IncomeLossFromContinuingOperationsBeforeIncomeTaxesExtraordinaryItemsNoncontrollingInterest,
IncomeLossFromContinuingOperationsBeforeIncomeTaxesMinorityInterestAndIncomeLossFromEquityMethodInvestments];
`income_tax` [IncomeTaxExpenseBenefit]; `net_income` [NetIncomeLoss, ProfitLoss].
Per share (never derived): `eps_basic` [EarningsPerShareBasic]; `eps_diluted`
[EarningsPerShareDiluted]; `dividends_per_share` [CommonStockDividendsPerShareDeclared,
CommonStockDividendsPerShareCashPaid]. Shares (never derived): `shares_basic`
[WeightedAverageNumberOfSharesOutstandingBasic]; `shares_diluted`
[WeightedAverageNumberOfDilutedSharesOutstanding].

Balance (instant): `cash` [CashAndCashEquivalentsAtCarryingValue, Cash,
CashCashEquivalentsRestrictedCashAndRestrictedCashEquivalents];
`short_term_investments` [ShortTermInvestments, MarketableSecuritiesCurrent,
AvailableForSaleSecuritiesDebtSecuritiesCurrent]; `receivables`
[AccountsReceivableNetCurrent, ReceivablesNetCurrent]; `inventory` [InventoryNet];
`current_assets` [AssetsCurrent]; `ppe_net` [PropertyPlantAndEquipmentNet];
`goodwill` [Goodwill]; `intangibles` [IntangibleAssetsNetExcludingGoodwill];
`total_assets` [Assets]; `accounts_payable` [AccountsPayableCurrent];
`current_liabilities` [LiabilitiesCurrent]; `long_term_debt` [LongTermDebtNoncurrent,
LongTermDebt, LongTermDebtAndCapitalLeaseObligations]; `total_liabilities`
[Liabilities]; `stockholders_equity` [StockholdersEquity,
StockholdersEquityIncludingPortionAttributableToNoncontrollingInterest];
`retained_earnings` [RetainedEarningsAccumulatedDeficit]; `shares_outstanding`
[CommonStockSharesOutstanding, dei:EntityCommonStockSharesOutstanding].

Cash flow (duration): `ocf` [NetCashProvidedByUsedInOperatingActivities,
NetCashProvidedByUsedInOperatingActivitiesContinuingOperations]; `capex`
[PaymentsToAcquirePropertyPlantAndEquipment, PaymentsToAcquireProductiveAssets];
`investing_cash_flow` [NetCashProvidedByUsedInInvestingActivities];
`financing_cash_flow` [NetCashProvidedByUsedInFinancingActivities];
`depreciation_amortization` [DepreciationDepletionAndAmortization,
DepreciationAndAmortization, DepreciationAmortizationAndAccretionNet];
`stock_based_compensation` [ShareBasedCompensation,
AllocatedShareBasedCompensationExpense]; `dividends_paid` [PaymentsOfDividends,
PaymentsOfDividendsCommonStock]; `buybacks` [PaymentsForRepurchaseOfCommonStock];
`debt_issued` [ProceedsFromIssuanceOfLongTermDebt]; `debt_repaid`
[RepaymentsOfLongTermDebt].

### 5.4 Line-item derivation rules (Python, `statements.py`)

Reused from investing's proven `refresh_fundamentals.py` logic:

- Only facts in unit `USD`, `USD/shares` or `shares` (per line item's kind).
- **Annual**: duration facts whose `period_end − period_start` is 350–380 days.
  Instants: facts whose `period_end` equals an annual period end found for that
  company. Grain is `period_end`; `fiscal_year` = the filing's `fy` when
  `fp = 'FY'` and the fact's end matches, else the year of `period_end`.
- **Quarter**: duration facts of 80–100 days. `fiscal_quarter` from `fp` when it is
  `Q1`–`Q3`, else derived from position within the fiscal year.
- **Latest filed wins**: among candidate facts for one (line item, period), the one
  with the greatest `filed` then `accn` is used. Restatements therefore replace
  originals in `financial_line_items`, while `sec_facts` keeps both.
- **Derived Q4**: for duration line items only (never per-share or share counts),
  when a fiscal year has exactly three reported quarters inside it, Q4 = FY − sum
  of those three, stored with `is_derived = 1`. When a sibling is missing, no Q4
  row is written.

## 6. Adapters

All adapters produce plain dataclasses (`PositionRow`, `CashRow`,
`TransactionRow`, `AccountRef`) and never touch SQL; `store.py` persists them.

**Fidelity positions CSV** (`adapters/fidelity_positions.py`)
- Detected by header starting (after an optional BOM) with
  `Account Number,Account Name,Symbol,Description,Quantity,Last Price`.
- Snapshot date: from the filename `Portfolio_Positions_<Mon>-<DD>-<YYYY>.csv`;
  else from the trailing `"Date downloaded <Mon>-<DD>-<YYYY> …"` line; else file
  mtime (logged as a warning).
- Numeric cleaning: strip `$`, `%`, `,`, `+`; `--` and blank become NULL.
- Rows whose `Symbol` is `Pending Activity` are skipped. Rows whose symbol is in
  the money-market list (`SPAXX`, `FDRXX`, `FZFXX`, `FCASH`, `FDLXX`, `SPRXX`,
  `FZDXX`, `FZCXX`, `FCASH**`, `CORE**`) or whose description contains
  `MONEY MARKET` become `cash_balances` rows (amount = Current Value).
- Trailing disclaimer block (blank line, then quoted lines) is ignored.
- Account label = `Account Name`; `Account Number` is discarded. Institution
  `fidelity`; type inferred from label (`ROTH` → `roth_ira`, `TRADITIONAL` →
  `traditional_ira`, else `brokerage`).

**Fidelity history CSV** (`adapters/fidelity_history.py`)
- Detected by header starting with `Run Date,Action,Symbol,Description,Type`
  (preceded by a BOM and blank lines). A leading `Account` column may be present.
- Account: the `Account` column when present; else `--account` on `fdc import`;
  else the token between `History_` and the next `_` in the filename; else the
  file is skipped with a message telling the user to import it with `--account`.
- Action → type by prefix: `YOU BOUGHT`→buy, `YOU SOLD`→sell,
  `DIVIDEND RECEIVED`→dividend, `REINVESTMENT`→reinvest,
  `ELECTRONIC FUNDS TRANSFER RECEIVED`/`CONTRIBUTION`/`DIRECT DEPOSIT`→contribution,
  `ELECTRONIC FUNDS TRANSFER PAID`/`DISTRIBUTION`→withdrawal, `INTEREST EARNED`→
  interest, `FEE`/`ADVISOR FEE`→fee, `TRANSFERRED`→transfer, else other. Raw action
  kept in `description`. `Cash Balance ($)` is ignored.

**SnapTrade JSON** (`adapters/snaptrade.py`, owner only)
- Config `[sources.snaptrade] dir = "../investing/fidelity/dashboard-data"`.
  Skipped silently when the directory does not exist.
- Ingests `live-positions.json` and every `snapshots/live-positions-*.json` whose
  sha256 is not in `ingested_files`. Snapshot date = `fetchedAt[:10]`.
- `accounts[].label/slug` → `accounts`; institution `webull` when the label
  contains `Webull`, else `fidelity`; a `Webull Crypto` label → type `crypto`.
- `holdings[]` → `position_snapshots` (`units`, `price`, `marketValue`,
  `averageCost`, `openPnl`); `cash[]` → `cash_balances`; a holding whose symbol is
  a money-market symbol is dropped (the `cash` array already carries it).
- `live-activity.json` `activities[]` → `transactions`: `BUY`→buy, `SELL`→sell,
  `DIVIDEND`→dividend, `CONTRIBUTION`→contribution, `REI`→reinvest, else other.

**Symbols** (`symbols.py`): canonical form uses a dot for share classes;
adapters normalise `BRK/B`, `BRKB`, `BRK-B` → `BRK.B` via a small alias table.
`to_tiingo`/`to_yfinance` produce `BRK-B`; `to_sec` matches the
`company_tickers.json` form (`BRK-B`).

## 7. Collectors

Both take an injected `fetch(url, headers) -> bytes` for testability and use a
shared `http.py` (timeout 30 s; retry 403/429/5xx with 1 s/2 s/4 s backoff; a 404
is final).

**Prices** (`collectors/prices.py`)
- Universe per §4 step 3. Money-market symbols are skipped; `crypto` symbols are
  skipped in this version.
- Provider: Tiingo when `TIINGO_API_TOKEN` is set
  (`/tiingo/daily/<sym>/prices?startDate=…&columns=date,close,adjClose,divCash,splitFactor`),
  else yfinance (`Ticker.history(start=…, auto_adjust=False, actions=True)` →
  Close, Adj Close, Dividends, Stock Splits with 0 mapped to 1). One provider per
  symbol per run; a Tiingo miss falls back to yfinance for that symbol and the
  source column records which.
- Incremental: start = last stored date + 1 day; first run start = today −
  `lookback_years`. A symbol returning no rows is logged, not an error.
- Pacing: 0.5 s between Tiingo calls (free tier: 50 req/h).

**SEC facts** (`collectors/sec_facts.py`, `collectors/sec_cik.py`)
- `SEC_USER_AGENT` (e.g. `Name email@example.com`) is required; the collector
  refuses to run without it and `fdc status` says why.
- CIK map from `https://www.sec.gov/files/company_tickers.json`, cached in
  `data/cache/company_tickers.json` for 7 days. Securities with no CIK are left
  `cik NULL` and skipped; `asset_type` for a symbol found in the map is set to
  `stock` unless already classified.
- Per company: GET `https://data.sec.gov/api/xbrl/companyfacts/CIK{cik:0>10}.json`,
  at most once per `sec_max_age_hours`. Every fact in every taxonomy is upserted
  into `sec_facts` in one transaction; then `statements.rebuild(cik)` replaces that
  company's `financial_line_items`. 1 s pause between companies.
- A company whose facts contain no `us-gaap` duration facts (a fund that somehow
  had a CIK) is flagged `asset_type = 'etf'` and skipped next time.

## 8. Configuration and CLI

`config.example.toml` (committed) → `config.toml` (gitignored):

```toml
[paths]
db = "data/warehouse.db"
inbox = "inbox"
watchlist = "watchlist.txt"        # optional, one symbol per line

[prices]
lookback_years = 5

[sec]
max_age_hours = 24

[sources.snaptrade]
enabled = true
dir = "../investing/fidelity/dashboard-data"   # ignored if missing

[classify]                          # manual overrides, symbol = asset_type
# "VXUS" = "etf"
```

`.env.example` → `.env`: `TIINGO_API_TOKEN` (optional), `SEC_USER_AGENT` (required
for the SEC collector).

CLI (`fdc`, via `pyproject.toml` entry point; `python -m financial_data_collector`
also works):

```
fdc init                        create data/, inbox/, config.toml, .env from examples; migrate
fdc sync [--only a,b] [--skip a] [--dry-run]
fdc import <file|dir> [--account LABEL]
fdc status                      per-step last run, age, row counts, latest snapshot date
fdc query "<sql>" [--csv]       ad-hoc read-only query
fdc mcp [--print-config]        Claude Desktop server / its config snippet
```

## 9. Claude access

- **Claude Code**: reads the file. `AGENTS.md` (with `CLAUDE.md` = `@AGENTS.md`)
  documents tables, views and `docs/QUERIES.md`.
- **Claude Desktop**: `fdc mcp` runs a stdio server (official `mcp` Python SDK,
  FastMCP). Tools: `schema()` → tables, views, columns, row counts; `query(sql,
  limit=500)` → rows as JSON, executed on a connection opened with
  `file:…?mode=ro` plus `PRAGMA query_only=1`, after a guard that rejects
  anything not starting with `SELECT`/`WITH`/`EXPLAIN` or containing `;` followed
  by more text; `status()` → `sync_status` rows. `--print-config` prints the
  `mcpServers` JSON with the venv's absolute python path.
- Optional extras in the README: pandas `read_sql`, Datasette.

## 10. Scheduling and failure handling

- WAL journal mode; `busy_timeout` 5 s.
- Each step logs its own `sync_runs` row; an exception in one step is caught,
  logged with the traceback in `message`, and the run continues. Per-symbol and
  per-company writes are single transactions.
- Exit code: 0 if any step succeeded; 1 only when every network step failed;
  2 for a configuration error (missing config, unreadable db).
- `fdc status` flags a step `stale` when its age exceeds twice its cadence
  (positions/prices 24 h, sec 7 d) and prints the last error message.
- Owner scheduling: `scripts/schedule_sync.ps1` registers the Windows task
  `FinancialDataCollector Sync` (trigger: at logon delayed 20 min, repeating every
  12 h) running `scripts/run_sync.ps1`, which activates the venv, runs `fdc sync`,
  and appends to `%LOCALAPPDATA%\financial-data-collector\sync.log`. The script is
  pure ASCII (see finance-tracker's lesson). Uses `Register-ScheduledTask`, never
  `schtasks` (warehouse CLAUDE.md).

## 11. Testing

`pytest`, fixtures under `tests/fixtures/` — **all synthetic**: invented account
names (`Sample Brokerage`, `Sample Roth`), invented round quantities and prices,
and only widely held symbols (`AAPL`, `KO`, `VTI`, `SPAXX`) so nothing in a fixture
can be traced to a real book. Network collectors are tested
with recorded, trimmed responses through the injected `fetch`.

Coverage required before the repo goes public:

- Each adapter: header detection, numeric cleaning, date extraction, cash routing,
  account-number discard, account label fallbacks.
- Idempotency: importing the same file twice, and overlapping history exports,
  change no counts.
- Migrations: apply on an empty database and on one at a prior version; backup
  file created.
- Prices: incremental start date, provider fallback, split factor default.
- SEC: facts upsert (re-fetch adds no duplicates); `statements.rebuild`: annual
  duration filter, latest-filed-wins, quarter detection, derived Q4 present only
  with three siblings, per-share never derived, instants at period ends.
- Views: `portfolio_daily` totals and the Fidelity-scope column; wide financial
  views have one column per seeded line item and correct margins.
- MCP guard: rejects `DELETE`, `PRAGMA`, `ATTACH`, stacked statements; caps rows.
- Privacy: `git ls-files` contains no `*.db`, `*.sqlite*`, `.env`, `config.toml`,
  nothing under `data/` or `inbox/`, and no `*.csv`/`*.json` outside
  `tests/fixtures/` and `seeds/`.

## 12. Repository layout

```
financial-data-collector/
  AGENTS.md  CLAUDE.md (@AGENTS.md)  README.md  LICENSE (MIT)  CHANGELOG.md
  pyproject.toml  config.example.toml  .env.example  .gitignore  watchlist.example.txt
  src/financial_data_collector/
    __init__.py  __main__.py  cli.py  config.py  store.py  migrate.py
    symbols.py  universe.py  statements.py  http.py  mcp_server.py
    adapters/  __init__.py  base.py  fidelity_positions.py  fidelity_history.py  snaptrade.py
    collectors/ __init__.py  prices.py  sec_cik.py  sec_facts.py
  migrations/  0001_init.sql  0002_views.sql
  seeds/       concept_map.csv
  tests/       test_*.py  fixtures/
  scripts/     schedule_sync.ps1  run_sync.ps1
  docs/        QUERIES.md  superpowers/specs/  superpowers/plans/
  data/        .gitkeep   (db, backups/, cache/ — gitignored)
  inbox/       .gitkeep   (processed/ — gitignored)
```

Dependencies: `requests`, `python-dotenv`, `yfinance`, `mcp`; dev: `pytest`.
Python 3.11+. No ORM, no pandas in the package itself (pandas is a consumer).

## 13. Success criteria

1. `fdc init && fdc sync` on this PC fills the database with every SnapTrade
   snapshot since 2026-05-30, the CSV archive, five years of prices for every held
   symbol, and full statements for every held operating company, and a second
   `fdc sync` writes zero data rows.
2. `fdc query "select * from portfolio_daily order by as_of_date desc limit 5"`
   matches the cockpit's household total for the same export date.
3. Claude Desktop, with the printed config pasted in, answers "what was my NVDA
   exposure each month versus its quarterly revenue" from the MCP tools alone.
4. A fresh clone on a machine with only a Fidelity positions CSV in `inbox/` and
   `SEC_USER_AGENT` set reaches the same state for that book.
5. The privacy test passes and the public repo contains no real balances, labels
   or credentials.

## 14. Follow-ups (not in this spec)

- Retire `code/personal/finance-tracker` and its scheduled task.
- Point investing's cockpit at this database (separate spec); then move its
  fetchers here.
- Optional Supabase mirror for claude.ai access.
- Fund look-through (holdings of FXAIX/SCHD/…) from investing's `fund_holdings/`.
