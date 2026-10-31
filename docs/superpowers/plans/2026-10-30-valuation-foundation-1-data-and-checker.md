# Valuation foundation, plan 1: correct data and the checker — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Every valuation number in the warehouse is either correct or explicitly "not known", and an independent checker keeps finding bad or stale values and holds them back from consumers.

**Architecture:** Part 1 fixes the collectors and `derive/valuation.py` so known failure classes become a `status` instead of a wrong number. Part 2 adds a `check/` package with its own entry point (`fdc check`), its own store methods over raw tables, and two new tables; `valuation_latest` becomes a view that applies open holds. The checker runs as the last sync step and on its own daily task; a failing check never fails a sync.

**Tech Stack:** Python 3.11+, SQLite (numbered migrations), pytest (no network), yfinance (lazy import), SEC EDGAR JSON APIs.

**Spec:** `docs/superpowers/specs/2026-10-30-valuation-foundation-design.md` (Parts 1 and 2). Parts 3-5 get plans 2-4 after this one ships.

## Global Constraints

- Only `store.py`, `migrate.py` and `readonly.py` execute SQL (AGENTS.md). The checker's reads are new `Store` methods prefixed `check_`.
- Schema changes are new migrations `0011_*.sql`, `0012_*.sql`; never edit an applied file.
- No network in tests: every collector and check takes `fetch` (and `yf` where it calls yfinance) as a parameter.
- Fixtures are synthetic: invented companies, widely held symbols only; `tests/test_privacy.py` stays green.
- Raw tables (`prices`, `sec_facts`, `valuation_daily`) are never changed by a hold.
- Columns are only added to `valuation_latest`, never removed or renamed.
- `check/` imports nothing from `derive/`, `statements.py` or `collectors/` (except `http`). A test enforces it.
- Keys only through `connections/keys.py`; every outside message through `connections/redact.py`.
- Tests run from the worktree with `PYTHONPATH=src ../../.venv/Scripts/python.exe -m pytest -q` (the venv's editable install points at the main checkout).

## Review Focus

1. A company whose newest TTM is 15+ months old but whose filings are otherwise fine (a late filer): it must show `stale`, never a ratio, and recover the day a new filing lands. Test in Task 4.
2. A hold whose finding later clears: `valuation_latest` must show the raw value again with no refetch. Test in Task 8.
3. A check run that crashes halfway: findings it never reached keep their previous state (not cleared), and the sync still reports `ok` for its other steps. Test in Task 7.
4. A symbol parked as a non-ticker that later appears in a position (a new listing): it must be un-parked by that position. Test in Task 2.
5. Two price providers disagreeing on a split day (one has applied the split, one hasn't): the second-source check must compare split-adjusted closes and not raise a 50% `hold`. Test in Task 11.

---

## File Structure

- `src/financial_data_collector/migrations/0011_valuation_status.sql` — `prices.final`, `prices.fetched_at`, `securities.parked_reason`, `securities.miss_runs`, `valuation_daily.status`, `valuation_daily.status_reason`.
- `src/financial_data_collector/migrations/0012_checks.sql` — `check_runs`, `check_findings`, the new `valuation_latest` view.
- `src/financial_data_collector/collectors/prices.py` — final-close rule, miss counting.
- `src/financial_data_collector/collectors/sec_facts.py` — `foreign_filer` and `fund` classification.
- `src/financial_data_collector/universe.py` — parked symbols skipped.
- `src/financial_data_collector/derive/valuation.py` — final bars only, staleness cut, status, share classes.
- `src/financial_data_collector/config.py` — `[share_classes]`, `[check]`.
- `src/financial_data_collector/seeds/concept_map.csv` — one capex row.
- `src/financial_data_collector/check/__init__.py` — `run_check(...)`, `CHECKS`.
- `src/financial_data_collector/check/findings.py` — `Finding`, keying, clearing rules.
- `src/financial_data_collector/check/offline.py` — fresh-enough, impossible values, gaps, recompute.
- `src/financial_data_collector/check/filings.py` — SEC submissions vs `sec_facts`.
- `src/financial_data_collector/check/outside.py` — second price source and outside figures (yfinance).
- `src/financial_data_collector/check/calendar.py` — NYSE trading days (rules + holiday list).
- `src/financial_data_collector/sync.py`, `cli.py`, `console/model.py` — the `check` step, `fdc check`, status lines.
- `scripts/schedule_check.ps1` — the daily backstop task.
- Tests: `tests/test_prices.py`, `tests/test_universe.py`, `tests/test_sec.py`, `tests/test_valuation.py`, `tests/test_check_*.py`, `tests/test_sync.py`, `tests/test_cli.py`.

---

### Task 1: Final closes

**Files:**
- Create: `src/financial_data_collector/migrations/0011_valuation_status.sql`
- Modify: `src/financial_data_collector/store.py` (`write_prices`, `prices_series`), `src/financial_data_collector/collectors/prices.py`
- Test: `tests/test_prices.py`, `tests/test_valuation.py`

**Interfaces:**
- Produces: `prices.final INTEGER NOT NULL DEFAULT 1`, `prices.fetched_at TEXT`; `Store.write_prices(symbol, bars, source, *, fetched_at: datetime | None = None) -> int` (None = a caller that doesn't know the time: written final, as today); `prices_mod.is_final(bar_date: str, fetched_at: datetime) -> bool`; `Store.prices_series(symbol, *, final_only: bool = False)` (the default keeps `prices.json`'s export as it is; `derive/valuation.py` passes `final_only=True`).
- The migration also adds the Task 2 and Task 4 columns (one migration for Part 1): `securities.parked_reason TEXT`, `securities.miss_runs INTEGER NOT NULL DEFAULT 0`, `valuation_daily.status TEXT NOT NULL DEFAULT 'ok'`, `valuation_daily.status_reason TEXT`.

- [ ] **Step 1: Write the failing tests**

```python
def test_bar_fetched_before_close_is_not_final():
    ny = ZoneInfo("America/New_York")
    assert not P.is_final("2026-10-30", datetime(2026, 10, 30, 15, 37, tzinfo=ny))
    assert P.is_final("2026-10-30", datetime(2026, 10, 30, 16, 15, tzinfo=ny))
    assert P.is_final("2026-10-29", datetime(2026, 10, 30, 9, 0, tzinfo=ny))

def test_later_fetch_makes_a_bar_final(tmp_path):
    store = Store.open(tmp_path / "w.db")
    bar = PriceBar("2026-10-30", 10.0, 10.0)
    store.write_prices("KO", [bar], "yfinance", fetched_at=datetime(2026, 10, 30, 19, 37, tzinfo=timezone.utc))
    assert store.query("SELECT final FROM prices")[0][0] == 0
    store.write_prices("KO", [bar], "tiingo", fetched_at=datetime(2026, 10, 31, 12, 0, tzinfo=timezone.utc))
    assert store.query("SELECT final, source FROM prices")[0][:] == (1, "tiingo")

def test_valuation_ignores_non_final_bars(tmp_path):  # in test_valuation.py
    # one final bar on 2025-05-01, one non-final on 2025-05-02 -> one valuation row, dated 2025-05-01
    ...
    assert [r["date"] for r in rows] == ["2025-05-01"]
```

- [ ] **Step 2: Run them to verify they fail**

Run: `PYTHONPATH=src ../../.venv/Scripts/python.exe -m pytest -q tests/test_prices.py tests/test_valuation.py`
Expected: FAIL (`is_final` not defined, `fetched_at` unexpected keyword).

- [ ] **Step 3: Implement**

`is_final`: a bar is final when `fetched_at` in New York is on a later date than the bar, or on the bar's date at 16:15 or later. `write_prices` writes `final` and `fetched_at` (UTC ISO `...Z`) and the upsert overwrites both. `collect_prices` passes its `now` (add `now: datetime` beside `today`; `_step_prices` already has it). Migration back-fills existing rows as `final = 1`, except rows whose date equals the newest date in `prices` (unknown fetch time) as `final = 0`; the next run's `REFRESH_DAYS` window rewrites them. `prices_series(final_only=True)` adds `AND final = 1`.

- [ ] **Step 4: Run the tests, then the whole suite**

Run: `PYTHONPATH=src ../../.venv/Scripts/python.exe -m pytest -q`
Expected: all pass, existing `write_prices` calls in tests unchanged.

- [ ] **Step 5: Commit** — `git commit -m "prices: a bar fetched before the close is not final; valuation uses final closes only"`

### Task 2: Park non-tickers

**Files:**
- Modify: `src/financial_data_collector/collectors/prices.py`, `universe.py`, `store.py`, `collectors/sec_facts.py`, `console/model.py`
- Test: `tests/test_universe.py`, `tests/test_prices.py`

**Interfaces:**
- Produces: `Store.count_miss(symbol: str) -> int` (increments `miss_runs`, returns the new count), `Store.reset_miss(symbol)`, `Store.park(symbol, reason: str)`, `Store.unpark(symbol)`; `PARK_AFTER = 3` in `universe.py`. `universe_symbols(...)` leaves out parked symbols.

- [ ] **Step 1: Write the failing tests**

```python
def test_symbol_with_no_cik_no_bars_no_position_parks_after_three_runs(tmp_path): ...
    # three collect_prices runs where both providers return nothing for "PRIVCO"
    sec = {r["symbol"]: r for r in store.securities()}
    assert sec["PRIVCO"]["parked_reason"] == "no price from any provider, no CIK, not held"
    assert "PRIVCO" not in universe_symbols(store, wl)

def test_a_parked_symbol_that_appears_in_a_position_is_unparked(tmp_path): ...
    # park it, then write_snapshot with a position in PRIVCO
    assert {r["symbol"]: r for r in store.securities()}["PRIVCO"]["parked_reason"] is None

def test_one_good_bar_resets_the_miss_count(tmp_path): ...
```

- [ ] **Step 2: Run them to verify they fail**

- [ ] **Step 3: Implement.** A run counts a miss only for the result "no bars from any provider" (not a provider failure, not "no new bars"). Park when `miss_runs >= PARK_AFTER`, `cik IS NULL` and the symbol has no row in `positions_latest`. `write_snapshot` unparks any symbol it writes a position for; a written bar resets the count. The SEC step skips parked symbols. The console's Sources list gets one line "Parked: X (reason)".

- [ ] **Step 4: Run the tests and the suite.** Expected: all pass.
- [ ] **Step 5: Commit** — `"universe: park a symbol nothing knows after three runs; a position unparks it"`

### Task 3: Foreign filers and fund-like filers

**Files:**
- Modify: `src/financial_data_collector/collectors/sec_facts.py`, `export/cockpit.py` (`FUND_TYPES`), `universe.py` (asset types in the `[classify]` comment), `config.example.toml`
- Test: `tests/test_sec.py`; new fixtures `tests/fixtures/sec/companyfacts_IFRS.json`, `companyfacts_LAPSED.json`, `companyfacts_TRUST.json`

**Interfaces:**
- Produces: asset types `foreign_filer` and `fund`; `sec_facts.classify_filer(facts: list[Fact], sec_name: str | None) -> str | None` returning `"foreign_filer"`, `"fund"` or `None` (stays a company); `FUND_TYPES` in `cockpit.py` and `_FUND_TYPES` in `sec_facts.py` include `fund` and `foreign_filer`.

- [ ] **Step 1: Write the failing tests**

```python
def test_ifrs_only_filer_is_a_foreign_filer():
    assert sec_facts.classify_filer(parse_company_facts(load("companyfacts_IFRS.json")), "Sample Mining SA") == "foreign_filer"

def test_us_gaap_until_2012_then_20f_is_a_foreign_filer():
    # LAPSED: us-gaap facts end 2012 (10-K), ifrs-full facts from 2013 on 20-F
    assert sec_facts.classify_filer(parse_company_facts(load("companyfacts_LAPSED.json")), "Sample Lapsed") == "foreign_filer"

def test_trust_that_files_10q_is_a_fund():
    assert sec_facts.classify_filer(parse_company_facts(load("companyfacts_TRUST.json")), "Sample Bitcoin Trust") == "fund"

def test_ordinary_company_is_none(fixtures):
    assert sec_facts.classify_filer(parse_company_facts(load("companyfacts_SAMPLE.json")), "Sample Corp") is None

def test_collect_sec_sets_foreign_filer_and_stops_shaping_it(...):
    # after collect_sec: asset_type == "foreign_filer", no financial_line_items for its cik,
    # and a second run skips it (no fetch call for its URL)
```

- [ ] **Step 2: Run them to verify they fail**

- [ ] **Step 3: Implement `classify_filer`.** `foreign_filer` when the facts' newest `form` (by `filed`) is `20-F`, `20-F/A`, `40-F` or `6-K`, or when operating facts exist only in `ifrs-full`. `fund` when `sec_name` matches `\b(trust|fund|etf)\b` (case-insensitive) and the filer has no `RevenueFromContractWithCustomer*` / `Revenues` facts in the last 2 years. Called in `collect_sec` right after `parse_company_facts`; on a non-None result: `set_asset_type`, `mark_sec_fetch`, `replace_line_items(cik, [])`, skip shaping. The test for an ordinary company runs over every existing SEC fixture, so no current company changes class. `[classify]` still overrides (it is applied by `build_universe` after).

- [ ] **Step 4: Run the tests and the suite.** Expected: all pass.
- [ ] **Step 5: Commit** — `"sec: foreign filers and fund-like filers are typed, never shaped as companies"`

### Task 4: Valuation status: unsupported and stale

**Files:**
- Modify: `src/financial_data_collector/derive/valuation.py`, `store.py` (`securities_with_cik` returns asset_type too)
- Test: `tests/test_valuation.py`

**Interfaces:**
- Consumes: Task 1's `valuation_daily.status`, `status_reason`; Task 3's asset types.
- Produces: `COLUMNS` gains `"status", "status_reason"`; `STALE_MONTHS = 15`; statuses `ok | stale | unsupported`.

- [ ] **Step 1: Write the failing tests**

```python
def test_ttm_older_than_15_months_is_stale_not_a_ratio():
    ttm = dict(TTM1, period_end="2012-12-31", available_from="2013-04-02")
    (r,) = _rows(V.build("X", [("2026-10-29", 11.35, 0.0, 1.0)], [ttm]))
    assert r["status"] == "stale" and r["pe"] is None and r["ps"] is None and r["p_fcf"] is None
    assert r["status_reason"] == "newest statements end 2012-12-31"

def test_stale_recovers_when_a_new_filing_lands():
    old = dict(TTM1, period_end="2024-03-31", available_from="2024-05-01")
    new = dict(TTM1, period_end="2025-06-30", available_from="2025-08-01")
    by = {r["date"]: r for r in _rows(V.build("X", [("2025-07-15", 10.0, 0.0, 1.0), ("2025-08-01", 10.0, 0.0, 1.0)], [old, new]))}
    assert by["2025-07-15"]["status"] == "stale" and by["2025-08-01"]["status"] == "ok"

def test_foreign_filer_gets_unsupported_rows(tmp_path):
    # rebuild() over a store where "SAMPLEF" is asset_type foreign_filer with prices
    assert latest["status"] == "unsupported" and latest["pe"] is None and latest["close"] == 11.35
```

- [ ] **Step 2: Run them to verify they fail**

- [ ] **Step 3: Implement.** Stale when `period_end` is more than 15 calendar months before the price date: keep `close`, `ttm_period_end`, `available_from`, set every other value NULL. `rebuild` writes, for each `foreign_filer` and `fund` symbol that has prices, rows with only `symbol, date, close, status='unsupported', status_reason` ("foreign filer: IFRS statements not read yet" / "fund: no company statements").

- [ ] **Step 4: Run the tests and the suite.**
- [ ] **Step 5: Commit** — `"valuation: stale and unsupported become a status, never a ratio"`

### Task 5: Share classes and the missing capex concept

**Files:**
- Modify: `src/financial_data_collector/config.py`, `config.example.toml`, `derive/valuation.py`, `seeds/concept_map.csv`
- Test: `tests/test_valuation.py`, `tests/test_config.py`, `tests/test_statements.py`

**Interfaces:**
- Produces: `Config.share_classes: dict[str, ShareClass]`, `@dataclass ShareClass(per: str, ratio: float)`; `V.build(..., share_class: ShareClass | None = None)`.

- [ ] **Step 1: Investigate before coding (read-only on the real database).** With `fdc query`, look at what a two-class filer's `sec_facts` carry for shares and EPS (concepts, units, taxonomies, newest `period_end`). Write what you find into the task's commit message. If no share count or per-share figure exists for any class after 2015, the deliverable for that company is `status = 'unsupported'`, `status_reason = 'share count not in its filings'`, via the config entry below with `ratio` and no figures to restate.

- [ ] **Step 2: Write the failing tests**

```python
def test_share_class_restates_eps_and_shares():
    ttm = dict(TTM1, eps_diluted=30000.0, shares_outstanding=1.4e6)  # per class-A terms
    (r,) = _rows(V.build("XYZ.B", [("2025-05-01", 500.0, 0.0, 1.0)], [ttm], share_class=ShareClass("XYZ.A", 1500)))
    assert r["eps_ttm"] == 20.0 and r["shares"] == 1.4e6 * 1500 and r["pe"] == 25.0

def test_config_reads_share_classes(tmp_path):
    # [share_classes] "XYZ.B" = { per = "XYZ.A", ratio = 1500 }
    assert cfg.share_classes["XYZ.B"] == ShareClass("XYZ.A", 1500.0)

def test_other_ppe_capex_maps_without_changing_other_companies(fixtures):
    # TTM for every existing SEC fixture is identical before and after the seed row;
    # a fixture with only PaymentsToAcquireOtherPropertyPlantAndEquipment now has capex and fcf
```

- [ ] **Step 3: Implement.** `ratio` = how many of `symbol` make one `per`. EPS is divided by `ratio`, shares multiplied, before the split scaling. Seed row: `capex,cashflow,duration,us-gaap,PaymentsToAcquireOtherPropertyPlantAndEquipment,3`. `rebuild` passes `cfg.share_classes.get(symbol)` (thread `cfg` into `rebuild(store, cfg)` from `_step_derive`).

- [ ] **Step 4: Run the tests and the suite.**
- [ ] **Step 5: Commit** — `"valuation: share classes restated from config; capex under 'other PP&E' mapped"`

### Task 6: The missing quarterly filing

**Files:** whatever the cause touches; test in `tests/test_sec.py` or `tests/test_statements.py`.

- [ ] **Step 1: Reproduce.** Use superpowers:systematic-debugging. Known: SEC's filing list shows a 10-Q filed 2026-07-29 for one large-cap; `sec_facts` has nothing with `filed` after 2026-04-29 for that CIK, although the step fetched its company facts on 2026-10-30. Fetch that company's companyfacts JSON once by hand (`fdc query` for the CIK, the URL in `FACTS_URL`, `SEC_USER_AGENT` from `.env` set in your shell, never printed) and check whether the July accession number is in it.
- [ ] **Step 2: Decide.** If SEC's JSON lacks it: no code change here; the checker's filings check (Task 10) holds the company's values until it appears; record the cause in `CHANGELOG.md`. If fdc dropped it: write a failing test with a synthetic fixture shaped like the real answer, fix the cause, and confirm the next `fdc sync --only sec,derive` gives that company a June-quarter TTM.
- [ ] **Step 3: Commit** — the fix with its test, or the CHANGELOG note.

### Task 7: The checker's frame: tables, findings, `fdc check`, the sync step

**Files:**
- Create: `src/financial_data_collector/migrations/0012_checks.sql`, `src/financial_data_collector/check/__init__.py`, `check/findings.py`
- Modify: `store.py`, `sync.py`, `cli.py`
- Test: `tests/test_check_frame.py`, `tests/test_sync.py`

**Interfaces:**
- Produces:
  - Tables: `check_runs(run_id TEXT PRIMARY KEY, started_at, finished_at, derive_run_id TEXT, checks_run INTEGER, holds INTEGER, warns INTEGER, error TEXT)`; `check_findings(id INTEGER PRIMARY KEY, check_name, symbol, field, date_from, date_to, severity CHECK IN ('hold','warn','info'), message, evidence TEXT, first_seen, last_seen, cleared_at, UNIQUE(check_name, symbol, field, date_from))`. `field` is a `valuation_daily` column name or `*` (every value column).
  - `@dataclass(frozen=True) Finding(check_name: str, symbol: str, field: str, date_from: str, date_to: str | None, severity: str, message: str, evidence: dict)`.
  - `Check = Callable[[CheckContext], list[Finding]]`; `@dataclass CheckContext(store: Store, cfg: Config, fetch: Fetch, yf: Callable | None, now: datetime)`.
  - `check.run_check(cfg, *, fetch, yf=None, now=None, only: list[str] | None = None) -> CheckReport` with `CheckReport(run_id, holds, warns, errors: list[str])`.
  - `Store.check_open_findings() -> list[Row]`, `Store.check_record(run_id, check_name, findings: list[Finding], now: str)`, `Store.check_log_run(...)`.
  - `CHECKS: dict[str, Check]` (empty here; Tasks 9-11 register).
  - Sync step `"check"` after `"export"`; `fdc check [--only NAME ...] [--summary]`.

- [ ] **Step 1: Write the failing tests**

```python
def test_a_repeat_updates_last_seen_not_a_new_row(tmp_path): ...
def test_a_check_that_finds_nothing_clears_its_open_findings(tmp_path): ...
    # cleared_at set for findings of THAT check only
def test_a_check_that_raises_leaves_its_findings_untouched(tmp_path): ...
    # CHECKS = {"a": raises, "b": finds nothing}: a's open findings stay open, b's clear, report.errors names "a"
def test_check_imports_nothing_it_checks():
    src = Path(check.__file__).parent
    text = "\n".join(p.read_text(encoding="utf-8") for p in src.glob("*.py"))
    assert not re.search(r"from \.\.(derive|statements|collectors\.(prices|sec_facts|sec_cik|macro))", text)
def test_sync_runs_check_last_and_a_failing_check_does_not_fail_the_sync(...):
    assert [s.step for s in rep.steps][-1] == "check" and rep.exit_code == 0
def test_check_run_records_the_derive_it_covered(tmp_path): ...
    # derive_run_id = run_id of the newest 'derive' row in sync_runs with status ok
```

- [ ] **Step 2: Run them to verify they fail**
- [ ] **Step 3: Implement.** Clearing is per check: after a check returns, its open findings not in the new list get `cleared_at = now`. A check that raises records the error and leaves its findings alone. The sync step calls `run_check` with the sync's `fetch`/`yf`/`now`, returns status `ok` even when individual checks error (message lists them); only an exception in the frame itself makes the step `error`, which `SyncReport.exit_code` ignores (it is not in `NETWORK_STEPS`). Update the step lists asserted in `tests/test_sync.py`.
- [ ] **Step 4: Run the tests and the suite.**
- [ ] **Step 5: Commit** — `"check: findings, holds and clearing; fdc check and the sync's last step"`

### Task 8: Holds reach `valuation_latest`

**Files:**
- Modify: `src/financial_data_collector/migrations/0012_checks.sql` (same migration as Task 7 if Task 7 is unreleased; otherwise `0013_valuation_latest.sql`)
- Test: `tests/test_views.py`

**Interfaces:**
- Produces: `valuation_latest` columns = every `valuation_daily` column, then `hold_ids TEXT` (comma-separated finding ids, NULL when none) and `checked_at TEXT`. `status` becomes `held` when any open `hold` covers the row; a held value column is NULL.

- [ ] **Step 1: Write the failing tests**

```python
def test_open_hold_nulls_its_field_and_says_held(tmp_path):
    # valuation row for KO on 2026-10-29 with pe 20; open hold (field 'pe', date_from 2026-10-01, date_to NULL)
    r = store.query("SELECT pe, ps, status, hold_ids FROM valuation_latest WHERE symbol='KO'")[0]
    assert r["pe"] is None and r["ps"] is not None and r["status"] == "held" and r["hold_ids"] == str(fid)

def test_star_hold_nulls_every_value_but_close(tmp_path): ...
def test_cleared_hold_restores_the_value(tmp_path): ...       # cleared_at set -> pe back to 20
def test_hold_outside_its_dates_does_not_apply(tmp_path): ... # date_to before the row's date
def test_checked_at_is_null_until_a_check_covers_the_newest_derive(tmp_path): ...
def test_valuation_latest_keeps_every_old_column_name(tmp_path):
    old = ["symbol","date","close","shares","market_cap","revenue_ttm","net_income_ttm","ocf_ttm","fcf_ttm",
           "eps_ttm","pe","ps","p_fcf","dividends_12m","dividend_yield","ttm_period_end","available_from"]
    cols = [r[1] for r in store.query("PRAGMA table_info(valuation_latest)")]
    assert cols[:len(old)] == old
```

- [ ] **Step 2: Run them to verify they fail**
- [ ] **Step 3: Implement the view.** For each value column `c`: `CASE WHEN EXISTS (open hold on v.symbol with field IN ('c','*') and v.date BETWEEN date_from AND COALESCE(date_to,'9999-12-31')) THEN NULL ELSE v.c END AS c`. `checked_at` = `finished_at` of the newest `check_runs` row whose `derive_run_id` equals the newest ok `derive` run in `sync_runs`. `readonly.py`'s guard and the MCP schema list pick the view up unchanged (check `test_mcp.py`). `fundamentals.json` is left as it is: it carries statement rows, not `valuation_daily` fields, so no field a check holds appears in it (the spec's "the export applies the same holds" has nothing to act on yet; a later check that holds a statement field adds it there).
- [ ] **Step 4: Run the tests and the suite.**
- [ ] **Step 5: Commit** — `"valuation_latest applies open holds, says why and when it was checked"`

### Task 9: Offline checks: fresh enough, impossible values, gaps, recompute

**Files:**
- Create: `src/financial_data_collector/check/offline.py`, `check/calendar.py`
- Modify: `check/__init__.py` (register), `store.py` (`check_*` readers over `valuation_daily`, `prices`, `securities`, `positions_latest`)
- Test: `tests/test_check_offline.py`

**Interfaces:**
- Consumes: Task 7's `Finding`, `CheckContext`, `CHECKS`.
- Produces: checks `"fresh"`, `"impossible"`, `"gaps"`, `"recompute"`; `calendar.trading_days(start: date, end: date) -> list[date]` (NYSE: weekends, the fixed and floating holidays, Good Friday).

- [ ] **Step 1: Write the failing tests** (one per rule, with the spec's values)

```python
def test_fresh_warns_after_5_months_holds_after_15(): ...
    # period_end 2026-04-30 on 2026-10-29 -> warn; 2025-06-30 -> hold, field '*'
def test_impossible_ratio_on_a_fund_holds(): ...
def test_impossible_one_day_move_over_40pct_without_split_holds(): ...
def test_impossible_move_on_a_split_day_is_fine(): ...
def test_gap_on_a_trading_day_warns_but_not_on_good_friday(): ...
def test_recompute_matches_within_1pct_and_holds_beyond(): ...
    # pe stored 20.0, recomputed close/eps 20.1 -> nothing; stored 25.0 -> hold on 'pe' with both in evidence
def test_recompute_reads_no_shaped_view():
    assert "financials_ttm" not in inspect.getsource(offline)
```

- [ ] **Step 2: Run them to verify they fail**
- [ ] **Step 3: Implement.** "Held symbols" = symbols in `positions_latest`; every check covers held symbols plus symbols with a `valuation_daily` row. Recompute uses the row's own `close`, `shares`, `eps_ttm`, `revenue_ttm`, `fcf_ttm`, `dividends_12m`: `pe = close/eps`, `ps = close*shares/revenue`, `p_fcf = close*shares/fcf`, `market_cap = close*shares`, `dividend_yield = dividends_12m/close`; compares each where both sides exist. Thresholds go in `[check]` config with these defaults: `fresh_warn_months = 5`, `fresh_hold_months = 15`, `recompute_tolerance = 0.01`, `max_daily_move = 0.40`.
- [ ] **Step 4: Run the tests and the suite.**
- [ ] **Step 5: Commit** — `"check: fresh, impossible, gaps and recompute"`

### Task 10: Filings caught up

**Files:**
- Create: `src/financial_data_collector/check/filings.py`; fixture `tests/fixtures/sec/submissions_SAMPLE.json`
- Test: `tests/test_check_filings.py`

**Interfaces:**
- Produces: check `"filings"`; `SUBMISSIONS_URL = "https://data.sec.gov/submissions/CIK{cik}.json"`; `filings.missing(recent: dict, have_accns: set[str], today: date, grace_days: int = 3) -> list[tuple[str, str, str]]` returning `(form, accession, filed)`.

- [ ] **Step 1: Write the failing tests**

```python
def test_a_10q_filed_four_days_ago_without_facts_holds_from_its_filing_date(): ...
    # finding: field '*', date_from = filed, severity 'hold', evidence {form, accession, filed, url}
def test_within_grace_days_nothing(): ...
def test_8k_and_proxy_forms_are_ignored(): ...                    # only 10-Q, 10-K, 20-F, and their /A
def test_the_hold_clears_once_facts_with_that_accession_exist(): ...
def test_uses_the_sec_user_agent_and_pacing(): ...                # header present, sleep(1.0) between companies
```

- [ ] **Step 2: Run them to verify they fail**
- [ ] **Step 3: Implement.** Read `filings.recent` (`form`, `accessionNumber`, `filingDate`) from the submissions JSON for each company with a CIK and asset type `stock`; compare accession numbers (dashes kept) with `SELECT DISTINCT accn FROM sec_facts WHERE cik = ?` (a `check_` store method). The evidence `url` is built as `https://www.sec.gov/Archives/edgar/data/{int(cik)}/{accn without dashes}/`. A fetch error is one `info` finding per company, never a hold.
- [ ] **Step 4: Run the tests and the suite.**
- [ ] **Step 5: Commit** — `"check: a filing SEC lists that the warehouse lacks holds the company's values"`

### Task 11: Second price source and outside figures

**Files:**
- Create: `src/financial_data_collector/check/outside.py`
- Test: `tests/test_check_outside.py`

**Interfaces:**
- Produces: checks `"prices2"` and `"figures"`; `outside.Quote(close: float, date: str)`, `outside.Figures(trailing_eps, market_cap, revenue, free_cash_flow)` (each `float | None`); `yf_quote(symbol) -> Quote | None` and `yf_figures(symbol) -> Figures | None` (lazy `yfinance` import; tests pass fakes through `CheckContext.yf`, here a small object with `.quote` and `.figures`).

- [ ] **Step 1: Write the failing tests**

```python
def test_closes_within_half_a_percent_are_fine(): ...
def test_closes_over_half_a_percent_warn_over_3pct_hold(): ...
def test_compares_the_other_provider_than_the_bar_came_from(): ...  # bar from tiingo -> yahoo quote, and vice versa
def test_split_day_compares_split_adjusted(): ...                    # Review Focus 5
def test_figure_null_here_but_present_outside_warns(): ...
def test_figure_15pct_apart_warns_40pct_holds_on_that_field(): ...   # market_cap -> field 'market_cap'
def test_outside_failure_is_info_never_hold(): ...
```

- [ ] **Step 2: Run them to verify they fail**
- [ ] **Step 3: Implement.** Covers held symbols and the newest final bar only. When the bar's source is `yfinance`, the second source is Tiingo (`collectors.prices` is off-limits: build the Tiingo URL here from `http` and the config token; no token → `info` "no second source"). Compare `close` on the same date; on a date with `split_factor != 1` compare after dividing the pre-split side by the factor. Figures map: `trailing_eps ↔ eps_ttm`, `market_cap ↔ market_cap`, `revenue ↔ revenue_ttm`, `free_cash_flow ↔ fcf_ttm`. Defaults in `[check]`: `price_warn = 0.005`, `price_hold = 0.03`, `figure_warn = 0.15`, `figure_hold = 0.40`. Every provider error message goes through `connections/redact.py` before it reaches a finding (the Tiingo URL carries no token, the header does; keep it that way).
- [ ] **Step 4: Run the tests and the suite.**
- [ ] **Step 5: Commit** — `"check: a second price source and outside figures"`

### Task 12: Reporting and the backstop task

**Files:**
- Create: `scripts/schedule_check.ps1`
- Modify: `cli.py` (`cmd_check`, `cmd_status`), `console/model.py`, `README.md`, `AGENTS.md`
- Test: `tests/test_cli.py`, `tests/test_console_model.py`

**Interfaces:**
- Produces: `fdc check --summary` prints one line per open finding, `hold` first: `HOLD  KO  pe  from 2026-10-01  recompute: stored 25.0, recomputed 20.0`; `fdc status` adds `check  <age>  <holds> holds, <warns> warnings`; the console Overview gets the same row.

- [ ] **Step 1: Write the failing tests** — the summary's exact line format above, holds before warns; the status line; the console row.
- [ ] **Step 2: Run them to verify they fail**
- [ ] **Step 3: Implement.** `schedule_check.ps1` mirrors `schedule_sync.ps1`: a daily task `FinancialDataCollector Check` at 07:00 running `fdc check` with origin `scheduled`, registered with `Register-ScheduledTask` (never `schtasks /change`). README: the checker, its tables, what a hold means for a consumer. AGENTS.md: the `check/` rule (imports nothing it checks) and the two tables in "Tables and views".
- [ ] **Step 4: Run the whole suite and `fdc check --summary` against a copy of the real database** (`copy data/warehouse.db` to the scratch folder; `fdc query` reads it with `mode=ro`): expect holds or warnings for the 2026-10-30 audit's findings that Tasks 3-6 did not already turn into a status.
- [ ] **Step 5: Commit** — `"check: summary, status line, console row and the daily backstop task"`

---

## After this plan

- Run `fdc sync` once on the real database (the owner's OK first: it writes the warehouse), then `fdc check --summary`, and compare with the audit table in the spec.
- Register the backstop task with `scripts/schedule_check.ps1` (owner's OK).
- The window (Claude OS's The Bull) treats `status` and `hold_ids` in its own spec; until then it already shows NULL as missing.
- Plans 2-4: Part 3 (benchmarks, fund snapshots, earnings history), Part 4 (the engine), Part 5 (the split).
  The spec's "engine sanity" checks join the checker in plans 3-4, with the outputs they check.
