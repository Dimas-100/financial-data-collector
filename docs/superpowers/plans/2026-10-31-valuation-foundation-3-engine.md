# Valuation foundation, plan 3: the valuation engine — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** For every holding and watched symbol, the warehouse says, in plain code with a source on every number, whether it is cheap or expensive against its own history (`valuation_scores`), what shape its price is in, and why it dropped (`price_moves`); a checker check holds back any score that is not sound.

**Architecture:** A new sync step `engine` runs **after** `check`: it builds only when the newest derive has been covered by a full, error-free check run (the spec's rule that money-driving outputs use checked values only; see "Calls this plan makes" 1). It reads the held values (open holds applied by its own code, over history too), writes `valuation_scores_raw` (served through the view `valuation_scores`, which applies the engine checks' holds) and `price_moves`, then runs the engine checks once more on what it wrote. Pure functions live in `derive/` (`sources.py`, `shape.py`, `scores.py`, `moves.py`, orchestrated by `engine.py`); two raw inputs are added first: SEC 8-Ks (from the submissions list the `earnings` step already fetches) and news headlines (from the sibling feed, by `ingest`). No network in the engine, no AI.

**Tech Stack:** Python 3.11+, SQLite (numbered migrations), pytest (no network).

**Spec:** `docs/superpowers/specs/2026-10-30-valuation-foundation-design.md`, Part 4 ("the valuation engine"), Part 2's "engine sanity" checks and "Unchecked values" rule, and "The builder's calls". Earlier rulings: `.superpowers/sdd/2026-10-30-valuation-foundation-1-data-and-checker/progress.md` (R1-R19) and `…-2-missing-data/progress.md` (R1-R7).

## Calls this plan makes (the owner may overrule any before the build)

1. **The engine is its own sync step, after `check`** (the spec says "built by `derive`"). Derive runs before the check, so a score built inside derive could never use values a check has covered, which Part 2 requires of bands. The code lives in `derive/`; only the step is new. When no full, error-free check run covers the newest derive, the step is `skipped` and the tables keep their last covered build (each row's `checked_at` says which).
2. **Scores are kept for each symbol's newest close only**, one row per (symbol, metric). The histories behind them stay where they are (`valuation_daily`, `prices`, `fund_snapshots`).
3. **Insider buying is a count**: Form 4 filings marked `buy` minus those marked `sell` in 90 days. The sibling feed gives each filing a direction, never shares or dollars.
4. **`price_moves` records drops only**, as the spec's title says; a jump is not recorded.
5. **EV/EBIT is "not known" when the filings give no debt or no cash figure**, rather than counting the missing one as 0 (a missing debt line read as no debt would make a company look cheap). A missing short-term-investments line counts as 0 (it only lowers EV).
6. **Headlines and 8-Ks are stored as raw rows**: the news feed keeps 14 days and `price_moves` is rebuilt every run, so a move's news cause would vanish two weeks later otherwise. Only headline, outlet, date and link are kept, never article text.
7. **The 30-day estimate direction is blank for 30 days after each report** (status `unsupported`, reason given): Yahoo's `0y` period can roll to the next fiscal year at a report, and a roll read as a revision would be a wrong number that looks right.
8. **Implied growth**: the yearly FCF growth for 10 years, then `terminal_growth` forever (a Gordon terminal value), discounted at `discount_rate`, that makes the present value equal today's market cap. Defaults 9% and 2.5%, in `[engine]`, shown as sources on the row.
9. **Holds reach scores twice**: at build time (the engine reads held inputs, past days included) and through the view `valuation_scores` (an open hold whose field is `*`, the metric's name, or one of the valuation columns the metric was built from blanks it). So a hold the 07:00 backstop check finds reaches the scores before the next sync. A `*` hold about statements (`fresh`, `filings`, `figures`) does not touch the price measures; one about prices (`prices2`, `impossible`, `closes`) does, as `valuation_latest` already keeps `close` visible under a statements hold.

## Global Constraints

- PUBLIC repo: no ticker tied to the owner, no value, account name, home path, key or SEC user agent in code, tests, docs or commit messages. Fixtures use invented companies (CIK `0000999002`, "Sample Earnings Co") and widely held symbols (SPY, XLK, AAPL, VFIAX). Headlines in fixtures are invented. `tests/test_privacy.py` stays green; `config.example.toml` must equal `CONFIG_EXAMPLE`.
- Only `store.py`, `migrate.py`, `readonly.py` execute SQL. The checker's reads are `Store.check_*` methods; the engine's reads are new `Store` methods named in each task.
- Schema changes are new migrations: `0018_engine_inputs.sql`, `0019_valuation_scores.sql`, `0020_price_moves.sql`. Never edit an applied file.
- No network in tests. The engine reads only the warehouse and two sibling files (`news.json` via `ingest`, `insider.json`), both under `cfg.investing_dir / "data"`, each ignored when missing.
- `check/` imports nothing from `derive/`, `statements.py` or the collectors. Constants both sides need (`SCORE_METRICS`) live in `models.py`; the checker keeps its own copy of the band rule (so one bug cannot fool both).
- Spec values, verbatim: drops at 4% in a day, 8% over 5 trading days, or beyond 2.5 standard deviations of the symbol's own 1-year daily excess moves over SPY; bands `cheap` under the 25th percentile, `premium` over the 75th; percentiles against the symbol's own 5-year daily history; a cause "market" or "sector" when the benchmark moved at least two thirds as much the same way; causes in the order earnings, filing, market, sector, news, `no cause found`.
- Every `valuation_scores_raw` row and every `price_moves` row carries `sources`: a JSON list of `{kind, label, date, ref}` plus `url` (filings: built from CIK + accession, never copied) or `period` (filings) where they apply. Kinds: `price`, `filing`, `feed`, `config`, `derived`.
- Statuses of a score: `ok | held | unsupported | not_enough_history`; bands `cheap | usual | premium` or NULL.
- Idempotent: a second `fdc sync` with nothing new leaves every row of the new tables identical.
- Tests run from the worktree: `PYTHONPATH=src ../../.venv/Scripts/python.exe -m pytest -q`.
- Each task updates the docs it changes: AGENTS.md (layout, tables list), README (what a sync builds), CHANGELOG `## Unreleased`.

## Review Focus

1. A stock split (4-for-1) or an ex-dividend day inside the window must not appear as a drop, nor bend the 52-week high or the 200-day average: price shape and moves use adjusted closes computed by `derive/adjust.adjusted_closes` from final bars, never stored `adj_close`. Tests in Tasks 3 and 7.
2. A sync whose check step failed, or that ran `--only engine` after a partial `fdc check --only fresh`, must not rebuild bands from unchecked values: the step is `skipped`, the tables untouched. Test in Task 3.
3. A hold over a past span leaves those days out of the 5-year history (the percentile is computed without them); a hold on the newest day makes the score `held` with no band; a hold found after the build blanks it through the view. Tests in Tasks 4 and 8.
4. An after-close report on the evening before the drop is the drop's earnings cause; a before-open report on that same day is not (it moved the day before). Test in Task 7.
5. A fund with eleven months of its own snapshots, or whose snapshot source changed from its own figures to a proxy ETF part-way, gets no band: percentiles count only snapshots with the newest one's `source`, and need `min_history_days` of them. Test in Task 6.

---

## File Structure

- `src/financial_data_collector/derive/sources.py` — new: builders for each source kind, `filing_url`, `safe_link` (Task 1).
- `src/financial_data_collector/derive/engine.py` — new: `run` (coverage gate, per-symbol build, writes), `apply_holds`, `held_bars` (Task 3; Tasks 4-7 add their builders to it).
- `src/financial_data_collector/derive/shape.py` — new: price shape (Task 3).
- `src/financial_data_collector/derive/scores.py` — new: `percentile`, `band`, the company history metrics (Task 4), the fundamentals metrics (Task 5), the fund metrics (Task 6).
- `src/financial_data_collector/derive/moves.py` — new: drops and their causes (Task 7).
- `src/financial_data_collector/check/engine.py` — new: check `scores` (Task 8).
- `src/financial_data_collector/migrations/0018_engine_inputs.sql`, `0019_valuation_scores.sql`, `0020_price_moves.sql` — new.
- `config.py`, `config.example.toml` — `[engine]` (Task 1). `models.py` — `SCORE_METRICS`, `ScoreRow`, `PriceMove`, `CurrentReport`, `Headline` (Task 1).
- `store.py` — the methods each task names; `COUNT_TABLES` gains the new tables.
- `collectors/earnings.py` (8-Ks, Task 2), `ingest.py` (headlines, Task 2), `sync.py` (step `engine`, Task 3; post-engine checks, Task 8), `check/__init__.py` (allowed finding fields, Task 8).
- Tests: `tests/test_sources.py`, `tests/test_engine_inputs.py`, `tests/test_engine.py`, `tests/test_scores.py`, `tests/test_fund_scores.py`, `tests/test_moves.py`, `tests/test_check_engine.py` (new); `tests/test_config.py`, `tests/test_sync.py`, `tests/test_check_frame.py`, `tests/test_earnings.py`, `tests/test_ingest.py`.

---

### Task 1: Engine settings, the source builders, the shared names

**Files:**
- Create: `src/financial_data_collector/derive/sources.py`, `tests/test_sources.py`
- Modify: `config.py`, `config.example.toml`, `models.py`
- Test: `tests/test_sources.py`, `tests/test_config.py`

**Interfaces:**
- Produces: `config.EngineSettings` (frozen) with defaults `history_years: int = 5`, `min_history_days: int = 365`, `cheap_percentile: float = 25.0`, `premium_percentile: float = 75.0`, `discount_rate: float = 0.09`, `terminal_growth: float = 0.025`, `drop_day: float = 0.04`, `drop_week: float = 0.08`, `excess_sd: float = 2.5`, `moves_days: int = 365`; `Config.engine: EngineSettings`.
- Produces: `models.SCORE_METRICS: tuple[str, ...]` = `("pe", "p_fcf", "ps", "ev_ebit", "dividend_yield", "implied_growth", "fcf_growth_5y", "revenue_growth_5y", "fcf_margin_change", "op_margin_change", "net_debt_ebitda", "buyback_yield", "last_surprise", "estimate_change_30d", "insider_net_90d", "expense_ratio", "off_high_52w", "vs_200d", "week_vs_market", "week_vs_sector")`.
- Produces: `models.ScoreRow(symbol: str, date: str, metric: str, value: float | None, percentile_5y: float | None, band: str | None, status: str, status_reason: str | None, inputs: tuple[str, ...], sources: tuple[dict, ...])` (frozen); `__post_init__` rejects a metric not in `SCORE_METRICS`, a status not in `("ok", "held", "unsupported", "not_enough_history")`, a band not in `(None, "cheap", "usual", "premium")`.
- Produces: `models.PriceMove(symbol, date, span: str ("1d" | "5d"), start_date, move: float, market_symbol: str | None, market_move: float | None, sector_symbol: str | None, sector_move: float | None, excess_sd: float | None, triggers: tuple[str, ...], causes: tuple[dict, ...], sources: tuple[dict, ...])` (frozen); `models.CurrentReport(cik, accession, form, filed, items: tuple[str, ...])`; `models.Headline(symbol, date, title, outlet: str | None, url: str | None, published: str | None, feed_as_of: str | None)`.
- Produces in `derive/sources.py`: `filing_url(cik: str, accession: str) -> str`; `safe_link(url: object) -> str | None`; `price(source: str, bar_date: str, fetched_at: str | None) -> dict`; `filing(cik: str, form: str, accession: str, filed: str, period: str | None = None) -> dict`; `feed(label: str, as_of: str | None, ref: str | None, url: str | None = None) -> dict`; `config(setting: str, value: object) -> dict`; `derived(label: str, ref: str, date: str | None = None) -> dict`.

Rules:
- `filing_url` = `https://www.sec.gov/Archives/edgar/data/{int(cik)}/{accession without dashes}/{accession}-index.htm`; it raises `ValueError` unless the accession matches `\d{10}-\d{2}-\d{6}` and the CIK is digits.
- `safe_link` keeps a string only when it starts with `https://` and holds no whitespace, quote, `<` or `>`; anything else is None.
- Shapes: `price` → `{"kind": "price", "label": f"{source} close", "date": bar_date, "ref": fetched_at}`; `filing` → `{"kind": "filing", "label": form, "date": filed, "ref": accession, "period": period, "url": filing_url(...)}` (`period` omitted when None); `feed` → `{"kind": "feed", "label", "date": as_of, "ref"}` plus `url` only when `safe_link` keeps it; `config` → `{"kind": "config", "label": f"[engine] {setting}", "date": None, "ref": str(value)}` (a setting already holding a `[` is used as given); `derived` → `{"kind": "derived", "label", "date", "ref"}`.
- `[engine]` validation in the style of `_check_settings`: unknown keys rejected by name; `history_years`, `min_history_days`, `moves_days` whole numbers ≥ 1; the rest numbers above 0; `cheap_percentile < premium_percentile < 100`; `terminal_growth < discount_rate`.
- `CONFIG_EXAMPLE` gains, after `[check]`:

```toml
[engine]                           # the valuation engine (fdc sync's engine step: no network, no AI); these are the defaults
history_years = 5                  # a score's percentile is against this many years of the symbol's own daily history
min_history_days = 365             # ... and needs at least this many days of it (else "not_enough_history", no band)
cheap_percentile = 25              # under this percentile of its own history: cheap (a yield: over 100 minus this)
premium_percentile = 75            # over this: premium (a yield: under 100 minus this)
discount_rate = 0.09               # implied growth: the yearly return the market cap is discounted at
terminal_growth = 0.025            # ... and the yearly growth after its 10 years
drop_day = 0.04                    # a drop: a final close down this fraction in a day
drop_week = 0.08                   # ... or this much over 5 trading days
excess_sd = 2.5                    # ... or a day's move minus SPY's beyond this many of its own 1-year standard deviations
moves_days = 365                   # price_moves covers this many calendar days back
```

- [ ] **Step 1: Write the failing tests**

```python
def test_filing_url_is_built_from_cik_and_accession():
    assert sources.filing_url("0000999002", "0000999002-26-000031") == \
        "https://www.sec.gov/Archives/edgar/data/999002/000099900226000031/0000999002-26-000031-index.htm"

def test_filing_url_refuses_a_malformed_accession():
    with pytest.raises(ValueError):
        sources.filing_url("0000999002", "../../x")

@pytest.mark.parametrize("url, kept", [("https://example.com/a", True), ("http://example.com/a", False),
    ("javascript:alert(1)", False), ("https://example.com/a b", False), ('https://x.com/"', False), (None, False)])
def test_safe_link(url, kept): ...

def test_filing_source_shape():
    s = sources.filing("0000999002", "10-Q", "0000999002-26-000031", "2026-07-31", "2026-06-30")
    assert s == {"kind": "filing", "label": "10-Q", "date": "2026-07-31", "ref": "0000999002-26-000031",
                 "period": "2026-06-30", "url": sources.filing_url("0000999002", "0000999002-26-000031")}

def test_feed_source_drops_an_unsafe_link():
    assert "url" not in sources.feed("news.json", "2026-10-28", "A headline", "javascript:x")

def test_score_row_refuses_an_unknown_metric():
    with pytest.raises(ValueError):
        ScoreRow("AAA", "2026-10-29", "pe_ratio", 1.0, None, None, "ok", None, (), ())

# tests/test_config.py
def test_engine_defaults_are_the_spec_values(tmp_path):
    cfg = _cfg(tmp_path)
    assert (cfg.engine.drop_day, cfg.engine.drop_week, cfg.engine.excess_sd) == (0.04, 0.08, 2.5)
    assert (cfg.engine.cheap_percentile, cfg.engine.premium_percentile, cfg.engine.history_years) == (25.0, 75.0, 5)

def test_engine_cheap_must_be_below_premium(tmp_path):
    with pytest.raises(C.ConfigError, match="cheap_percentile"):
        _cfg(tmp_path, "[engine]\ncheap_percentile = 80\n")

def test_engine_terminal_growth_must_be_below_the_discount_rate(tmp_path): ...
def test_engine_unknown_setting_is_named(tmp_path): ...
```

- [ ] **Step 2: Run them to verify they fail** — `... -m pytest tests/test_sources.py tests/test_config.py -q`; expected FAIL (`ModuleNotFoundError: ...derive.sources`).

- [ ] **Step 3: Implement** per the Interfaces and rules; rewrite `config.example.toml` to equal `CONFIG_EXAMPLE`. Docs: AGENTS layout `derive/` line gains `sources.py`; CHANGELOG.

- [ ] **Step 4: Run the whole suite** — expected: all pass.

- [ ] **Step 5: Commit** — `git commit -m "engine: [engine] settings, source builders, the score and move shapes"`

---

### Task 2: Raw inputs: 8-Ks from SEC, headlines from the news feed

**Files:**
- Create: `migrations/0018_engine_inputs.sql`, `tests/test_engine_inputs.py`, `tests/fixtures/investing/data/news.json`
- Modify: `collectors/earnings.py`, `ingest.py`, `store.py`, `sync.py` (`_step_ingest`), `tests/fixtures/sec/submissions_EARN.json` (only if it lacks an 8-K with an item other than 2.02 / 9.01; it already has a `5.07`)

**Interfaces:**
- Consumes: Task 1's `CurrentReport`, `Headline`, `sources.safe_link`.
- Produces: tables

```sql
CREATE TABLE current_reports (            -- every 8-K / 8-K/A SEC's submissions list shows; kept, never rewritten
  accession TEXT PRIMARY KEY, cik TEXT NOT NULL, form TEXT NOT NULL, filed TEXT NOT NULL,
  items TEXT NOT NULL DEFAULT '');         -- as SEC writes them: "2.02,9.01"
CREATE INDEX idx_current_reports_cik ON current_reports (cik, filed);
CREATE TABLE news_headlines (             -- headline, outlet, date and link from the sibling news feed; never article text
  symbol TEXT NOT NULL, date TEXT NOT NULL, title TEXT NOT NULL, outlet TEXT, url TEXT, published TEXT,
  feed_as_of TEXT, first_seen TEXT NOT NULL,
  PRIMARY KEY (symbol, date, title));
```

- Produces: `earnings.current_reports(cik: str, recent: dict) -> list[CurrentReport]` (forms `8-K`, `8-K/A`; accession matching `ACCESSION`; items split on `,`, stripped, empty dropped); `Store.write_current_reports(reports: Iterable[CurrentReport]) -> int` (insert-or-ignore; returns rows new); `Store.current_reports_of(cik: str, start: str, end: str) -> list[CurrentReport]` (filed in `[start, end]`, oldest first).
- Produces: `ingest.read_news(path: Path) -> tuple[list[Headline], str | None]` (the feed's `by_ticker[*]` items, symbol canonical, `outlet` = the item's `source`, `url` through `safe_link`; the second value an error note, or None; a missing file → `([], None)`); `ingest.ingest_news(store, cfg) -> tuple[int, str | None]` (rows new, note); `Store.write_headlines(rows: Iterable[Headline], first_seen: str) -> int` (insert-or-ignore on the key; returns rows new); `Store.headlines_of(symbol: str, start: str, end: str) -> list[Headline]` (date in `[start, end]`, oldest first, then title).

Rules:
- The `earnings` step stores the 8-Ks of every submissions answer it gets (the same answer it reads report dates from), inside the per-company try; the count joins the step's rows and its message gains `", R 8-Ks new"`.
- `_step_ingest` calls `ingest_news` after the broker feed; its rows join the step's rows, its message gains `"N headlines new"` (and the note when the feed can't be read). `cfg.investing_dir` None or no `data/news.json` → nothing, no message.
- Nothing from an item but `date`, `published`, `title`, `source`, `url` is read. An item without a `date` or `title` is skipped.

- [ ] **Step 1: Write the failing tests** (`tests/test_engine_inputs.py`; the news fixture: `as_of` `"2026-10-28T12:00:00+00:00"`, `by_ticker` `AAPL` with two invented headlines on 2026-10-27 and 2026-10-28 (one with a `javascript:` url), `SPY: []`, plus `description` and `tags` fields that must not be stored)

```python
def test_current_reports_reads_8ks_with_their_items():
    reps = earnings.current_reports("0000999002", EARN["filings"]["recent"])
    assert {(r.form, r.items) for r in reps} >= {("8-K", ("2.02", "9.01")), ("8-K", ("5.07",))}
    assert all(r.form in ("8-K", "8-K/A") for r in reps)

def test_earnings_step_stores_8ks_once(store, cfg_with_agent):
    # run collect_earnings twice on different New York days with the same submissions answer
    assert store.write_current_reports(reps) == 0          # second write of the same answer
    assert [r.items for r in store.current_reports_of("0000999002", "2026-01-01", "2026-12-31")]  # oldest first

def test_read_news_keeps_headline_outlet_date_and_a_safe_link(fixtures):
    rows, note = ingest.read_news(fixtures / "investing" / "data" / "news.json")
    assert note is None and [r.symbol for r in rows] == ["AAPL", "AAPL"]
    assert [r.url for r in rows].count(None) == 1           # the javascript: link is dropped
    assert not hasattr(rows[0], "description")

def test_ingest_news_is_idempotent(store, cfg):             # second run: 0 new, rows unchanged
def test_missing_news_feed_is_quiet(store, cfg_without_investing):
def test_unreadable_news_feed_is_a_note_not_an_error(store, cfg, tmp_path):   # invalid JSON
```

- [ ] **Step 2: Run them to verify they fail** — expected FAIL (`AttributeError: ... 'current_reports'`).

- [ ] **Step 3: Implement** per the Interfaces and rules. `Store.COUNT_TABLES` gains `current_reports`, `news_headlines`. Docs: AGENTS tables list (both tables), README ("a sync also keeps each company's 8-Ks and the news feed's headlines, for drop causes"), CHANGELOG.

- [ ] **Step 4: Run the whole suite** — expected: all pass (`test_full_sync_then_noop` sees both new tables unchanged on the second run).

- [ ] **Step 5: Commit** — `git commit -m "inputs for drop causes: each company's 8-Ks from SEC, the news feed's headlines kept"`

---

### Task 3: The engine step, holds, price shape

**Files:**
- Create: `derive/engine.py`, `derive/shape.py`, `migrations/0019_valuation_scores.sql`, `tests/test_engine.py`
- Modify: `store.py`, `sync.py`, `tests/test_sync.py`

**Interfaces:**
- Consumes: Task 1's `ScoreRow`, `sources.*`, `Config.engine`; `derive/adjust.adjusted_closes`.
- Produces: table and view

```sql
CREATE TABLE valuation_scores_raw (       -- what the engine built (held inputs already applied); served through valuation_scores
  symbol TEXT NOT NULL, date TEXT NOT NULL, metric TEXT NOT NULL,
  value REAL, percentile_5y REAL, band TEXT CHECK (band IN ('cheap', 'usual', 'premium')),
  status TEXT NOT NULL CHECK (status IN ('ok', 'held', 'unsupported', 'not_enough_history')),
  status_reason TEXT,
  inputs TEXT NOT NULL DEFAULT '',        -- the valuation_daily columns it was built from, as ",pe," (a hold on one blanks it)
  sources TEXT NOT NULL DEFAULT '[]',     -- JSON list of {kind, label, date, ref[, period][, url]}
  checked_at TEXT,                        -- finish time of the check run that covered the derive it was built from
  PRIMARY KEY (symbol, metric));
```

  View `valuation_scores`, written in 0013's style: every raw column, with `value`, `percentile_5y`, `band` NULL and `status` `'held'` (only when it was `ok`), `status_reason` the hold messages, when an open hold (`severity = 'hold'`, `cleared_at IS NULL`, same symbol, `date` within `date_from..COALESCE(date_to, '9999-12-31')`) has `field = metric`, or `instr(inputs, ',' || field || ',') > 0`, or `field = '*'` and either the row is not a price row (`inputs <> ',close,'`) or the hold's check is one of `prices2`, `impossible`, `closes`; plus `hold_ids` (comma list, NULL when none). Columns are only ever added to this view.
- Produces: `Store.engine_coverage() -> dict | None` = `{"derive_run_id", "check_run_id", "checked_at"}` for the newest ok derive and the newest `check_runs` row with that `derive_run_id` and `error IS NULL`; None when there is none. `Store.engine_symbols() -> list[sqlite3.Row]` (symbol, asset_type, cik, sector, benchmark, held: securities not parked, not `money_market` / `crypto`, with a final bar, and `benchmark = 0` or held). `Store.final_bars(symbol) -> list[tuple[str, float, float, float, str, str | None]]` (date, close, dividend, split_factor, source, fetched_at; final only, ascending). `Store.open_holds(symbol) -> list[dict]` (id, check_name, field, date_from, date_to, message; severity hold, not cleared). `Store.replace_scores(rows: list[ScoreRow], checked_at: str) -> int`.
- Produces in `derive/engine.py`: `apply_holds(rows: list[dict], holds: list[dict], fields: tuple[str, ...]) -> list[dict]` (copies; a value field is None on a date an open hold covers when the hold's field is that field or `*`; each copy gains `"_held": [hold ids]`); `PRICE_CHECKS = ("prices2", "impossible", "closes")`; `price_holds(holds) -> list[dict]` (the holds that touch a close: field `close`, or `*` from a `PRICE_CHECKS` check; a `*` hold from `fresh`, `filings` or `figures` is about statements and leaves prices alone, as `valuation_latest` always shows `close`); `held_bars(bars, holds) -> list[tuple]` (bars on a date a price hold covers are dropped); `EngineReport(symbols: int, scores: int, held: int, moves: int, skipped: str | None)`; `run(store: Store, cfg: Config) -> EngineReport`.
- Produces in `derive/shape.py`: `price_shape(symbol: str, day: str, bars, market: tuple[str, list] | None, sector: tuple[str, list] | None) -> list[ScoreRow]` (bars = held final bars of the symbol; `market` / `sector` = (benchmark symbol, its held final bars) or None).
- Produces: `STEPS` = `(..., "derive", "export", "check", "engine")`; `STALE_HOURS["engine"] = 48`; `_step_engine` in `_RUNNERS`.

Rules:
- `run`: no coverage → `EngineReport(..., skipped="waiting for a check run to cover derive <id>")` (or `"... no derive has finished yet"`), nothing written. Otherwise, per `engine_symbols()` row: `day` = its newest final bar's date (held or not); the price-shape rows; Tasks 4-7 add theirs. Every row is written with the coverage's `checked_at`.
- When a price hold covers `day`, every price-shape row is `held` (value NULL, reason the hold messages) on `day`; held bars before `day` are left out of every window.
- Price shape, on adjusted closes (`adjusted_closes` over the held final bars):
  - `off_high_52w` = close / max(adjusted close over the 365 calendar days ending `day`) − 1; needs a bar 300+ days back, else `not_enough_history`;
  - `vs_200d` = close / mean(the last 200 adjusted closes) − 1; needs 200 bars, else `not_enough_history`;
  - `week_vs_market` = the symbol's 5-trading-bar return − the market fund's return between the same two dates (`unsupported`, reason `"no SPY close on <date>"`, when the benchmark lacks either date);
  - `week_vs_sector` likewise against `cfg.benchmarks.sectors[sector]` (`unsupported`, `"no sector"`, when the security has none or the config names no fund for it).
  - Each row's `inputs` is `("close",)`; sources: the two `price` sources of the bars it compares (the symbol's, and the benchmark's when used), and a `derived` source naming the window (`"adjusted closes 2025-10-29..2026-10-29 (251 bars)"`).
- `_step_engine`: `rows` = scores + moves written; message `"N symbols, S scores (H held), M moves"`, or the skip reason with status `skipped`.

- [ ] **Step 1: Write the failing tests** (`tests/test_engine.py`; helpers `_weekday_bars(store, symbol, end="2026-10-29", n=260, close=100.0, overrides={})`, `_cover(store)` writing an ok derive `sync_runs` row and a check run covering it)

```python
def test_engine_waits_for_a_check_run(store, cfg):                       # Review Focus 2
    _weekday_bars(store, "AAA"); store.log_run("r1", "derive", "ok", 0, "", NOW_S, NOW_S, "manual")
    rep = engine.run(store, cfg)
    assert rep.skipped.startswith("waiting for a check run") and store.query("SELECT COUNT(*) FROM valuation_scores_raw")[0][0] == 0

def test_a_partial_or_failed_check_run_does_not_cover(store, cfg):
    # check_runs row with derive_run_id NULL (partial) and one with error set: still skipped

def test_price_shape(store, cfg):
    # AAA: 260 weekday bars at 100, a 125 on the 240th bar from the end, the last close 95, the close 5 bars before 100
    # SPY: the same dates, a 2% fall over the last 5 bars
    rows = _scores(store, "AAA")
    assert rows["off_high_52w"]["value"] == pytest.approx(95 / 125 - 1)
    assert rows["vs_200d"]["value"] == pytest.approx(95 / ((199 * 100 + 95) / 200) - 1)
    assert rows["week_vs_market"]["value"] == pytest.approx(-0.05 - (-0.02))
    assert rows["week_vs_sector"]["status"] == "unsupported" and rows["week_vs_sector"]["status_reason"] == "no sector"
    assert json.loads(rows["vs_200d"]["sources"])[0]["kind"] == "price"

def test_a_split_does_not_bend_the_high_or_the_average(store, cfg):     # Review Focus 1
    # 4-for-1 split 100 bars ago: closes 400 before, 100 after (split_factor 4 on the ex-date)
    assert _scores(store, "AAA")["off_high_52w"]["value"] == pytest.approx(0.0, abs=1e-9)

def test_held_close_days_are_left_out(store, cfg):
    # an open 'impossible' hold, field '*', over the 125 day: off_high_52w = 95 / 100 - 1

def test_a_stale_statements_hold_leaves_price_shape_alone(store, cfg):
    # an open 'fresh' hold, field '*', from a year ago, still going: off_high_52w still 95 / 125 - 1, status ok

def test_a_price_hold_on_the_newest_day_holds_the_shape(store, cfg):
    # an open 'prices2' hold, field '*', on 2026-10-29: vs_200d status held, value None, date 2026-10-29

def test_view_blanks_a_score_held_after_the_build(store, cfg):          # Review Focus 3 (view part)
    # build, then insert an open hold field 'close' for AAA on the newest date
    r = store.query("SELECT value, band, status, hold_ids FROM valuation_scores WHERE symbol='AAA' AND metric='vs_200d'")[0]
    assert (r["value"], r["status"]) == (None, "held") and r["hold_ids"]

def test_benchmark_only_symbols_get_no_scores_but_a_held_benchmark_does(store, cfg):
def test_rows_carry_the_covering_check_runs_finish_time(store, cfg):
def test_second_run_changes_no_row(store, cfg):                         # same rows, same checked_at

# tests/test_sync.py
def test_engine_runs_after_check():
    assert sync.STEPS[-2:] == ("check", "engine")
```

- [ ] **Step 2: Run them to verify they fail** — expected FAIL (`ModuleNotFoundError: ...derive.engine`).

- [ ] **Step 3: Implement** per the Interfaces and rules. `COUNT_TABLES` gains `valuation_scores_raw`. Existing step-list assertions in `tests/test_sync.py` gain `"engine"`. Docs: AGENTS layout (`sync.py` step list, `derive/` line), tables list (`valuation_scores` / `valuation_scores_raw`: one row per symbol and metric at its newest final close; statuses; how holds reach it), README (the engine step and why it runs after the check), CHANGELOG.

- [ ] **Step 4: Run the whole suite** — expected: all pass.

- [ ] **Step 5: Commit** — `git commit -m "engine step: runs after a check covers the derive, reads held values, writes price shape"`

---

### Task 4: Company scores against their own history

**Files:**
- Create: `derive/scores.py`, `tests/test_scores.py`
- Modify: `derive/engine.py`, `store.py`

**Interfaces:**
- Consumes: Task 3's `run`, `apply_holds`, `ScoreRow`, coverage; `valuation_daily` (`derive/valuation.COLUMNS`).
- Produces in `derive/scores.py`: `HIGH_IS_PREMIUM = ("pe", "p_fcf", "ps", "ev_ebit")`, `HIGH_IS_CHEAP = ("dividend_yield",)`; `percentile(history: list[float], value: float) -> float` (mid-rank: `100 * (below + 0.5 * equal) / len(history)`, the value itself counted in `history`, rounded to 1 decimal); `band(metric: str, pct: float | None, s: EngineSettings) -> str | None`; `ev_ebit(market_cap, debt, cash, short_term, operating_income) -> float | None`; `history_scores(symbol: str, day: str, rows: list[dict], ttm: dict[str, dict], filing: dict | None, cik: str, s: EngineSettings) -> list[ScoreRow]` (rows = held `valuation_daily` rows of the symbol, ascending; `ttm` = financials_ttm rows of its company by `period_end`; `filing` = the statement filing of the newest row's `ttm_period_end`).
- Produces: `Store.valuation_rows(symbol) -> list[dict]` (valuation_daily, ascending); `Store.ttm_rows(cik) -> dict[str, dict]` (financials_ttm by period_end: `operating_income`, `long_term_debt`, `cash`, `short_term_investments`, `depreciation_amortization`, `buybacks`, `revenue`, `fcf`, `period_end`, `available_from`); `Store.statement_filing(cik, period_end) -> dict | None` (the earliest-filed `10-Q`/`10-K` (`/A` too) in `sec_facts` reporting a fact for that period end: `form`, `accn`, `filed`).

Rules:
- Metrics for `asset_type = 'stock'`: `pe`, `p_fcf`, `ps` (straight from the held row), `ev_ebit` (computed per date from the row's `market_cap` and the TTM row whose `period_end` = the row's `ttm_period_end`: `(market_cap + long_term_debt − cash − short_term_investments) / operating_income`; NULL when `operating_income <= 0` or debt or cash is missing; a missing short-term line counts 0), `dividend_yield`.
- History: the metric's non-NULL values on `status = 'ok'` rows dated within `history_years` calendar years up to `day`. Percentile when the oldest of them is at least `min_history_days` before `day`; else `not_enough_history` with reason `"<N> days of its own history; a band needs <min_history_days>"`.
- `band`: `HIGH_IS_PREMIUM`: `pct < cheap_percentile` → `cheap`, `pct > premium_percentile` → `premium`, else `usual`; `HIGH_IS_CHEAP` mirrored (`pct > 100 − cheap_percentile` → `cheap`, `pct < 100 − premium_percentile` → `premium`).
- The newest row decides the status: its status `stale` or `unsupported` → `unsupported`, its `status_reason` kept; a value NULL on an `ok` row because a hold covers it → `held`, reason the hold messages (`_held` ids); NULL otherwise → `unsupported` with `"earnings not positive"` (pe), `"free cash flow not positive"` (p_fcf), `"no revenue"` (ps), `"no debt or cash figure in its filings"` / `"operating income not positive"` (ev_ebit), `"no dividend"` (dividend_yield).
- `inputs`: pe `("pe",)`, p_fcf `("p_fcf",)`, ps `("ps",)`, ev_ebit `("market_cap",)`, dividend_yield `("dividend_yield",)`.
- Sources of an `ok` row: the newest bar's `price` source, the `filing` source of the statement filing (when known), `derived` `("valuation_daily pe", "<first>..<day> (<n> days)")`, and `config` sources for `history_years`, `cheap_percentile`, `premium_percentile`.

- [ ] **Step 1: Write the failing tests**

```python
def test_percentile_is_mid_rank():
    assert scores.percentile([10, 20, 30, 40], 30) == 62.5

@pytest.mark.parametrize("metric, pct, want", [("pe", 10, "cheap"), ("pe", 50, "usual"), ("pe", 90, "premium"),
    ("pe", 25, "usual"), ("dividend_yield", 90, "cheap"), ("dividend_yield", 10, "premium"), ("pe", None, None)])
def test_band(metric, pct, want): ...

def test_pe_today_low_against_five_years_is_cheap(store, cfg):
    # AAA: 1300 weekday valuation rows, pe 30 throughout, the newest 12  -> percentile 0.0, band cheap
    r = _scores(store, "AAA")["pe"]
    assert (r["status"], r["band"], r["percentile_5y"], r["value"]) == ("ok", "cheap", 0.0, 12.0)
    assert {s["kind"] for s in json.loads(r["sources"])} >= {"price", "derived", "config"}

def test_a_past_hold_span_is_left_out_of_the_history(store, cfg):       # Review Focus 3
    # pe 30 for 4 years, 5 for a held 6-month span, 20 since; newest 20: without the span its percentile is mid-rank
    # among 20s and 30s only (the 5s would have pushed it up)

def test_a_hold_on_the_newest_day_makes_it_held_with_no_band(store, cfg):
    assert (_scores(store, "AAA")["pe"]["status"], _scores(store, "AAA")["pe"]["band"]) == ("held", None)

def test_under_a_year_of_history_has_no_band(store, cfg):
    r = _scores(store, "AAA")["pe"]; assert r["status"] == "not_enough_history" and r["value"] is not None

def test_negative_earnings_is_unsupported_and_says_why(store, cfg):
def test_a_stale_row_is_unsupported_with_its_reason(store, cfg):

def test_ev_ebit_needs_debt_and_cash():
    assert scores.ev_ebit(1000.0, 200.0, 50.0, None, 100.0) == pytest.approx(11.5)
    assert scores.ev_ebit(1000.0, None, 50.0, 0.0, 100.0) is None
    assert scores.ev_ebit(1000.0, 200.0, 50.0, 0.0, -5.0) is None

def test_ev_ebit_uses_the_statements_in_force_each_day(store, cfg):    # two TTM rows, the switch on available_from
def test_the_filing_source_names_the_10q_the_ttm_came_from(store, cfg):
    s = [x for x in json.loads(_scores(store, "AAA")["pe"]["sources"]) if x["kind"] == "filing"][0]
    assert (s["label"], s["ref"], s["period"]) == ("10-Q", "0000999002-26-000031", "2026-06-30")
```

- [ ] **Step 2: Run them to verify they fail** — expected FAIL (`ModuleNotFoundError: ...derive.scores`).

- [ ] **Step 3: Implement** per the Interfaces and rules; `engine.run` adds `history_scores` for each `stock` symbol with a CIK and valuation rows. Docs: README (what a band means, its thresholds), CHANGELOG.

- [ ] **Step 4: Run the whole suite** — expected: all pass.

- [ ] **Step 5: Commit** — `git commit -m "engine: P/E, P/FCF, P/S, EV/EBIT and yield against each company's own 5-year history, with bands"`

---

### Task 5: Company fundamentals: implied growth, quality, earnings, buybacks, insiders

**Files:**
- Modify: `derive/scores.py`, `derive/engine.py`, `store.py`
- Test: `tests/test_scores.py`

**Interfaces:**
- Consumes: Task 4's `scores.py`, `Store.ttm_rows`, `Store.statement_filing`, `Store.earnings_events_of`.
- Produces in `derive/scores.py`: `implied_growth(fcf: float, market_cap: float, discount_rate: float, terminal_growth: float) -> float | None` (bisection on `[-0.5, 1.0]` to 1e-6, rounded to 4 places; None when `fcf <= 0` or the answer lies outside the range); `cagr(new: float | None, old: float | None, years: int) -> float | None` (None unless both > 0); `fundamental_scores(symbol, day, newest: dict, ttm: dict[str, dict], annual: list[dict], events: list[EarningsEvent], estimates: list[dict], insider: tuple[list[dict], str | None] | None, cik: str, s: EngineSettings) -> list[ScoreRow]` (`newest` = the held newest valuation row; `insider` = the symbol's feed entries and the feed's `as_of`, or None when the feed or the symbol is absent).
- Produces: `Store.estimate_rows(symbol, period: str) -> list[dict]` (date, eps_avg; ascending); `scores.read_insider(path: Path) -> tuple[dict[str, list[dict]], str | None]` (canonical symbol → entries with `filed`, `accession`, `direction`; and `as_of`; missing or unreadable → `({}, None)`).

Rules (each metric's value, its status when not computable, its `inputs`):
- `implied_growth` from the newest row's `fcf_ttm` and `market_cap`, `cfg.engine` rate and terminal growth; `unsupported` `"free cash flow not positive"` or `"needs more than 100% or less than -50% a year"`; inputs `("market_cap", "fcf_ttm")`; sources include `config` `discount_rate` and `terminal_growth` and `derived` `("method", "10 years of FCF growth, then terminal_growth, discounted at discount_rate, = market cap")`.
- `fcf_growth_5y`, `revenue_growth_5y`: `cagr` between the newest `financials_annual` row and the one with `fiscal_year` 5 lower; `not_enough_history` when there is none; sources: both annual rows' `filing` sources (`statement_filing` of each `period_end`).
- `fcf_margin_change`, `op_margin_change`: margin of the TTM row in force (the newest row's `ttm_period_end`) minus the margin of the TTM row whose `period_end` is 350-380 days earlier (margins: `fcf / revenue`, `operating_income / revenue`, revenue > 0); `not_enough_history` without that earlier row.
- `net_debt_ebitda` = `(long_term_debt − cash − short_term_investments) / (operating_income + depreciation_amortization)` of the TTM row in force; `unsupported` when EBITDA ≤ 0 or debt or cash is missing.
- `buyback_yield` = TTM `buybacks` / the newest `market_cap`; a missing buybacks line is 0 (the concept is only reported by companies that bought back); inputs `("market_cap",)`.
- `last_surprise` = `surprise_pct` (percent) of the newest event dated ≤ `day` with one; `unsupported` `"no earnings surprise on record"`; sources: the 8-K `filing` source when the event has an accession, and `feed` `("Yahoo earnings dates", None, "<report_date>")`.
- `estimate_change_30d` = `(now − then) / |then|` of `0y` `eps_avg`, `now` the newest snapshot ≤ `day`, `then` the newest snapshot ≥ 30 days before `now`; `not_enough_history` `"estimate snapshots start <first date>"` without `then`; `unsupported` `"a report fell between the two snapshots; the year may have rolled"` when an earnings event is dated in `(then, now]`; `then == 0` → `unsupported`.
- `insider_net_90d` = buys − sells among the symbol's feed entries filed within the 90 days ending `day` (`mixed` and `other` count 0); `unsupported` `"insider feed not found"` / `"not in the insider feed"`; sources: `feed` `("insider.json", as_of, "<buys> buys, <sells> sells")` plus a `filing` source (form `4`) per counted entry with a valid accession, built with the company's CIK.
- None of these has a percentile or band.

- [ ] **Step 1: Write the failing tests**

```python
def test_implied_growth_is_zero_when_the_price_needs_none():
    assert scores.implied_growth(100.0, 1307.8751, 0.09, 0.025) == pytest.approx(0.0, abs=1e-4)
    assert scores.implied_growth(100.0, 2779.5894, 0.09, 0.025) == pytest.approx(0.10, abs=1e-4)

def test_implied_growth_outside_the_range_or_without_fcf_is_none():
    assert scores.implied_growth(100.0, 50.0, 0.09, 0.025) is None
    assert scores.implied_growth(-5.0, 1000.0, 0.09, 0.025) is None

def test_cagr():
    assert scores.cagr(161.051, 100.0, 5) == pytest.approx(0.10)
    assert scores.cagr(100.0, -5.0, 5) is None

def test_margins_compare_with_the_ttm_a_year_earlier(store, cfg):     # fcf margin 0.20 -> 0.15: change -0.05
def test_net_debt_ebitda_needs_positive_ebitda(store, cfg):
def test_buyback_yield_counts_a_missing_line_as_zero(store, cfg):
def test_last_surprise_cites_the_8k(store, cfg):
def test_estimate_change_needs_a_snapshot_30_days_old(store, cfg):
def test_estimate_change_is_blank_when_a_report_fell_between(store, cfg):
def test_insider_counts_buys_minus_sells_in_90_days(store, cfg, tmp_path):   # buy, buy, sell, other, a buy 120 days old -> 1
def test_insider_feed_missing_says_so(store, cfg):
```

- [ ] **Step 2: Run them to verify they fail** — expected FAIL (`AttributeError: ... 'implied_growth'`).

- [ ] **Step 3: Implement** per the Interfaces and rules; `engine.run` adds `fundamental_scores` for each `stock` with a CIK, reading `insider.json` once per run from `cfg.investing_dir / "data"`. Docs: README (each metric in one line, with its method), CHANGELOG.

- [ ] **Step 4: Run the whole suite** — expected: all pass.

- [ ] **Step 5: Commit** — `git commit -m "engine: implied growth, margin trends, leverage, buyback yield, earnings surprise, estimate direction, insider buying"`

---

### Task 6: Fund scores

**Files:**
- Modify: `derive/scores.py`, `derive/engine.py`, `store.py`
- Create: `tests/test_fund_scores.py`

**Interfaces:**
- Consumes: Task 4's `percentile`, `band`; Task 3's engine loop; `fund_snapshots`.
- Produces: `Store.fund_snapshot_rows(symbol) -> list[dict]` (date, trailing_pe, dividend_yield, expense_ratio, source, fetched_at; ascending); `scores.fund_scores(symbol, day, snaps: list[dict], s: EngineSettings) -> list[ScoreRow]`; `scores.SNAPSHOT_MAX_AGE_DAYS = 7`.

Rules:
- For `etf`, `mutual_fund`, `fund`: `pe` (trailing P/E), `dividend_yield`, `expense_ratio`, from the newest snapshot dated ≤ `day`; older than `SNAPSHOT_MAX_AGE_DAYS` → `unsupported` `"newest snapshot is from <date>"`; none → `unsupported` `"no fund snapshot yet"`.
- `pe` and `dividend_yield` get a percentile and band like a company's, over snapshots with the **same `source` as the newest one** within `history_years`; fewer than `min_history_days` of them → `not_enough_history`, reason `"<N> days of its own snapshots; a band needs <min_history_days>"` (the spec's "uses price measures only, and says so").
- `expense_ratio`: value only.
- Sources: `feed` `("fund_snapshots: <source>", <snapshot date>, <fetched_at>)`, `derived` for the history window, `config` for the thresholds. `inputs` `()` (funds have no `valuation_daily` row).

- [ ] **Step 1: Write the failing tests**

```python
def test_a_fund_with_eleven_months_of_snapshots_has_no_band(store, cfg):    # Review Focus 5
    r = _scores(store, "VFIAX")["pe"]
    assert (r["status"], r["band"]) == ("not_enough_history", None) and r["value"] is not None
    assert "a band needs 365" in r["status_reason"]

def test_a_year_of_snapshots_gives_a_band(store, cfg):
def test_proxy_snapshots_do_not_count_toward_own_history(store, cfg):       # Review Focus 5
    # 400 days 'proxy: SPY via yfinance', then 30 days 'yfinance': not_enough_history (30 days of its own)
def test_an_old_snapshot_is_unsupported(store, cfg):
def test_a_fund_gets_price_shape_too(store, cfg):
```

- [ ] **Step 2: Run them to verify they fail** — expected FAIL.

- [ ] **Step 3: Implement**; `engine.run` adds `fund_scores` for fund types. Docs: README (funds: P/E and yield against their own snapshots once a year exists; until then price measures only), CHANGELOG.

- [ ] **Step 4: Run the whole suite** — expected: all pass.

- [ ] **Step 5: Commit** — `git commit -m "engine: fund P/E and yield against their own snapshots once a year of them exists; expense ratio"`

---

### Task 7: Drops and their causes

**Files:**
- Create: `derive/moves.py`, `migrations/0020_price_moves.sql`, `tests/test_moves.py`
- Modify: `derive/engine.py`, `store.py`

**Interfaces:**
- Consumes: Task 1's `PriceMove`, `sources.*`; Task 2's `Store.current_reports_of`, `Store.headlines_of`; Task 3's held bars, engine loop; `Store.earnings_events_of`.
- Produces: table

```sql
CREATE TABLE price_moves (                -- drops of a final close and their causes, rebuilt by the engine step
  symbol TEXT NOT NULL, date TEXT NOT NULL,
  span TEXT NOT NULL CHECK (span IN ('1d', '5d')),
  start_date TEXT NOT NULL,               -- the close the move is measured from
  move REAL NOT NULL,                     -- -0.052 = down 5.2%
  market_symbol TEXT, market_move REAL, sector_symbol TEXT, sector_move REAL,
  excess_sd REAL,                         -- (move - market_move) / the symbol's 1-year sd of daily excess moves (1d only)
  triggers TEXT NOT NULL,                 -- "drop_day", "drop_week", "excess", comma-joined
  causes TEXT NOT NULL,                   -- JSON list of {kind, text, sources}, in the spec's order
  sources TEXT NOT NULL,
  checked_at TEXT,
  PRIMARY KEY (symbol, date, span));
```

- Produces in `derive/moves.py`: `ITEM_LABELS: dict[str, str]` (8-K items: 1.01 "a material agreement", 1.02 "an agreement ended", 1.03 "bankruptcy", 2.01 "an acquisition or sale of assets", 2.03 "a new debt obligation", 2.04 "a debt obligation accelerated", 2.05 "restructuring costs", 2.06 "an impairment", 3.01 "a delisting notice", 3.02 "unregistered share sales", 4.01 "an auditor change", 4.02 "earlier statements no longer to be relied on", 5.02 "a director or officer change", 5.07 "a shareholder vote", 7.01 "a Regulation FD disclosure", 8.01 "other events"; any other item is shown as `"item 9.99"`); `MARKET_SHARE = 2 / 3`; `find_moves(symbol, bars: list[tuple[str, float]], market: tuple[str, list] | None, sector: tuple[str, list] | None, since: str, s: EngineSettings) -> list[PriceMove]` (bars = (date, adjusted close) of held final bars; causes empty); `add_causes(move: PriceMove, events, reports, headlines, cik: str | None, sector_name: str | None) -> PriceMove`; `Store.replace_moves(moves: list[PriceMove], checked_at: str) -> int`.

Rules:
- Only days after `since` = `day − moves_days` are recorded; earlier bars still feed the 1-year deviation.
- `1d`: return between consecutive held bars ≤ `−drop_day` → trigger `drop_day`; or, when the market fund has bars on both dates and at least 120 earlier daily excess moves exist in the 252 bars before, `(move − market_move) / sd ≤ −excess_sd` → trigger `excess` (`excess_sd` stored for every 1d row it can compute).
- `5d`: return over 5 held bars ≤ `−drop_week`, recorded only on the first day it crosses (the previous day's 5-bar return was above `−drop_week`, or did not exist).
- Market and sector moves are the benchmark's returns between the move's own `start_date` and `date` (None when it lacks either bar).
- Causes, in order, each `{kind, text, sources}`:
  1. `earnings`: an event with `report_date` in `(start_date, date]`, or on `start_date` with `time_of_day` `after_close` or unknown. Text `"reported earnings on 2026-10-28 after the close: EPS 2.02 vs 1.89 expected (+6.9%)"`; without EPS figures `"reported earnings on 2026-10-28"`; `before the open` / `during the day` / nothing for the other times.
  2. `filing`: an 8-K filed in `[start_date, date]` whose items, without `2.02` and `9.01`, are not empty. Text `"filed an 8-K on 2026-10-28: a director or officer change; other events"`; sources: its `filing` source.
  3. `market`: `market_move <= MARKET_SHARE * move` → `"the market fell 5.0% (SPY)"`.
  4. `sector`: `sector_move <= MARKET_SHARE * move` → `"its sector fell 4.1% (XLK, Technology)"`.
  5. `news`: up to 3 headlines dated in `[start_date, date]`, newest first, one cause each: `"<title> (<outlet>, <date>)"`; sources: `feed` `("news.json: <outlet>", <feed_as_of>, <title>, <url>)`.
  6. none of these → `{"kind": "none", "text": "no cause found", "sources": []}`.
- Row sources: `price` sources of the two bars (and the benchmarks' bars used), `config` sources of the thresholds that fired.
- Funds and stocks alike; a symbol with no sector has no sector cause.

- [ ] **Step 1: Write the failing tests**

```python
def test_a_split_and_an_ex_dividend_day_are_not_drops(store, cfg):        # Review Focus 1
    # 400 -> 100 with split_factor 4; 100 -> 96 with dividend 4.0 on that bar: no price_moves rows

def test_a_day_down_four_percent_is_recorded_with_its_market_move():
    (m,) = moves.find_moves("AAA", _series([100, 95]), ("SPY", _series([500, 497.5])), None, SINCE, S)
    assert (m.span, m.move, m.market_move, m.triggers) == ("1d", pytest.approx(-0.05), pytest.approx(-0.005), ("drop_day",))

def test_an_excess_move_beyond_the_symbols_own_spread_is_recorded():
    # 252 days of +/-0.5% excess, then -2.0% with SPY flat: excess, not drop_day

def test_a_five_day_slide_is_recorded_once():
    # -2.5% a day for 6 days: one '5d' row, on the day the 5-bar return first passes -8%

def test_after_close_report_the_evening_before_is_the_cause():            # Review Focus 4
    m = moves.add_causes(_move("2026-10-28", "2026-10-29"), [_event("2026-10-28", "after_close")], [], [], CIK, None)
    assert m.causes[0]["kind"] == "earnings" and "after the close" in m.causes[0]["text"]

def test_before_open_report_on_the_day_before_is_not():                   # Review Focus 4
    m = moves.add_causes(_move("2026-10-28", "2026-10-29"), [_event("2026-10-28", "before_open")], [], [], CIK, None)
    assert m.causes == ({"kind": "none", "text": "no cause found", "sources": []},)

def test_causes_come_in_the_spec_order():
    # earnings + an 8-K 5.02 + SPY -5% of a -6% move + XLK -4.5% + one headline
    assert [c["kind"] for c in m.causes] == ["earnings", "filing", "market", "sector", "news"]
    assert m.causes[1]["sources"][0]["url"].startswith("https://www.sec.gov/Archives/edgar/data/999002/")

def test_an_8k_with_only_2_02_and_9_01_is_not_a_filing_cause():
def test_news_older_than_the_feed_still_explains_an_old_move(store, cfg): # headline stored by ingest weeks ago
def test_moves_older_than_moves_days_are_not_recorded():
def test_engine_writes_moves_with_checked_at(store, cfg):

def test_every_ok_row_has_a_source_and_every_link_is_built(store, cfg):   # the spec's sources test, over a full fixture
    # a company with statements, events, an 8-K, a headline and the insider feed; a fund with snapshots; SPY and XLK
    for r in store.query("SELECT status, sources FROM valuation_scores_raw UNION ALL SELECT 'ok', sources FROM price_moves"):
        srcs = json.loads(r["sources"])
        assert r["status"] != "ok" or srcs
        for s in srcs:
            if s["kind"] == "filing":
                assert s["url"] == sources.filing_url(CIK, s["ref"])
```

- [ ] **Step 2: Run them to verify they fail** — expected FAIL (`ModuleNotFoundError: ...derive.moves`).

- [ ] **Step 3: Implement**; `engine.run` writes moves for every engine symbol. `COUNT_TABLES` gains `price_moves`. Docs: AGENTS tables list, README (a drop, its triggers and the causes' order), `docs/QUERIES.md` (one query: last month's drops with their first cause), CHANGELOG.

- [ ] **Step 4: Run the whole suite** — expected: all pass.

- [ ] **Step 5: Commit** — `git commit -m "engine: drops of 4% a day, 8% a week or 2.5 sd over SPY, each with its causes and their sources"`

---

### Task 8: The engine checks

**Files:**
- Create: `check/engine.py`, `tests/test_check_engine.py`
- Modify: `check/__init__.py`, `check/findings.py` (docstring), `store.py`, `sync.py` (`_step_engine`)
- Test: `tests/test_check_frame.py`, `tests/test_sync.py`

**Interfaces:**
- Consumes: Task 1's `SCORE_METRICS`; Task 3's `valuation_scores_raw` and `_step_engine`; `Config.engine`.
- Produces: `check.engine.ENGINE_CHECKS = ("scores",)`; check `scores` registered in `CHECKS` after the existing ones; `Store.check_score_rows() -> list[sqlite3.Row]` (the raw table); the frame accepts a finding whose `field` is a `valuation_daily` column, `"*"`, or a name in `SCORE_METRICS`.
- Produces: `_step_engine` runs `check_mod.run_check(cfg, fetch=fetch, now=now, store=store, sleep=sleep, only=list(ENGINE_CHECKS))` after a build (not after a skip) and appends `"; checks: <holds> holds, <warns> warnings, <errors> errors"` to its message.

Rules of check `scores` (its own code, nothing from `derive/`; one finding per row, field = the metric, `date_from` = `date_to` = the row's date, severity `hold`):
- a row with status `ok` and an empty or unreadable `sources` → `"no sources"`;
- a source whose `url` does not start with `https://`, or a `filing` source whose `url` is not `https://www.sec.gov/Archives/edgar/data/<digits>/<18 digits>/<accession>-index.htm` with that source's own `ref` as the accession → `"a source link was not built"`;
- `percentile_5y` outside `[0, 100]` → `"percentile out of range"`;
- a band on a row that is not `ok`, a band without a percentile, or a band other than what the checker's own rule gives from the percentile, the metric and `[engine]` thresholds (premium-high: pe, p_fcf, ps, ev_ebit; cheap-high: dividend_yield; no band for any other metric) → `"band does not follow from its percentile"` with both in the evidence.

These run in the main `check` step too (on the scores then served); the post-engine run re-examines what was just written. A post-engine partial run never stamps `checked_at` (run_check's existing rule).

- [ ] **Step 1: Write the failing tests**

```python
def test_an_ok_score_with_no_sources_is_held(store, cfg):
    _raw(store, "AAA", "pe", status="ok", sources="[]")
    (f,) = check_engine.scores(_ctx(store, cfg))
    assert (f.check_name, f.symbol, f.field, f.severity, f.date_to) == ("scores", "AAA", "pe", "hold", "2026-10-29")

def test_a_filing_url_that_was_not_built_is_held(store, cfg):          # e.g. a url copied from a feed
def test_a_band_that_does_not_follow_from_its_percentile_is_held(store, cfg):   # pe 10th percentile with band premium
def test_a_band_on_a_metric_that_has_none_is_held(store, cfg):         # off_high_52w with band cheap
def test_a_sound_row_gives_nothing(store, cfg):
def test_a_held_score_is_blank_in_the_view(store, cfg):                # Review Focus 3: run_check(only=["scores"]) then the view
    assert store.query("SELECT value, status FROM valuation_scores WHERE metric='pe'")[0][:] == (None, "held")
def test_the_engine_check_imports_nothing_it_checks():                 # the existing guard test covers check/engine.py

# tests/test_check_frame.py
def test_a_finding_on_a_score_metric_is_accepted():
def test_a_finding_on_an_unknown_field_is_still_an_error():

# tests/test_sync.py
def test_engine_step_rechecks_what_it_wrote_and_does_not_stamp_checked_at(project):
```

- [ ] **Step 2: Run them to verify they fail** — expected FAIL (`ImportError: cannot import name 'engine'`).

- [ ] **Step 3: Implement** per the rules. Docs: AGENTS `check/` line (`engine.py`: scores), tables list (`check_findings.field` may also name a score metric), README checker section (the engine checks), CHANGELOG.

- [ ] **Step 4: Run the whole suite** — expected: all pass.

- [ ] **Step 5: Commit** — `git commit -m "check: scores holds a score with no sources, an unbuilt link or a band its percentile does not give"`

---

### Task 9: A real run on a copy (no code)

**Files:** none in the repo. Work in the session's scratch folder; record results in the ledger as counts only.

- [ ] **Step 1:** Copy the owner's `data/warehouse.db` (from fdc's main checkout) with SQLite's backup API (`sqlite3.connect("file:...?mode=ro", uri=True).backup(dest)`), never a file copy. Scratch `config.toml`: `[paths] db` = the copy, `[sources.investing] content_dir` = the real sibling folder (read-only use), `[export.cockpit] enabled = false`, `[sources.snaptrade] enabled = false`. No `.env`.

- [ ] **Step 2:** Run `PYTHONPATH=src ../../.venv/Scripts/python.exe -m financial_data_collector --root <scratch> sync --only ingest,derive,check,engine` (in the background if long), then the same command again.

Expected, recorded as counts only (no symbols tied to the owner, no values): the engine step `ok`; one score row per (symbol, metric) built; statuses tallied; every `ok` row with sources; bands present only on banded metrics; no fund band (snapshots began 2026-10-31); `price_moves` rows within the last year with a cause tally by kind (no `filing` causes expected: no SEC agent on the copy, so `current_reports` is empty; say so); the post-engine `scores` check finds no hold; the second run changes no row of `valuation_scores_raw` or `price_moves` (row hashes before and after). Spot-check three `ok` P/E percentiles by hand against `valuation_daily`, and two drops against `prices`.

- [ ] **Step 3:** Anything unexpected is a finding for a fix task before the whole-branch review, not a note.

---

## Not in this plan

- The weekly split (plan 4, Part 5): it reads `valuation_scores` (the view), its `ok` bands of P/E, P/FCF, EV/EBIT and P/S (companies) and P/E and yield (funds), and the quality changes. Its engine-sanity checks (more than the waiting cash, money to a `hold` name) come with it.
- Exports of scores or moves to the cockpit, and the window's pages (their own spec).
- Statement-based scores for foreign filers (Part 1 keeps them `unsupported`) and percentiles of the fundamentals metrics (only the five history metrics are banded, as the spec lists).
- Plan 1's deferred "funds and ETFs get no `valuation_daily` row" is confirmed by design: funds are valued from `fund_snapshots` here, `valuation_daily` stays company-only.
- Backfilling 8-Ks older than SEC's `recent` block, or headlines older than the feed's first read.
