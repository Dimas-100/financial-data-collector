"""Company scores against their own history: P/E, P/FCF, P/S, EV/EBIT and dividend yield, each with a percentile
over the symbol's own 5-year daily history and a cheap / usual / premium band (Task 4 of the valuation engine)."""
import json
from datetime import date

import pytest

from financial_data_collector.config import EngineSettings
from financial_data_collector.derive import engine, scores
from financial_data_collector.derive import valuation as V
from financial_data_collector.models import Fact, LineItem, PriceBar
from tests.test_engine import END, FETCHED, _cover, _hold, _scores, _weekday_bars, cfg, store  # noqa: F401  (fixtures)

CIK = "0000999002"
ACCN = "0000999002-26-000031"
HISTORY = ("pe", "p_fcf", "ps", "ev_ebit", "dividend_yield")
BASE = {"close": 100.0, "shares": 10.0, "market_cap": 1000.0, "revenue_ttm": 500.0, "net_income_ttm": 50.0,
        "ocf_ttm": 60.0, "fcf_ttm": 40.0, "eps_ttm": 5.0, "pe": 30.0, "ps": 2.0, "p_fcf": 25.0, "dividends_12m": 2.0,
        "dividend_yield": 0.02, "ttm_period_end": "2026-06-30", "available_from": "2026-08-01", "status": "ok",
        "status_reason": None}


def _company(store, n: int = 1300, row=None, symbol: str = "AAA") -> list[str]:
    """`n` weekday bars and valuation_daily rows of AAA (CIK 0000999002) ending END, each BASE updated by
    `row(i, date)` (a dict; Python indexing from the newest is i - n). Returns the dates."""
    dates = _weekday_bars(store, symbol, n=n)
    store.set_security_cik(symbol, CIK, "Sample Earnings Co")
    rows = []
    for i, d in enumerate(dates):
        r = dict(BASE, symbol=symbol, date=d)
        r.update((row(i, d) if row else None) or {})
        rows.append(tuple(r[c] for c in V.COLUMNS))
    store.replace_rows("valuation_daily", V.COLUMNS, rows)
    return dates


FILED = {"2025-06-30": "2025-08-01", "2025-09-30": "2025-11-01", "2025-12-31": "2026-02-01",
         "2026-03-31": "2026-05-01", "2026-06-30": "2026-08-01"}


def _statements(store, debt: dict[str, float | None], cash: float = 50.0, op_q: float = 25.0,
                short_term: float | None = None) -> None:
    """Quarterly statements 2025-06-30 .. 2026-06-30 (each filed as FILED says): operating income `op_q` a quarter,
    cash `cash`, short-term investments `short_term` (None = not reported), long-term debt per quarter end from
    `debt` (None or absent = not reported). Full TTM rows exist for 2026-03-31 and 2026-06-30."""
    items = []
    for k, (e, filed) in enumerate(FILED.items()):
        q = k % 4 + 1
        items.append(LineItem("operating_income", "quarter", "", e, int(e[:4]), q, op_q, "OperatingIncomeLoss", filed,
                              ACCN))
        items.append(LineItem("cash", "quarter", "", e, int(e[:4]), q, cash, "CashAndCashEquivalentsAtCarryingValue",
                              filed, ACCN))
        if short_term is not None:
            items.append(LineItem("short_term_investments", "quarter", "", e, int(e[:4]), q, short_term,
                                  "ShortTermInvestments", filed, ACCN))
        if debt.get(e) is not None:
            items.append(LineItem("long_term_debt", "quarter", "", e, int(e[:4]), q, debt[e], "LongTermDebtNoncurrent",
                                  filed, ACCN))
    store.replace_line_items(CIK, items)


def _fact(form: str, accn: str, filed: str, period_end: str) -> Fact:
    return Fact("us-gaap", "Revenues", "USD", "", period_end, 125.0, 2026, "Q2", form, filed, accn)


def _run(store, cfg):
    _cover(store)
    rep = engine.run(store, cfg)
    assert rep.skipped is None
    return _scores(store, "AAA")


# ---- pure pieces ----------------------------------------------------------------------------------------------------

def test_percentile_is_mid_rank():
    assert scores.percentile([10, 20, 30, 40], 30) == 62.5
    assert scores.percentile([30.0] * 1299 + [12.0], 12.0) == 0.0          # 100 * 0.5 / 1300 = 0.038 -> 0.0
    assert scores.percentile([5.0, 5.0], 5.0) == 50.0
    assert scores.percentile([1.0, 2.0, 3.0], 3.0) == pytest.approx(83.3)


@pytest.mark.parametrize("metric, pct, want", [
    ("pe", 10, "cheap"), ("pe", 50, "usual"), ("pe", 90, "premium"), ("pe", 25, "usual"), ("pe", 75, "usual"),
    ("pe", 24.9, "cheap"), ("pe", 75.1, "premium"), ("ev_ebit", 5, "cheap"), ("p_fcf", 80, "premium"),
    ("ps", 30, "usual"), ("dividend_yield", 90, "cheap"), ("dividend_yield", 10, "premium"),
    ("dividend_yield", 75, "usual"), ("dividend_yield", 25, "usual"), ("pe", None, None),
    ("dividend_yield", None, None)])
def test_band(metric, pct, want):
    assert scores.band(metric, pct, EngineSettings()) == want


def test_band_reads_the_configured_thresholds():
    s = EngineSettings(cheap_percentile=10.0, premium_percentile=90.0)
    assert [scores.band("pe", p, s) for p in (9, 10, 50, 90, 91)] == ["cheap", "usual", "usual", "usual", "premium"]
    assert [scores.band("dividend_yield", p, s) for p in (91, 90, 10, 9)] == ["cheap", "usual", "usual", "premium"]


def test_band_refuses_a_metric_without_a_direction():
    with pytest.raises(ValueError):
        scores.band("off_high_52w", 50.0, EngineSettings())


def test_ev_ebit_needs_debt_and_cash():
    assert scores.ev_ebit(1000.0, 200.0, 50.0, None, 100.0) == pytest.approx(11.5)
    assert scores.ev_ebit(1000.0, 200.0, 50.0, 30.0, 100.0) == pytest.approx(11.2)
    assert scores.ev_ebit(1000.0, None, 50.0, 0.0, 100.0) is None
    assert scores.ev_ebit(1000.0, 200.0, None, 0.0, 100.0) is None
    assert scores.ev_ebit(1000.0, 200.0, 50.0, 0.0, -5.0) is None
    assert scores.ev_ebit(1000.0, 200.0, 50.0, 0.0, 0.0) is None
    assert scores.ev_ebit(1000.0, 200.0, 50.0, 0.0, None) is None
    assert scores.ev_ebit(None, 200.0, 50.0, 0.0, 100.0) is None


# ---- the history and the band ---------------------------------------------------------------------------------------

def test_pe_today_low_against_five_years_is_cheap(store, cfg):
    # AAA: 1300 weekday valuation rows, pe 30 throughout, the newest 12  -> percentile 0.0, band cheap
    dates = _company(store, row=lambda i, d: {"pe": 12.0} if i == 1299 else None)
    r = _run(store, cfg)["pe"]
    assert (r["status"], r["band"], r["percentile_5y"], r["value"]) == ("ok", "cheap", 0.0, 12.0)
    assert r["date"] == END and r["inputs"] == ",pe,close," and r["status_reason"] is None
    src = json.loads(r["sources"])
    assert {s["kind"] for s in src} >= {"price", "derived", "config"}
    assert src[0] == {"kind": "price", "label": "tiingo close", "date": END, "ref": "2026-10-30T12:00:00Z"}
    derived = [s for s in src if s["kind"] == "derived"]
    assert derived == [{"kind": "derived", "label": "valuation_daily pe", "date": END,
                        "ref": f"{dates[0]}..{END} (1300 days)"}]
    conf = {s["label"]: s["ref"] for s in src if s["kind"] == "config"}
    assert conf == {"[engine] history_years": "5", "[engine] cheap_percentile": "25.0",
                    "[engine] premium_percentile": "75.0"}


def test_every_history_metric_gets_a_row_with_its_inputs(store, cfg):
    _company(store)
    _statements(store, {"2026-03-31": 200.0, "2026-06-30": 200.0})
    rows = _run(store, cfg)
    assert set(HISTORY) <= set(rows)
    want = {"pe": ",pe,close,", "p_fcf": ",p_fcf,close,", "ps": ",ps,close,", "ev_ebit": ",market_cap,close,",
            "dividend_yield": ",dividend_yield,close,"}
    for m in HISTORY:
        assert rows[m]["status"] == "ok" and rows[m]["band"] == "usual" and rows[m]["percentile_5y"] == 50.0
        assert rows[m]["inputs"] == want[m]
    assert rows["ev_ebit"]["value"] == pytest.approx((1000 + 200 - 50) / 100)
    assert (rows["pe"]["value"], rows["ps"]["value"], rows["p_fcf"]["value"]) == (30.0, 2.0, 25.0)
    assert rows["dividend_yield"]["value"] == 0.02


def test_a_high_yield_is_cheap(store, cfg):
    _company(store, row=lambda i, d: {"dividend_yield": 0.05} if i == 1299 else None)
    r = _run(store, cfg)["dividend_yield"]
    assert (r["status"], r["percentile_5y"], r["band"]) == ("ok", 100.0, "cheap")


def test_a_high_pe_is_premium(store, cfg):
    _company(store, row=lambda i, d: {"pe": 45.0} if i == 1299 else None)
    r = _run(store, cfg)["pe"]
    assert (r["percentile_5y"], r["band"]) == (100.0, "premium")


def test_rows_older_than_five_years_are_left_out(store, cfg):
    # 1600 weekdays (6 years): pe 40 before 2021-10-29, 30 since; the newest 30 sits mid-rank among 30s only
    _company(store, n=1600, row=lambda i, d: {"pe": 40.0} if d < "2021-10-29" else None)
    r = _run(store, cfg)["pe"]
    assert (r["percentile_5y"], r["band"]) == (50.0, "usual")
    derived = [s for s in json.loads(r["sources"]) if s["kind"] == "derived"][0]
    assert derived["ref"].startswith("2021-10-29..")


def test_rows_that_are_not_ok_are_left_out_of_the_history(store, cfg):
    # an old stale span keeps a yield of 0.10 (stale rows keep dividends): not part of the yield's history
    _company(store, row=lambda i, d: {"status": "stale", "status_reason": "newest statements end 2021-12-31",
                                      "dividend_yield": 0.10, "pe": None} if 100 <= i < 400 else None)
    r = _run(store, cfg)["dividend_yield"]
    assert (r["percentile_5y"], r["band"]) == (50.0, "usual")
    assert [s for s in json.loads(r["sources"]) if s["kind"] == "derived"][0]["ref"].endswith("(1000 days)")


def test_a_past_hold_span_is_left_out_of_the_history(store, cfg):       # Review Focus 3
    # pe 30 for 4 years, 5 for a held 6-month span, 20 since; newest 20: without the span its percentile is mid-rank
    # among 20s and 30s only (the 5s would have pushed it up)
    dates = _company(store, row=lambda i, d: {"pe": 30.0 if i < 1040 else 5.0 if i < 1170 else 20.0})
    _hold(store, "figures", "AAA", "pe", dates[1040], dates[1169], "P/E disagrees with an outside figure")
    r = _run(store, cfg)["pe"]
    assert r["value"] == 20.0 and r["status"] == "ok"
    assert r["percentile_5y"] == round(100 * (0.5 * 130) / 1170, 1) == 5.6          # with the 5s it would be 15.0
    assert r["band"] == "cheap"
    assert [s for s in json.loads(r["sources"]) if s["kind"] == "derived"][0]["ref"].endswith("(1170 days)")


def test_a_past_price_hold_leaves_those_days_out_of_every_metric(store, cfg):
    dates = _company(store, row=lambda i, d: {"ps": 9.0} if 500 <= i < 600 else None)
    _hold(store, "impossible", "AAA", "*", dates[500], dates[599], "a close below zero")
    rows = _run(store, cfg)
    assert rows["ps"]["percentile_5y"] == 50.0 and rows["ps"]["band"] == "usual"
    assert all(rows[m]["status"] == "ok" for m in ("pe", "ps", "p_fcf", "dividend_yield"))


def test_a_hold_on_the_newest_day_makes_it_held_with_no_band(store, cfg):
    _company(store)
    _hold(store, "figures", "AAA", "pe", END, END, "P/E disagrees with an outside figure")
    rows = _run(store, cfg)
    r = rows["pe"]
    assert (r["status"], r["band"], r["value"], r["percentile_5y"]) == ("held", None, None, None)
    assert r["status_reason"] == "P/E disagrees with an outside figure"
    assert json.loads(r["sources"])[0]["kind"] == "price"
    assert rows["ps"]["status"] == "ok" and rows["ps"]["band"] == "usual"         # only the held field


def test_a_statements_star_hold_holds_every_history_metric(store, cfg):
    _company(store)
    _statements(store, {"2026-03-31": 200.0, "2026-06-30": 200.0})
    _hold(store, "fresh", "AAA", "*", "2026-10-01", None, "statements are stale")
    _hold(store, "filings", "AAA", "*", "2026-10-15", None, "a 10-Q is missing")
    rows = _run(store, cfg)
    for m in HISTORY:
        assert (rows[m]["status"], rows[m]["value"], rows[m]["band"]) == ("held", None, None)
        assert rows[m]["status_reason"] == "statements are stale; a 10-Q is missing"
    assert rows["off_high_52w"]["status"] != "held"                               # a statements hold leaves prices


def test_a_close_hold_on_the_newest_day_holds_every_history_metric(store, cfg):
    # every history metric is built on the close, so a held close holds them all
    _company(store)
    _statements(store, {"2026-03-31": 200.0, "2026-06-30": 200.0})
    _hold(store, "closes", "AAA", "close", END, END, "close looks wrong")
    rows = _run(store, cfg)
    for m in HISTORY:
        assert (rows[m]["status"], rows[m]["status_reason"]) == ("held", "close looks wrong")


def test_view_blanks_history_rows_under_a_close_hold_opened_after_the_build(store, cfg):   # R7
    _company(store)
    _statements(store, {"2026-03-31": 200.0, "2026-06-30": 200.0})
    assert all(r["status"] == "ok" for m, r in _run(store, cfg).items() if m in HISTORY)
    _hold(store, "closes", "AAA", "close", END, END, "close looks wrong")
    view = _scores(store, "AAA", "valuation_scores")
    for m in HISTORY:
        assert (view[m]["status"], view[m]["value"], view[m]["percentile_5y"], view[m]["band"]) ==                ("held", None, None, None)
        assert view[m]["status_reason"] == "close looks wrong" and view[m]["hold_ids"]
    raw = _scores(store, "AAA")
    assert all(raw[m]["status"] == "ok" and raw[m]["value"] is not None for m in HISTORY)    # raw is never changed


def test_view_a_statements_star_hold_after_the_build_blanks_history_rows_not_price_rows(store, cfg):
    _company(store)
    _statements(store, {"2026-03-31": 200.0, "2026-06-30": 200.0})
    _run(store, cfg)
    _hold(store, "fresh", "AAA", "*", "2026-10-01", None, "statements are stale")
    view = _scores(store, "AAA", "valuation_scores")
    assert all(view[m]["status"] == "held" for m in HISTORY)
    assert view["vs_200d"]["status"] == "ok"                                       # inputs ',close,': a price row


def test_a_hold_on_an_unrelated_field_does_not_hold_the_metric(store, cfg):
    _company(store)
    _hold(store, "figures", "AAA", "revenue_ttm", END, END, "revenue disagrees")
    rows = _run(store, cfg)
    assert rows["pe"]["status"] == "ok"
    # ps itself was not blanked (the hold names revenue_ttm, not ps): it stays ok, as valuation_latest shows it
    assert rows["ps"]["status"] == "ok"


def test_under_a_year_of_history_has_no_band(store, cfg):
    dates = _company(store, n=200)
    r = _run(store, cfg)["pe"]
    assert r["status"] == "not_enough_history" and r["value"] is not None
    assert (r["band"], r["percentile_5y"]) == (None, None)
    span = (date.fromisoformat(END) - date.fromisoformat(dates[0])).days
    assert r["status_reason"] == f"{span} days of its own history; a band needs 365"


def test_a_year_exactly_is_enough(store, cfg):
    _company(store, n=400, row=lambda i, d: {"pe": None, "status": "unsupported", "status_reason": "x"}
             if d < "2025-10-29" else None)
    assert _run(store, cfg)["pe"]["status"] == "ok"


def test_negative_earnings_is_unsupported_and_says_why(store, cfg):
    _company(store, row=lambda i, d: {"pe": None, "p_fcf": None, "ps": None, "dividend_yield": None}
             if i == 1299 else None)
    rows = _run(store, cfg)
    want = {"pe": "earnings not positive", "p_fcf": "free cash flow not positive", "ps": "no revenue",
            "dividend_yield": "no dividend"}
    for m, why in want.items():
        assert (rows[m]["status"], rows[m]["status_reason"], rows[m]["value"], rows[m]["band"]) == \
               ("unsupported", why, None, None)


def test_a_stale_row_is_unsupported_with_its_reason(store, cfg):
    _company(store, row=lambda i, d: {"status": "stale", "status_reason": "newest statements end 2025-06-30",
                                      "pe": None, "ps": None, "p_fcf": None, "market_cap": None}
             if i == 1299 else None)
    rows = _run(store, cfg)
    for m in HISTORY:
        assert (rows[m]["status"], rows[m]["status_reason"], rows[m]["value"]) == \
               ("unsupported", "newest statements end 2025-06-30", None)


def test_an_unsupported_row_is_unsupported_with_its_reason(store, cfg):
    _company(store, row=lambda i, d: {"status": "unsupported", "status_reason": "share count not in its filings",
                                      "pe": None} if i == 1299 else None)
    assert _run(store, cfg)["pe"]["status_reason"] == "share count not in its filings"


def test_a_valuation_row_behind_the_newest_close_is_unsupported(store, cfg):
    # prices moved on after the derive: the score must not stamp yesterday's P/E with today's date
    _company(store)
    store.write_prices("AAA", [PriceBar("2026-10-30", 100.0, 100.0)], "tiingo",
                       fetched_at=FETCHED.replace(day=31))
    r = _run(store, cfg)["pe"]
    assert (r["date"], r["status"], r["value"]) == ("2026-10-30", "unsupported", None)
    assert r["status_reason"] == "no valuation row on 2026-10-30; the newest is 2026-10-29"


# ---- EV/EBIT -------------------------------------------------------------------------------------------------------

def test_ev_ebit_uses_the_statements_in_force_each_day(store, cfg):    # two TTM rows, the switch on available_from
    # until the June 10-Q is public (2026-08-01) the March TTM is in force: debt 200 -> EV/EBIT 11.5; after it, 9.5
    dates = _company(store, row=lambda i, d: {"ttm_period_end": "2026-03-31", "available_from": "2026-05-01"}
                     if d < "2026-08-01" else None)
    _statements(store, {"2026-03-31": 200.0, "2026-06-30": 0.0})
    ttm = store.ttm_rows(CIK)
    assert (ttm["2026-03-31"]["available_from"], ttm["2026-06-30"]["available_from"]) == ("2026-05-01", "2026-08-01")
    r = _run(store, cfg)["ev_ebit"]
    newer = sum(1 for d in dates if d >= "2026-08-01")
    assert r["value"] == pytest.approx(9.5) and r["status"] == "ok"
    assert r["percentile_5y"] == round(100 * 0.5 * newer / len(dates), 1)       # every 11.5 sits above it
    assert r["band"] == "cheap"


def test_ev_ebit_without_a_debt_figure_is_unsupported(store, cfg):
    _company(store)
    _statements(store, {"2026-03-31": 200.0})                               # the June quarter reports no debt
    r = _run(store, cfg)["ev_ebit"]
    assert (r["status"], r["status_reason"], r["value"]) == ("unsupported", "no debt or cash figure in its filings",
                                                             None)


def test_ev_ebit_with_an_operating_loss_is_unsupported(store, cfg):
    _company(store)
    _statements(store, {"2026-03-31": 200.0, "2026-06-30": 200.0}, op_q=-10.0)
    r = _run(store, cfg)["ev_ebit"]
    assert (r["status"], r["status_reason"]) == ("unsupported", "operating income not positive")


def test_ev_ebit_counts_short_term_investments(store, cfg):
    _company(store)
    _statements(store, {"2026-03-31": 200.0, "2026-06-30": 200.0}, short_term=30.0)
    assert _run(store, cfg)["ev_ebit"]["value"] == pytest.approx((1000 + 200 - 50 - 30) / 100)


# ---- sources --------------------------------------------------------------------------------------------------------

def test_the_filing_source_names_the_10q_the_ttm_came_from(store, cfg):
    _company(store)
    store.write_sec_facts(CIK, [
        _fact("10-Q", "0000999002-26-000020", "2026-05-01", "2026-03-31"),        # another period
        _fact("10-Q", ACCN, "2026-08-01", "2026-06-30"),
        _fact("10-Q/A", "0000999002-26-000040", "2026-09-15", "2026-06-30"),      # filed later: not the first
        _fact("8-K", "0000999002-26-000019", "2026-07-20", "2026-06-30"),         # not a statement filing
    ])
    rows = _run(store, cfg)
    s = [x for x in json.loads(rows["pe"]["sources"]) if x["kind"] == "filing"][0]
    assert (s["label"], s["ref"], s["period"]) == ("10-Q", "0000999002-26-000031", "2026-06-30")
    assert s["date"] == "2026-08-01"
    assert s["url"] == "https://www.sec.gov/Archives/edgar/data/999002/000099900226000031/0000999002-26-000031-index.htm"
    assert [x["kind"] for x in json.loads(rows["pe"]["sources"])] == \
           ["price", "filing", "derived", "config", "config", "config"]


def test_without_a_known_filing_there_is_no_filing_source(store, cfg):
    _company(store)
    rows = _run(store, cfg)
    assert all(x["kind"] != "filing" for x in json.loads(rows["pe"]["sources"]))


# ---- store reads ----------------------------------------------------------------------------------------------------

def test_statement_filing_is_the_earliest_10q_or_10k(store):
    store.write_sec_facts(CIK, [_fact("10-K", "0000999002-26-000050", "2026-08-10", "2026-06-30"),
                                _fact("10-K/A", "0000999002-26-000030", "2026-07-30", "2026-06-30"),
                                _fact("8-K", "0000999002-26-000010", "2026-07-01", "2026-06-30")])
    assert store.statement_filing(CIK, "2026-06-30") == {"form": "10-K/A", "accn": "0000999002-26-000030",
                                                         "filed": "2026-07-30"}
    assert store.statement_filing(CIK, "2026-03-31") is None
    assert store.statement_filing("0000999003", "2026-06-30") is None


def test_ttm_rows_by_period_end(store):
    _statements(store, {"2026-03-31": 200.0, "2026-06-30": 0.0})
    ttm = store.ttm_rows(CIK)
    t = ttm["2026-06-30"]
    assert {"operating_income", "long_term_debt", "cash", "short_term_investments", "depreciation_amortization",
            "buybacks", "revenue", "fcf", "period_end", "available_from"} <= set(t)
    assert (t["operating_income"], t["long_term_debt"], t["cash"]) == (100.0, 0.0, 50.0)
    assert ttm["2026-03-31"]["long_term_debt"] == 200.0 and ttm["2026-03-31"]["operating_income"] == 100.0
    assert store.ttm_rows("0000999003") == {}


def test_valuation_rows_are_ascending_dicts(store):
    dates = _company(store, n=5)
    rows = store.valuation_rows("AAA")
    assert [r["date"] for r in rows] == dates and rows[0]["pe"] == 30.0 and set(V.COLUMNS) <= set(rows[0])
    assert store.valuation_rows("ZZZ") == []


# ---- what gets these scores -----------------------------------------------------------------------------------------

def test_only_stocks_with_a_cik_and_valuation_rows_get_history_scores(store, cfg):
    _company(store)
    _weekday_bars(store, "BBB")                                             # a stock with no CIK
    _weekday_bars(store, "CCC")
    store.set_security_cik("CCC", "0000999004", "Another Sample Co")         # a CIK but no valuation rows
    _weekday_bars(store, "SPY", asset_type="etf")                           # a fund
    store.set_security_cik("SPY", "0000999005", "Sample Fund Trust")
    _cover(store)
    engine.run(store, cfg)
    assert set(HISTORY) <= set(_scores(store, "AAA"))
    for sym in ("BBB", "CCC", "SPY"):
        assert not set(HISTORY) & set(_scores(store, sym))


def test_second_run_changes_no_row(store, cfg):
    _company(store)
    _statements(store, {"2026-03-31": 200.0, "2026-06-30": 200.0})
    _cover(store)
    engine.run(store, cfg)
    first = [tuple(r) for r in store.query("SELECT * FROM valuation_scores_raw ORDER BY symbol, metric")]
    before = store.conn.total_changes
    engine.run(store, cfg)
    assert [tuple(r) for r in store.query("SELECT * FROM valuation_scores_raw ORDER BY symbol, metric")] == first
    assert store.conn.total_changes == before


def test_history_scores_is_pure_and_in_metric_order():
    rows = [dict(BASE, symbol="AAA", date=d, _held=[]) for d in ("2026-10-28", "2026-10-29")]
    out = scores.history_scores("AAA", "2026-10-29", rows, {}, None, CIK, EngineSettings())
    assert [r.metric for r in out] == list(HISTORY)
    assert out[0].status == "not_enough_history" and out[0].value == 30.0
    assert out[0].status_reason == "1 days of its own history; a band needs 365"
