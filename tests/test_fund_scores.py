"""Fund scores: P/E and dividend yield against the fund's own daily snapshots (a band only once a year of them exists),
and the expense ratio as a value (Task 6 of the valuation engine)."""
import json
from datetime import date, timedelta

import pytest

from financial_data_collector.config import EngineSettings
from financial_data_collector.derive import engine, scores
from financial_data_collector.models import FundSnapshot
from tests.test_engine import (END, FETCHED, PRICE_METRICS, _cover, _hold, _scores, _weekday_bars, cfg,  # noqa: F401
                               store)

FETCHED_AT = "2026-10-29T22:00:00Z"
FUND_METRICS = ("pe", "dividend_yield", "expense_ratio")


def _days(end: str, n: int) -> list[str]:
    """n calendar days ending `end`, ascending (a snapshot is written every New York day)."""
    d = date.fromisoformat(end)
    return [(d - timedelta(days=n - 1 - i)).isoformat() for i in range(n)]


def _snapshots(store, symbol: str, days: list[str], pe=lambda i: 20.0, yld=lambda i: 0.015, expense: float | None = 0.0004,
               source: str = "yfinance") -> None:
    for i, d in enumerate(days):
        store.write_fund_snapshot(FundSnapshot(symbol, d, pe(i), None, yld(i), expense, source), FETCHED_AT)


def _fund(store, cfg, symbol: str = "VFIAX", asset_type: str = "mutual_fund", **kw) -> dict:
    _weekday_bars(store, symbol, asset_type=asset_type)
    return _build(store, cfg, symbol)


def _build(store, cfg, symbol: str) -> dict:
    _cover(store)
    engine.run(store, cfg)
    return _scores(store, symbol)


def _ramp(n: int):
    """pe 10..(10 + n - 1): the newest is the highest."""
    return lambda i: 10.0 + i


# ---- the band needs a year of the fund's own snapshots ---------------------------------------------------------------

def test_a_fund_with_eleven_months_of_snapshots_has_no_band(store, cfg):         # Review Focus 5
    _weekday_bars(store, "VFIAX", asset_type="mutual_fund")
    _snapshots(store, "VFIAX", _days(END, 335), pe=_ramp(335), yld=lambda i: 0.01 + i / 100000)
    r = _build(store, cfg, "VFIAX")
    for m in ("pe", "dividend_yield"):
        assert (r[m]["status"], r[m]["band"], r[m]["percentile_5y"]) == ("not_enough_history", None, None)
        assert r[m]["value"] is not None
        assert r[m]["status_reason"] == "334 days of its own snapshots; a band needs 365"
    assert r["pe"]["value"] == 10.0 + 334 and r["pe"]["date"] == END
    assert r["pe"]["inputs"] == ""


def test_a_year_of_snapshots_gives_a_band(store, cfg):
    _weekday_bars(store, "VFIAX", asset_type="mutual_fund")
    _snapshots(store, "VFIAX", _days(END, 366), pe=_ramp(366), yld=lambda i: 0.05 - i / 100000)
    r = _build(store, cfg, "VFIAX")
    pe, y = r["pe"], r["dividend_yield"]
    assert (pe["status"], pe["band"], pe["value"], pe["status_reason"]) == ("ok", "premium", 375.0, None)
    assert pe["percentile_5y"] == pytest.approx(100 * (365 + 0.5) / 366, abs=0.1)
    assert (y["status"], y["band"]) == ("ok", "premium")           # the yield is the lowest of its year: premium
    assert y["percentile_5y"] == pytest.approx(100 * 0.5 / 366, abs=0.1)
    src = json.loads(pe["sources"])
    assert src[0] == {"kind": "feed", "label": "fund_snapshots: yfinance", "date": END, "ref": FETCHED_AT}
    derived = [s for s in src if s["kind"] == "derived"]
    assert derived == [{"kind": "derived", "label": "fund_snapshots trailing_pe", "date": END,
                        "ref": f"{_days(END, 366)[0]}..{END} (366 days)"}]
    conf = {s["label"]: s["ref"] for s in src if s["kind"] == "config"}
    assert conf == {"[engine] history_years": "5", "[engine] cheap_percentile": "25.0",
                    "[engine] premium_percentile": "75.0"}


def test_a_cheap_fund_reads_low_pe_and_high_yield_as_cheap(store, cfg):
    _weekday_bars(store, "VFIAX", asset_type="mutual_fund")
    _snapshots(store, "VFIAX", _days(END, 400), pe=lambda i: 30.0 - i / 100 if i < 399 else 5.0,
               yld=lambda i: 0.01 if i < 399 else 0.04)
    r = _build(store, cfg, "VFIAX")
    assert (r["pe"]["band"], r["dividend_yield"]["band"]) == ("cheap", "cheap")


def test_proxy_snapshots_do_not_count_toward_own_history(store, cfg):             # Review Focus 5
    _weekday_bars(store, "VFIAX", asset_type="mutual_fund")
    days = _days(END, 430)
    _snapshots(store, "VFIAX", days[:400], source="proxy: SPY via yfinance")
    _snapshots(store, "VFIAX", days[400:], source="yfinance")
    r = _build(store, cfg, "VFIAX")
    assert (r["pe"]["status"], r["pe"]["band"]) == ("not_enough_history", None)
    assert r["pe"]["status_reason"] == "29 days of its own snapshots; a band needs 365"
    assert json.loads(r["pe"]["sources"])[0]["label"] == "fund_snapshots: yfinance"


def test_a_fund_that_switched_from_own_figures_to_a_proxy_starts_over(store, cfg):
    _weekday_bars(store, "VFIAX", asset_type="mutual_fund")
    days = _days(END, 430)
    _snapshots(store, "VFIAX", days[:200], source="yfinance")
    _snapshots(store, "VFIAX", days[200:], source="proxy: SPY via yfinance")
    r = _build(store, cfg, "VFIAX")
    assert r["pe"]["status_reason"] == "229 days of its own snapshots; a band needs 365"
    assert json.loads(r["pe"]["sources"])[0]["label"] == "fund_snapshots: proxy: SPY via yfinance"


def test_a_day_without_a_pe_is_left_out_of_that_metrics_history(store, cfg):
    _weekday_bars(store, "VFIAX", asset_type="mutual_fund")
    days = _days(END, 400)
    _snapshots(store, "VFIAX", days, pe=lambda i: None if i < 100 else 20.0)
    r = _build(store, cfg, "VFIAX")
    assert r["pe"]["status_reason"] == "299 days of its own snapshots; a band needs 365"   # starts at the 101st day
    assert r["dividend_yield"]["status"] == "ok"                                          # its yield has all 400


# ---- the expense ratio -----------------------------------------------------------------------------------------------

def test_the_expense_ratio_is_a_value_only(store, cfg):
    _weekday_bars(store, "VFIAX", asset_type="mutual_fund")
    _snapshots(store, "VFIAX", _days(END, 3), expense=0.0004)
    r = _build(store, cfg, "VFIAX")["expense_ratio"]
    assert (r["status"], r["value"], r["band"], r["percentile_5y"], r["status_reason"]) == ("ok", 0.0004, None, None, None)
    assert r["inputs"] == ""
    assert [s["kind"] for s in json.loads(r["sources"])] == ["feed"]


def test_a_missing_figure_is_unsupported_and_says_which(store, cfg):
    _weekday_bars(store, "VFIAX", asset_type="mutual_fund")
    _snapshots(store, "VFIAX", _days(END, 3), pe=lambda i: None, yld=lambda i: None, expense=None)
    r = _build(store, cfg, "VFIAX")
    assert {m: (r[m]["status"], r[m]["status_reason"]) for m in FUND_METRICS} == {
        "pe": ("unsupported", "no trailing P/E in the newest snapshot"),
        "dividend_yield": ("unsupported", "no dividend yield in the newest snapshot"),
        "expense_ratio": ("unsupported", "no expense ratio in the newest snapshot")}


# ---- stale or missing snapshots --------------------------------------------------------------------------------------

def test_an_old_snapshot_is_unsupported(store, cfg):
    _weekday_bars(store, "VFIAX", asset_type="mutual_fund")
    _snapshots(store, "VFIAX", _days("2026-10-20", 400))
    r = _build(store, cfg, "VFIAX")
    for m in FUND_METRICS:
        assert (r[m]["status"], r[m]["value"], r[m]["band"]) == ("unsupported", None, None)
        assert r[m]["status_reason"] == "newest snapshot is from 2026-10-20"
    assert json.loads(r["pe"]["sources"])[0]["date"] == "2026-10-20"


def test_a_snapshot_seven_days_old_is_still_used(store, cfg):
    _weekday_bars(store, "VFIAX", asset_type="mutual_fund")
    _snapshots(store, "VFIAX", _days("2026-10-22", 3))
    assert END == "2026-10-29"
    assert _build(store, cfg, "VFIAX")["pe"]["status"] == "not_enough_history"


def test_a_fund_with_no_snapshot_yet(store, cfg):
    r = _fund(store, cfg)
    for m in FUND_METRICS:
        assert (r[m]["status"], r[m]["status_reason"], r[m]["value"]) == ("unsupported", "no fund snapshot yet", None)
        assert json.loads(r[m]["sources"]) == []


def test_a_snapshot_after_the_day_is_ignored(store, cfg):
    _weekday_bars(store, "VFIAX", asset_type="mutual_fund")
    _snapshots(store, "VFIAX", ["2026-10-30"])
    assert _build(store, cfg, "VFIAX")["pe"]["status_reason"] == "no fund snapshot yet"


# ---- the engine ------------------------------------------------------------------------------------------------------

def test_a_fund_gets_price_shape_too(store, cfg):
    _weekday_bars(store, "SPY", asset_type="etf")
    store.mark_benchmarks(["SPY"])
    r = _fund(store, cfg, "VFIAX")
    assert set(r) == set(PRICE_METRICS) | set(FUND_METRICS)
    assert r["off_high_52w"]["status"] == "ok"


@pytest.mark.parametrize("asset_type", ["etf", "mutual_fund", "fund"])
def test_every_fund_type_is_scored(store, cfg, asset_type):
    _weekday_bars(store, "VFIAX", asset_type=asset_type)
    _snapshots(store, "VFIAX", _days(END, 3))
    assert set(_build(store, cfg, "VFIAX")) >= set(FUND_METRICS)


def test_a_stock_gets_no_fund_rows(store, cfg):
    _weekday_bars(store, "AAA")
    _snapshots(store, "AAA", _days(END, 3))
    assert not set(_build(store, cfg, "AAA")) & set(FUND_METRICS)


def test_a_star_hold_covering_the_day_holds_the_fund_rows(store, cfg):
    _weekday_bars(store, "VFIAX", asset_type="mutual_fund")
    _snapshots(store, "VFIAX", _days(END, 400), pe=_ramp(400))
    _hold(store, "fresh", "VFIAX", "*", END, None, "stale feed")
    r = _build(store, cfg, "VFIAX")
    for m in FUND_METRICS:
        assert (r[m]["status"], r[m]["status_reason"]) == ("held", "stale feed")
        assert (r[m]["value"], r[m]["percentile_5y"], r[m]["band"]) == (None, None, None)
    # the same hold seen through the view
    v = _scores(store, "VFIAX", "valuation_scores")
    assert all(v[m]["status"] == "held" for m in FUND_METRICS)


def test_a_hold_on_another_day_does_not_hold_the_fund_rows(store, cfg):
    _weekday_bars(store, "VFIAX", asset_type="mutual_fund")
    _snapshots(store, "VFIAX", _days(END, 400))
    _hold(store, "fresh", "VFIAX", "*", "2026-01-01", "2026-01-10")
    assert _build(store, cfg, "VFIAX")["pe"]["status"] == "ok"


def test_a_score_hold_opened_after_the_build_reaches_the_view_only(store, cfg):
    _weekday_bars(store, "VFIAX", asset_type="mutual_fund")
    _snapshots(store, "VFIAX", _days(END, 400))
    assert _build(store, cfg, "VFIAX")["pe"]["status"] == "ok"
    _hold(store, "scores", "VFIAX", "score:pe", END, None, "odd P/E")
    assert _scores(store, "VFIAX")["pe"]["status"] == "ok"
    v = _scores(store, "VFIAX", "valuation_scores")
    assert (v["pe"]["status"], v["pe"]["value"]) == ("held", None) and v["dividend_yield"]["status"] == "ok"


def test_a_second_run_rewrites_no_fund_row(store, cfg):
    _weekday_bars(store, "VFIAX", asset_type="mutual_fund")
    _snapshots(store, "VFIAX", _days(END, 400))
    _build(store, cfg, "VFIAX")
    before = store.conn.total_changes
    engine.run(store, cfg)
    assert store.conn.total_changes == before


def test_fund_snapshot_rows_are_ascending_dicts(store):
    _snapshots(store, "VFIAX", ["2026-10-02", "2026-10-01"], expense=0.0004)
    rows = store.fund_snapshot_rows("VFIAX")
    assert [r["date"] for r in rows] == ["2026-10-01", "2026-10-02"]
    assert set(rows[0]) == {"date", "trailing_pe", "dividend_yield", "expense_ratio", "source", "fetched_at"}
    assert store.fund_snapshot_rows("NOPE") == []


def test_fund_scores_is_pure(store):
    s = EngineSettings()
    snaps = [{"date": "2026-10-28", "trailing_pe": 20.0, "dividend_yield": None, "expense_ratio": 0.001,
              "source": "yfinance", "fetched_at": FETCHED_AT}]
    rows = scores.fund_scores("VFIAX", "2026-10-29", snaps, s)
    assert [r.metric for r in rows] == list(FUND_METRICS)
    assert [r.status for r in rows] == ["not_enough_history", "unsupported", "ok"]
    assert all(r.inputs == () for r in rows)
