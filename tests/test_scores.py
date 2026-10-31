"""Company scores against their own history: P/E, P/FCF, P/S, EV/EBIT and dividend yield, each with a percentile
over the symbol's own 5-year daily history and a cheap / usual / premium band (Task 4 of the valuation engine)."""
import json
from datetime import date, datetime, timedelta, timezone

import pytest

from financial_data_collector.check import CheckContext, offline
from financial_data_collector.config import EngineSettings
from financial_data_collector.derive import engine, scores
from financial_data_collector.derive import valuation as V
from financial_data_collector.models import EarningsEvent, Estimate, Fact, LineItem, PriceBar
from tests.test_engine import (END, FETCHED, _cover, _held_position, _hold, _scores, _weekday_bars,  # noqa: F401
                               cfg, store)  # (fixtures)

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


STALE_NOW = datetime(2026, 11, 6, 12, 0, tzinfo=timezone.utc)   # a Friday: 10-30, 11-02 .. 11-05 missed since END
PRICE_SHAPE = ("off_high_52w", "vs_200d", "week_vs_market", "week_vs_sector")


def _stopped_feed_holds(store, cfg) -> list:
    """What the real closes check finds for AAA (held, priced to END) a week later, recorded as fdc check would."""
    found = offline.closes(CheckContext(store=store, cfg=cfg, fetch=None, outside=None, now=STALE_NOW))
    assert [(f.symbol, f.severity, f.field, f.date_from) for f in found] == [("AAA", "hold", "*", END)]
    store.check_record("closes", found, "2026-11-06T12:00:00Z")
    return found


def test_a_stopped_feed_holds_the_price_and_history_rows_at_build(store, cfg):          # R16
    _company(store)
    _statements(store, {"2026-03-31": 200.0, "2026-06-30": 200.0})
    _held_position(store, "AAA")
    _stopped_feed_holds(store, cfg)
    rows = _run(store, cfg)
    view = _scores(store, "AAA", "valuation_scores")
    for m in PRICE_SHAPE + HISTORY:
        for r in (rows[m], view[m]):
            assert (r["date"], r["status"], r["value"], r["band"]) == (END, "held", None, None), m
            assert "no final close for 5 trading days since 2026-10-29" in r["status_reason"]


def test_a_stopped_feed_found_after_the_build_holds_them_in_the_view(store, cfg):        # R16
    _company(store)
    _statements(store, {"2026-03-31": 200.0, "2026-06-30": 200.0})
    _held_position(store, "AAA")
    raw = _run(store, cfg)
    assert all(raw[m]["status"] != "held" for m in PRICE_SHAPE + HISTORY)
    _stopped_feed_holds(store, cfg)
    view = _scores(store, "AAA", "valuation_scores")
    for m in PRICE_SHAPE + HISTORY:
        if raw[m]["status"] == "ok":
            assert (view[m]["status"], view[m]["value"], view[m]["band"]) == ("held", None, None), m
    assert all(view[m]["status"] == "held" for m in HISTORY)
    assert view["off_high_52w"]["status"] == "held"


@pytest.mark.parametrize("newest", [0.0, 4e-13])           # float leftovers of a zero sum count as zero
def test_a_zero_yield_is_no_dividend_not_a_rank(store, cfg, newest):                     # R17 (M3)
    _company(store, row=lambda i, d: {"dividend_yield": newest, "dividends_12m": 0.0} if i == 1299 else None)
    r = _run(store, cfg)["dividend_yield"]
    assert (r["status"], r["status_reason"], r["value"], r["percentile_5y"], r["band"]) ==            ("unsupported", "no dividend in 12 months", None, None, None)


def test_only_positive_yields_are_ranked(store, cfg):                                   # R17 (M3)
    # 650 days with no dividend, 649 at 2%, the newest 1%: among the positive yields the newest is the lowest
    # (premium); counting the zeros it would sit at the 50th percentile (usual)
    _company(store, row=lambda i, d: {"dividend_yield": 0.0 if i < 650 else (0.01 if i == 1299 else 0.02)})
    r = _run(store, cfg)["dividend_yield"]
    assert (r["status"], r["band"], r["percentile_5y"]) == ("ok", "premium", 0.1)
    derived = [x for x in json.loads(r["sources"]) if x["kind"] == "derived"]
    assert derived[0]["ref"].endswith("(650 days)")


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
            "dividend_yield": "no close"}               # valuation_daily leaves a yield out only without a close
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
    for sym in ("BBB", "CCC"):
        assert not set(HISTORY) & set(_scores(store, sym))
    fund = _scores(store, "SPY")                                            # a fund: its own rows, no company history
    assert not {"p_fcf", "ps", "ev_ebit"} & set(fund)
    assert {m: fund[m]["status_reason"] for m in ("pe", "dividend_yield")} == {
        "pe": "no fund snapshot yet", "dividend_yield": "no fund snapshot yet"}


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


# ==== company fundamentals (Task 5) ==================================================================================

FUNDAMENTALS = ("implied_growth", "fcf_growth_5y", "revenue_growth_5y", "fcf_margin_change", "op_margin_change",
                "net_debt_ebitda", "buyback_yield", "last_surprise", "estimate_change_30d", "insider_net_90d")
QUARTERS = ("2024-09-30", "2024-12-31", "2025-03-31", "2025-06-30", "2025-09-30", "2025-12-31", "2026-03-31",
            "2026-06-30")
ACCN_8K = "0000999002-26-000077"


def _quarters(store, first: dict, second: dict | None = None, annual=()) -> None:
    """Quarterly line items of Sample Earnings Co: `first` ({line_item: value}) for the four quarters to 2025-06-30,
    `second` (default `first`) for the four to 2026-06-30, so full TTM rows exist for both; `annual`: (fiscal_year,
    period_end, {line_item: value}) rows."""
    items = []
    for k, e in enumerate(QUARTERS):
        vals = first if k < 4 or second is None else second
        filed = (date.fromisoformat(e) + timedelta(days=33)).isoformat()
        items += [LineItem(it, "quarter", "", e, int(e[:4]), k % 4 + 1, v, it, filed, ACCN)
                  for it, v in vals.items() if v is not None]
    for fy, end, vals in annual:
        items += [LineItem(it, "annual", "", end, fy, None, v, it, f"{fy + 1}-02-15", ACCN)
                  for it, v in vals.items() if v is not None]
    store.replace_line_items(CIK, items)


def _days_before(day: str, n: int) -> str:
    return (date.fromisoformat(day) - timedelta(days=n)).isoformat()


def _event(day: str, surprise: float | None, accession: str | None = None) -> EarningsEvent:
    return EarningsEvent("AAA", day, "after_close", accession, 1.0, 1.05 if surprise is not None else None, surprise,
                         "sec" if accession else "yahoo", "yahoo", "yahoo" if surprise is not None else None)


def _estimates(store, by_day: dict) -> None:
    for d, eps in by_day.items():
        store.write_estimates("AAA", d, [Estimate("0y", eps, None, None, 20), Estimate("0q", 9.0, None, None, 20)],
                              d + "T21:00:00Z")


def _insider(path, by_ticker, as_of="2026-10-30") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"as_of": as_of, "source": "SEC Form 4", "by_ticker": by_ticker}), encoding="utf-8")


def _entry(days: int, direction: str, accession: str | None) -> dict:
    return {"ticker": "AAA", "filed": _days_before(END, days), "accession": accession, "direction": direction,
            "url": "https://example.invalid/form4"}


# ---- pure pieces ----------------------------------------------------------------------------------------------------

def test_implied_growth_is_zero_when_the_price_needs_none():
    assert scores.implied_growth(100.0, 1307.8751, 0.09, 0.025) == pytest.approx(0.0, abs=1e-4)
    assert scores.implied_growth(100.0, 2779.5894, 0.09, 0.025) == pytest.approx(0.10, abs=1e-4)
    assert str(scores.implied_growth(100.0, 1307.8751, 0.09, 0.025)) == "0.0"         # never -0.0


def test_implied_growth_outside_the_range_or_without_fcf_is_none():
    assert scores.implied_growth(100.0, 50.0, 0.09, 0.025) is None
    assert scores.implied_growth(-5.0, 1000.0, 0.09, 0.025) is None
    assert scores.implied_growth(0.0, 1000.0, 0.09, 0.025) is None
    assert scores.implied_growth(100.0, 10.0 ** 9, 0.09, 0.025) is None           # more than 100% a year


def test_implied_growth_is_rounded_to_four_places():
    g = scores.implied_growth(40.0, 1000.0, 0.09, 0.025)
    assert g is not None and g == round(g, 4) and -0.5 < g < 1.0


def test_cagr():
    assert scores.cagr(161.051, 100.0, 5) == pytest.approx(0.10)
    assert scores.cagr(100.0, -5.0, 5) is None
    assert scores.cagr(-1.0, 100.0, 5) is None
    assert scores.cagr(None, 100.0, 5) is None and scores.cagr(100.0, None, 5) is None
    assert scores.cagr(100.0, 0.0, 5) is None


def test_read_insider_keeps_canonical_symbols_and_three_fields(tmp_path):
    p = tmp_path / "insider.json"
    _insider(p, {"aapl": [{"ticker": "AAPL", "filed": "2026-10-01", "accession": "0000999002-26-000101",
                           "direction": "buy", "url": "https://example.invalid/x"},
                          {"ticker": "AAPL", "filed": "not a date", "accession": None, "direction": "buy"},
                          "not an entry"],
                 "BRK-B": [{"filed": "2026-09-01T10:00:00", "accession": "bad", "direction": "SELL"}],
                 "XLK": "not a list"})
    by, as_of = scores.read_insider(p)
    assert as_of == "2026-10-30"
    assert by["AAPL"] == [{"filed": "2026-10-01", "accession": "0000999002-26-000101", "direction": "buy"}]
    assert by["BRK.B"] == [{"filed": "2026-09-01", "accession": "bad", "direction": "sell"}]
    assert "XLK" not in by


def test_read_insider_missing_or_unreadable_is_empty(tmp_path):
    assert scores.read_insider(tmp_path / "none.json") == ({}, None)
    bad = tmp_path / "bad.json"
    bad.write_text("{not json", encoding="utf-8")
    assert scores.read_insider(bad) == ({}, None)
    bad.write_text(json.dumps({"as_of": "2026-10-30"}), encoding="utf-8")
    assert scores.read_insider(bad) == ({}, None)


def test_fundamental_scores_is_pure_and_in_metric_order():
    newest = dict(BASE, symbol="AAA", date="2026-10-29", _held=[])
    out = scores.fundamental_scores("AAA", "2026-10-29", newest, {}, [], [], [], None, CIK, EngineSettings())
    assert [r.metric for r in out] == list(FUNDAMENTALS)
    assert all(r.percentile_5y is None and r.band is None for r in out)
    by = {r.metric: r for r in out}
    assert by["implied_growth"].status == "ok"
    assert by["insider_net_90d"].status_reason == "insider feed not found"
    assert by["fcf_margin_change"].status == "unsupported"                      # no TTM row for 2026-06-30


# ---- through the engine ---------------------------------------------------------------------------------------------

def test_every_fundamental_gets_a_row_with_its_inputs(store, cfg):
    _company(store)
    rows = _run(store, cfg)
    assert set(FUNDAMENTALS) <= set(rows)
    for m in FUNDAMENTALS:
        want = {"implied_growth": ",market_cap,fcf_ttm,close,", "buyback_yield": ",market_cap,close,",
                "fcf_margin_change": ",revenue_ttm,fcf_ttm,", "op_margin_change": ",revenue_ttm,"}.get(m, "")
        assert rows[m]["inputs"] == want, m
        assert rows[m]["percentile_5y"] is None and rows[m]["band"] is None


def test_implied_growth_from_the_newest_row(store, cfg):
    _company(store)
    r = _run(store, cfg)["implied_growth"]
    assert r["status"] == "ok" and r["value"] == scores.implied_growth(40.0, 1000.0, 0.09, 0.025)
    src = json.loads(r["sources"])
    assert src[0]["kind"] == "price"
    conf = {s["label"]: s["ref"] for s in src if s["kind"] == "config"}
    assert conf == {"[engine] discount_rate": "0.09", "[engine] terminal_growth": "0.025"}
    assert {"kind": "derived", "label": "method", "date": None,
            "ref": "10 years of FCF growth, then terminal_growth, discounted at discount_rate, = market cap"} in src


def test_implied_growth_without_positive_fcf_says_why(store, cfg):
    _company(store, row=lambda i, d: {"fcf_ttm": -4.0, "p_fcf": None})
    r = _run(store, cfg)["implied_growth"]
    assert (r["status"], r["value"], r["status_reason"]) == ("unsupported", None, "free cash flow not positive")


def test_implied_growth_out_of_range_says_why(store, cfg):
    _company(store, row=lambda i, d: {"fcf_ttm": 0.1, "p_fcf": 10000.0})     # 10000 times FCF
    r = _run(store, cfg)["implied_growth"]
    assert (r["status"], r["status_reason"]) == ("unsupported", "needs more than 100% or less than -50% a year")


def test_a_close_hold_holds_implied_growth_and_buybacks_not_margins(store, cfg):
    _company(store)
    _quarters(store, {"revenue": 100.0, "ocf": 25.0, "capex": 5.0, "operating_income": 30.0})
    _hold(store, "closes", "AAA", "close", END, END, "close looks wrong")
    rows = _run(store, cfg)
    for m in ("implied_growth", "buyback_yield"):
        assert (rows[m]["status"], rows[m]["value"], rows[m]["status_reason"]) == ("held", None, "close looks wrong")
    assert rows["fcf_margin_change"]["status"] == "ok"


def test_an_fcf_hold_holds_implied_growth_only(store, cfg):
    _company(store)
    _quarters(store, {"revenue": 100.0})
    _hold(store, "figures", "AAA", "fcf_ttm", END, END, "free cash flow disagrees")
    rows = _run(store, cfg)
    assert (rows["implied_growth"]["status"], rows["implied_growth"]["status_reason"]) == \
           ("held", "free cash flow disagrees")
    assert rows["buyback_yield"]["status"] == "ok"


def test_a_star_hold_holds_every_fundamental_that_would_be_ok(store, cfg, tmp_path):
    _company(store)
    _quarters(store, {"revenue": 100.0, "ocf": 25.0, "capex": 5.0, "operating_income": 30.0})
    store.replace_earnings("AAA", [_event("2026-07-30", 4.2)])
    cfg.investing_dir = tmp_path / "nowhere"
    _hold(store, "fresh", "AAA", "*", "2026-10-01", None, "statements are stale")
    rows = _run(store, cfg)
    for m in ("implied_growth", "buyback_yield", "fcf_margin_change", "op_margin_change", "last_surprise"):
        assert (rows[m]["status"], rows[m]["value"], rows[m]["status_reason"]) == \
               ("held", None, "statements are stale"), m
    # a row that was not ok keeps its own status and reason, as the view does
    assert rows["insider_net_90d"]["status_reason"] == "insider feed not found"
    assert rows["fcf_growth_5y"]["status"] == "not_enough_history"


def test_view_blanks_implied_growth_under_a_close_hold_after_the_build_not_margins(store, cfg):
    _company(store)
    _quarters(store, {"revenue": 100.0, "ocf": 25.0, "capex": 5.0, "operating_income": 30.0})
    _run(store, cfg)
    _hold(store, "closes", "AAA", "close", END, END, "close looks wrong")
    view = _scores(store, "AAA", "valuation_scores")
    assert view["implied_growth"]["status"] == "held" and view["buyback_yield"]["status"] == "held"
    assert view["fcf_margin_change"]["status"] == "ok"


def test_five_year_growth_compares_with_the_fiscal_year_five_lower(store, cfg):
    _company(store)
    annual = [(2019, "2019-12-31", {"revenue": 50.0, "ocf": 10.0, "capex": 1.0}),
              (2020, "2020-12-31", {"revenue": 100.0, "ocf": 30.0, "capex": 10.0})]
    annual += [(y, f"{y}-12-31", {"revenue": 120.0, "ocf": 40.0, "capex": 10.0}) for y in (2021, 2022, 2023, 2024)]
    annual += [(2025, "2025-12-31", {"revenue": 161.051, "ocf": 50.0, "capex": 10.0})]
    _quarters(store, {"revenue": 100.0}, annual=annual)
    store.write_sec_facts(CIK, [_fact("10-K", "0000999002-26-000011", "2026-02-15", "2025-12-31"),
                                _fact("10-K", "0000999002-21-000011", "2021-02-15", "2020-12-31")])
    rows = _run(store, cfg)
    r = rows["revenue_growth_5y"]
    assert r["status"] == "ok" and r["value"] == pytest.approx(0.10)
    assert rows["fcf_growth_5y"]["value"] == pytest.approx((40.0 / 20.0) ** 0.2 - 1)
    filings = [s for s in json.loads(r["sources"]) if s["kind"] == "filing"]
    assert [(s["ref"], s["period"]) for s in filings] == [("0000999002-21-000011", "2020-12-31"),
                                                         ("0000999002-26-000011", "2025-12-31")]
    assert all(s["url"].startswith("https://www.sec.gov/Archives/edgar/data/999002/") for s in filings)


def test_five_year_growth_without_the_old_year_is_not_enough_history(store, cfg):
    _company(store)
    _quarters(store, {"revenue": 100.0},
              annual=[(y, f"{y}-12-31", {"revenue": 100.0}) for y in (2022, 2023, 2024, 2025)])
    r = _run(store, cfg)["revenue_growth_5y"]
    assert (r["status"], r["status_reason"]) == ("not_enough_history", "no FY2020 statements to compare FY2025 with")


def test_five_year_growth_with_a_loss_year_is_unsupported(store, cfg):
    _company(store)
    annual = [(y, f"{y}-12-31", {"revenue": 100.0, "ocf": 10.0, "capex": 20.0}) for y in range(2020, 2026)]
    _quarters(store, {"revenue": 100.0}, annual=annual)
    r = _run(store, cfg)["fcf_growth_5y"]
    assert (r["status"], r["value"]) == ("unsupported", None)
    assert r["status_reason"] == "free cash flow not positive in FY2020 or FY2025"


def test_margins_compare_with_the_ttm_a_year_earlier(store, cfg):     # fcf margin 0.20 -> 0.15: change -0.05
    _company(store)
    _quarters(store, {"revenue": 100.0, "ocf": 25.0, "capex": 5.0, "operating_income": 30.0},
              {"revenue": 100.0, "ocf": 20.0, "capex": 5.0, "operating_income": 20.0})
    rows = _run(store, cfg)
    assert rows["fcf_margin_change"]["status"] == "ok"
    assert rows["fcf_margin_change"]["value"] == pytest.approx(-0.05)
    assert rows["op_margin_change"]["value"] == pytest.approx(-0.10)
    derived = [s for s in json.loads(rows["fcf_margin_change"]["sources"]) if s["kind"] == "derived"]
    assert derived and "2025-06-30" in derived[0]["ref"] and "2026-06-30" in derived[0]["ref"]


def test_a_revenue_hold_on_the_newest_day_holds_both_margins(store, cfg):     # R11
    _company(store)
    _quarters(store, {"revenue": 100.0, "ocf": 25.0, "capex": 5.0, "operating_income": 30.0})
    _hold(store, "figures", "AAA", "revenue_ttm", END, END, "revenue disagrees")
    rows = _run(store, cfg)
    for m in ("fcf_margin_change", "op_margin_change"):
        assert (rows[m]["status"], rows[m]["value"], rows[m]["status_reason"]) == ("held", None, "revenue disagrees")
    assert rows["net_debt_ebitda"]["status"] != "held" and rows["implied_growth"]["status"] == "ok"


def test_an_fcf_hold_holds_the_fcf_margin_not_the_operating_margin(store, cfg):
    _company(store)
    _quarters(store, {"revenue": 100.0, "ocf": 25.0, "capex": 5.0, "operating_income": 30.0})
    _hold(store, "figures", "AAA", "fcf_ttm", END, END, "free cash flow disagrees")
    rows = _run(store, cfg)
    assert rows["fcf_margin_change"]["status"] == "held" and rows["op_margin_change"]["status"] == "ok"


def test_view_blanks_both_margins_under_a_revenue_hold_after_the_build(store, cfg):
    _company(store)
    _quarters(store, {"revenue": 100.0, "ocf": 25.0, "capex": 5.0, "operating_income": 30.0})
    rows = _run(store, cfg)
    assert rows["fcf_margin_change"]["inputs"] == ",revenue_ttm,fcf_ttm,"
    assert rows["op_margin_change"]["inputs"] == ",revenue_ttm,"
    _hold(store, "figures", "AAA", "revenue_ttm", END, END, "revenue disagrees")
    view = _scores(store, "AAA", "valuation_scores")
    for m in ("fcf_margin_change", "op_margin_change"):
        assert (view[m]["status"], view[m]["value"], view[m]["status_reason"]) == ("held", None, "revenue disagrees")
    assert _scores(store, "AAA")["op_margin_change"]["status"] == "ok"                 # raw is never changed


def test_margins_without_a_ttm_a_year_earlier_are_not_enough_history(store, cfg):
    _company(store)
    _quarters(store, {}, {"revenue": 100.0, "ocf": 20.0, "capex": 5.0, "operating_income": 20.0})   # one TTM row
    r = _run(store, cfg)["op_margin_change"]
    assert (r["status"], r["status_reason"]) == \
           ("not_enough_history", "no TTM statements 350-380 days before 2026-06-30")


def test_net_debt_ebitda_needs_positive_ebitda(store, cfg):
    _company(store)
    q = {"operating_income": 25.0, "depreciation_amortization": 25.0, "long_term_debt": 300.0, "cash": 50.0,
         "short_term_investments": 50.0}
    _quarters(store, q)
    r = _run(store, cfg)["net_debt_ebitda"]
    assert r["status"] == "ok" and r["value"] == pytest.approx((300.0 - 50.0 - 50.0) / 200.0)
    _quarters(store, dict(q, operating_income=-40.0, depreciation_amortization=10.0))
    r = _run(store, cfg)["net_debt_ebitda"]
    assert (r["status"], r["value"], r["status_reason"]) == ("unsupported", None, "EBITDA not positive")
    _quarters(store, dict(q, long_term_debt=None))
    r = _run(store, cfg)["net_debt_ebitda"]
    assert (r["status"], r["status_reason"]) == ("unsupported", "no debt or cash figure in its filings")


def test_buyback_yield_counts_a_missing_line_as_zero(store, cfg):
    _company(store)
    _quarters(store, {"revenue": 100.0})
    r = _run(store, cfg)["buyback_yield"]
    assert (r["status"], r["value"]) == ("ok", 0.0)
    _quarters(store, {"revenue": 100.0, "buybacks": 10.0})
    assert _run(store, cfg)["buyback_yield"]["value"] == pytest.approx(40.0 / 1000.0)


def test_last_surprise_cites_the_8k(store, cfg):
    _company(store)
    store.replace_earnings("AAA", [_event("2026-04-29", -3.1), _event("2026-07-30", 6.74, ACCN_8K),
                                   _event("2026-10-28", None), _event("2026-11-30", 9.9)])
    r = _run(store, cfg)["last_surprise"]
    assert (r["status"], r["value"]) == ("ok", 6.74)
    src = json.loads(r["sources"])
    f = [s for s in src if s["kind"] == "filing"][0]
    assert (f["label"], f["ref"], f["date"]) == ("8-K", ACCN_8K, "2026-07-30")
    assert f["url"] == "https://www.sec.gov/Archives/edgar/data/999002/000099900226000077/0000999002-26-000077-index.htm"
    assert {"kind": "feed", "label": "Yahoo earnings dates", "date": None, "ref": "2026-07-30"} in src


def test_last_surprise_without_one_says_so(store, cfg):
    _company(store)
    store.replace_earnings("AAA", [_event("2026-10-28", None)])
    r = _run(store, cfg)["last_surprise"]
    assert (r["status"], r["status_reason"]) == ("unsupported", "no earnings surprise on record")


def test_estimate_change_needs_a_snapshot_30_days_old(store, cfg):
    _company(store)
    _estimates(store, {_days_before(END, 10): 5.0, _days_before(END, 2): 5.2, END: 5.5})
    r = _run(store, cfg)["estimate_change_30d"]
    assert (r["status"], r["value"]) == ("not_enough_history", None)
    assert r["status_reason"] == f"estimate snapshots start {_days_before(END, 10)}"
    _estimates(store, {_days_before(END, 45): 4.0, _days_before(END, 31): 5.0, "2026-11-02": 99.0})
    r = _run(store, cfg)["estimate_change_30d"]
    assert r["status"] == "ok" and r["value"] == pytest.approx(0.10)       # (5.5 - 5.0) / 5.0, not 4.0 or 99.0
    src = json.loads(r["sources"])
    assert src[0]["kind"] == "feed" and _days_before(END, 31) in src[0]["ref"] and END in src[0]["ref"]


def test_estimate_change_is_blank_when_a_report_fell_between(store, cfg):
    _company(store)
    _estimates(store, {_days_before(END, 40): 5.0, END: 5.5})
    store.replace_earnings("AAA", [_event(_days_before(END, 20), 1.0)])
    r = _run(store, cfg)["estimate_change_30d"]
    assert (r["status"], r["value"]) == ("unsupported", None)
    assert r["status_reason"] == "a report fell between the two snapshots; the year may have rolled"


def test_estimate_change_from_zero_is_unsupported(store, cfg):
    _company(store)
    _estimates(store, {_days_before(END, 40): 0.0, END: 0.5})
    r = _run(store, cfg)["estimate_change_30d"]
    assert (r["status"], r["value"]) == ("unsupported", None)


def test_estimate_change_needs_a_fresh_newest_snapshot(store, cfg):          # R9: 7 days old is fine, 8 is not
    assert scores.SNAPSHOT_MAX_AGE_DAYS == 7
    _company(store)
    now = _days_before(END, 7)
    _estimates(store, {_days_before(now, 40): 5.0, now: 5.5})
    r = _run(store, cfg)["estimate_change_30d"]
    assert r["status"] == "ok" and r["value"] == pytest.approx(0.10)
    store.conn.execute("DELETE FROM estimate_snapshots")
    store.conn.commit()
    now = _days_before(END, 8)
    _estimates(store, {_days_before(now, 40): 5.0, now: 5.5})
    r = _run(store, cfg)["estimate_change_30d"]
    assert (r["status"], r["value"], r["status_reason"]) == \
           ("unsupported", None, f"newest estimate snapshot is from {now}")


def test_estimate_change_needs_then_at_most_45_days_before_now(store, cfg):  # 45 days is fine, 46 is not
    _company(store)
    _estimates(store, {_days_before(END, 45): 5.0, END: 5.5})
    r = _run(store, cfg)["estimate_change_30d"]
    assert r["status"] == "ok" and r["value"] == pytest.approx(0.10)
    store.conn.execute("DELETE FROM estimate_snapshots")
    store.conn.commit()
    _estimates(store, {_days_before(END, 46): 5.0, _days_before(END, 10): 5.2, END: 5.5})
    r = _run(store, cfg)["estimate_change_30d"]
    assert (r["status"], r["value"], r["status_reason"]) == \
           ("not_enough_history", None, f"no estimate snapshot 30-45 days before {END}")


def test_estimate_rows_are_one_period_ascending(store):
    _weekday_bars(store, "AAA", n=5)
    _estimates(store, {"2026-10-20": 5.0, "2026-10-01": 4.0})
    assert store.estimate_rows("AAA", "0y") == [{"date": "2026-10-01", "eps_avg": 4.0},
                                                {"date": "2026-10-20", "eps_avg": 5.0}]
    assert store.estimate_rows("AAA", "+1y") == []


def test_insider_counts_buys_minus_sells_in_90_days(store, cfg, tmp_path):
    # buy, buy (an accession that does not validate: counted, no link), sell, other, a buy 120 days old -> 1
    _company(store)
    cfg.investing_dir = tmp_path / "inv"
    _insider(tmp_path / "inv" / "data" / "insider.json", {"AAA": [
        _entry(5, "buy", "0000999002-26-000201"), _entry(30, "buy", "not-an-accession"),
        _entry(60, "sell", "0000999002-26-000203"), _entry(70, "other", "0000999002-26-000204"),
        _entry(120, "buy", "0000999002-26-000205")]})
    r = _run(store, cfg)["insider_net_90d"]
    assert (r["status"], r["value"]) == ("ok", 1.0)
    src = json.loads(r["sources"])
    assert src[0] == {"kind": "feed", "label": "insider.json", "date": "2026-10-30", "ref": "2 buys, 1 sells"}
    filings = [s for s in src if s["kind"] == "filing"]
    assert [(s["label"], s["ref"], s["date"]) for s in filings] == [            # oldest first
        ("4", "0000999002-26-000203", _days_before(END, 60)), ("4", "0000999002-26-000201", _days_before(END, 5))]
    assert "example.invalid" not in r["sources"]


def test_insider_feed_missing_says_so(store, cfg, tmp_path):
    _company(store)
    cfg.investing_dir = tmp_path / "nowhere"
    r = _run(store, cfg)["insider_net_90d"]
    assert (r["status"], r["value"], r["status_reason"]) == ("unsupported", None, "insider feed not found")
    cfg.investing_dir = None
    assert _run(store, cfg)["insider_net_90d"]["status_reason"] == "insider feed not found"


def test_an_old_insider_feed_is_unsupported(store, cfg, tmp_path):           # R9: as_of 7 days old is fine, 8 is not
    _company(store)
    cfg.investing_dir = tmp_path / "inv"
    path = tmp_path / "inv" / "data" / "insider.json"
    _insider(path, {"AAA": [_entry(5, "buy", "0000999002-26-000201")]}, as_of=_days_before(END, 7) + "T06:00:00Z")
    r = _run(store, cfg)["insider_net_90d"]
    assert (r["status"], r["value"]) == ("ok", 1.0)
    _insider(path, {"AAA": [_entry(5, "buy", "0000999002-26-000201")]}, as_of=_days_before(END, 8) + "T06:00:00Z")
    r = _run(store, cfg)["insider_net_90d"]
    assert (r["status"], r["value"], r["status_reason"]) == \
           ("unsupported", None, f"insider feed is from {_days_before(END, 8)}")


def test_an_insider_feed_without_a_date_is_unsupported(store, cfg, tmp_path):
    _company(store)
    cfg.investing_dir = tmp_path / "inv"
    _insider(tmp_path / "inv" / "data" / "insider.json", {"AAA": [_entry(5, "buy", "0000999002-26-000201")]},
             as_of=None)
    r = _run(store, cfg)["insider_net_90d"]
    assert (r["status"], r["value"], r["status_reason"]) == ("unsupported", None, "insider feed has no date")


def test_a_symbol_not_in_the_insider_feed_says_so(store, cfg, tmp_path):
    _company(store)
    cfg.investing_dir = tmp_path / "inv"
    _insider(tmp_path / "inv" / "data" / "insider.json", {"AAPL": []})
    r = _run(store, cfg)["insider_net_90d"]
    assert (r["status"], r["status_reason"]) == ("unsupported", "not in the insider feed")


def test_the_insider_feed_is_read_once_per_run(store, cfg, tmp_path, monkeypatch):
    _company(store)
    _company(store, symbol="BBB")
    cfg.investing_dir = tmp_path / "inv"
    _insider(tmp_path / "inv" / "data" / "insider.json", {"AAA": []})
    calls = []
    real = scores.read_insider
    monkeypatch.setattr(scores, "read_insider", lambda p: calls.append(p) or real(p))
    _run(store, cfg)
    assert len(calls) == 1


def test_second_run_with_fundamentals_changes_no_row(store, cfg, tmp_path):
    _company(store)
    _quarters(store, {"revenue": 100.0, "ocf": 25.0, "capex": 5.0, "operating_income": 30.0, "buybacks": 3.0})
    _estimates(store, {_days_before(END, 40): 5.0, END: 5.5})
    cfg.investing_dir = tmp_path / "inv"
    _insider(tmp_path / "inv" / "data" / "insider.json", {"AAA": [_entry(5, "buy", "0000999002-26-000201")]})
    _cover(store)
    engine.run(store, cfg)
    first = [tuple(r) for r in store.query("SELECT * FROM valuation_scores_raw ORDER BY symbol, metric")]
    before = store.conn.total_changes
    engine.run(store, cfg)
    assert [tuple(r) for r in store.query("SELECT * FROM valuation_scores_raw ORDER BY symbol, metric")] == first
    assert store.conn.total_changes == before
