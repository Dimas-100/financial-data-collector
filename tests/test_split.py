"""The weekly split: a holding's single band from its primary valuation scores (plan 4, Task 2), and the split of
the waiting cash itself (Task 3)."""
import math

import pytest

from financial_data_collector.derive.split import (PRIMARY_COMPANY, PRIMARY_FUND, Household, SplitPlan,
                                                   holding_band, household_of, plan_split)
from financial_data_collector.models import Cap, Sleeve, SplitLine, TargetEntry, Targets

DAY = "2026-10-29"


def _row(metric: str, band: str | None = "usual", status: str = "ok", value: float | None = 1.0) -> dict:
    return {"symbol": "AAA", "date": DAY, "metric": metric, "value": value if status != "held" else None,
            "band": band if status == "ok" else None, "status": status, "sources": "[]"}


def _company(pe="usual", p_fcf="usual", ev_ebit="usual", ps="usual", fcf_margin=0.01, op_margin=0.01) -> list[dict]:
    rows = [_row("pe", pe) if pe else None, _row("p_fcf", p_fcf) if p_fcf else None,
            _row("ev_ebit", ev_ebit) if ev_ebit else None, _row("ps", ps) if ps else None]
    if fcf_margin is not None:
        rows.append(_row("fcf_margin_change", None, value=fcf_margin))
    if op_margin is not None:
        rows.append(_row("op_margin_change", None, value=op_margin))
    return [r for r in rows if r]


def test_primary_metrics():
    assert PRIMARY_COMPANY == ("pe", "p_fcf", "ev_ebit", "ps")
    assert PRIMARY_FUND == ("pe", "dividend_yield")


def test_two_cheap_of_three_is_cheap():
    band, reasons, rows = holding_band(_company(pe="cheap", p_fcf="cheap", ev_ebit="usual", ps=None), False)
    assert band == "cheap"
    assert reasons == ("missing:ps", "cheap_by:pe+p_fcf")
    assert [r["metric"] for r in rows] == ["pe", "p_fcf", "ev_ebit"]


def test_one_premium_blocks_cheap():
    band, reasons, _ = holding_band(_company(pe="cheap", p_fcf="cheap", ev_ebit="cheap", ps="premium"), False)
    assert (band, reasons) == ("usual", ("disagree",))


def test_most_premium_is_premium():
    band, reasons, rows = holding_band(_company(pe="premium", p_fcf="premium", ev_ebit="cheap", ps="usual"), False)
    assert (band, reasons) == ("usual", ("disagree",))                       # 2 of 4 is not more than half
    band, reasons, rows = holding_band(_company(pe="premium", p_fcf="premium", ev_ebit="premium", ps="cheap"), False)
    assert (band, reasons) == ("premium", ("premium_by:pe+p_fcf+ev_ebit",))
    assert [r["metric"] for r in rows] == ["pe", "p_fcf", "ev_ebit", "ps"]


def test_premium_beats_cheap_when_it_is_the_majority():
    band, reasons, _ = holding_band(_company(pe="premium", p_fcf="premium", ev_ebit="cheap", ps=None), False)
    assert (band, reasons) == ("premium", ("missing:ps", "premium_by:pe+p_fcf"))


def test_single_metric_is_usual():
    band, reasons, rows = holding_band(_company(pe="cheap", p_fcf=None, ev_ebit=None, ps=None), False)
    assert (band, reasons) == ("usual", ("missing:p_fcf", "missing:ev_ebit", "missing:ps", "one_metric"))
    assert [r["metric"] for r in rows] == ["pe"]


def test_no_rows_at_all_is_usual_with_no_metrics():
    band, reasons, rows = holding_band([], False)
    assert band == "usual" and rows == ()
    assert reasons == ("missing:pe", "missing:p_fcf", "missing:ev_ebit", "missing:ps", "no_metrics")


def test_held_metric_does_not_count_and_is_named():
    rows = _company(pe=None, p_fcf="cheap", ev_ebit="usual", ps="cheap") + [_row("pe", status="held")]
    band, reasons, decided = holding_band(rows, False)
    assert band == "cheap"
    assert reasons == ("held:pe", "cheap_by:p_fcf+ps")
    assert "pe" not in [r["metric"] for r in decided]


def test_unsupported_and_no_history_are_named_in_primary_order():
    rows = [_row("pe", status="unsupported"), _row("p_fcf", status="not_enough_history"),
            _row("ev_ebit", "cheap"), _row("ps", "cheap")]
    band, reasons, _ = holding_band(rows, False)
    assert band == "cheap"
    assert reasons[:2] == ("unsupported:pe", "no_history:p_fcf")


def test_an_ok_row_without_a_band_does_not_count():
    rows = _company(pe="cheap", p_fcf="cheap", ev_ebit="cheap", ps="cheap")
    rows[0]["band"] = None
    band, reasons, decided = holding_band(rows, False)
    assert reasons[0] == "unsupported:pe" and len(decided) == 3
    assert band == "cheap"


def test_quality_falling_blocks_cheap():
    band, reasons, rows = holding_band(_company(pe="cheap", p_fcf="cheap", ev_ebit="cheap", ps="cheap",
                                                fcf_margin=-0.02, op_margin=-0.01), False)
    assert (band, reasons) == ("usual", ("quality_falling",))
    assert {"fcf_margin_change", "op_margin_change"} <= {r["metric"] for r in rows}


def test_one_falling_margin_does_not_block_cheap():
    band, reasons, rows = holding_band(_company(pe="cheap", p_fcf="cheap", ev_ebit="cheap", ps="cheap",
                                                fcf_margin=-0.02, op_margin=0.01), False)
    assert (band, reasons) == ("cheap", ("cheap_by:pe+p_fcf+ev_ebit+ps",))
    assert [r["metric"] for r in rows] == ["pe", "p_fcf", "ev_ebit", "ps"]


def test_quality_unknown_does_not_block_but_is_said():
    rows = _company(pe="cheap", p_fcf="cheap", ev_ebit="usual", ps="cheap", fcf_margin=None, op_margin=-0.01)
    rows.append(_row("fcf_margin_change", status="not_enough_history"))
    band, reasons, decided = holding_band(rows, False)
    assert band == "cheap"
    assert reasons == ("cheap_by:pe+p_fcf+ps", "quality_unknown")
    assert {"fcf_margin_change", "op_margin_change"} <= {r["metric"] for r in decided}
    band, reasons, _ = holding_band(_company(pe="cheap", p_fcf="cheap", ev_ebit="cheap", ps="cheap",
                                             fcf_margin=None, op_margin=None), False)
    assert (band, reasons) == ("cheap", ("cheap_by:pe+p_fcf+ev_ebit+ps", "quality_unknown"))


def test_quality_is_not_read_for_a_company_that_is_not_cheap():
    band, reasons, rows = holding_band(_company(fcf_margin=None, op_margin=None), False)
    assert (band, reasons) == ("usual", ("disagree",))
    assert len(rows) == 4


def test_fund_without_a_year_is_usual():
    rows = [_row("pe", status="not_enough_history"), _row("dividend_yield", status="not_enough_history")]
    band, reasons, decided = holding_band(rows, True)
    assert (band, reasons, decided) == ("usual", ("no_history:pe", "no_history:dividend_yield", "no_metrics"), ())


def test_fund_two_cheap_is_cheap():
    band, reasons, rows = holding_band([_row("pe", "cheap"), _row("dividend_yield", "cheap")], True)
    assert (band, reasons) == ("cheap", ("cheap_by:pe+dividend_yield",))
    assert [r["metric"] for r in rows] == ["pe", "dividend_yield"]


def test_fund_has_no_quality_check():
    rows = [_row("pe", "cheap"), _row("dividend_yield", "cheap"), _row("fcf_margin_change", None, value=-1.0),
            _row("op_margin_change", None, value=-1.0)]
    band, reasons, decided = holding_band(rows, True)
    assert (band, reasons) == ("cheap", ("cheap_by:pe+dividend_yield",))
    assert len(decided) == 2


def test_fund_split_decision_is_usual():
    band, reasons, _ = holding_band([_row("pe", "cheap"), _row("dividend_yield", "premium")], True)
    assert (band, reasons) == ("usual", ("disagree",))


def test_a_fund_ignores_company_metrics():
    rows = [_row("pe", "premium"), _row("dividend_yield", "premium"), _row("ps", "cheap"), _row("p_fcf", "cheap")]
    band, reasons, _ = holding_band(rows, True)
    assert (band, reasons) == ("premium", ("premium_by:pe+dividend_yield",))


def test_reasons_come_back_as_a_tuple_and_the_rows_are_the_inputs():
    rows = _company(pe="cheap", p_fcf="cheap", ev_ebit="cheap", ps="cheap")
    band, reasons, decided = holding_band(rows, False)
    assert isinstance(reasons, tuple) and isinstance(decided, tuple)
    assert decided[0] is rows[0]


def test_no_reason_says_buy():
    cases = [
        (_company(pe="cheap", p_fcf="cheap", ev_ebit="usual", ps=None), False),
        (_company(pe="cheap", p_fcf="cheap", ev_ebit="cheap", ps="premium"), False),
        (_company(pe="premium", p_fcf="premium", ev_ebit="premium", ps="cheap"), False),
        (_company(pe="cheap", p_fcf=None, ev_ebit=None, ps=None), False),
        (_company(pe=None, p_fcf="cheap", ev_ebit="usual", ps="cheap") + [_row("pe", status="held")], False),
        (_company(pe="cheap", p_fcf="cheap", ev_ebit="cheap", ps="cheap", fcf_margin=-0.1, op_margin=-0.1), False),
        (_company(pe="cheap", p_fcf="cheap", ev_ebit="cheap", ps="cheap", fcf_margin=None, op_margin=None), False),
        ([_row("pe", status="not_enough_history"), _row("dividend_yield", status="not_enough_history")], True),
        ([_row("pe", "cheap"), _row("dividend_yield", "cheap")], True),
        ([], False),
        ([], True),
    ]
    codes = [c for rows, is_fund in cases for c in holding_band(rows, is_fund)[1]]
    assert codes
    assert not any("buy" in c.lower() for c in codes)


# ---------------------------------------------------------------------------------------------------------------------
# Part two: the split itself (plan 4, Task 3). Every number is synthetic; the arithmetic is worked in each test.
# Percent targets, bands, sleeve and cap bounds are in percent (15.0 = 15%); ratio targets are plain ratios.
# ---------------------------------------------------------------------------------------------------------------------

TILT = {"cheap": 1.5, "usual": 1.0, "premium": 0.5}


def _e(symbol: str, target: float, high: float | None = None, *, kind: str = "pct_of_account",
       rule: str = "contributions", account: str = "brokerage") -> TargetEntry:
    high = round(target * 1.25, 6) if high is None else high
    return TargetEntry(account, symbol, kind, target, round(target * 0.75, 6), high, rule,
                       "Hold?" if rule == "unknown" else None)


def _hh(total: float = 10_000.0, direct=None, indirect=None, look_through=None, funds=()) -> Household:
    return Household(total, dict(direct or {}), dict(indirect or {}), dict(look_through or {}), frozenset(funds))


def _usual(*symbols: str) -> dict:
    return {s: ("usual", ("disagree",)) for s in symbols}


def _plan(entries, cash, *, base=1000.0, held=None, hh=None, bands=None, sleeves=(), caps=(), min_line=10,
          account="brokerage") -> SplitPlan:
    targets = Targets(tuple(entries), tuple(sleeves), tuple(caps), "2026-01-15", "sizing-targets.json", ())
    if bands is None:
        bands = _usual(*(e.symbol for e in entries))
    return plan_split(account, cash, base, dict(held or {}), hh or _hh(), targets, bands, TILT, min_line)


def _d(plan: SplitPlan) -> dict[str, int]:
    return {line.symbol: line.dollars for line in plan.lines}


def _line(plan: SplitPlan, symbol: str) -> SplitLine:
    return next(line for line in plan.lines if line.symbol == symbol)


def _notes(plan: SplitPlan) -> list[tuple[str, str]]:
    return [(n["symbol"], n["reason"]) for n in plan.notes]


def test_household_of_counts_funds_through_their_files():
    # SPY (fund, file: AAPL 7%, KO 1%) 1000 -> AAPL indirect 70, KO indirect 10, the other 92% counts nowhere.
    # VFIAX (fund, no file) 2000 -> its own direct. AAPL 500 + 200 in two accounts -> direct 700. KO 300 direct.
    # A row with no market value adds nothing. Total = 1000 + 500 + 200 + 2000 + 300 + cash 100 + 50 = 4150.
    positions = [
        {"account": "Sample Brokerage", "symbol": "SPY", "market_value": 1000.0},
        {"account": "Sample Brokerage", "symbol": "AAPL", "market_value": 500.0},
        {"account": "Sample Brokerage", "symbol": "VFIAX", "market_value": 2000.0},
        {"account": "Sample Roth", "symbol": "AAPL", "market_value": 200.0},
        {"account": "Sample Roth", "symbol": "KO", "market_value": 300.0},
        {"account": "Sample Roth", "symbol": "JNJ", "market_value": None},
    ]
    hh = household_of(positions, {"Sample Brokerage": 100.0, "Sample Roth": 50.0},
                      {"SPY": {"AAPL": 0.07, "KO": 0.01}}, frozenset({"SPY", "VFIAX"}))
    assert hh.total == pytest.approx(4150.0)
    assert hh.direct == pytest.approx({"AAPL": 700.0, "VFIAX": 2000.0, "KO": 300.0, "JNJ": 0.0})
    assert hh.indirect == pytest.approx({"AAPL": 70.0, "KO": 10.0})
    assert "SPY" not in hh.direct                                   # a fund with a file is only its look-through
    assert hh.funds == frozenset({"SPY", "VFIAX"}) and hh.look_through == {"SPY": {"AAPL": 0.07, "KO": 0.01}}


def test_household_of_a_file_for_a_name_that_is_not_a_fund_is_not_used():
    hh = household_of([{"account": "A", "symbol": "SPY", "market_value": 100.0}], {}, {"SPY": {"AAPL": 0.5}},
                      frozenset())
    assert hh.direct == {"SPY": 100.0} and hh.indirect == {}


def test_shares_follow_gaps():
    # base 1000. SPY 40% held 100: gap 400-100 = 300, top 500-100 = 400. KO 20% held 100: gap 100, top 150.
    # cash 200 shared 300:100 -> 150 / 50; k = min(1, 400/150, 150/50) = 1, so nothing stops: 150 / 50.
    plan = _plan([_e("SPY", 40), _e("KO", 20)], 200, held={"SPY": 100, "KO": 100})
    assert _d(plan) == {"SPY": 150, "KO": 50}
    assert [line.symbol for line in plan.lines] == ["SPY", "KO"]          # dollars descending
    spy = _line(plan, "SPY")
    assert (spy.target_gap, spy.band, spy.tilt, spy.reasons) == (300.0, "usual", 1.0, ("disagree",))
    assert _line(plan, "KO").target_gap == 100.0
    assert plan.waits == 0.0 and plan.notes == ()


def test_tilt_cheap_and_premium():
    # AAPL and JNJ 50% each of base 1000, none held: gaps 500 / 500, tops 625 / 625.
    # weights 500 x 1.5 = 750 (cheap) and 500 x 0.5 = 250 (premium); cash 300 -> 225 / 75.
    bands = {"AAPL": ("cheap", ("cheap_by:pe+p_fcf",)), "JNJ": ("premium", ("premium_by:pe+ps",))}
    plan = _plan([_e("AAPL", 50), _e("JNJ", 50)], 300, bands=bands)
    assert _d(plan) == {"AAPL": 225, "JNJ": 75}
    aapl, jnj = _line(plan, "AAPL"), _line(plan, "JNJ")
    assert (aapl.band, aapl.tilt, aapl.target_gap, aapl.reasons) == ("cheap", 1.5, 500.0, ("cheap_by:pe+p_fcf",))
    assert (jnj.band, jnj.tilt, jnj.reasons) == ("premium", 0.5, ("premium_by:pe+ps",))


def test_hold_name_gets_nothing_even_with_a_gap():
    # KO says hold: a 500 gap and a cheap band, still no line. SPY 30%: gap 300, top 375 -> all 200.
    # KO is also reachable through VOO's file: that gives it no line of its own either.
    bands = {"KO": ("cheap", ("cheap_by:pe+ps",)), **_usual("SPY")}
    hh = _hh(look_through={"VOO": {"KO": 0.01}}, funds={"VOO"}, indirect={"KO": 5.0})
    plan = _plan([_e("KO", 50, rule="hold"), _e("SPY", 30)], 200, bands=bands, hh=hh)
    assert _d(plan) == {"SPY": 200}
    assert _notes(plan) == [("KO", "hold")]


def test_unknown_rule_gets_nothing():
    plan = _plan([_e("KO", 50, rule="unknown"), _e("SPY", 30)], 200)
    assert _d(plan) == {"SPY": 200}
    assert _notes(plan) == [("KO", "unknown_rule")]


def test_line_stops_at_band_top_and_rest_goes_on():
    # base 1000. AAPL 13% (max 14%) held 100: gap 30, top 40. VOO 50% (max 100%), none held: gap 500, top 1000.
    # cash 1000, weights 30 / 500 (sum 530): inc AAPL 56.60, VOO 943.40; k = 40 / 56.60 = 0.7067 (AAPL's top).
    # AAPL -> 40 (stops); VOO -> 666.67; 293.33 left goes to VOO alone (room 333.33): VOO 960. Nothing waits.
    plan = _plan([_e("AAPL", 13, 14), _e("VOO", 50, 100)], 1000, held={"AAPL": 100})
    assert _d(plan) == {"VOO": 960, "AAPL": 40}
    assert _line(plan, "AAPL").reasons == ("disagree", "band_top")
    assert "band_top" not in _line(plan, "VOO").reasons
    assert plan.waits == 0.0


def test_money_beyond_every_top_waits():
    # SPY 10%: gap 100, top 125. KO 5%: gap 50, top 62.5. cash 500: inc 333.33 / 166.67, both stop at k = 0.375.
    # SPY 125, KO floor(62.5) = 62; 500 - 187 = 313 waits.
    plan = _plan([_e("SPY", 10), _e("KO", 5)], 500)
    assert _d(plan) == {"SPY": 125, "KO": 62}
    assert plan.waits == 313.0
    assert all("band_top" in line.reasons for line in plan.lines)


def test_sleeve_max_cuts_its_members():
    # base 1000; sleeve Defensives (KO, JNJ) max 20% -> room 200 - (100 + 50) held = 50.
    # KO 20% held 100: gap 100, top 150. JNJ 15% held 50: gap 100, top 137.5. SPY 40%: gap 400, top 500.
    # cash 300, weights 100/100/400: inc KO 50, JNJ 50, SPY 200; the sleeve's use 100 against room 50 -> k = 0.5.
    # KO 25, JNJ 25 (sleeve full, cut), SPY 100; the 150 left goes to SPY: 250. Members together = 50 = room.
    sleeve = Sleeve("brokerage", "Defensives", ("KO", "JNJ"), 5.0, 20.0, "contributions")
    plan = _plan([_e("KO", 20), _e("JNJ", 15), _e("SPY", 40)], 300, held={"KO": 100, "JNJ": 50},
                 sleeves=[sleeve], min_line=10)
    assert _d(plan) == {"SPY": 250, "JNJ": 25, "KO": 25}
    assert _line(plan, "KO").reasons == ("disagree", "sleeve_cut:Defensives")
    assert "sleeve_cut:Defensives" not in _line(plan, "SPY").reasons
    assert plan.waits == 0.0


def test_a_sleeve_label_with_a_comma_gives_a_comma_free_code():
    # Same numbers as above: the sleeve cuts KO; its label's comma becomes "+" (reasons are stored comma-joined).
    sleeve = Sleeve("brokerage", "Staples, health", ("KO", "JNJ"), None, 20.0, "contributions")
    plan = _plan([_e("KO", 20)], 100, held={"JNJ": 180.0}, sleeves=[sleeve])
    assert _line(plan, "KO").reasons == ("disagree", "sleeve_cut:Staples+ health")
    assert not any("," in code for line in plan.lines for code in line.reasons)


def test_sleeve_room_counts_members_held_without_a_line():
    # Sleeve Defensives (KO, JNJ) max 20% of 1000 = 200; JNJ (no entry here) holds 180, KO 0: room 20.
    # KO 20%: gap 200 -> 20 (cut), 80 waits.
    sleeve = Sleeve("brokerage", "Defensives", ("KO", "JNJ"), None, 20.0, "contributions")
    plan = _plan([_e("KO", 20)], 100, held={"JNJ": 180.0}, sleeves=[sleeve])
    assert _d(plan) == {"KO": 20} and plan.waits == 80.0
    assert _line(plan, "KO").reasons == ("disagree", "sleeve_cut:Defensives")


def test_sleeve_of_another_account_or_without_max_does_not_limit():
    other = Sleeve("roth", "Defensives", ("KO",), None, 1.0, "contributions")
    no_max = Sleeve("brokerage", "Loose", ("KO",), 1.0, None, "contributions")
    plan = _plan([_e("KO", 20)], 100, sleeves=[other, no_max])
    assert _d(plan) == {"KO": 100}


def test_cap_cuts_the_fund_line_through_look_through():
    # Cap AAPL 5% of a 10000 household = 500; AAPL held 300 direct + 200 via funds = 500: room 0.
    # VOO (file: AAPL 7%) 50%: gap 500, top 625; coef 0.07 -> frozen at 0 in round one (k = 0).
    # KO 20%: gap 200, top 250 -> takes all it can: 250 (k = 250 / 300); the other 50 waits.
    hh = _hh(direct={"AAPL": 300.0}, indirect={"AAPL": 200.0}, look_through={"VOO": {"AAPL": 0.07}}, funds={"VOO"})
    plan = _plan([_e("VOO", 50), _e("KO", 20)], 300, hh=hh, caps=[Cap("AAPL", 5.0)])
    assert _d(plan) == {"KO": 250}
    assert _notes(plan) == [("VOO", "cap_cut:AAPL")]
    assert plan.waits == 50.0
    # Room 7 left (193 via funds): VOO may take 7 / 0.07 = 100. Weights 500 / 200, inc 214.29 / 85.71; the cap's
    # use 0.07 x 214.29 = 15 against 7 -> k = 0.4667: VOO 100 (cut), KO 40; the 160 left goes to KO: 200.
    hh = _hh(direct={"AAPL": 300.0}, indirect={"AAPL": 193.0}, look_through={"VOO": {"AAPL": 0.07}}, funds={"VOO"})
    plan = _plan([_e("VOO", 50), _e("KO", 20)], 300, hh=hh, caps=[Cap("AAPL", 5.0)])
    assert _d(plan) == {"KO": 200, "VOO": 100}
    assert _line(plan, "VOO").dollars * 0.07 <= 7.0 + 1e-9
    assert _line(plan, "VOO").reasons == ("disagree", "cap_cut:AAPL")
    assert "look_through_assumed" not in _line(plan, "VOO").reasons


def test_cap_cuts_the_capped_name_itself():
    # Cap AAPL 5% of 10000 = 500, held 450 directly: room 50. AAPL 10% of base 1000: gap 100 -> 50 (cut), 50 waits.
    hh = _hh(direct={"AAPL": 450.0})
    plan = _plan([_e("AAPL", 10)], 100, hh=hh, caps=[Cap("AAPL", 5.0)])
    assert _d(plan) == {"AAPL": 50}
    assert _line(plan, "AAPL").reasons == ("disagree", "cap_cut:AAPL")
    assert plan.waits == 50.0


def test_cap_over_its_max_is_room_zero_not_negative():
    # AAPL held 900 against a 500 cap: room 0 (never negative), AAPL frozen at 0, all 100 waits.
    plan = _plan([_e("AAPL", 10)], 100, hh=_hh(direct={"AAPL": 900.0}), caps=[Cap("AAPL", 5.0)])
    assert plan.lines == () and plan.waits == 100.0
    assert _notes(plan) == [("AAPL", "cap_cut:AAPL")]


def test_fund_without_a_file_is_assumed_at_the_largest_weight():
    # VFIAX is a fund with no file; files on record hold AAPL at 6.5% (SPY) and 7% (VOO) -> assumed 0.07.
    # Cap AAPL 5% of 10000 = 500; held 400 + 93 = 493: room 7 -> VFIAX at most 7 / 0.07 = 100. 300 - 100 waits.
    hh = _hh(direct={"AAPL": 400.0}, indirect={"AAPL": 93.0},
             look_through={"SPY": {"AAPL": 0.065}, "VOO": {"AAPL": 0.07}}, funds={"VFIAX", "SPY", "VOO"})
    plan = _plan([_e("VFIAX", 60, account="roth")], 300, hh=hh, caps=[Cap("AAPL", 5.0)], account="roth")
    assert _d(plan) == {"VFIAX": 100}
    assert _line(plan, "VFIAX").reasons == ("disagree", "look_through_assumed", "cap_cut:AAPL")
    assert plan.waits == 200.0
    # No file names KO: the fund is not limited by a KO cap and nothing is assumed (KO is far over its cap).
    hh = _hh(direct={"KO": 5000.0}, look_through={"VOO": {"AAPL": 0.07}}, funds={"VFIAX", "VOO"})
    plan = _plan([_e("VFIAX", 60, account="roth")], 300, hh=hh, caps=[Cap("KO", 1.0)], account="roth")
    assert _d(plan) == {"VFIAX": 300}
    assert _line(plan, "VFIAX").reasons == ("disagree",)


def test_cap_counts_money_already_in_a_fund_without_a_file():
    # The reviewer's case. Household (household_of): VFIAX 5000 (fund, no file), AAPL 450, VOO 100 (file: AAPL 7%),
    # KO 4150, cash 300 -> total 10000. Cap AAPL 5% = 500. Used: 450 direct + 7 via VOO + 5000 x 0.07 (VFIAX at the
    # largest AAPL weight on file) = 807 > 500 -> room 0. AAPL (coef 1) and VFIAX (coef 0.07) are frozen at 0.
    # KO 20% of base 1000: gap 200, top 250 -> 250; 50 waits.
    positions = [{"account": "Sample Roth", "symbol": "VFIAX", "market_value": 5000.0},
                 {"account": "Sample Brokerage", "symbol": "AAPL", "market_value": 450.0},
                 {"account": "Sample Brokerage", "symbol": "VOO", "market_value": 100.0},
                 {"account": "Sample Brokerage", "symbol": "KO", "market_value": 4150.0}]
    hh = household_of(positions, {"Sample Brokerage": 300.0}, {"VOO": {"AAPL": 0.07}}, frozenset({"VOO", "VFIAX"}))
    assert hh.total == pytest.approx(10_000.0)
    assert hh.fund_values == pytest.approx({"VFIAX": 5000.0, "VOO": 100.0})
    assert hh.direct["VFIAX"] == 5000.0 and "VOO" not in hh.direct              # direct/indirect keep their meaning
    entries = [_e("AAPL", 50), _e("VFIAX", 30), _e("KO", 20)]
    plan = _plan(entries, 300, hh=hh, held={"AAPL": 450.0}, caps=[Cap("AAPL", 5.0)])
    assert _d(plan) == {"KO": 250}
    assert _notes(plan) == [("AAPL", "cap_cut:AAPL"), ("VFIAX", "cap_cut:AAPL")]
    assert plan.waits == 50.0
    # Without the assumption the same cap would have had 500 - 457 = 43 of room: the held fund is what closes it.
    # A file that does not name the capped name assumes 0: VFIAX's 5000 adds nothing to a KO cap.
    plan = _plan([_e("KO", 45)], 300, hh=hh, caps=[Cap("KO", 43.0)])           # 4300 - 4150 = 150 of room
    assert _d(plan) == {"KO": 150}
    assert _line(plan, "KO").reasons == ("disagree", "cap_cut:KO")


def test_cap_on_a_fund_counts_the_funds_own_value():
    # Cap VOO 10% of a 10000 household = 1000. VOO (file: AAPL 7%) held 950, which household_of keeps out of
    # `direct` (its file spreads it); the cap still counts it through fund_values: room 50. VOO 60% of 1000: gap 600
    # -> 50 (cut); KO 10%: gap 100, top 125. Weights 600 / 100: inc 257.14 / 42.86, the cap stops VOO at
    # k = 50 / 257.14; KO alone then reaches its top 125. 300 - 175 = 125 waits. SPY (no file) names no VOO: adds 0.
    positions = [{"account": "Sample Brokerage", "symbol": "VOO", "market_value": 950.0},
                 {"account": "Sample Brokerage", "symbol": "SPY", "market_value": 9050.0}]
    hh = household_of(positions, {}, {"VOO": {"AAPL": 0.07}}, frozenset({"VOO", "SPY"}))
    assert hh.total == 10_000.0 and "VOO" not in hh.direct
    plan = _plan([_e("VOO", 60), _e("KO", 10)], 300, hh=hh, caps=[Cap("VOO", 10.0)])
    assert _d(plan) == {"KO": 125, "VOO": 50}
    assert _line(plan, "VOO").reasons == ("disagree", "cap_cut:VOO")
    assert plan.waits == 125.0
    # A cap on a fund with no file: its own value is its direct holding, counted once (not twice). SPY 9050 against a
    # 91% cap (9100): room 50.
    plan = _plan([_e("SPY", 60)], 300, hh=hh, caps=[Cap("SPY", 91.0)])
    assert _d(plan) == {"SPY": 50}


def test_a_name_that_is_not_a_fund_is_not_limited_by_another_names_cap():
    hh = _hh(direct={"AAPL": 5000.0}, look_through={"VOO": {"AAPL": 0.07}}, funds={"VOO"})
    plan = _plan([_e("KO", 20)], 100, hh=hh, caps=[Cap("AAPL", 1.0)])
    assert _d(plan) == {"KO": 100}


def test_ratio_entry_gap_and_no_look_through():
    # JNJ direct/via-funds 1.5 (max 2.0): household direct 100, via funds 200 -> gap 1.5 x 200 - 100 = 200,
    # top 2.0 x 200 - 100 = 300. The account's own JNJ (999) is not what a ratio measures. cash 250 -> 250.
    # KO has no fund on file holding it -> no_look_through. AAPL 0.5: 0.5 x 300 - 200 < 0 -> at_or_over_target.
    hh = _hh(direct={"JNJ": 100.0, "AAPL": 200.0}, indirect={"JNJ": 200.0, "AAPL": 300.0})
    entries = [_e("JNJ", 1.5, 2.0, kind="direct_passive_ratio"), _e("KO", 1.0, kind="direct_passive_ratio"),
               _e("AAPL", 0.5, kind="direct_passive_ratio")]
    plan = _plan(entries, 250, hh=hh, held={"JNJ": 999.0})
    assert _d(plan) == {"JNJ": 250}
    assert _line(plan, "JNJ").target_gap == 200.0
    assert _notes(plan) == [("KO", "no_look_through"), ("AAPL", "at_or_over_target")]
    # Past its top the rest waits: cash 400 -> JNJ 300, 100 waits.
    plan = _plan(entries, 400, hh=hh)
    assert _d(plan) == {"JNJ": 300} and plan.waits == 100.0


def test_pct_of_brokerage_only_in_brokerage():
    # GLD is filed under roth as pct_of_brokerage (10% of the brokerage): it is the brokerage's entry, never roth's.
    entries = [_e("VFIAX", 60, account="roth"), _e("GLD", 10, kind="pct_of_brokerage", account="roth")]
    roth = _plan(entries, 50, account="roth", base=1000.0)
    assert _d(roth) == {"VFIAX": 50} and roth.notes == ()
    # brokerage base 1000, GLD held 0: gap 100 -> all 50. VFIAX (roth's own) is not this account's.
    brokerage = _plan(entries, 50, account="brokerage", base=1000.0)
    assert _d(brokerage) == {"GLD": 50} and brokerage.notes == ()


def test_duplicate_symbol_keeps_the_first():
    # GLD twice in the brokerage's list: the first (hold) wins and the second is a duplicate.
    entries = [_e("GLD", 10, rule="hold"), _e("GLD", 10, kind="pct_of_brokerage", account="roth"), _e("SPY", 10)]
    plan = _plan(entries, 50)
    assert _d(plan) == {"SPY": 50}
    assert _notes(plan) == [("GLD", "hold"), ("GLD", "duplicate")]


def test_at_or_over_target_gets_nothing_and_missing_scores_are_usual():
    # SPY 10% of 1000 held 100: gap 0. KO has no scores at all: usual (tilt 1.0), reason no_scores.
    # Its rule is trades, which the split reads as contributions and says so.
    plan = _plan([_e("SPY", 10), _e("KO", 10, rule="trades")], 50, held={"SPY": 100.0}, bands={})
    assert _d(plan) == {"KO": 50}
    ko = _line(plan, "KO")
    assert (ko.band, ko.tilt, ko.reasons) == ("usual", 1.0, ("trades_as_contributions", "no_scores"))
    assert _notes(plan) == [("SPY", "at_or_over_target")]


def test_lines_under_min_are_folded_into_the_largest_within_its_room():
    # SPY 90% (max 100%): gap 900, top 1000. KO and JNJ 5%: gap 50 each. cash 100 -> 90 / 5 / 5.
    # KO and JNJ are under the $10 minimum: their 10 go to SPY (room 910) -> SPY 100, nothing waits.
    entries = [_e("SPY", 90, 100), _e("KO", 5), _e("JNJ", 5)]
    plan = _plan(entries, 100)
    assert _d(plan) == {"SPY": 100}
    assert _line(plan, "SPY").reasons == ("disagree", "folded")
    assert _notes(plan) == [("KO", "under_min"), ("JNJ", "under_min")]
    assert plan.waits == 0.0
    # A sleeve over SPY alone with max 9.3% (room 93): the fill stops nobody (use 90 <= 93), but the fold may only
    # add 93 - 90 = 3. SPY 93, 7 waits.
    sleeve = Sleeve("brokerage", "Core", ("SPY",), None, 9.3, "contributions")
    plan = _plan(entries, 100, sleeves=[sleeve])
    assert _d(plan) == {"SPY": 93} and plan.waits == 7.0
    # A fund receiving the fold: VOO (file: AAPL 10%) under a cap AAPL 1% of a 1000 household = 10, AAPL held 0.5
    # directly: room 9.5. The fill gives VOO 90 (use 9.0 <= 9.5); the fold may add (9.5 - 9.0) / 0.1 = 5: VOO 95.
    hh = _hh(total=1000.0, direct={"AAPL": 0.5}, look_through={"VOO": {"AAPL": 0.10}}, funds={"VOO"})
    plan = _plan([_e("VOO", 90, 100), _e("KO", 5), _e("JNJ", 5)], 100, hh=hh, caps=[Cap("AAPL", 1.0)])
    assert _d(plan) == {"VOO": 95} and plan.waits == 5.0
    assert _line(plan, "VOO").reasons == ("disagree", "folded")
    assert _line(plan, "VOO").dollars * 0.10 + 0.5 <= 10.0 + 1e-9


def test_fold_goes_to_the_largest_and_ties_go_alphabetical():
    # SPY and AAPL 45% each (max 100%): 45 / 45; KO 10%: 10 -> with min_line 20, KO's 10 folds into AAPL (tie: A).
    plan = _plan([_e("SPY", 45, 100), _e("AAPL", 45, 100), _e("KO", 10)], 100, min_line=20)
    assert _d(plan) == {"AAPL": 55, "SPY": 45}
    assert "folded" in _line(plan, "AAPL").reasons and "folded" not in _line(plan, "SPY").reasons


def test_fold_never_passes_a_top():
    # SPY 9% with max 9%: gap 90 = top 90. KO, JNJ 0.5%: gap 5 each. cash 100 -> 90 / 5 / 5 (k = 1, SPY at its top).
    # KO and JNJ fold, but SPY has no room: their 10 waits.
    plan = _plan([_e("SPY", 9, 9), _e("KO", 0.5), _e("JNJ", 0.5)], 100)
    assert _d(plan) == {"SPY": 90}
    assert _line(plan, "SPY").reasons == ("disagree", "band_top")
    assert plan.waits == 10.0
    assert _notes(plan) == [("KO", "under_min"), ("JNJ", "under_min")]


def test_cash_guard_drops_a_line_it_trims_under_the_minimum():
    # The guard is never reached through plan_split (floors cannot pass the cash); fed 60 + 12 against 70 of cash it
    # trims the smallest first: KO 12 -> 10 (still >= 10, kept). Against 65: KO 12 -> 5, under 10, dropped; 60 stays.
    from financial_data_collector.derive.split import _within_cash
    dollars = {"SPY": 60, "KO": 12}
    assert _within_cash(dollars, ["KO", "SPY"], 70.0, 10) == ["KO", "SPY"] and dollars == {"SPY": 60, "KO": 10}
    dollars = {"SPY": 60, "KO": 12}
    assert _within_cash(dollars, ["KO", "SPY"], 65.0, 10) == ["SPY"] and dollars["SPY"] == 60
    dollars = {"SPY": 60, "KO": 12}
    assert _within_cash(dollars, ["KO", "SPY"], 72.0, 10) == ["KO", "SPY"] and dollars == {"SPY": 60, "KO": 12}


THREE = [_e("SPY", 40), _e("KO", 30), _e("JNJ", 20)]
THREE_HELD = {"SPY": 66.67, "KO": 77.78, "JNJ": 88.89}   # gaps 333.33 / 222.22 / 111.11; tops 433.33 / 297.22 / 161.11


@pytest.mark.parametrize("cash", [0, 9, 10, 25.75, 1000.99])
def test_lines_never_sum_above_cash(cash):
    plan = _plan(THREE, cash, held=THREE_HELD)
    total = sum(line.dollars for line in plan.lines)
    assert total <= cash
    assert plan.waits == round(cash - total, 2)
    tops = {"SPY": 433.33, "KO": 297.22, "JNJ": 161.11}
    for line in plan.lines:
        assert isinstance(line.dollars, int) and line.dollars >= 10
        assert line.dollars <= tops[line.symbol] + 1e-9


def test_three_line_sweep_values():
    # 9 and 10: every share is under $10 (10 -> 5 / 3.33 / 1.67), so everything folds into the largest (SPY): 9 is
    # still under the minimum and waits; 10 makes one line of 10.
    assert _d(_plan(THREE, 9, held=THREE_HELD)) == {} and _plan(THREE, 9, held=THREE_HELD).waits == 9.0
    assert _d(_plan(THREE, 10, held=THREE_HELD)) == {"SPY": 10} and _plan(THREE, 10, held=THREE_HELD).waits == 0.0
    # 25.75: 12.875 / 8.58 / 4.29 -> floors 12 / 8 / 4; 8 + 4 fold into SPY: 24, 1.75 waits.
    plan = _plan(THREE, 25.75, held=THREE_HELD)
    assert _d(plan) == {"SPY": 24} and plan.waits == 1.75
    # 1000.99 is more than every top together (891.66): each stops at its top, floors 433 / 297 / 161, 109.99 waits.
    plan = _plan(THREE, 1000.99, held=THREE_HELD)
    assert _d(plan) == {"SPY": 433, "KO": 297, "JNJ": 161} and plan.waits == 109.99


def test_wide_sweep_never_passes_cash_tops_or_groups():
    # Many cash amounts over a plan with a sleeve, a cap through a fund and a ratio line: every invariant at once.
    hh = _hh(total=20_000.0, direct={"AAPL": 600.0, "JNJ": 50.0}, indirect={"AAPL": 300.0, "JNJ": 80.0},
             look_through={"VOO": {"AAPL": 0.07, "JNJ": 0.01}}, funds={"VOO", "VFIAX"})
    sleeve = Sleeve("brokerage", "Defensives", ("KO", "JNJ"), None, 12.0, "contributions")
    entries = [_e("VOO", 40), _e("VFIAX", 10), _e("AAPL", 8), _e("KO", 9),
               _e("JNJ", 1.2, 1.5, kind="direct_passive_ratio"), _e("GLD", 4, rule="hold")]
    held = {"VOO": 150.0, "AAPL": 30.0, "KO": 40.0, "JNJ": 20.0}
    base = 1000.0
    bands = {"VOO": ("cheap", ()), "AAPL": ("premium", ()), "KO": ("usual", ())}
    for cents in range(0, 200_000, 997):
        cash = cents / 100
        plan = _plan(entries, cash, base=base, held=held, hh=hh, bands=bands, sleeves=[sleeve],
                     caps=[Cap("AAPL", 5.0)])
        d = _d(plan)
        assert sum(d.values()) <= cash
        assert plan.waits == round(cash - sum(d.values()), 2)
        assert "GLD" not in d
        assert all(v >= 10 for v in d.values())
        tops = {"VOO": 500 - 150, "VFIAX": 125, "AAPL": 100 - 30, "KO": 112.5 - 40, "JNJ": 1.5 * 80 - 50}
        assert all(d[s] <= tops[s] + 1e-6 for s in d)
        assert d.get("KO", 0) + d.get("JNJ", 0) <= 120 - 60 + 1e-6                    # the sleeve's room
        assert d.get("AAPL", 0) + 0.07 * d.get("VOO", 0) + 0.07 * d.get("VFIAX", 0) <= 1000 - 900 + 1e-6  # the cap


def test_zero_cash():
    for cash in (0, 0.0, -5.0, 0.004):
        plan = _plan([_e("KO", 50, rule="hold"), _e("SPY", 40), _e("AAPL", 10)], cash)
        assert plan.lines == () and plan.waits == 0.0
        assert _notes(plan) == [("KO", "hold"), ("SPY", "no_cash"), ("AAPL", "no_cash")]
    # No eligible entry: everything waits.
    plan = _plan([_e("KO", 50, rule="hold")], 100.0)
    assert plan.lines == () and plan.waits == 100.0 and _notes(plan) == [("KO", "hold")]
    plan = _plan([], 100.0)
    assert plan == SplitPlan((), 100.0, ())


def test_whole_dollars_only():
    plan = _plan(THREE, 777.77, held=THREE_HELD)
    assert plan.lines
    for line in plan.lines:
        assert type(line.dollars) is int
    assert plan.waits == round(777.77 - sum(line.dollars for line in plan.lines), 2)
    assert math.isclose(plan.waits * 100, round(plan.waits * 100))


def test_same_inputs_same_plan():
    bands = {"SPY": ("cheap", ("cheap_by:pe+ps",)), "KO": ("premium", ("premium_by:pe+ps",)), "JNJ": ("usual", ())}
    sleeve = Sleeve("brokerage", "Defensives", ("KO", "JNJ"), None, 20.0, "contributions")
    a = _plan(THREE, 555.55, held=THREE_HELD, bands=bands, sleeves=[sleeve])
    b = _plan(THREE, 555.55, held=dict(reversed(list(THREE_HELD.items()))),
              bands=dict(reversed(list(bands.items()))), sleeves=[sleeve])
    assert a == b
    assert a == _plan(THREE, 555.55, held=THREE_HELD, bands=bands, sleeves=[sleeve])


def test_split_reasons_and_notes_never_say_buy():
    hh = _hh(direct={"AAPL": 300.0}, indirect={"AAPL": 200.0}, look_through={"VOO": {"AAPL": 0.07}},
             funds={"VOO", "VFIAX"})
    entries = [_e("VOO", 50), _e("VFIAX", 10, rule="trades"), _e("KO", 5, rule="hold"), _e("JNJ", 1, rule="unknown"),
               _e("SPY", 10), _e("GLD", 0.5)]
    plan = _plan(entries, 400, hh=hh, held={"SPY": 200.0}, caps=[Cap("AAPL", 5.0)], bands={})
    codes = [r for line in plan.lines for r in line.reasons] + [n["reason"] for n in plan.notes]
    assert codes and not any("buy" in c.lower() for c in codes)


# ---- final fix wave: a hold anywhere in the account wins, everything under the minimum folds, untyped names

def test_a_hold_entry_after_a_contributions_duplicate_still_holds():
    # GLD is filed twice in the brokerage: first contributions, then hold. Any hold entry makes it hold.
    entries = [_e("GLD", 10), _e("GLD", 10, rule="hold"), _e("SPY", 10)]
    plan = _plan(entries, 50)
    assert _d(plan) == {"SPY": 50}
    assert _notes(plan) == [("GLD", "hold"), ("GLD", "duplicate")]
    # A pct_of_brokerage hold filed under roth counts in the brokerage too.
    entries = [_e("GLD", 10), _e("GLD", 10, kind="pct_of_brokerage", rule="hold", account="roth"), _e("SPY", 10)]
    assert _d(_plan(entries, 50)) == {"SPY": 50}


def test_when_no_line_reaches_the_minimum_everything_folds_into_the_largest():
    # Six entries of 10% of 1000 (gap 100, top 125 each), cash 50: 8.33 each, every floor (8) under 10. All 50 fold
    # into the largest share (a tie: AAPL, alphabetical first), well within its top: one line of 50, nothing waits.
    entries = [_e(s, 10) for s in ("SPY", "KO", "JNJ", "VOO", "GLD", "AAPL")]
    plan = _plan(entries, 50)
    assert _d(plan) == {"AAPL": 50} and plan.waits == 0.0
    assert _line(plan, "AAPL").reasons == ("disagree", "folded")
    assert sorted(_notes(plan)) == sorted((s, "under_min") for s in ("SPY", "KO", "JNJ", "VOO", "GLD"))


def test_the_all_under_fold_goes_to_the_largest_share_within_its_room():
    # SPY gap 300 (30%), KO gap 100 (10%): cash 12 -> 9 / 3, both under 10. SPY is the largest: 12 -> SPY 12.
    plan = _plan([_e("SPY", 30), _e("KO", 10)], 12)
    assert _d(plan) == {"SPY": 12} and "folded" in _line(plan, "SPY").reasons
    # Its room is the smallest of its top and every group over it: a sleeve on SPY alone with 1.1% of 1000 = 11.
    sleeve = Sleeve("brokerage", "Core", ("SPY",), None, 1.1, "contributions")
    plan = _plan([_e("SPY", 30), _e("KO", 10)], 12, sleeves=[sleeve])
    assert _d(plan) == {"SPY": 11} and plan.waits == 1.0
    # Still under the minimum after the fold: everything waits.
    plan = _plan([_e("SPY", 30), _e("KO", 10)], 9)
    assert plan.lines == () and plan.waits == 9.0
    small = Sleeve("brokerage", "Core", ("SPY",), None, 0.9, "contributions")      # room 9: under 10
    plan = _plan([_e("SPY", 30), _e("KO", 10)], 12, sleeves=[small])
    assert plan.lines == () and plan.waits == 12.0


def test_an_untyped_name_is_capped_as_a_fund_without_a_file_and_says_so():
    # SPY's type is unknown and no file shows what it holds: for caps it is a fund without a file, taken at the largest
    # AAPL weight on file (VOO: 10%). Cap AAPL 5% of 10000 = 500, held 480: room 20 -> SPY at most 200.
    hh = Household(10_000.0, {"AAPL": 480.0}, {}, {"VOO": {"AAPL": 0.10}}, frozenset({"VOO", "SPY"}),
                   untyped=frozenset({"SPY"}))
    plan = _plan([_e("SPY", 50)], 300, hh=hh, caps=[Cap("AAPL", 5.0)])
    assert _d(plan) == {"SPY": 200} and plan.waits == 100.0
    assert _line(plan, "SPY").reasons == ("disagree", "type_unknown", "look_through_assumed", "cap_cut:AAPL")
    # Without a cap it is not cut, and still says its type is unknown.
    plan = _plan([_e("SPY", 50)], 300, hh=hh)
    assert _d(plan) == {"SPY": 300} and _line(plan, "SPY").reasons == ("disagree", "type_unknown")


def test_household_of_charges_an_untyped_holding_at_the_assumed_weight():
    # A held SPY of unknown type and no file: a direct holding of itself and a fund value for the caps.
    positions = [{"account": "A", "symbol": "SPY", "market_value": 1000.0},
                 {"account": "A", "symbol": "VOO", "market_value": 1000.0}]
    hh = household_of(positions, {}, {"VOO": {"AAPL": 0.07}}, frozenset({"VOO", "SPY"}), untyped=frozenset({"SPY"}))
    assert hh.direct == {"SPY": 1000.0} and hh.fund_values == {"SPY": 1000.0, "VOO": 1000.0}
    assert hh.untyped == frozenset({"SPY"})
    from financial_data_collector.derive.split import cap_used
    assert cap_used(hh, "AAPL") == pytest.approx(70.0 + 70.0)       # via VOO's file + SPY at the assumed 7%


def test_with_pending_adds_another_accounts_open_lines():
    from financial_data_collector.derive.split import with_pending
    hh = _hh(total=5000.0, direct={"AAPL": 100.0}, look_through={"VOO": {"AAPL": 0.07}}, funds={"VOO", "VFIAX"})
    got = with_pending(hh, [("AAPL", 300), ("VOO", 1000), ("VFIAX", 200)], add_to_total=False)
    assert got.direct == {"AAPL": 400.0, "VFIAX": 200.0}
    assert got.indirect == pytest.approx({"AAPL": 70.0})
    assert got.fund_values == {"VOO": 1000.0, "VFIAX": 200.0}
    assert got.total == 5000.0                                       # that account's cash is already counted
    assert with_pending(hh, [("AAPL", 300)], add_to_total=True).total == 5300.0
    assert hh.direct == {"AAPL": 100.0}                              # the household itself is not changed
