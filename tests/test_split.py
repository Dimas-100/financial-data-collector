"""The weekly split, part one: a holding's single band from its primary valuation scores (plan 4, Task 2)."""
from financial_data_collector.derive.split import PRIMARY_COMPANY, PRIMARY_FUND, holding_band

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
    assert reasons == ("missing:ps", "cheap_by:pe,p_fcf")
    assert [r["metric"] for r in rows] == ["pe", "p_fcf", "ev_ebit"]


def test_one_premium_blocks_cheap():
    band, reasons, _ = holding_band(_company(pe="cheap", p_fcf="cheap", ev_ebit="cheap", ps="premium"), False)
    assert (band, reasons) == ("usual", ("disagree",))


def test_most_premium_is_premium():
    band, reasons, rows = holding_band(_company(pe="premium", p_fcf="premium", ev_ebit="cheap", ps="usual"), False)
    assert (band, reasons) == ("usual", ("disagree",))                       # 2 of 4 is not more than half
    band, reasons, rows = holding_band(_company(pe="premium", p_fcf="premium", ev_ebit="premium", ps="cheap"), False)
    assert (band, reasons) == ("premium", ("premium_by:pe,p_fcf,ev_ebit",))
    assert [r["metric"] for r in rows] == ["pe", "p_fcf", "ev_ebit", "ps"]


def test_premium_beats_cheap_when_it_is_the_majority():
    band, reasons, _ = holding_band(_company(pe="premium", p_fcf="premium", ev_ebit="cheap", ps=None), False)
    assert (band, reasons) == ("premium", ("missing:ps", "premium_by:pe,p_fcf"))


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
    assert reasons == ("held:pe", "cheap_by:p_fcf,ps")
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
    assert (band, reasons) == ("cheap", ("cheap_by:pe,p_fcf,ev_ebit,ps",))
    assert [r["metric"] for r in rows] == ["pe", "p_fcf", "ev_ebit", "ps"]


def test_quality_unknown_does_not_block_but_is_said():
    rows = _company(pe="cheap", p_fcf="cheap", ev_ebit="usual", ps="cheap", fcf_margin=None, op_margin=-0.01)
    rows.append(_row("fcf_margin_change", status="not_enough_history"))
    band, reasons, decided = holding_band(rows, False)
    assert band == "cheap"
    assert reasons == ("cheap_by:pe,p_fcf,ps", "quality_unknown")
    assert {"fcf_margin_change", "op_margin_change"} <= {r["metric"] for r in decided}
    band, reasons, _ = holding_band(_company(pe="cheap", p_fcf="cheap", ev_ebit="cheap", ps="cheap",
                                             fcf_margin=None, op_margin=None), False)
    assert (band, reasons) == ("cheap", ("cheap_by:pe,p_fcf,ev_ebit,ps", "quality_unknown"))


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
    assert (band, reasons) == ("cheap", ("cheap_by:pe,dividend_yield",))
    assert [r["metric"] for r in rows] == ["pe", "dividend_yield"]


def test_fund_has_no_quality_check():
    rows = [_row("pe", "cheap"), _row("dividend_yield", "cheap"), _row("fcf_margin_change", None, value=-1.0),
            _row("op_margin_change", None, value=-1.0)]
    band, reasons, decided = holding_band(rows, True)
    assert (band, reasons) == ("cheap", ("cheap_by:pe,dividend_yield",))
    assert len(decided) == 2


def test_fund_split_decision_is_usual():
    band, reasons, _ = holding_band([_row("pe", "cheap"), _row("dividend_yield", "premium")], True)
    assert (band, reasons) == ("usual", ("disagree",))


def test_a_fund_ignores_company_metrics():
    rows = [_row("pe", "premium"), _row("dividend_yield", "premium"), _row("ps", "cheap"), _row("p_fcf", "cheap")]
    band, reasons, _ = holding_band(rows, True)
    assert (band, reasons) == ("premium", ("premium_by:pe,dividend_yield",))


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
