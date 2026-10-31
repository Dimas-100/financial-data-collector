# Valuation foundation, plan 2: collect what's missing — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** The warehouse holds what Parts 4-5 will need and plan 1 left out: benchmark prices (the market and each sector), each company's sector, a daily valuation snapshot for every fund, earnings history with report dates sourced to SEC filings, daily estimate snapshots; and the checker notices a price feed that stopped.

**Architecture:** Benchmarks are ordinary securities marked `benchmark = 1` and collected by the existing prices step. Two new network sync steps, `funds` and `earnings`, run after `sec`; both ask Yahoo through one injected `YahooClient` seam (`collectors/yahoo.py`, yfinance imported lazily), and `earnings` also reads SEC's submissions list. Sectors are assigned at the end of the `earnings` step from the sibling classifications feed, else the company's SEC SIC code through `seeds/sic_sector.csv`. The checker gains one offline check, `closes`. No AI, no derived scores (that is plan 3, Part 4).

**Tech Stack:** Python 3.11+, SQLite (numbered migrations), pytest (no network), yfinance 1.7 (lazy import), SEC EDGAR submissions JSON.

**Spec:** `docs/superpowers/specs/2026-10-30-valuation-foundation-design.md`, Part 3, plus four items plan 1's final review deferred here (ledger `.superpowers/sdd/2026-10-30-valuation-foundation-1-data-and-checker/progress.md`, "Final: parked").

## Global Constraints

- PUBLIC repo: no ticker tied to the owner, no value, account name, home path, key or SEC user agent in code, tests, docs or commit messages. Fixtures use invented companies and widely held symbols (SPY, the sector SPDRs, AAPL, VFIAX are fine as public examples). `tests/test_privacy.py` stays green; `config.example.toml` must equal `CONFIG_EXAMPLE`.
- Only `store.py`, `migrate.py`, `readonly.py` execute SQL. The checker's reads are `Store.check_*` methods.
- Schema changes are new migrations, starting at `0014`; never edit an applied file. This plan adds `0014_benchmarks.sql`, `0015_fund_snapshots.sql`, `0016_earnings.sql`, `0017_sectors.sql`.
- No network in tests: collectors take `fetch`, `yahoo` and `sleep`; `tests/conftest.py` stubs `collectors.yahoo.default_client` (Task 2) as it already stubs the checker's outside source.
- `check/` imports nothing from `derive/`, `statements.py` or the collectors (except `http`); the existing guard test covers the new check.
- Idempotent: a second `fdc sync` on the same New York day makes no second Yahoo or SEC call for a symbol already fetched that day and changes no row.
- Every message from a fetcher passes through `connections/redact.py` with `cfg.sec_user_agent` and `cfg.tiingo_token`.
- Snapshots are stored in the provider's numbers converted to fractions: a yield or expense ratio of 1.21% is stored `0.0121`. Units found live on 2026-10-30 (yfinance 1.7.0): `info["yield"]` is a fraction (SPY 0.0099), `info["dividendYield"]` is in percent (0.99, never used here), `info["netExpenseRatio"]` is in percent (0.0945 = 0.0945%), `info["annualReportExpenseRatio"]` a fraction; `get_earnings_dates()` has columns `EPS Estimate`, `Reported EPS`, `Surprise(%)` (percent) and a tz-aware index; `earnings_estimate` has rows `0q`, `+1q`, `0y`, `+1y` and columns `avg`, `low`, `high`, `numberOfAnalysts`.
- Tests run from the worktree: `PYTHONPATH=src ../../.venv/Scripts/python.exe -m pytest -q`.
- Each task updates the docs it changes: AGENTS.md (layout line, tables list), README (the section on what a sync collects), CHANGELOG `## Unreleased`.

## Review Focus

1. Yahoo changes a unit again (a fund's `yield` arrives as 0.99 meaning 0.99%): the snapshot must not store a 99% yield. Values outside `0 <= yield < 0.25` and `0 <= expense_ratio < 0.05` are stored NULL and the step's message names them. Test in Task 2.
2. The owner actually holds a benchmark symbol (SPY in an account): it is still a holding everywhere (positions, export, the checker's held set), and the benchmark mark changes nothing about that. Test in Task 1.
3. An after-close earnings release whose 8-K is filed the next morning: one event, not two; and an old event whose 8-K has fallen out of SEC's `recent` block, and whose Yahoo row has fallen out of Yahoo's list, keeps its SEC date and its EPS. Tests in Task 3.
4. A sync that runs four times in one day: one Yahoo call per symbol per day for funds and for earnings, and no row changed by the later runs. Tests in Tasks 2 and 3.
5. A holiday or long weekend (Good Friday, then the weekend): the `closes` check must not warn about days the market was shut; and a held symbol that only has a non-final bar is "no final close", not "never priced". Tests in Task 5.

---

## File Structure

- `src/financial_data_collector/sectors.py` — new: the 11 sector names, their default ETFs (Task 1); Yahoo's names mapped to them, the SIC table reader, the feed reader, `assign_sectors` (Task 4).
- `src/financial_data_collector/seeds/sic_sector.csv` — new (Task 4).
- `src/financial_data_collector/collectors/yahoo.py` — new: the `YahooClient` seam and its yfinance implementation (Tasks 2-3).
- `src/financial_data_collector/collectors/funds.py` — new: fund snapshots (Task 2).
- `src/financial_data_collector/collectors/earnings.py` — new: SEC 8-K report dates, Yahoo EPS, the merge, estimate snapshots (Task 3).
- `src/financial_data_collector/migrations/0014_benchmarks.sql` … `0017_sectors.sql` — new.
- `src/financial_data_collector/config.py`, `config.example.toml` — `[benchmarks]` (T1), `[fund_proxies]` (T2), `[check] close_warn_days / close_hold_days` (T5).
- `src/financial_data_collector/models.py` — `FundSnapshot` (T2), `YahooEarning`, `Estimate`, `EarningsEvent` (T3).
- `src/financial_data_collector/store.py` — the write and read methods each task names.
- `src/financial_data_collector/universe.py`, `export/cockpit.py`, `cli.py` — benchmarks in the universe, out of the export (T1); `cmd_check` comment (T5).
- `src/financial_data_collector/sync.py` — steps `funds`, `earnings`; `run_sync(..., yahoo=None)`.
- `src/financial_data_collector/check/offline.py` — check `closes` (T5); `check/outside.py` — `prices2` also covers benchmarks (T1).
- Tests: `tests/test_benchmarks.py`, `tests/test_funds.py`, `tests/test_earnings.py`, `tests/test_sectors.py` (new); `tests/test_check_offline.py`, `tests/test_check_outside.py`, `tests/test_sync.py`, `tests/test_config.py`, `tests/test_export.py`, `tests/conftest.py`.

---

### Task 1: Benchmarks

**Files:**
- Create: `src/financial_data_collector/sectors.py`, `src/financial_data_collector/migrations/0014_benchmarks.sql`, `tests/test_benchmarks.py`
- Modify: `config.py`, `config.example.toml`, `store.py`, `universe.py`, `sync.py` (`_step_prices`, `_step_export`), `cli.py` (`cmd_export`), `check/outside.py`
- Test: `tests/test_benchmarks.py`, `tests/test_config.py`, `tests/test_check_outside.py`, `tests/test_export.py`

**Interfaces:**
- Produces: `sectors.SECTORS: tuple[str, ...]` = `("Communication Services", "Consumer Discretionary", "Consumer Staples", "Energy", "Financials", "Health Care", "Industrials", "Materials", "Real Estate", "Technology", "Utilities")`; `sectors.DEFAULT_SECTOR_ETFS: dict[str, str]` = Communication Services XLC, Consumer Discretionary XLY, Consumer Staples XLP, Energy XLE, Financials XLF, Health Care XLV, Industrials XLI, Materials XLB, Real Estate XLRE, Technology XLK, Utilities XLU.
- Produces: `config.Benchmarks(market: str = "SPY", sectors: dict[str, str] = DEFAULT_SECTOR_ETFS copy)`, frozen, with property `symbols -> tuple[str, ...]` (market first, then the sector ETFs in `SECTORS` order, each once); `Config.benchmarks: Benchmarks`.
- Produces: `securities.benchmark INTEGER NOT NULL DEFAULT 0`; `Store.mark_benchmarks(symbols: Iterable[str]) -> None` (inserts missing rows with `asset_type = 'etf'`, sets `asset_type = 'etf'` on listed rows still `unknown`, sets `benchmark = 1` on listed rows and `0` on every other row, one transaction); `Store.held_symbols() -> set[str]` (positions_latest); `Store.check_benchmark_symbols() -> list[str]` (sorted).
- Produces: `universe_symbols(..., *, include_benchmarks: bool = True)` and `build_universe(..., *, include_benchmarks: bool = True)`; with `False`, a `benchmark = 1` symbol is left out unless held or on the watchlist / research lists.

- [ ] **Step 1: Write the failing tests** (`tests/test_benchmarks.py`, plus config cases)

```python
def test_default_benchmarks_are_spy_and_the_eleven_sector_spdrs(tmp_path):
    cfg = _cfg(tmp_path)                       # init_project + load_config, no [benchmarks] table
    assert cfg.benchmarks.symbols == ("SPY", "XLC", "XLY", "XLP", "XLE", "XLF", "XLV", "XLI", "XLB", "XLRE", "XLK", "XLU")

def test_benchmarks_table_replaces_the_sector_list(tmp_path):
    cfg = _cfg(tmp_path, '[benchmarks]\nmarket = "VTI"\nsectors = { "Technology" = "VGT" }\n')
    assert cfg.benchmarks.symbols == ("VTI", "VGT")

def test_unknown_sector_name_is_a_config_error(tmp_path):
    with pytest.raises(C.ConfigError, match="Tech"):
        _cfg(tmp_path, '[benchmarks]\nsectors = { "Tech" = "XLK" }\n')

def test_prices_step_registers_benchmarks_as_etfs_and_collects_them(project, fixtures):
    rep = sync.run_sync(project, only=["prices"], fetch=_fetch(fixtures), now=NOW, yf=_bars_for_any, sleep=lambda s: None)
    # _fetch as in tests/test_sync.py; _bars_for_any answers two final bars for any symbol
    rows = {r["symbol"]: r for r in store.securities()}
    assert rows["SPY"]["benchmark"] == 1 and rows["SPY"]["asset_type"] == "etf"
    assert store.last_price_date("XLK") is not None

def test_removing_a_benchmark_from_config_unmarks_it(store):
    store.mark_benchmarks(["SPY", "XLK"]); store.mark_benchmarks(["SPY"])
    assert {r["symbol"]: r["benchmark"] for r in store.securities()}["XLK"] == 0

def test_a_benchmark_keeps_a_known_asset_type(store):
    store.upsert_security("SPY", "S&P 500 ETF", "mutual_fund"); store.mark_benchmarks(["SPY"])
    assert _type(store, "SPY") == "mutual_fund"

def test_a_benchmark_is_not_exported_unless_held_or_listed(store, cfg):      # Review Focus 2
    store.mark_benchmarks(["SPY", "XLK"])
    _hold(store, "SPY")                                                      # the owner holds SPY
    assert "SPY" in universe_symbols(store, cfg.watchlist, include_benchmarks=False)
    assert "XLK" not in universe_symbols(store, cfg.watchlist, include_benchmarks=False)
    assert "XLK" in universe_symbols(store, cfg.watchlist)
    assert store.held_symbols() == {"SPY"} and store.check_held_symbols() == ["SPY"]

def test_prices2_also_compares_benchmarks(store, cfg):                       # in test_check_outside.py
    store.mark_benchmarks(["SPY"]); _final_bar(store, "SPY", "2026-10-28", 500.0, source="tiingo")
    found = outside.second_price(_ctx(store, cfg, outside=_Fake(quote=Quote(530.0, "2026-10-28"))))
    assert [(f.symbol, f.severity) for f in found] == [("SPY", "hold")]
```

Plus in `tests/test_export.py`: a sync's export with `XLK` marked benchmark and not held has no `XLK` key in `prices.json`'s `by_ticker` nor in `misses`.

- [ ] **Step 2: Run them to verify they fail**

Run: `PYTHONPATH=src ../../.venv/Scripts/python.exe -m pytest tests/test_benchmarks.py tests/test_config.py tests/test_check_outside.py tests/test_export.py -q`
Expected: FAIL (`AttributeError: ... 'benchmarks'`, no `benchmark` column).

- [ ] **Step 3: Implement**

- `0014_benchmarks.sql`: `ALTER TABLE securities ADD COLUMN benchmark INTEGER NOT NULL DEFAULT 0;` with a header comment in the style of 0011.
- `config.py`: `_benchmarks(raw, cfg_path) -> Benchmarks` — `market` a non-empty string (canonical upper), `sectors` a table whose keys are in `SECTORS` (error names the bad key and lists the valid ones) and values non-empty strings; a given `sectors` replaces the default, it is not merged. `CONFIG_EXAMPLE` gains, after `[connections]`:

```toml
[benchmarks]                       # what a move is measured against; their prices are collected like any symbol, never counted as holdings
market = "SPY"
# sectors = { "Technology" = "XLK", "Health Care" = "XLV" }   # replaces the default: the 11 Select Sector SPDRs
```

  and `config.example.toml` is rewritten to match.
- `_step_prices`: `store.mark_benchmarks(cfg.benchmarks.symbols)` before `build_universe`. `_step_export` and `cmd_export`: `build_universe(..., include_benchmarks=False)`.
- `check/outside.py` `second_price`: covers `_covered(ctx)` plus `ctx.store.check_benchmark_symbols()` (same type filter, each symbol once, sorted); `outside_figures` unchanged.
- AGENTS.md: `benchmark` on `securities` in the tables list, `sectors.py` in the layout. README: a "Benchmarks" paragraph. CHANGELOG line.

- [ ] **Step 4: Run the whole suite**

Run: `PYTHONPATH=src ../../.venv/Scripts/python.exe -m pytest -q`
Expected: all pass (the existing sync tests now also see 12 benchmark symbols in the prices step; adjust their expected counts only where a test counts universe symbols, never by removing a benchmark).

- [ ] **Step 5: Commit** — `git commit -m "benchmarks: SPY and the sector SPDRs collected like any symbol, marked, kept out of the export"`

---

### Task 2: Fund snapshots

**Files:**
- Create: `collectors/yahoo.py`, `collectors/funds.py`, `migrations/0015_fund_snapshots.sql`, `tests/test_funds.py`
- Modify: `config.py` (`[fund_proxies]`), `config.example.toml`, `models.py`, `store.py`, `sync.py`, `tests/conftest.py`, `tests/test_sync.py`

**Interfaces:**
- Consumes: Task 1's `build_universe`, `Config`.
- Produces: `collectors.yahoo.YahooClient` (Protocol) with `fund_info(symbol: str) -> dict | None`; `YfinanceClient` (lazy `import yfinance`; silences the `yfinance` logger inside `try/finally`, restoring its level); `default_client() -> YahooClient`; `PAUSE_SECONDS = 0.5` (between symbols asked).
- Produces: `models.FundSnapshot(symbol: str, date: str, trailing_pe: float | None, forward_pe: float | None, dividend_yield: float | None, expense_ratio: float | None, source: str)`.
- Produces: table `fund_snapshots (symbol, date, trailing_pe, forward_pe, dividend_yield, expense_ratio, source NOT NULL, fetched_at NOT NULL, PRIMARY KEY (symbol, date))`; `securities.last_fund_fetch TEXT` (a New York date).
- Produces: `Store.write_fund_snapshot(snap: FundSnapshot, fetched_at: str) -> int` (1 when inserted or a value or source changed, else 0 and the row, `fetched_at` included, is untouched); `Store.mark_fund_fetch(symbol, day: str)`.
- Produces: `funds.FUND_TYPES = ("etf", "mutual_fund", "fund")`; `funds.figures_from_info(info: dict) -> tuple[dict, list[str]]` (the four figures as fractions, plus notes for values out of range); `funds.collect_funds(store, universe, cfg, *, yahoo, now, sleep, progress=None) -> list[FundResult]`, `FundResult(symbol, source: str | None, written: int, message: str, failed: bool)`.
- Produces: `Config.fund_proxies: dict[str, str]` (canonical upper both sides); `run_sync(..., yahoo=None)` — `None` means `collectors.yahoo.default_client()`, looked up at call time; every step runner receives `yahoo=`. `STEPS` = `(..., "sec", "funds", "derive", ...)`, `"funds"` in `NETWORK_STEPS`, `STALE_HOURS["funds"] = 48`.

Rules the code implements:
- A symbol is asked when it is in the universe, its type is in `FUND_TYPES`, and `last_fund_fetch` is not today (New York date of `now`). It is marked only when the asking did not raise.
- `figures_from_info`: `trailing_pe = trailingPE`, `forward_pe = forwardPE`, `dividend_yield = yield` (already a fraction; `dividendYield` is never read), `expense_ratio = netExpenseRatio / 100` if present, else `annualReportExpenseRatio`. NaN, inf, non-numbers → None. `dividend_yield` outside `[0, 0.25)` and `expense_ratio` outside `[0, 0.05)` → None plus a note `"yield 0.99 out of range"`.
- Own figures count when `trailing_pe` or `dividend_yield` is set. Without them, a `[fund_proxies]` entry is asked instead, and the row takes the proxy's `trailing_pe`, `forward_pe`, `dividend_yield`, with `source = "proxy: SPY via yfinance"` (the proxy's symbol in place of SPY); its `expense_ratio` is always the fund's own (or None), never the proxy's. Own figures give `source = "yfinance"`.
- No figure at all (and no usable proxy): nothing written, message `"no figures"`, still marked fetched.
- `CONFIG_EXAMPLE` gains `[fund_proxies]  # a fund Yahoo has no P/E or yield for: an ETF tracking the same index stands in; fund_snapshots.source says so` and a commented `# "XYZFX" = "SPY"`.
- `_step_funds` message: `"N snapshots written, M without figures, K failed"` (+ the notes and failures by symbol); `status = "error"` only when every asked symbol failed.

- [ ] **Step 1: Write the failing tests** (`tests/test_funds.py` with a `_FakeYahoo(infos: dict[str, dict])`)

```python
SPY_INFO = {"trailingPE": 24.0, "forwardPE": None, "yield": 0.0099, "dividendYield": 0.99, "netExpenseRatio": 0.0945}

def test_snapshot_stores_fractions():
    figs, notes = funds.figures_from_info(SPY_INFO)
    assert figs == {"trailing_pe": 24.0, "forward_pe": None, "dividend_yield": 0.0099, "expense_ratio": pytest.approx(0.000945)}
    assert notes == []

def test_annual_report_expense_ratio_is_already_a_fraction():
    figs, _ = funds.figures_from_info({"trailingPE": 20.0, "annualReportExpenseRatio": 0.0004})
    assert figs["expense_ratio"] == 0.0004

def test_a_yield_in_percent_is_refused_not_stored():                   # Review Focus 1
    figs, notes = funds.figures_from_info({"trailingPE": 20.0, "yield": 0.99})
    assert figs["dividend_yield"] is None and notes == ["yield 0.99 out of range"]

def test_proxy_stands_in_and_says_so(store, cfg_with_proxy):          # [fund_proxies] "XYZFX" = "SPY"
    yahoo = _FakeYahoo({"XYZFX": {"netExpenseRatio": 0.015}, "SPY": SPY_INFO})
    funds.collect_funds(store, ["XYZFX"], cfg_with_proxy, yahoo=yahoo, now=NOW, sleep=lambda s: None)
    row = _snapshot(store, "XYZFX")
    assert row["source"] == "proxy: SPY via yfinance" and row["trailing_pe"] == 24.0
    assert row["expense_ratio"] == pytest.approx(0.00015)                # the fund's own, not SPY's

def test_only_fund_types_are_asked(store, cfg):                         # a stock is never asked
def test_one_call_per_symbol_per_new_york_day_and_no_row_change(store, cfg):   # Review Focus 4
    yahoo = _FakeYahoo({"SPY": SPY_INFO})
    for hour in (12, 16, 22):                                            # 08:00, 12:00, 18:00 New York
        funds.collect_funds(store, ["SPY"], cfg, yahoo=yahoo, now=NOW.replace(hour=hour), sleep=lambda s: None)
    assert yahoo.calls == ["SPY"] and _snapshot(store, "SPY")["fetched_at"] == "2026-10-29T12:00:00Z"

def test_a_failed_ask_is_not_marked_and_is_asked_again(store, cfg):
def test_no_figures_writes_nothing(store, cfg):
def test_write_fund_snapshot_returns_zero_for_the_same_values(store):

# tests/test_sync.py
def test_steps_include_funds_after_sec():
    assert sync.STEPS[sync.STEPS.index("sec") + 1] == "funds" and "funds" in sync.NETWORK_STEPS
```

`tests/conftest.py`: an autouse `no_real_yahoo` fixture monkeypatching `collectors.yahoo.default_client` to a client whose every method answers None / `[]`. The existing step-list assertions in `tests/test_sync.py` gain `"funds"`.

- [ ] **Step 2: Run them to verify they fail** — `... -m pytest tests/test_funds.py tests/test_sync.py -q`; expected FAIL (`ModuleNotFoundError: ...collectors.funds`).

- [ ] **Step 3: Implement** per the Interfaces and rules above. Docs: AGENTS layout gains `yahoo.py`, `funds.py`; the tables list gains `fund_snapshots` (history starts the day this ships; one row per fund per New York day; `source` `yfinance` or `proxy: X via yfinance`; yields and expense ratios are fractions). README, CHANGELOG.

- [ ] **Step 4: Run the whole suite** — expected: all pass.

- [ ] **Step 5: Commit** — `git commit -m "funds step: a daily valuation snapshot per fund from Yahoo, a proxy ETF when a fund has none"`

---

### Task 3: Earnings history and estimate snapshots

**Files:**
- Create: `collectors/earnings.py`, `migrations/0016_earnings.sql`, `tests/test_earnings.py`, `tests/fixtures/sec/submissions_EARN.json`
- Modify: `collectors/yahoo.py`, `models.py`, `store.py`, `sync.py`, `tests/conftest.py`, `tests/test_sync.py`

**Interfaces:**
- Consumes: Task 2's `YahooClient`, `run_sync(yahoo=)`, the `yahoo` runner kwarg.
- Produces: `YahooClient.earnings_dates(symbol: str, limit: int) -> list[YahooEarning]` and `YahooClient.estimates(symbol: str) -> list[Estimate]`; `models.YahooEarning(when: datetime (tz-aware), eps_expected: float | None, eps_reported: float | None, surprise_pct: float | None)`; `models.Estimate(period: str, eps_avg, eps_low, eps_high: float | None, analysts: int | None)`; `models.EarningsEvent(symbol, report_date: str, time_of_day: str | None, accession: str | None, eps_expected, eps_reported, surprise_pct: float | None, date_source: str, time_source: str | None, eps_source: str | None)`.
- Produces: tables

```sql
CREATE TABLE earnings_events (
  symbol TEXT NOT NULL, report_date TEXT NOT NULL,
  time_of_day TEXT CHECK (time_of_day IN ('before_open', 'during', 'after_close')),
  accession TEXT,                       -- the 8-K (item 2.02) that dates it, as SEC writes it
  eps_expected REAL, eps_reported REAL, surprise_pct REAL,   -- surprise in percent, as Yahoo gives it (6.74 = +6.74%)
  date_source TEXT NOT NULL CHECK (date_source IN ('sec', 'yahoo')),
  time_source TEXT, eps_source TEXT,
  PRIMARY KEY (symbol, report_date));
CREATE TABLE estimate_snapshots (
  symbol TEXT NOT NULL, date TEXT NOT NULL,
  period TEXT NOT NULL CHECK (period IN ('0q', '+1q', '0y', '+1y')),
  eps_avg REAL, eps_low REAL, eps_high REAL, analysts INTEGER,
  source TEXT NOT NULL, fetched_at TEXT NOT NULL,
  PRIMARY KEY (symbol, date, period));
ALTER TABLE securities ADD COLUMN last_earnings_fetch TEXT;
ALTER TABLE securities ADD COLUMN sic INTEGER;
```

- Produces: `earnings.report_dates(recent: dict) -> list[tuple[str, str]]` (`(filing_date, accession)` of each `8-K` whose `items` split on `,` holds `2.02`, accession matching `\d{10}-\d{2}-\d{6}`); `earnings.time_of_day(when: datetime) -> str | None`; `earnings.merge(symbol, sec: list[tuple[str, str]], yahoo: list[YahooEarning], stored: list[EarningsEvent], today: date) -> list[EarningsEvent]` (pure, sorted by date); `earnings.collect_earnings(store, universe, cfg, *, fetch, yahoo, now, sleep, progress=None) -> list[EarningsResult]`, `EarningsResult(symbol, events: int, estimates: int, message: str, failed: bool)`; `YAHOO_LIMIT = 24`; `MATCH_DAYS = 3`.
- Produces: `Store.earnings_events_of(symbol) -> list[EarningsEvent]`; `Store.replace_earnings(symbol, events) -> int` (writes only when the set differs from what is stored; returns the rows added, removed or changed); `Store.write_estimates(symbol, day, estimates, fetched_at: str) -> int` (idempotent like `write_fund_snapshot`); `Store.mark_earnings_fetch(symbol, day)`; `Store.set_sic(symbol, sic: int | None)`.
- Produces: `STEPS` = `(..., "sec", "funds", "earnings", "derive", ...)`, `"earnings"` in `NETWORK_STEPS`, `STALE_HOURS["earnings"] = 48`.

Rules the code implements:
- Asked: universe symbols typed `stock` or `foreign_filer` whose `last_earnings_fetch` is not today (New York); marked when no part raised. Per symbol: SEC submissions (`check/filings.py`'s URL shape, but the collector's own constant and headers: same user agent, `PAUSE_SECONDS = 1.0` between companies, a CIK shared by several symbols fetched once a run) when `cfg.sec_user_agent` and a CIK are set; it stores `sic` (the submissions JSON's `sic`, an int, or None). Then Yahoo earnings dates (`limit=YAHOO_LIMIT`) and estimates.
- Without `SEC_USER_AGENT` the SEC part is skipped for everyone, the Yahoo part still runs, the step is `ok`, and its message begins `"SEC part skipped: SEC_USER_AGENT is not set; "`. A failed SEC fetch for one company keeps its Yahoo part and leaves it unmarked (asked again next run).
- `time_of_day` (New York time): exactly 00:00 → None (Yahoo knows only the day); before 09:30 → `before_open`; at or after 16:00 → `after_close`; otherwise `during`.
- `merge`, in this order:
  1. SEC events = the stored rows with an accession, plus `sec`; the same accession once (the new date wins).
  2. Yahoo rows dated after `today` (New York) are dropped (the next date lives in investing's feed).
  3. For each SEC event, oldest first: the unused Yahoo row with `y <= s <= y + MATCH_DAYS` days, nearest `s`, gives it EPS (`eps_source = "yahoo"`); its time only when `y == s` (`time_source = "yahoo"`). No match: the stored row with that accession keeps its EPS and time, else they are None.
  4. Each unused Yahoo row whose day has no SEC event within `[y, y + MATCH_DAYS]`: a `date_source = "yahoo"` event at `y`.
  5. Each stored `date_source = "yahoo"` row not replaced by steps 3-4 and with no SEC event within `[date, date + MATCH_DAYS]` is kept as stored.
- Estimate snapshots: one row per period Yahoo gives, `date` = New York day of `now`, `source = "yfinance"`; NaN → None.
- `_step_earnings` message: `"N companies asked, E events changed, S estimate rows written"` (+ failures by symbol, redacted).

- [ ] **Step 1: Write the failing tests** (`tests/test_earnings.py`; fixture `submissions_EARN.json` is an invented CIK `0000999002`, name "Sample Earnings Co", `sic` 3571, `recent` with `items`: two `8-K` rows `"2.02,9.01"` filed 2026-07-31 and 2026-05-01, one `8-K` `"5.07"`, one `10-Q`)

```python
def _y(day, hour, minute=0, eps=(1.89, 2.02, 6.74)):
    return YahooEarning(datetime(*day, hour, minute, tzinfo=NY), *eps)

def test_report_dates_are_8ks_with_item_2_02():
    assert earnings.report_dates(EARN["filings"]["recent"]) == [("2026-07-31", "0000999002-26-000031"), ("2026-05-01", "0000999002-26-000018")]

@pytest.mark.parametrize("hm, want", [((0, 0), None), ((8, 0), "before_open"), ((9, 30), "during"), ((16, 0), "after_close")])
def test_time_of_day(hm, want): ...

def test_same_day_8k_takes_yahoo_eps_and_time():
    (e,) = earnings.merge("SMPL", [("2026-07-30", A1)], [_y((2026, 7, 30), 16)], [], TODAY)
    assert (e.report_date, e.accession, e.date_source, e.time_of_day, e.eps_reported, e.surprise_pct) == \
           ("2026-07-30", A1, "sec", "after_close", 2.02, 6.74)

def test_next_morning_8k_is_one_event_not_two():                      # Review Focus 3
    (e,) = earnings.merge("SMPL", [("2026-07-31", A1)], [_y((2026, 7, 30), 16)], [], TODAY)
    assert (e.report_date, e.date_source, e.time_of_day, e.eps_reported) == ("2026-07-31", "sec", None, 2.02)

def test_yahoo_only_and_sec_only_events():
def test_future_yahoo_rows_are_left_out():
def test_old_event_keeps_its_date_and_eps_when_both_lists_have_dropped_it():   # Review Focus 3
    old = EarningsEvent("SMPL", "2024-07-31", None, A0, 1.30, 1.40, 7.7, "sec", None, "yahoo")
    events = earnings.merge("SMPL", [], [], [old], TODAY)
    assert events == [old]

def test_a_stored_yahoo_row_is_absorbed_when_its_8k_appears_a_day_later():
    stored = [EarningsEvent("SMPL", "2026-07-30", "after_close", None, 1.89, 2.02, 6.74, "yahoo", "yahoo", "yahoo")]
    (e,) = earnings.merge("SMPL", [("2026-07-31", A1)], [], stored, TODAY)
    assert (e.report_date, e.accession, e.eps_reported) == ("2026-07-31", A1, 2.02)

def test_collect_stores_sic_events_and_estimates(store, cfg_with_agent):
def test_without_sec_user_agent_the_yahoo_part_still_runs(store, cfg):
def test_one_call_per_symbol_per_new_york_day_and_no_row_change(store, cfg_with_agent):   # Review Focus 4
def test_a_failed_sec_fetch_keeps_yahoo_and_is_asked_again(store, cfg_with_agent):
def test_sec_errors_are_redacted(store, cfg_with_agent):              # the agent string never appears in a message
```

`tests/conftest.py`'s `no_real_yahoo` stub gains `earnings_dates` and `estimates` answering `[]`; the step lists in `tests/test_sync.py` gain `"earnings"`.

- [ ] **Step 2: Run them to verify they fail** — expected FAIL (`ModuleNotFoundError: ...collectors.earnings`).

- [ ] **Step 3: Implement** per the Interfaces and rules. `YfinanceClient.earnings_dates` reads `Ticker.get_earnings_dates(limit=...)` (None or empty → `[]`); `estimates` reads `Ticker.earnings_estimate`. Docs: AGENTS layout and tables list (`earnings_events`, `estimate_snapshots`, `securities.sic`), README, CHANGELOG.

- [ ] **Step 4: Run the whole suite** — expected: all pass.

- [ ] **Step 5: Commit** — `git commit -m "earnings step: report dates from SEC 8-K item 2.02, EPS and surprise from Yahoo, daily estimate snapshots"`

---

### Task 4: Sectors

**Files:**
- Create: `seeds/sic_sector.csv`, `migrations/0017_sectors.sql`, `tests/test_sectors.py`, `tests/fixtures/investing/data/classifications.json`
- Modify: `sectors.py`, `store.py`, `sync.py` (`_step_earnings`) (`pyproject.toml` already ships `seeds/*.csv`)

**Interfaces:**
- Consumes: Task 1's `SECTORS`; Task 3's `securities.sic` and `_step_earnings`.
- Produces: `securities.sector TEXT`, `securities.sector_source TEXT`; `Store.set_sector(symbol, sector: str | None, source: str | None) -> bool` (True when changed).
- Produces: `sectors.YAHOO_SECTORS: dict[str, str]` = Communication Services → Communication Services, Consumer Cyclical → Consumer Discretionary, Consumer Defensive → Consumer Staples, Energy → Energy, Financial Services → Financials, Healthcare → Health Care, Industrials → Industrials, Basic Materials → Materials, Real Estate → Real Estate, Technology → Technology, Utilities → Utilities.
- Produces: `sectors.load_sic_ranges() -> list[tuple[int, int, str]]` (package seed; raises `ValueError` on a sector not in `SECTORS`); `sectors.sector_for_sic(sic: int | None, ranges) -> str | None` (the narrowest range holding it); `sectors.read_feed(path: Path) -> tuple[dict[str, str], str | None]` (canonical symbol → sector from `by_ticker[*].sector` through `YAHOO_SECTORS`, unknown names skipped; and the file's `as_of`; a missing or unreadable file → `({}, None)`); `sectors.assign_sectors(store, cfg) -> int` (rows changed).

Rules:
- Assigned for securities typed `stock` or `foreign_filer`: the feed (`cfg.investing_dir / "data" / "classifications.json"`) first, `sector_source = "feed: classifications.json as of 2026-10-27"` (the date part of its `as_of`); else the SIC table, `sector_source = "sec: SIC 3571"`; else both NULL. Run at the end of `_step_earnings`, every run (it reads only stored data and the sibling file); the step's message adds `", K sectors changed"`.
- `seeds/sic_sector.csv`, columns `sic_from,sic_to,sector,what`, exactly:

```
sic_from,sic_to,sector,what
100,999,Consumer Staples,agriculture
1000,1299,Materials,metal and coal mining
1300,1399,Energy,oil and gas extraction
1400,1499,Materials,nonmetallic minerals
1500,1799,Industrials,construction
2000,2199,Consumer Staples,food and tobacco
2200,2399,Consumer Discretionary,textiles and apparel
2400,2499,Materials,lumber and wood
2500,2599,Consumer Discretionary,furniture
2600,2699,Materials,paper
2700,2799,Communication Services,printing and publishing
2800,2899,Materials,chemicals
2830,2836,Health Care,drugs
2840,2844,Consumer Staples,soap and cosmetics
2900,2999,Energy,petroleum refining
3000,3099,Materials,rubber and plastics
3100,3199,Consumer Discretionary,leather
3200,3399,Materials,stone clay glass and primary metals
3400,3499,Industrials,fabricated metal
3500,3599,Industrials,machinery
3570,3579,Technology,computers and office equipment
3600,3699,Industrials,electrical equipment
3630,3639,Consumer Discretionary,household appliances
3651,3652,Consumer Discretionary,household audio and video
3660,3679,Technology,communications equipment and electronic components
3700,3799,Industrials,transportation equipment
3710,3716,Consumer Discretionary,motor vehicles
3800,3899,Industrials,instruments
3820,3829,Technology,measuring instruments
3840,3851,Health Care,medical instruments
3900,3999,Consumer Discretionary,miscellaneous manufacturing
4000,4799,Industrials,transportation
4800,4899,Communication Services,communications
4900,4999,Utilities,electric gas and water
4950,4959,Industrials,refuse systems
5000,5199,Industrials,wholesale
5120,5122,Health Care,drug wholesale
5140,5149,Consumer Staples,grocery wholesale
5200,5999,Consumer Discretionary,retail
5400,5499,Consumer Staples,food stores
5912,5912,Consumer Staples,drug stores
6000,6499,Financials,finance and insurance
6500,6599,Real Estate,real estate
6700,6799,Financials,holding and investment offices
6798,6798,Real Estate,real estate investment trusts
7000,7099,Consumer Discretionary,hotels
7200,7299,Consumer Discretionary,personal services
7300,7399,Industrials,business services
7370,7379,Technology,computer services and software
7500,7599,Consumer Discretionary,auto services
7800,7899,Communication Services,motion pictures
7900,7999,Consumer Discretionary,amusement and recreation
8000,8099,Health Care,health services
8200,8299,Consumer Discretionary,education
8700,8799,Industrials,engineering and management services
```

- [ ] **Step 1: Write the failing tests**

```python
@pytest.mark.parametrize("sic, want", [(3571, "Technology"), (2834, "Health Care"), (6798, "Real Estate"),
    (6022, "Financials"), (1311, "Energy"), (3634, "Consumer Discretionary"), (5912, "Consumer Staples"),
    (7372, "Technology"), (9999, None), (None, None)])
def test_sector_for_sic(sic, want):
    assert sectors.sector_for_sic(sic, sectors.load_sic_ranges()) == want

def test_no_sic_code_has_two_equally_narrow_ranges():
    ranges = sectors.load_sic_ranges()
    for code in range(100, 10000):
        spans = sorted(hi - lo for lo, hi, _ in ranges if lo <= code <= hi)
        assert len(spans) < 2 or spans[0] != spans[1], code

def test_every_seed_sector_is_a_known_one_and_has_a_default_etf():
def test_feed_maps_yahoo_names_and_skips_unknown_ones(fixtures):      # fixture: AAPL Technology, JPM Financial Services, ZZZ "Odd"
def test_feed_wins_over_sic_and_says_where_it_came_from(store, cfg):
    # AAPL in the feed as Technology with SIC 6022 stored: Technology, "feed: classifications.json as of 2026-10-27"
def test_sic_is_the_fallback_and_says_so(store, cfg):               # "sec: SIC 2834" -> Health Care
def test_a_fund_gets_no_sector(store, cfg):
def test_assign_twice_changes_nothing_the_second_time(store, cfg):
def test_missing_feed_file_falls_back_to_sic(store, cfg):
```

- [ ] **Step 2: Run them to verify they fail** — expected FAIL (`AttributeError: module ... has no attribute 'load_sic_ranges'`).

- [ ] **Step 3: Implement** per the Interfaces and rules; `load_sic_ranges` reads `migrate.SEEDS_DIR / "sic_sector.csv"` (the folder `concept_map.csv` is read from) with `csv.DictReader`. Docs: AGENTS tables list (`sector`, `sector_source`), README ("sector: the sibling feed's, else the SEC SIC code's; the sector's benchmark is `[benchmarks] sectors`"), CHANGELOG.

- [ ] **Step 4: Run the whole suite** — expected: all pass; `tests/test_privacy.py` accepts the new CSV and JSON (both under allowed folders).

- [ ] **Step 5: Commit** — `git commit -m "sectors: each company's sector from the classifications feed, else its SEC SIC code"`

---

### Task 5: The checker sees a stopped price feed; two plan-1 leftovers

**Files:**
- Modify: `check/offline.py`, `config.py` (`CheckSettings`), `config.example.toml`, `store.py`, `cli.py` (`cmd_check` comment), `README.md`, `AGENTS.md`
- Test: `tests/test_check_offline.py`, `tests/test_config.py`

**Interfaces:**
- Consumes: Task 1's `Store.check_benchmark_symbols()`; existing `check_held_symbols`, `check_asset_types`, `check_newest_final_bar`, `check_first_price_date`, `calendar.trading_days`.
- Produces: `CheckSettings.close_warn_days: int = 2`, `close_hold_days: int = 5` (whole numbers ≥ 1, warn ≤ hold, same validation style as the month settings); `Store.check_first_seen() -> dict[str, str]` (securities.first_seen); `offline.closes(ctx) -> list[Finding]`, registered as `CHECKS["closes"]` after `gaps`.

Rules:
- Covered: held symbols plus benchmarks, each once, not typed `money_market` or `crypto`.
- Missed days = NYSE trading days strictly after the newest final bar's date and strictly before today (New York date of `ctx.now`). `missed >= close_hold_days` → `hold`, field `"*"`; else `missed >= close_warn_days` → `warn`, field `"close"`. `date_from` = the first missed day, `date_to` None; message `"newest final close is from 2026-10-23, N trading days ago (the feed may have stopped)"`; evidence `{"newest": ..., "missed": [...]}`.
- No final bar but some bar: `warn`, field `"close"`, `date_from` = the first bar's date, message `"no final closing price yet, only a bar fetched before the close"`.
- No bar at all: `warn`, field `"close"`, `date_from` = the symbol's `first_seen` (stable, so it doesn't churn daily), message `"held, never priced: no price from any provider"` (for a benchmark: `"benchmark, never priced: …"`).
- `CONFIG_EXAMPLE` `[check]` gains `close_warn_days = 2  # the newest final close is this many trading days old: warn` and `close_hold_days = 5  # ... this many: hold the symbol's values`.
- The gaps test fix: in `test_gaps_count_only_days_before_the_newest_final_bar`, also write a NON-final bar on `2026-04-09` (fetched 2026-04-09 15:00 UTC) and assert `f.evidence["missing"] == ["2026-04-09"]` still (a non-final bar inside the range is a missing day).
- The `cmd_check` comment, replaced by: `valuation_latest.checked_at is the finish time of the newest check run whose derive_run_id is the newest ok derive; a check that ran while a sync was mid-derive would cover the previous derive, so wait for a running sync first`.

- [ ] **Step 1: Write the failing tests** (in `tests/test_check_offline.py`, using its `_hold`, `_bars_on`, `_weekdays`)

```python
THU = datetime(2026, 10, 29, 12, 0, tzinfo=timezone.utc)      # 08:00 New York

def test_a_close_two_trading_days_old_warns():
    # newest final bar Mon 2026-10-26: Tue, Wed missed (Thu is today)
    (f,) = offline.closes(_ctx(store, cfg, THU))
    assert (f.check_name, f.severity, f.field, f.date_from, f.evidence["missed"]) == \
           ("closes", "warn", "close", "2026-10-27", ["2026-10-27", "2026-10-28"])

def test_yesterdays_close_is_fine():
def test_five_missed_trading_days_hold_every_value():                 # newest Wed 10-21 -> 22, 23, 26, 27, 28
    (f,) = offline.closes(_ctx(store, cfg, THU)); assert (f.severity, f.field) == ("hold", "*")

def test_good_friday_and_the_weekend_are_not_missed_days():           # Review Focus 5
    # newest final bar Thu 2026-04-02; today Mon 2026-04-06 08:00 New York: Good Friday is shut -> nothing
    assert offline.closes(_ctx(store, cfg, datetime(2026, 4, 6, 12, 0, tzinfo=timezone.utc))) == []

def test_only_a_non_final_bar_is_not_never_priced():                  # Review Focus 5
def test_held_never_priced_warns_with_a_stable_date():
    (f,) = offline.closes(...); assert f.date_from == _first_seen(store, "NEWCO") and "never priced" in f.message
def test_benchmarks_are_covered_and_money_markets_are_not():
def test_thresholds_come_from_config():                               # close_warn_days=3: the 2-day case is quiet
def test_closes_is_registered_after_gaps():
    names = list(check.CHECKS); assert names.index("closes") == names.index("gaps") + 1

# tests/test_config.py
def test_close_warn_days_must_not_exceed_close_hold_days(tmp_path):
```

- [ ] **Step 2: Run them to verify they fail** — expected FAIL (`AttributeError: module ... offline has no attribute 'closes'`).

- [ ] **Step 3: Implement** per the rules; amend the gaps test and the `cmd_check` comment. README's checker section and AGENTS' `check/` line name `closes`.

- [ ] **Step 4: Run the whole suite** — expected: all pass (the frame test that runs every registered check on an empty store still passes with `closes`).

- [ ] **Step 5: Commit** — `git commit -m "check: closes warns, then holds, when a symbol's newest final close falls behind; held-never-priced warns"`

---

### Task 6: A real run on a copy (no code)

**Files:** none in the repo. Work in the session's scratch folder; record results in the ledger.

- [ ] **Step 1:** Copy the owner's `data/warehouse.db` (from fdc's main checkout) into a scratch folder with SQLite's backup API (`sqlite3.connect("file:...?mode=ro", uri=True).backup(dest)`), never a file copy of a live WAL database. Write a scratch `config.toml` whose `[paths] db` is the copy's absolute path and whose `[sources.investing] content_dir` points at the real sibling folder (read-only use). No `.env` there: the SEC part of `earnings` is skipped by design, which this run also verifies.

- [ ] **Step 2:** Run `PYTHONPATH=src ../../.venv/Scripts/python.exe -m financial_data_collector --root <scratch folder> sync --only prices,funds,earnings,derive,check`, in the background if it runs long.

Expected, and recorded in the ledger as counts only (no symbols tied to the owner, no values): 12 benchmarks priced 5 years back; one `fund_snapshots` row per fund held; `earnings_events` rows from Yahoo only (`date_source = 'yahoo'`, since no agent); `estimate_snapshots` rows for most stocks; sectors filled for stocks the feed lists; the `earnings` message begins `SEC part skipped`; `fdc check --summary` on the copy shows no `closes` hold that a look at the copy's prices does not justify; a second run of `--only funds,earnings` the same day makes no Yahoo call (the step messages say `0 asked`) and changes no row (compare `total_changes` or row hashes).

- [ ] **Step 3:** Anything unexpected is a finding for a fix task before the whole-branch review, not a note.

---

## Not in this plan

- Scores, bands, price shape, drop causes (plan 3, Part 4) and the split (plan 4, Part 5). They will read `fund_snapshots`, `earnings_events`, `estimate_snapshots`, `securities.sector` and `[benchmarks]`.
- Checking the new tables (a fund P/E out of range, an estimate jump): Part 4's money-driving outputs wait for a check run; what those checks are is decided with Part 4's design.
- News for drop causes stays in the sibling feed and is read by Part 4.
- Earnings events older than SEC's `filings.recent` block (it usually reaches a few years back) and Yahoo's 24 rows: no backfill from SEC's older `files` pages yet.
- SEC's `acceptanceDateTime` for the time of day: its time zone was not verified, so the time comes from Yahoo only.
