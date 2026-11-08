# fdc: complete fund holdings, joined exposure, and a reachable price contract — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.
>
> **Move this file to `financial-data-collector/docs/superpowers/plans/` before executing.** It was drafted from a session in the `investing` repo and has not been committed anywhere.

**Goal:** Give Mr. Buffett's panel exposure figures it can compare against a line — complete fund holdings instead of a top-10 cache, constituents joined to the US lines the portfolio holds, each figure published with its base and its completeness — and make the price contract claude-os needs reachable from outside this repo.

**Architecture:** Four changes in `financial-data-collector`. A fund-holdings collector that fetches each publisher's full holdings file, so look-through stops being a floor. An exposure-scoped alias map used only in the look-through path — deliberately NOT in `symbols.canonical`. A new derived `*_raw` table plus view carrying the panel's figures, following `derive/split.py::publish` exactly. And a view exposing the trading-day calendar and final-close rule that already exist here but are unreachable from another repo.

**Tech Stack:** Python 3.11, SQLite via `store.py`, numbered SQL migrations in `src/financial_data_collector/migrations/`, pytest with `tests/conftest.py` and `tests/fixtures/`. No network in tests.

**Spec:** `investing/docs/superpowers/specs/2026-11-06-mr-buffett-mr-bayes-design.md` — §4 decisions 3 and 4, §6, §11. Read it first; it carries the owner's reasoning and the eight measures this data serves.

## Where this plan corrects the spec

Reading the code found the spec overstating this repo's work in two places. Both corrections are load-bearing; carry them back into the spec rather than building what it says.

1. **§6 asks for "a complete-versus-partial flag on price bars". It already exists.** `prices.final` was added by migration `0011_valuation_status.sql`: "`prices.final = 0` marks a bar fetched before its close; the next run's trailing refresh rewrites it." Nothing needs building here — claude-os needs to *read* `final = 1`, which is its own plan's work. Task 4 only makes the contract reachable and documented.
2. **§6 also asks for a trading-day calendar. It exists too** — `src/financial_data_collector/check/calendar.py`, `is_trading_day(d)` and `trading_days(start, end)`, NYSE days by rule with no data file and no network. It is Python in this repo, so another repo cannot import it; Task 4 publishes it.
3. **§11 says to fix the ticker gaps in `symbols.py::canonical`. Do not.** `canonical()` governs symbol *identity* for every adapter — positions, prices, securities. Mapping `GOOG` to `GOOGL` there would rewrite a real GOOG holding into GOOGL in the warehouse itself. Exposure joining is a different question from identity, and belongs only in the look-through path. Task 1 does it there.

## Global Constraints

- `investing/allocation/targets.txt` is the source of truth for targets; `config/sizing-targets.json` is its structured rendering, which this repo reads and never writes.
- `planned` stays out of the weekly contribution split. `derive/targets.py::_NOT_ACCOUNTS` is `("sleeves", "caps", "planned")` and the investing repo has just added structured `ticker`/`type`/`target` to those entries. A change that lets a planned name reach the split sends real money to a name the owner deliberately withheld it from.
- Derived tables are rebuilt from scratch and a row already correct is not rewritten (`derive/split.py::publish`, migration `0023_week.sql`).
- Read other repos by their existing sibling paths; never write outside this one.
- Add no Claude call. Add no real-order path.
- No network in tests. Every fetcher is tested against a saved fixture.
- Account numbers never appear in output.

## Review Focus

Inputs the spec implies that no happy path exercises. Each gets its test in the task that owns the code.

1. **A publisher's holdings file that changes shape or 404s.** The top-10 yfinance cache exists today and works; a new fetcher that fails must leave the previous holdings in place rather than blanking a fund, the same rule `investing/pipeline/refresh_fund_holdings.py` already follows ("never blanks a file on a failed fetch"). Pinned to Task 2.
2. **Weights summing to something other than 100.** A complete file should sum to ~100 but will not sum exactly; a partial or double-counted file may exceed it. The published coverage figure must be the measured sum, and over-100 must be visible rather than clamped. Pinned to Task 2.
3. **An alias changing where money goes.** `read_look_through` feeds `derive/split.py::household_of`, which the weekly contribution split uses. Joining GOOG into GOOGL legitimately increases GOOGL's indirect exposure and can therefore change the split's recommendation. That is correct, and it must be deliberate and tested, not discovered later. Pinned to Task 1.
4. **A figure published without its base.** The same holdings read 58.5% on the brokerage base and about 65% rolled up across both Fidelity accounts. A row whose base is implicit will be read against the wrong line. Pinned to Task 3.
5. **A consumer reading a non-final bar.** `prices` holds `final = 0` rows for a bar fetched before its close, and the newest date is exactly where a page looks. A published view that does not filter on `final` hands claude-os the partial bar the spec is trying to eliminate. Pinned to Task 4.

## File Structure

- `src/financial_data_collector/derive/exposure.py` — **create.** The panel's figures: top-three, sector weights against plan, stacked direct-vs-passive, factor share. One responsibility: turn a Household plus targets into published rows.
- `src/financial_data_collector/collectors/fund_holdings.py` — **create.** Fetch and store complete holdings per fund.
- `src/financial_data_collector/derive/targets.py` — **modify.** `read_look_through` applies the exposure alias map and returns coverage.
- `src/financial_data_collector/symbols.py` — **modify.** Add `exposure_alias()` beside `canonical()`, with a comment saying why it is separate. `canonical()` itself is unchanged.
- `src/financial_data_collector/migrations/00NN_exposure.sql` — **create.** `exposure_figures_raw` + `exposure_figures` view + `trading_days` view. Next free number.
- `src/financial_data_collector/store.py` — **modify.** `replace_exposure_figures`, following `replace_allocation_targets`.
- `tests/test_exposure.py`, `tests/test_fund_holdings.py` — **create.** `tests/test_targets.py` — **modify.**
- `tests/fixtures/` — **add** one saved holdings file per publisher shape.

---

### Task 1: Exposure-scoped alias map in the look-through path

**Files:**
- Modify: `src/financial_data_collector/symbols.py`, `src/financial_data_collector/derive/targets.py:139-165` (`read_look_through`)
- Modify: `tests/test_targets.py`

**Interfaces:**
- Produces: `symbols.exposure_alias(sym: str) -> str` — canonical form first, then the exposure map. `read_look_through` applies it to every constituent ticker. `canonical()` is untouched, and a test proves it.
- Consumes: nothing.

The nine mappings, each verified against the constituent's own name column in `investing/fund_holdings/*.csv`. Note the canonical form here keeps separators as dots (`BRK.B`), unlike the investing rollup which strips them — reuse the pairs, not the output spelling.

| Raw | Company | Exposure symbol |
|---|---|---|
| `GOOG` | Alphabet Inc Class C | `GOOGL` |
| `2330.TW` | Taiwan Semiconductor Manufacturing Co Ltd | `TSM` |
| `ASML.AS` | ASML Holding NV | `ASML` |
| `AZN.L` | AstraZeneca PLC | `AZN` |
| `HSBA.L` | HSBC Holdings PLC | `HSBC` |
| `NOVN.SW` | Novartis AG | `NVS` |
| `ROP.SW` | Roche Holding AG | `RHHBY` |
| `SHEL.L` | Shell PLC | `SHEL` |
| `0700.HK` | Tencent Holdings Ltd | `TCEHY` |

`005930.KQ` (Samsung Electronics) and `000660.KQ` (SK hynix) stay unmapped: neither has a US line this portfolio could hold.

- [ ] **Step 1: Write the failing tests**

```python
def test_exposure_alias_joins_share_classes_and_foreign_listings():
    assert exposure_alias("GOOG") == "GOOGL"
    assert exposure_alias("2330.TW") == "TSM"
    assert exposure_alias("AZN.L") == "AZN"


def test_roche_maps_to_its_adr_not_to_roper():
    """ROP.SW is Roche; ROP on the NYSE is Roper Technologies."""
    assert exposure_alias("ROP.SW") == "RHHBY"
    assert exposure_alias("ROP") == "ROP"


def test_listings_without_a_us_line_stay_unmapped():
    assert exposure_alias("005930.KQ") == "005930.KQ"
    assert exposure_alias("000660.KQ") == "000660.KQ"


def test_canonical_is_not_changed_by_the_exposure_map():
    """Identity is not exposure: a real GOOG position must stay GOOG in the warehouse."""
    assert canonical("GOOG") == "GOOG"
    assert canonical("BRKB") == "BRK.B"


def test_look_through_joins_alphabet_into_one_constituent(tmp_path):
    """FXAIX carries GOOGL and GOOG as separate rows; exposure sums them."""
    # fixture: FXAIX.csv with GOOGL 3.06 and GOOG 2.45
    out = read_look_through(tmp_path)
    assert out["FXAIX"]["GOOGL"] == pytest.approx(0.0551)
    assert "GOOG" not in out["FXAIX"]


def test_look_through_reports_coverage_per_fund(tmp_path):
    """Review Focus 2 — the measured sum, not an assumption of 100."""
    out, coverage = read_look_through_with_coverage(tmp_path)
    assert coverage["FXAIX"] == pytest.approx(5.51)
```

- [ ] **Step 2: Run them to verify they fail**

Run: `.venv/Scripts/python -m pytest tests/test_targets.py -v -k "exposure or look_through"`
Expected: FAIL — `ImportError` on `exposure_alias`.

- [ ] **Step 3: Add `exposure_alias` to `symbols.py` and apply it in `read_look_through`**

Put `_EXPOSURE_ALIASES` beside `_NO_SEPARATOR_ALIASES` with a comment stating that it is for exposure joining only and must never be reached from `canonical()`. `read_look_through` sums weights that collide after aliasing (two rows becoming one symbol must add, not overwrite) and also returns the measured coverage per fund — either as a second return value with the existing name kept as a thin wrapper, or as a richer return type; decide and state it in the Interfaces block of Task 3 before that task consumes it.

- [ ] **Step 4: Run them to verify they pass**

Run: `.venv/Scripts/python -m pytest tests/test_targets.py -v`
Expected: PASS.

- [ ] **Step 5: Prove the money-routing change is the intended one**

Review Focus 3. Run the full suite and read every split/household failure rather than updating expectations by reflex:
`.venv/Scripts/python -m pytest tests -q`
Expected: either green, or failures only in split/household expectations that assumed Alphabet's two rows were separate. For each, confirm the new number is the correct exposure before changing the expectation, and note in the commit that the weekly split's recommendation can move because a name's measured passive exposure is now larger.

- [ ] **Step 6: Commit**

```bash
git add src/financial_data_collector/symbols.py src/financial_data_collector/derive/targets.py tests/test_targets.py
git commit -m "exposure: join share classes and foreign listings in the look-through path"
```

---

### Task 2: Complete fund holdings

**Files:**
- Create: `src/financial_data_collector/collectors/fund_holdings.py`, `tests/test_fund_holdings.py`
- Add: one fixture per publisher shape under `tests/fixtures/`

**Interfaces:**
- Consumes: Task 1's alias map, applied to every constituent.
- Produces: complete holdings for the five funds the owner holds, stored where `read_look_through` reads them, with a per-fund coverage figure that should now be near 100 rather than the measured floors below.

Coverage today, from the top-10 yfinance cache (measured 2026-11-06): FXAIX **39.0%** of the fund, SCHD **41.3%**, QQQM **46.6%**, VXUS **13.5%**, AVUV **8.1%**, FTIHX 11.9%. Five of those are live holdings. Each publisher offers a full holdings file; choose the source per fund and record the choice and its URL in the module docstring.

**The AZN case is the acceptance test for this whole task.** `investing/theses/VXUS.md` records AstraZeneca at 0.62% of VXUS; the top-10 snapshot has no AstraZeneca row at all, so the rollup printed its passive exposure as $0.00. Task 1's alias cannot fix that — there is no row to join. If AZN still reads zero after this task, the task is not done.

- [ ] **Step 1: Write the failing tests**

```python
def test_parses_a_publishers_full_holdings_file():
    """Against a saved fixture; no network."""
    rows = parse_holdings(FIXTURE_VXUS, fund="VXUS")
    assert len(rows) > 100
    assert sum(r.weight_pct for r in rows) == pytest.approx(100, abs=2)


def test_astrazeneca_is_present_in_vxus():
    """The motivating defect: AZN's passive exposure read $0.00 because no row existed."""
    rows = parse_holdings(FIXTURE_VXUS, fund="VXUS")
    azn = [r for r in rows if exposure_alias(r.ticker) == "AZN"]
    assert azn and azn[0].weight_pct == pytest.approx(0.62, abs=0.3)


def test_a_failed_fetch_leaves_the_previous_holdings_alone():
    """Review Focus 1 — never blank a fund on a bad fetch."""
    ...


def test_weights_over_one_hundred_are_reported_not_clamped():
    """Review Focus 2."""
    ...
```

- [ ] **Step 2: Run them to verify they fail**
- [ ] **Step 3: Implement the collector**

One fetch function per publisher shape, a parser that returns `(ticker, name, weight_pct)`, and a store step that writes a fund's holdings only when the fetch produced a usable file. Follow `investing/pipeline/refresh_fund_holdings.py`'s two rules: idempotent (rewrite only on change) and never blank on failure.

- [ ] **Step 4: Run them to verify they pass**
- [ ] **Step 5: Fetch for real, once, and record the coverage**

Run the collector against the five live funds and record each fund's measured coverage in the commit message. Expected: near 100% for each, against today's 39.0 / 41.3 / 46.6 / 13.5 / 8.1.

- [ ] **Step 6: Confirm AZN's passive exposure is no longer zero**

Run `investing`'s rollup against the new holdings and check AstraZeneca appears with a non-zero indirect value. This is the acceptance test named above.

- [ ] **Step 7: Commit**

---

### Task 3: Publish the panel's exposure figures

**Files:**
- Create: `src/financial_data_collector/derive/exposure.py`, `src/financial_data_collector/migrations/00NN_exposure.sql`, `tests/test_exposure.py`
- Modify: `src/financial_data_collector/store.py`

**Interfaces:**
- Consumes: Task 1's aliased look-through with coverage, Task 2's complete holdings, `derive/split.py::household_of`, and `read_targets` (which now carries the planned percentages the investing repo added, so the full 13-name plan is available).
- Produces: table `exposure_figures_raw` and view `exposure_figures`, and `store.replace_exposure_figures(rows)`. **Every row carries its base and its completeness** — see Review Focus 4 and 5.

Follow `derive/split.py::publish` and migration `0023_week.sql` exactly: a derived table rebuilt from scratch, a row already correct not rewritten, a view over the raw table.

The figures, with their bases (from the spec's §5):

| Figure | Base |
|---|---|
| top three | brokerage |
| sector weight vs plan-implied weight | brokerage |
| stacked exposure, direct vs passive per name | rolled-up Fidelity |
| factor share (MSFT + NVDA + AMZN + GOOGL, direct + index weight) | brokerage |

Plan-implied lines come from `read_targets`: the seven live `brokerage` rows plus the six `planned` rows, summing to 100%. Do **not** compute a line from the live rows alone — that denominator is 59% and every line would be wrong.

- [ ] **Step 1: Write the failing tests**

```python
def test_every_published_row_names_its_base():
    """Review Focus 4 — a base-less figure gets read against the wrong line."""
    rows = build_exposure(household, targets, coverage)
    assert all(r.base in {"brokerage", "fidelity_rolled_up"} for r in rows)


def test_every_published_row_states_its_completeness():
    """Review Focus 5 — partial look-through must travel with the number."""
    rows = build_exposure(household, targets, coverage)
    assert all(r.look_through_complete in {0, 1} for r in rows)
    partial = [r for r in rows if not r.look_through_complete]
    assert all(r.coverage_pct is not None for r in partial)


def test_plan_lines_use_all_thirteen_names():
    """A line computed from the seven live rows alone has a 59% denominator."""
    rows = build_exposure(household, targets, coverage)
    top3 = next(r for r in rows if r.figure == "top_three")
    assert top3.line_pct == pytest.approx(32.0)


def test_rebuild_is_idempotent():
    """Derived table: a row already right is not rewritten."""
    ...
```

- [ ] **Step 2–6:** RED, implement, GREEN, wire the step into the engine where `split` runs, commit.

---

### Task 4: Make the price contract reachable from claude-os

**Files:**
- Modify: `src/financial_data_collector/migrations/00NN_exposure.sql` (same migration), `tests/test_exposure.py`
- Modify: the schema documentation this repo keeps for consumers

**Interfaces:**
- Produces: a `trading_days` view and a documented rule that consumers read only `final = 1` bars. Nothing new is computed — `prices.final` and `check/calendar.py` already exist; this makes both reachable from another repo over the read-only SQLite connection.

claude-os reads this warehouse `mode=ro` and cannot import this repo's Python, which is why the by-rule calendar in `check/calendar.py` does not help it today and why it currently takes its trading days from `webull/data/tiingo/SPY.csv` instead.

- [ ] **Step 1: Write the failing tests**

```python
def test_trading_days_view_excludes_non_final_bars():
    """Review Focus 5 — the newest date is exactly where a partial bar lives."""
    # fixture: prices with a final=0 row on the newest date
    days = [r[0] for r in conn.execute("SELECT day FROM trading_days ORDER BY day DESC")]
    assert newest_partial_date not in days


def test_trading_days_view_agrees_with_the_by_rule_calendar():
    """The view must not invent a day the NYSE calendar rejects."""
    days = {r[0] for r in conn.execute("SELECT day FROM trading_days")}
    assert all(is_trading_day(date.fromisoformat(d)) for d in days)
```

- [ ] **Step 2–5:** RED, add the view, GREEN, commit.

- [ ] **Step 6: Tell the claude-os session the contract**

Message that session with the exact names: the `exposure_figures` view and its columns, the `base` and `look_through_complete` / `coverage_pct` fields, and the `trading_days` view. It cannot code against this until those names are fixed, and it owns the other half of the spec.

---

## Not in this plan

- **The crash-rehearsal history.** The spec's measure 5 deliberately uses published historical drawdowns applied to the current allocation, not a price backfill. `prices` starts 2021-09-27 across all 92 symbols and is not being extended. Do not add a pre-2021 fetcher.
- **Anything in `claude-os` or `investing`.** Separate plans, separate sessions.
