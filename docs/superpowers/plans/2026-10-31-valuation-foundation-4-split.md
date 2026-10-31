# Valuation foundation, plan 4: the weekly split — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** When new money lands in an account's money market fund, the warehouse proposes, in whole dollars and in plain code, how to split it across that account's holdings: from the owner's targets file, his caps and sleeves (measured household-wide with fund look-through), and plan 3's valuation bands, leaning careful. Every line says why and where its numbers came from, and the checker holds back a proposal that breaks a rule.

**Architecture:** The split runs inside the `engine` step, after the scores, so it shares their gate (a full, error-free check run covers the newest derive) and reads the scores through the `valuation_scores` view (holds applied). Pure functions in `derive/targets.py` (read the targets file and the fund look-through CSVs) and `derive/split.py` (`holding_band`, `plan_split`, the lifecycle `run`). Proposals are a record, kept and closed, never rebuilt (Calls 2). The checker gains a `split` check with its own reading of the targets file; the views `allocation_proposals` / `allocation_lines` apply its holds.

**Tech Stack:** Python 3.11+, SQLite (numbered migrations), pytest (no network).

**Spec:** `docs/superpowers/specs/2026-10-30-valuation-foundation-design.md`, Part 5 ("the weekly split"), Part 2's "engine sanity" checks, "Decided in conversation" and "The builder's calls". Earlier rulings: `.superpowers/sdd/2026-10-31-valuation-foundation-3-engine/progress.md` (R1-R17; its deferred items for this plan: the fund snapshot-count floor and the boundary tests).

## Calls this plan makes (the owner may overrule any before the build)

1. **The split runs in the `engine` step, after the scores**, under the same gate. When the gate is shut the step is `skipped` and no proposal is opened, replaced or closed: the spec's "a proposal waits for the next check".
2. **Proposals are a record, not a derived table.** AGENTS.md says derived tables are rebuilt from scratch; a proposal cannot be (the scores it was built from are only kept for the newest day), so `allocation_proposals_raw` / `allocation_lines_raw` are kept like `check_findings`: a run only adds a proposal or closes one. An open proposal's lines are never rewritten, even when scores change; only new cash makes a new one.
3. **Accounts are named the targets file's way.** `[allocation.accounts]` in `config.toml` maps each targets-file account name to an fdc account label (`brokerage = "<label>"`, `roth = "<label>"`). The stored `account_key` is the targets-file name, never the label. `pct_of_brokerage` entries always measure, and send money to, the account mapped as `brokerage`, wherever they are filed (the existing reader's rule).
4. **The waiting cash** is the account's newest `cash_balances` row (the adapters already turn the money market position into cash).
5. **A percent target is measured on the account's value with the waiting cash in it** (positions plus cash, since the cash is about to be placed): gap = max(0, target% × base − the holding's value in that account).
6. **Money is shared by gap × tilt; a line can grow past its target up to its band's top** (the entry's `max`, else target × 1.25) once others are full; it never passes the band's top, a sleeve's `max` or a cap. Money that fits nowhere waits for next week. A holding at or over its target gets nothing.
7. **The direct/via-funds ratio** (`direct_passive_ratio`): gap = target × (household dollars held via funds) − (household dollars held directly), its top = band top × the same. A name no fund on file holds gets nothing ("no look-through"). The fund money in the same proposal is not counted as raising the via-funds side (small, and it errs low).
8. **A sleeve limits; its members' own entries decide who gets money.** A sleeve's `max` caps the sum of its members in that account; its `min` is not pushed toward (the split never sells and never forces money in); its own `rebalance` does not block a member whose entry says `contributions`. *(Alternative the owner may prefer: a sleeve marked `hold` gives no new money to any member.)*
9. **The household for caps and look-through** is every account whose newest positions snapshot is within 7 days of the newest snapshot of any account, cash included (the existing reader counts cash in the total). An older account (a closed one) is left out and the proposal's notes say so.
10. **An unknown or misspelled `rebalance` gets no money** (note `unknown_rule`); a missing one is `trades`, the existing reader's default, and `trades` is treated like `contributions` (the split never sells).
11. **Quality only blocks `cheap` when it is known to be falling** (FCF margin and operating margin change both `ok` and both below 0). When either is not known, `cheap` stands and the line says `quality_unknown`.
12. **Lifecycle numbers:** a proposal is `spent` when the cash is at least $1 below the proposal's cash (placed in full or in part); after that, a new proposal needs the cash to rise `min_new` above the lowest balance seen since. The first proposal ever needs only `cash ≥ min_new`. A rise of `min_new` over an open proposal's cash replaces it with one covering all the cash.
13. **A hold on a proposal** is a finding of check `split` with `symbol` = the account key and `field` = `proposal:<id>`; the views blank its dollars and show `status = 'held'` while it is open.

## Global Constraints

- PUBLIC repo: no ticker tied to the owner, no value, account name, label, home path or key in code, tests, docs or commit messages. Fixture accounts are invented (`"Sample Brokerage"`, `"Sample Roth"`), fixture symbols widely held (SPY, VOO, AAPL, KO, JNJ, VFIAX, GLD). The fixture targets file is invented, in the documented shape. `tests/test_privacy.py` stays green; `config.example.toml` must equal `CONFIG_EXAMPLE`.
- Only `store.py`, `migrate.py`, `readonly.py` execute SQL. New reads are the `Store` methods each task names; the checker's are `Store.check_*`.
- Schema changes: one new migration `0022_allocation.sql`. Never edit an applied file.
- No network in tests. The split reads only the warehouse and two owner files by configured path (the targets file, the fund look-through folder), each ignored when missing.
- `check/` imports nothing from `derive/`, `statements.py` or the collectors; it reads the targets file with its own code. `symbols.canonical` and `models` may be shared.
- Spec values, verbatim: `min_new` default $25; per-line minimum (`min_line`) default $10; tilt `cheap` 1.5, `usual` 1.0, `premium` 0.5; the ±25% default band when an entry gives none; amounts whole dollars; lines under the minimum folded into the largest line; the remainder "waits for next week"; primary metrics `pe`, `p_fcf`, `ev_ebit`, `ps` for a company, `pe`, `dividend_yield` for a fund, each counted only when `ok`.
- Never: an order, a sale, money across accounts, more than the waiting cash, a `hold` name, the word "buy" in any reason or note.
- Every line's `sources` is a JSON list of `{kind, label, date, ref}` (plan 3's `derive/sources.py` builders): the cash row (`derived`, `cash_balances`), the targets entry (`feed`, the file's **name only**, its `last_revised`, `<account>/<symbol>`), each score row that decided the band (`derived`, `valuation_scores <metric>`), and the tilt (`config`, `[allocation] tilt.<band>`).
- Idempotent: a second `fdc sync` with nothing new leaves every row of the new tables identical.
- Tests run from the worktree: `PYTHONPATH=src ../../.venv/Scripts/python.exe -m pytest` (pyproject already adds `-q`).
- Each task updates the docs it changes: AGENTS.md (layout, tables list), README, CHANGELOG `## Unreleased`.

## Review Focus

1. A name whose own entry says `hold` but which a fund-heavy proposal could still reach through look-through must get no line of its own; and a cap name held mostly via funds must cut the **fund** lines, not only its own. Test in Task 3.
2. Rounding and folding must never make the lines sum above the cash, nor push the receiving line over its band top, a sleeve max or a cap. Test in Task 3.
3. A sync whose check failed, a re-run with the same cash, and a run after the owner placed part of the money must each leave the right lifecycle (no new proposal / nothing changed / `spent`, then the next deposit's proposal covers the leftover too). Tests in Task 5.
4. A targets file that is missing, unreadable, an array, or has an entry with no target, an inverted band or a misspelled rule must never produce money for that entry, and must never crash the sync. Tests in Tasks 1 and 5.
5. A fund with a year of snapshots but few of them (two snapshots a year apart), or 364 days of them, gets no band and so leans `usual`. Test in Task 2.

---

## File Structure

- `src/financial_data_collector/derive/targets.py` — new: `read_targets`, `read_look_through`, `TARGET_BAND`, `RULES` (Task 1).
- `src/financial_data_collector/derive/split.py` — new: `holding_band` (Task 2), `Household`, `plan_split` (Task 3), `run` (Task 5).
- `src/financial_data_collector/check/split.py` — new: check `split` (Task 6).
- `src/financial_data_collector/migrations/0022_allocation.sql` — new (Task 4).
- `config.py`, `config.example.toml` — `[allocation]` (Task 1). `models.py` — `TargetEntry`, `Sleeve`, `Cap`, `Targets`, `SplitLine`, `AllocationProposal`, `PROPOSAL_STATUSES`; `ENGINE_CHECK_NAMES` gains `split` (Tasks 1, 4, 6).
- `store.py` — the methods each task names; `COUNT_TABLES` gains the two raw tables.
- `derive/scores.py` — `MIN_FUND_SNAPSHOTS` (Task 2). `derive/engine.py` — calls `split.run` (Task 5). `sync.py` — the step's message (Task 5). `check/__init__.py` — the `proposal:<id>` field and the new check (Task 6). `check/engine.py` — fund snapshot sanity (Task 6).
- Tests: `tests/test_targets.py`, `tests/test_split.py`, `tests/test_split_run.py`, `tests/test_check_split.py` (new); `tests/test_fund_scores.py`, `tests/test_scores.py`, `tests/test_config.py`, `tests/test_sync.py`, `tests/test_check_engine.py`, `tests/test_check_frame.py`. Fixtures: `tests/fixtures/investing/config/sizing-targets.json`, `tests/fixtures/investing/fund_holdings/VOO.csv`, `VFIAX.csv` (invented weights).

---

### Task 1: `[allocation]` settings, the targets file, the look-through

**Files:**
- Create: `src/financial_data_collector/derive/targets.py`, `tests/test_targets.py`, the three fixtures above
- Modify: `config.py`, `config.example.toml`, `models.py`
- Test: `tests/test_targets.py`, `tests/test_config.py`

**Interfaces:**
- Produces: `config.AllocationSettings` (frozen): `targets: Path | None`, `fund_holdings: Path | None`, `accounts: dict[str, str]` (targets-file name → fdc label; default empty), `min_new: float = 25.0`, `min_line: int = 10`, `tilt: dict[str, float]` = `{"cheap": 1.5, "usual": 1.0, "premium": 0.5}`, `household_days: int = 7`; `Config.allocation: AllocationSettings`. Paths resolve against the config's folder like `[sources.investing]`; defaults `../investing/config/sizing-targets.json` and `../investing/fund_holdings`.
- Produces in `models.py` (all frozen): `TargetEntry(account: str, symbol: str, kind: str, target: float, low: float, high: float, rule: str, raw_rule: str | None)` — `kind` in `("pct_of_account", "pct_of_brokerage", "direct_passive_ratio")`, `rule` in `("trades", "contributions", "hold", "unknown")`; `Sleeve(account: str, label: str, members: tuple[str, ...], low: float | None, high: float | None, rule: str)`; `Cap(symbol: str, high: float)`; `Targets(entries: tuple[TargetEntry, ...], sleeves: tuple[Sleeve, ...], caps: tuple[Cap, ...], last_revised: str | None, file_name: str, problems: tuple[str, ...])`.
- Produces in `derive/targets.py`: `TARGET_BAND = 0.25`; `RULES = ("trades", "contributions", "hold")`; `read_targets(path: Path | None) -> Targets | None`; `read_look_through(fund_dir: Path | None) -> dict[str, dict[str, float]]` (fund symbol → {held symbol: weight as a fraction}).

Rules (`read_targets`, the way `investing/kestrel_feed/targets.py` reads the file):
- `None` path or missing file → `None`. Unreadable JSON or a top level that is not an object → `ValueError` with a short message (no path in it).
- Every top-level key that is a list and not `_`-prefixed, `sleeves`, `caps` or `planned` is an account; `planned` is ignored. Each dict entry with a `ticker` and a finite `target` becomes a `TargetEntry`; others are skipped with a `problems` line naming the account and ticker.
- `symbol = symbols.canonical(ticker)` (`BRKB` → `BRK.B`). `kind` defaults to `pct_of_account`; another value → skipped, problem.
- Band: `low` = entry `min` if finite else `round(target × 0.75, 6)`; `high` = entry `max` if finite else `round(target × 1.25, 6)`; `low > high` → skipped, problem ("inverted band").
- `rule`: missing → `"trades"`; one of `RULES` (exact, case-sensitive) → itself; anything else → `"unknown"` with `raw_rule` kept and a problem line.
- Sleeves: `account`, `label`, canonical `members`, finite `min`/`max` or None, rule as above. Caps: canonical `ticker`, finite `max` (no `max` → skipped, problem).
- `file_name` = `path.name` only; `last_revised` as given (string) or None.
- `read_look_through`: each `<FUND>.csv` (columns `ticker,name,weight_pct,…`; `utf-8-sig`) → `{canonical(ticker): weight_pct / 100}`, rows with a non-finite weight dropped, an empty file left out; files starting `_` and non-CSV files ignored; missing folder → `{}`. The fund key is `canonical(stem)`.
- `[allocation]` validation in the style of `_engine_settings`: unknown keys rejected by name; `min_new` > 0; `min_line` whole ≥ 1; `household_days` whole ≥ 1; each `tilt` key one of `cheap|usual|premium`, value finite > 0; `accounts` a table of strings.
- `CONFIG_EXAMPLE` gains, after `[engine]`:

```toml
[allocation]                       # the weekly split (fdc sync's engine step): proposals only, never an order
targets = "../investing/config/sizing-targets.json"   # owner-only targets file; no file, no proposals
fund_holdings = "../investing/fund_holdings"          # owner-only <FUND>.csv look-through weights (top holdings)
min_new = 25                       # a proposal is made when an account's waiting cash rises this many dollars
min_line = 10                      # a line under this many dollars is folded into the largest line
household_days = 7                 # caps count accounts whose newest snapshot is this close to the newest
tilt = { cheap = 1.5, usual = 1.0, premium = 0.5 }    # a holding's share of the money, by its band

[allocation.accounts]              # targets-file account name = fdc account label (fdc accounts lists them)
# brokerage = "Sample Brokerage"
# roth = "Sample Roth"
```

- [ ] **Step 1: Write the failing tests** — `tests/test_targets.py`: `test_reads_the_documented_shape` (fixture: two accounts, a `pct_of_brokerage`, a `direct_passive_ratio`, one entry with its own `min`/`max`, one with none → band 0.75×/1.25×, a sleeve, a cap, a `planned` list ignored); `test_missing_file_is_none`; `test_array_or_bad_json_raises_value_error`; `test_entry_without_target_or_with_inverted_band_is_skipped_with_a_problem`; `test_rule_missing_is_trades_and_misspelled_is_unknown` (`"Hold"` → `unknown`, `raw_rule == "Hold"`); `test_tickers_are_canonical` (`BRKB` → `BRK.B`); `test_file_name_only` (`targets.file_name == "sizing-targets.json"`); `test_look_through_reads_weights_as_fractions` (`VOO.csv` AAPL 7.0 → 0.07; a `_archive` folder and a `.json` ignored; a missing folder → `{}`). `tests/test_config.py`: defaults, a full `[allocation]`, each rejected value, `CONFIG_EXAMPLE` parses.
- [ ] **Step 2: Run them, expect failures** — `PYTHONPATH=src ../../.venv/Scripts/python.exe -m pytest tests/test_targets.py tests/test_config.py` → FAIL (`ModuleNotFoundError` / missing attribute).
- [ ] **Step 3: Implement** `models` dataclasses, `AllocationSettings`, `_allocation_settings`, `derive/targets.py`, `CONFIG_EXAMPLE`, `config.example.toml`.
- [ ] **Step 4: Run them, expect PASS**, then the whole suite (`test_privacy` included).
- [ ] **Step 5: Commit** — `feat(split): [allocation] settings and the targets and look-through readers`.

---

### Task 2: A holding's band, and the fund history floor

**Files:**
- Create: `src/financial_data_collector/derive/split.py` (the `holding_band` part), `tests/test_split.py`
- Modify: `derive/scores.py`, `tests/test_fund_scores.py`, `tests/test_scores.py`

**Interfaces:**
- Produces in `derive/split.py`: `PRIMARY_COMPANY = ("pe", "p_fcf", "ev_ebit", "ps")`; `PRIMARY_FUND = ("pe", "dividend_yield")`; `holding_band(rows: list[dict], is_fund: bool) -> tuple[str, tuple[str, ...], tuple[dict, ...]]` — rows are the symbol's `valuation_scores` view rows (`metric`, `value`, `band`, `status`, `date`); returns (band, reason codes, the rows that decided it).
- Produces in `derive/scores.py`: `MIN_FUND_SNAPSHOTS = 200`.

Rules (`holding_band`):
- Available = primary metrics whose row is `ok` with a non-NULL band. Every other primary row adds a reason `held:<m>`, `unsupported:<m>` or `no_history:<m>` (status `not_enough_history`); a missing row adds `missing:<m>`.
- Fewer than 2 available → `usual`, `one_metric` (1) or `no_metrics` (0).
- More than half say `premium` → `premium`, `premium_by:<m,m>`.
- None says `premium` and more than half say `cheap`: a company whose `fcf_margin_change` and `op_margin_change` rows are both `ok` and both below 0 → `usual`, `quality_falling`; otherwise `cheap`, `cheap_by:<m,m>`, plus `quality_unknown` when either quality row is not `ok` (Calls 11). A fund has no quality check.
- Anything else → `usual`, `disagree`.
- Reason codes never contain the word "buy".

Fund floor (`scores.fund_scores`): `pe` and `dividend_yield` get a band only when their own-source snapshots span `min_history_days` **and** number at least `MIN_FUND_SNAPSHOTS`; short on count → `not_enough_history`, reason `"<N> snapshots of its own; a band needs 200"`. The existing days reason stays as it is.

- [ ] **Step 1: Write the failing tests** — `tests/test_split.py`: `test_two_cheap_of_three_is_cheap`; `test_one_premium_blocks_cheap` (3 cheap, 1 premium → `usual`, `disagree`); `test_most_premium_is_premium`; `test_single_metric_is_usual`; `test_held_metric_does_not_count_and_is_named` (pe `held`, p_fcf/ps cheap → `cheap`, reasons include `held:pe`); `test_quality_falling_blocks_cheap`; `test_quality_unknown_does_not_block_but_is_said`; `test_fund_without_a_year_is_usual` (pe/yield `not_enough_history` → `usual`, `no_metrics`, `no_history:pe`); `test_fund_two_cheap_is_cheap`; `test_no_reason_says_buy` (every code over all the cases above). `tests/test_fund_scores.py`: `test_two_snapshots_a_year_apart_get_no_band`; `test_364_days_no_band_365_days_band` (200+ snapshots both). `tests/test_scores.py` (plan 3's deferred boundaries): insider window 90 vs 91 days; a year-earlier TTM row at 349 / 350 / 380 / 381 days (`YEAR_EARLIER_DAYS`); estimate `then` at 29 / 30 / 45 / 46 days; a snapshot 7 vs 8 days old.
- [ ] **Step 2: Run, expect failures** (the boundary tests that already pass are kept as pins; note them in the report).
- [ ] **Step 3: Implement** `holding_band` and the floor.
- [ ] **Step 4: Run, expect PASS**; whole suite.
- [ ] **Step 5: Commit** — `feat(split): a holding's band from its primary metrics; a fund band needs 200 snapshots`.

---

### Task 3: The split itself

**Files:**
- Modify: `src/financial_data_collector/derive/split.py`, `tests/test_split.py`

**Interfaces:**
- Consumes: `Targets`, `TargetEntry`, `Sleeve`, `Cap` (Task 1); `holding_band` output (Task 2).
- Produces: `Household(total: float, direct: dict[str, float], indirect: dict[str, float], look_through: dict[str, dict[str, float]], funds: frozenset[str])` (frozen); `household_of(positions: list[dict], cash: dict[str, float], look_through, fund_symbols: frozenset[str]) -> Household` (positions rows: `account`, `symbol`, `market_value`; a fund with a look-through file adds `value × weight` to each name's `indirect`, its untracked rest to nothing; a fund without one counts as a direct holding of the fund, as the existing reader does); `models.SplitLine(symbol: str, dollars: int, target_gap: float, band: str, tilt: float, reasons: tuple[str, ...])`; `plan_split(account: str, cash: float, base: float, held: dict[str, float], household: Household, targets: Targets, bands: dict[str, tuple[str, tuple[str, ...]]], tilt: dict[str, float], min_line: int) -> SplitPlan` with `SplitPlan(lines: tuple[SplitLine, ...], waits: float, notes: tuple[dict, ...])` (`notes`: `{"symbol", "reason"}` for every entry that got nothing and why).

Rules:
- The account's entries: those filed under `account` that are not `pct_of_brokerage`, plus every `pct_of_brokerage` entry when `account == "brokerage"`. A symbol twice → the first, note `duplicate`. `hold` → note `hold`; `unknown` → note `unknown_rule`; `trades` gets reason `trades_as_contributions`.
- Gap and top: percent entries `gap = max(0, target/100 × base − held[s])`, `top = high/100 × base − held[s]`; ratio entries `gap = max(0, target × indirect[s] − direct[s])`, `top = high × indirect[s] − direct[s]`, `indirect[s] == 0` → note `no_look_through`. `gap == 0` → note `at_or_over_target`.
- Weight = `gap × tilt[band]`; band from `bands` (absent → `usual`, reason `no_scores`).
- Groups (`Σ coef × line ≤ room`): each sleeve of this account with a `high`: coef 1 on its eligible members, `room = high/100 × base − Σ held[members]`; each cap: `room = high/100 × household.total − (direct + indirect)[cap]`; coef 1 on the cap symbol, `look_through[fund][cap]` on a fund with a file, the largest weight of the cap name in any file on a fund without one (reason `look_through_assumed`), 0 on others. A group with no positive coef is dropped; a negative room is 0. A line a group cuts gets `sleeve_cut:<label>` / `cap_cut:<symbol>`.
- Filling (progressive, deterministic; symbols in sorted order wherever order matters):

```
remaining = cash; x[s] = 0 for every eligible s; active = {s : weight[s] > 0 and top[s] > 0}
while remaining > 0.005 and active:
    inc[s] = remaining × weight[s] / Σ_active weight          (s in active)
    k = 1
    for s in active: k = min(k, (top[s] − x[s]) / inc[s])
    for g in groups: used = Σ coef_g[s] × inc[s]; if used > 0: k = min(k, (room_g − Σ coef_g × x) / used)
    k = max(k, 0)
    x[s] += k × inc[s]; remaining −= k × Σ inc
    drop from active every s with top[s] − x[s] ≤ 0.005, and every s with coef > 0 in a group whose room left ≤ 0.005
    if k == 1: break
```

- Rounding: `dollars = floor(x)`; lines under `min_line` are removed and their dollars added to the largest remaining line (ties: alphabetical) only up to its own room left (its top and every group it is in, divided by its coef), reason `folded`; what does not fit waits. No line left → everything waits. `waits = round(cash − Σ dollars, 2)`; a line's `band_top` reason when its top stopped it.
- Zero (or negative) cash, or no eligible entry → no lines, `waits = max(cash, 0)`.

- [ ] **Step 1: Write the failing tests** (`tests/test_split.py`, synthetic numbers): `test_shares_follow_gaps` (two usual lines, gaps 300/100, cash 200 → 150/50); `test_tilt_cheap_and_premium` (equal gaps 500/500, cash 300, cheap vs premium → 225/75); `test_hold_name_gets_nothing_even_with_a_gap`; `test_unknown_rule_gets_nothing`; `test_line_stops_at_band_top_and_rest_goes_on` (one line's top 40 → 40 and the rest to the other); `test_money_beyond_every_top_waits`; `test_sleeve_max_cuts_its_members`; `test_cap_cuts_the_fund_line_through_look_through` (cap on AAPL with 0 room left: the VOO line, 7% AAPL, gets nothing more than the room allows, `cap_cut:AAPL`); `test_fund_without_a_file_is_assumed_at_the_largest_weight`; `test_ratio_entry_gap_and_no_look_through`; `test_pct_of_brokerage_only_in_brokerage`; `test_lines_under_min_are_folded_into_the_largest_within_its_room`; `test_fold_never_passes_a_top` (largest line at its top: the small line's dollars wait); `test_lines_never_sum_above_cash` (a parametrized sweep of cash 0, 9, 10, 25.75, 1000.99 over a three-line plan); `test_zero_cash`; `test_whole_dollars_only`; `test_same_inputs_same_plan`.
- [ ] **Step 2: Run, expect failures.**
- [ ] **Step 3: Implement** `Household`, `household_of`, `SplitLine`, `SplitPlan`, `plan_split`.
- [ ] **Step 4: Run, expect PASS**; whole suite.
- [ ] **Step 5: Commit** — `feat(split): split waiting cash by target gap and band, inside every top, sleeve and cap`.

---

### Task 4: The proposal tables

**Files:**
- Create: `src/financial_data_collector/migrations/0022_allocation.sql`
- Modify: `models.py`, `store.py`, `tests/test_split_run.py` (new: the store part)

**Interfaces:**
- Produces in `models.py`: `PROPOSAL_STATUSES = ("open", "replaced", "spent")`; `AllocationProposal(account_key: str, cash_date: str, cash: float, placed: int, waits: float, lines: tuple[SplitLine, ...], line_sources: tuple[tuple[dict, ...], ...], notes: tuple[dict, ...], sources: tuple[dict, ...])` (frozen; `line_sources[i]` belongs to `lines[i]`).
- Produces in `store.py`: `split_positions() -> list[dict]` (`positions_latest`: `account`, `as_of_date`, `symbol`, `market_value`, `asset_type`); `split_cash() -> dict[str, tuple[str, float]]` (label → newest `cash_balances` USD (as_of_date, amount)); `split_cash_low_since(label: str, after: str) -> float | None` (lowest amount dated after `after`); `split_scores(symbols: list[str]) -> dict[str, list[dict]]` (from the `valuation_scores` view); `open_proposal(account_key: str) -> dict | None`; `last_spent_proposal(account_key: str) -> dict | None`; `write_proposal(p: AllocationProposal, *, created_at: str, checked_at: str | None, replaces: int | None) -> int` (one transaction: the old one `replaced` with `closed_at`, the new one and its lines); `close_proposal(proposal_id: int, *, closed_at: str, closed_cash: float) -> None` (status `spent`); `check_split_rows() -> list[dict]` (every open raw proposal with `id`, `account_key`, `cash_date`, `cash`, and its lines `symbol`, `dollars`, `sources`).

Migration `0022_allocation.sql` (header comment in plan 3's style: what each table is, that it is a record kept like `check_findings`, and how the views apply holds):
- `allocation_proposals_raw(id INTEGER PRIMARY KEY, account_key TEXT NOT NULL, created_at TEXT NOT NULL, cash_date TEXT NOT NULL, cash REAL NOT NULL, placed INTEGER NOT NULL, waits REAL NOT NULL, status TEXT NOT NULL CHECK (status IN ('open','replaced','spent')), closed_at TEXT, closed_cash REAL, notes TEXT NOT NULL DEFAULT '[]', sources TEXT NOT NULL DEFAULT '[]', checked_at TEXT)`; `CREATE UNIQUE INDEX … ON allocation_proposals_raw(account_key) WHERE status = 'open'`.
- `allocation_lines_raw(proposal_id INTEGER NOT NULL REFERENCES allocation_proposals_raw(id), symbol TEXT NOT NULL, dollars INTEGER NOT NULL CHECK (dollars > 0), target_gap REAL NOT NULL, band TEXT NOT NULL CHECK (band IN ('cheap','usual','premium')), tilt REAL NOT NULL, reasons TEXT NOT NULL DEFAULT '', sources TEXT NOT NULL DEFAULT '[]', PRIMARY KEY (proposal_id, symbol))`.
- View `allocation_proposals`: every raw column, with `status` = `'held'` when the raw status is `open` and an open hold exists (`check_name = 'split'`, `severity = 'hold'`, `cleared_at IS NULL`, `field = 'proposal:' || id`), plus `hold_ids` (comma-joined finding ids, NULL when none).
- View `allocation_lines`: every raw column, `dollars` NULL while its proposal is held, plus `held` (0/1).

- [ ] **Step 1: Write the failing tests** (`tests/test_split_run.py`): `test_write_and_read_an_open_proposal` (lines, sources and notes round-trip as JSON); `test_one_open_proposal_per_account` (a second open insert without `replaces` raises `sqlite3.IntegrityError`); `test_replace_closes_the_old_one`; `test_close_marks_spent`; `test_view_holds_a_proposal` (insert a `split` hold finding on `proposal:<id>`: view status `held`, line dollars NULL, `held = 1`; clear it: back to `open`); `test_split_cash_low_since`; `test_counts_include_the_new_tables` (`fdc status`'s counts).
- [ ] **Step 2: Run, expect failures.**
- [ ] **Step 3: Implement** the migration, models and store methods; `COUNT_TABLES`.
- [ ] **Step 4: Run, expect PASS**; whole suite (the migration test that lists every migration included).
- [ ] **Step 5: Commit** — `feat(split): allocation_proposals and allocation_lines, held through their views`.

---

### Task 5: The lifecycle, in the engine step

**Files:**
- Modify: `derive/split.py` (`run`), `derive/engine.py`, `sync.py`, `tests/test_split_run.py`, `tests/test_sync.py`

**Interfaces:**
- Consumes: Tasks 1-4; `derive/sources.py` builders (plan 3).
- Produces: `SplitReport(opened: int, replaced: int, spent: int, notes: tuple[str, ...], skipped: str | None)`; `split.run(store: Store, cfg: Config, *, checked_at: str | None, now: datetime) -> SplitReport`; `EngineReport` gains `split: SplitReport | None`; the step's message gains `"; split: N new, N replaced, N spent"` or `"; split: <why skipped>"`.

Rules (`run`, called by `engine.run` after the scores are written, only when the gate passed):
- No targets file → skipped `"no targets file"`; `ValueError` from it → skipped `"targets file unreadable: <message>"`; no `[allocation.accounts]` → skipped `"no accounts in [allocation.accounts]"`. A skip never touches the tables and never fails the step.
- Household: `split_positions()` rows of accounts whose newest `as_of_date` is within `household_days` of the newest any account has; their `split_cash()` added to the total; each left-out account named in `notes` by its count only ("1 account older than 7 days left out"), never its label.
- For each `account_key, label` (sorted): no cash row → note, next. `cash = amount`, `cash_date`.
  - Open proposal `P`: `cash ≥ P.cash + min_new` → a new proposal for all of `cash`, written with `replaces = P.id`; `cash ≤ P.cash − 1` → `close_proposal(P, closed_at = now, closed_cash = cash)`; else nothing.
  - No open proposal: `baseline` = min(`last_spent.closed_cash`, `split_cash_low_since(label, last_spent.closed_at's date)`) when a spent one exists, else 0. `cash ≥ baseline + min_new` → a new proposal.
- A new proposal: `base` = the account's positions value + cash; `held` = its positions by symbol; `bands` from `holding_band(split_scores(...))` per eligible symbol (a fund = `asset_type` in `FUND_TYPES`); `plan_split(...)`. Sources: proposal-level the cash row (`derived`, label `cash_balances`, date `cash_date`, ref the account key) and the targets file (`feed`, `file_name`, `last_revised`, ref `account_key`); each line its targets entry, its deciding score rows and its tilt. `notes` = the plan's notes plus the targets file's `problems`.
- Idempotent: the same cash and no change → nothing written (no `created_at` churn).

- [ ] **Step 1: Write the failing tests** (`tests/test_split_run.py`, a fixture warehouse with two invented accounts, scores rows inserted directly): `test_first_deposit_opens_a_proposal`; `test_rerun_same_cash_changes_nothing` (every row identical, including `created_at`); `test_rise_of_min_new_replaces`; `test_rise_under_min_new_keeps_the_open_one`; `test_partial_placement_is_spent_and_leftover_joins_the_next` (cash 500 → proposal; 300 → spent; 300 → nothing; 340 → new proposal for 340); `test_placed_down_to_zero_then_a_small_deposit` (0 then 30 → new proposal); `test_no_targets_file_skips_quietly`; `test_bad_targets_file_skips_with_reason`; `test_an_old_account_is_left_out_of_the_household`; `test_sources_name_the_file_not_its_path` (no `/` or `\\` in any targets `label`); `test_hold_name_never_in_lines` (end to end). `tests/test_sync.py`: `test_engine_step_skipped_leaves_proposals_untouched` (check failed → no proposal), `test_engine_step_message_names_the_split`.
- [ ] **Step 2: Run, expect failures.**
- [ ] **Step 3: Implement** `run`, the engine call and the message.
- [ ] **Step 4: Run, expect PASS**; whole suite.
- [ ] **Step 5: Commit** — `feat(split): open, replace and close proposals as the waiting cash moves`.

---

### Task 6: The checker's `split` check, and fund snapshot sanity

**Files:**
- Create: `src/financial_data_collector/check/split.py`, `tests/test_check_split.py`
- Modify: `check/__init__.py`, `check/engine.py`, `models.py`, `store.py` (`check_split_rows`, and the asset type on `check_score_rows`), `tests/test_check_frame.py`, `tests/test_check_engine.py`

**Interfaces:**
- Produces: check `split` registered in `CHECKS`; `models.ENGINE_CHECK_NAMES = ("scores", "split")` (the engine step re-runs both); `run_check` accepts a finding field matching `proposal:\d+`.

Rules (`check/split.py`, its own reading of the targets file at `cfg.allocation.targets`: a JSON object, account lists, `ticker` through `symbols.canonical`, `rebalance == "hold"` exactly, `pct_of_brokerage` entries counted under `brokerage`). For each open proposal from `check_split_rows()` (field `proposal:<id>`, symbol = account key, `date_from` = `cash_date`):
- lines sum above the cash (more than half a cent) → `hold`, message with both numbers;
- a line whose dollars are not a whole number ≥ 1 → `hold`;
- a line to a symbol the file marks `hold` in that account → `hold`;
- a line to a symbol with no entry in that account (or, for `brokerage`, no `pct_of_brokerage` entry anywhere) → `hold`;
- a line with no sources, or sources that are not a JSON list of objects with `kind` and `label` → `hold`;
- the targets file missing or unreadable while a proposal is open → `warn` ("cannot check hold names").
- No finding for a closed proposal; nothing raises on a bad row (a bad row is a finding).

Fund snapshot sanity (in the `scores` check, `check/engine.py`): an `ok` fund row (`asset_type` in `FUND_TYPES`) with `pe` ≤ 0 or above 200, `dividend_yield` above 0.25, or `expense_ratio` above 0.03 → `hold` on `score:<metric>` with the value in the evidence.

- [ ] **Step 1: Write the failing tests** — `tests/test_check_split.py`: one test per rule above (each builds the bad proposal directly through `Store`, so the derive code is not used); `test_clean_proposal_has_no_findings`; `test_a_held_proposal_shows_held_in_the_view` (run the check, read `allocation_proposals`); `test_check_imports_nothing_from_derive` (the existing import-guard test extended to `check/split.py`). `tests/test_check_frame.py`: `proposal:12` accepted, `proposal:x` rejected. `tests/test_check_engine.py`: the three fund sanity cases and an in-range fund row that passes.
- [ ] **Step 2: Run, expect failures.**
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run, expect PASS**; whole suite.
- [ ] **Step 5: Commit** — `feat(check): split holds a proposal over the cash, to a hold name or without sources; fund snapshot sanity`.

---

### Task 7: Docs, and a real run on a copy

**Files:**
- Modify: `AGENTS.md`, `README.md`, `CHANGELOG.md`, `docs/QUERIES.md`

- [ ] **Step 1: Docs.** AGENTS.md layout (`derive/targets.py`, `derive/split.py`, `check/split.py`, migration 0022) and tables list (`allocation_proposals` / `allocation_lines`, the views, that they are a record, the lifecycle, the hold field `proposal:<id>`); README (what a sync now proposes, the `[allocation]` settings, "never an order"); CHANGELOG `## Unreleased`; QUERIES (the open proposal per account with its lines; why a line got its band).
- [ ] **Step 2: Whole suite**, expect PASS.
- [ ] **Step 3: Commit** — `docs: the weekly split`.
- [ ] **Step 4: Real run on a copy (no commit; results go to the ledger only).** Copy the owner's `data/warehouse.db` with SQLite's backup API into the scratch folder, with a scratch `config.toml` (`[allocation]` pointing at the real targets file and look-through folder read-only, `[allocation.accounts]` filled from `fdc accounts` labels in the scratch config only, export off). Run `fdc sync --only derive,check,engine` against the copy, twice. Record: proposals opened per account key, cash, lines (symbol, dollars, band, reasons), what waits and why, the notes; that the lines sum to at most the cash; that no `hold` name has a line; that every line has sources; that the second run changed nothing; the `split` check's findings. Hand-check one account's gaps against the targets file.

---

## Not in this plan

- The window's pages and the assistant's opinion (the window's own spec).
- Recording whether the owner followed a proposal (the window's own data, per the spec).
- A sleeve's `min` pulling money in, money moved between accounts, any sale.
- Registering a scheduled task, a push, or a live sync on the owner's database: each needs his OK.
