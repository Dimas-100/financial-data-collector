"""The offline checks: fresh enough, impossible values, gaps, recompute. Each rule has its spec value."""
import inspect
from dataclasses import replace
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pytest

from financial_data_collector import check, config as C
from financial_data_collector.check import CheckContext, calendar as nyse, offline
from financial_data_collector.derive import valuation as V
from financial_data_collector.models import AccountRef, PositionRow, PriceBar, Snapshot
from financial_data_collector.store import Store

NOW = datetime(2026, 10, 29, 12, 0, tzinfo=timezone.utc)      # a Thursday
APRIL = datetime(2026, 4, 20, 12, 0, tzinfo=timezone.utc)     # the Monday after Good Friday 2026-04-03


@pytest.fixture
def cfg(tmp_path: Path):
    C.init_project(tmp_path)
    Store.open(tmp_path / "data" / "warehouse.db").close()
    return C.load_config(tmp_path)


@pytest.fixture
def store(cfg):
    s = Store.open(cfg.db_path, migrate=False)
    yield s
    s.close()


def _ctx(store, cfg, now=NOW):
    return CheckContext(store=store, cfg=cfg, fetch=None, outside=None, now=now)


def _row(symbol="AAA", d="2026-10-29", **kw):
    base = dict(symbol=symbol, date=d, close=100.0, shares=None, market_cap=None, revenue_ttm=None,
                net_income_ttm=None, ocf_ttm=None, fcf_ttm=None, eps_ttm=None, pe=None, ps=None, p_fcf=None,
                dividends_12m=None, dividend_yield=None, ttm_period_end="2026-06-30", available_from="2026-08-01",
                status="ok", status_reason=None)
    base.update(kw)
    return tuple(base[c] for c in V.COLUMNS)


def _valuation(store, *rows):
    store.replace_rows("valuation_daily", V.COLUMNS, rows)


def _security(store, symbol, asset_type="stock"):
    store.upsert_security(symbol, symbol, asset_type)


def _hold(store, symbol, qty=10.0):
    acct = AccountRef(label="Test account", institution="fidelity", account_type="brokerage")
    store.write_snapshot(Snapshot("2026-10-28", "test", [PositionRow(acct, symbol, symbol, qty, 100.0, qty * 100.0)], []))


def _hold_all(store, *symbols):
    """One snapshot holding all of `symbols` (a second _hold would replace the first's positions)."""
    acct = AccountRef(label="Test account", institution="fidelity", account_type="brokerage")
    store.write_snapshot(Snapshot("2026-10-28", "test", [PositionRow(acct, s, s, 10.0, 100.0, 1000.0) for s in symbols], []))


def _bars(store, symbol, closes, start="2026-09-01", splits=None):
    """Consecutive weekdays from `start`, one final bar per close; splits = {index: factor}."""
    splits = splits or {}
    d, out = date.fromisoformat(start), []
    for i, c in enumerate(closes):
        while d.weekday() >= 5:
            d += timedelta(days=1)
        out.append(PriceBar(d.isoformat(), c, c, 0.0, splits.get(i, 1.0)))
        d += timedelta(days=1)
    store.write_prices(symbol, out, "test")
    return [b.date for b in out]


def _weekdays(start: date, end: date, skip=()):
    d, out = start, []
    while d <= end:
        if d.weekday() < 5 and d.isoformat() not in skip:
            out.append(d.isoformat())
        d += timedelta(days=1)
    return out


def _bars_on(store, symbol, days):
    store.write_prices(symbol, [PriceBar(d, 100.0, 100.0) for d in days], "test")


# ---- fresh -------------------------------------------------------------------------------------

def test_fresh_warns_after_5_months_holds_after_15(store, cfg):
    for s in ("WARN", "HOLD", "FINE", "EDGE"):
        _security(store, s)
    _valuation(store,
               _row("WARN", ttm_period_end="2026-04-30"),
               _row("HOLD", ttm_period_end="2025-06-30"),
               _row("FINE", ttm_period_end="2026-08-31"),
               _row("EDGE", ttm_period_end="2026-05-29"))        # +5 months is 2026-10-29 itself: not more than
    found = {f.symbol: f for f in offline.fresh(_ctx(store, cfg))}
    assert set(found) == {"WARN", "HOLD"}
    warn, hold = found["WARN"], found["HOLD"]
    assert (warn.check_name, warn.severity, warn.field, warn.date_from, warn.date_to) == ("fresh", "warn", "*", "2026-09-30", None)
    assert (hold.check_name, hold.severity, hold.field, hold.date_from, hold.date_to) == ("fresh", "hold", "*", "2026-09-30", None)
    assert "2025-06-30" in hold.message and "16 months" in hold.message and "2026-10-29" in hold.message


def test_fresh_clamps_the_day_and_uses_a_stale_row_but_not_an_unsupported_one(store, cfg):
    for s in ("STALE", "NONE"):
        _security(store, s)
    _valuation(store,
               _row("STALE", ttm_period_end="2025-05-31", status="stale", status_reason="old"),   # +15 months: 2026-08-31
               _row("NONE", ttm_period_end="2020-01-31", status="unsupported", status_reason="fund"))
    (f,) = offline.fresh(_ctx(store, cfg))
    assert (f.symbol, f.severity, f.date_from) == ("STALE", "hold", "2026-08-31")


def test_fresh_clamps_the_day_when_the_month_is_shorter(store, cfg):
    _security(store, "AAA")
    _valuation(store, _row("AAA", ttm_period_end="2026-01-31"))          # +5 months: June has 30 days
    (f,) = offline.fresh(_ctx(store, cfg))
    assert (f.severity, f.date_from) == ("warn", "2026-06-30")


def test_fresh_thresholds_come_from_config(store, cfg):
    _security(store, "AAA")
    _valuation(store, _row("AAA", ttm_period_end="2026-06-30"))              # four months old
    assert offline.fresh(_ctx(store, cfg)) == []
    cfg2 = replace(cfg, check=replace(cfg.check, fresh_warn_months=3))
    (f,) = offline.fresh(_ctx(store, cfg2))
    assert (f.severity, f.date_from) == ("warn", "2026-09-30")


# ---- impossible --------------------------------------------------------------------------------

def test_impossible_ratio_on_a_fund_holds(store, cfg):
    for s, t in (("ETFX", "etf"), ("MFUND", "mutual_fund"), ("COIN", "crypto"), ("FOR", "foreign_filer"),
                 ("FUNDX", "fund"), ("MM", "money_market"), ("CORP", "stock"), ("QUIET", "etf")):
        _security(store, s, t)
    _valuation(store,
               _row("ETFX", pe=20.0),
               _row("MFUND", ps=2.0),
               _row("COIN", market_cap=1e9),
               _row("FOR", p_fcf=10.0),
               _row("FUNDX", pe=1.0),
               _row("MM", ps=1.0),
               _row("CORP", pe=20.0, ps=2.0, market_cap=5e9),              # a company may have ratios
               _row("QUIET"))                                              # a fund with none is right
    found = offline.impossible(_ctx(store, cfg))
    assert sorted(f.symbol for f in found) == ["COIN", "ETFX", "FOR", "FUNDX", "MFUND", "MM"]
    for f in found:
        assert (f.check_name, f.field, f.severity, f.date_from) == ("impossible", "*", "hold", "2026-10-29")
    etf = next(f for f in found if f.symbol == "ETFX")
    assert "etf" in etf.message and "pe" in etf.message


def test_impossible_negative_shares_holds(store, cfg):
    _security(store, "AAA")
    _valuation(store, _row("AAA", shares=-5.0))
    (f,) = offline.impossible(_ctx(store, cfg))
    assert (f.symbol, f.field, f.severity, f.date_from) == ("AAA", "*", "hold", "2026-10-29")
    assert "shares" in f.message


def test_impossible_findings_on_one_row_share_one_finding(store, cfg):
    _security(store, "ETFX", "etf")
    _valuation(store, _row("ETFX", pe=20.0, shares=-1.0))
    (f,) = offline.impossible(_ctx(store, cfg))
    assert "pe" in f.message and "shares" in f.message


def test_impossible_one_day_move_over_40pct_without_split_holds(store, cfg):
    for s in ("UP", "DOWN", "OK"):
        _security(store, s)
    _valuation(store, *[_row(s) for s in ("UP", "DOWN", "OK")])       # covered: they have a valuation row
    up = _bars(store, "UP", [100, 101, 150, 151])
    down = _bars(store, "DOWN", [100, 100, 55])
    _bars(store, "OK", [100, 139, 100])                                  # +39%, then -28%: both under
    found = {f.symbol: f for f in offline.impossible(_ctx(store, cfg))}
    assert set(found) == {"UP", "DOWN"}
    f = found["UP"]
    assert (f.severity, f.field, f.date_from, f.date_to) == ("hold", "*", up[2], up[2])
    assert found["DOWN"].date_from == found["DOWN"].date_to == down[2]
    assert "101" in f.message and "150" in f.message and up[2] in f.message


def test_impossible_move_on_a_split_day_is_fine(store, cfg):
    _security(store, "SPLIT"), _security(store, "REV")
    _valuation(store, _row("SPLIT"), _row("REV"))
    _bars(store, "SPLIT", [100, 100, 50, 50], splits={2: 2.0})           # a 2-for-1 halves the close
    _bars(store, "REV", [10, 10, 40], splits={2: 0.25})                  # a 1-for-4 reverse split
    assert offline.impossible(_ctx(store, cfg)) == []


def test_impossible_looks_back_over_the_last_30_final_bars_only(store, cfg):
    _security(store, "OLD"), _security(store, "NEWER")
    _valuation(store, _row("OLD"), _row("NEWER"))
    _bars(store, "OLD", [100.0] + [200.0] * 30, start="2026-08-03")      # 31 bars: the jump is before the last 30
    assert offline.impossible(_ctx(store, cfg)) == []
    _bars(store, "NEWER", [100.0] + [200.0] * 29, start="2026-08-03")    # 30 bars: the jump is inside
    assert [f.symbol for f in offline.impossible(_ctx(store, cfg))] == ["NEWER"]


def test_impossible_threshold_comes_from_config(store, cfg):
    _security(store, "AAA")
    _valuation(store, _row("AAA"))
    _bars(store, "AAA", [100, 130])
    assert offline.impossible(_ctx(store, cfg)) == []
    cfg2 = replace(cfg, check=replace(cfg.check, max_daily_move=0.25))
    assert [f.symbol for f in offline.impossible(_ctx(store, cfg2))] == ["AAA"]


# ---- gaps --------------------------------------------------------------------------------------

def test_gap_on_a_trading_day_warns_but_not_on_good_friday(store, cfg):
    for s in ("FULL", "GAPPY"):
        _security(store, s)
        _hold(store, s)
    holidays = ("2026-02-16", "2026-04-03")                              # Presidents' Day, Good Friday
    _bars_on(store, "FULL", _weekdays(date(2026, 1, 5), date(2026, 4, 17), skip=holidays))
    _bars_on(store, "GAPPY", _weekdays(date(2026, 1, 5), date(2026, 4, 17), skip=holidays + ("2026-03-12", "2026-03-13")))
    found = offline.gaps(_ctx(store, cfg, APRIL))
    assert [f.symbol for f in found] == ["GAPPY"]
    f = found[0]
    assert (f.check_name, f.severity, f.field, f.date_from) == ("gaps", "warn", "close", "2026-03-12")
    assert f.evidence["missing"] == ["2026-03-12", "2026-03-13"]
    assert "2026-03-12" in f.message


def test_gaps_ignore_days_before_the_first_price_and_before_the_window(store, cfg):
    _security(store, "NEW"), _hold(store, "NEW")
    _bars_on(store, "NEW", _weekdays(date(2026, 4, 1), date(2026, 4, 17), skip=("2026-04-03",)))    # starts mid-window
    _security(store, "OLDGAP"), _hold(store, "OLDGAP")
    _bars_on(store, "OLDGAP", _weekdays(date(2025, 12, 1), date(2026, 4, 17), skip=("2026-04-03", "2026-02-16", "2026-01-13")))
    assert offline.gaps(_ctx(store, cfg, APRIL)) == []                   # Jan 13 is older than the 60 trading days


def test_gaps_count_only_days_before_the_newest_final_bar(store, cfg):
    _security(store, "AAA"), _hold(store, "AAA")
    # Thursday the 9th is a hole; Friday the 17th has no bar yet because no sync ran after its close
    _bars_on(store, "AAA", _weekdays(date(2026, 4, 1), date(2026, 4, 16), skip=("2026-04-03", "2026-04-09")))
    (f,) = offline.gaps(_ctx(store, cfg, APRIL))
    assert f.evidence["missing"] == ["2026-04-09"] and f.date_to == "2026-04-09"
    store.write_prices("AAA", [PriceBar("2026-04-17", 100.0, 100.0)], "test",
                       fetched_at=datetime(2026, 4, 17, 15, 0, tzinfo=timezone.utc))   # before the close: not final
    (f,) = offline.gaps(_ctx(store, cfg, APRIL))
    assert f.evidence["missing"] == ["2026-04-09"]
    # a non-final bar inside the range does not fill its day: that day is still missing
    store.write_prices("AAA", [PriceBar("2026-04-09", 100.0, 100.0)], "test",
                       fetched_at=datetime(2026, 4, 9, 15, 0, tzinfo=timezone.utc))
    (f,) = offline.gaps(_ctx(store, cfg, APRIL))
    assert f.evidence["missing"] == ["2026-04-09"]


def test_gaps_say_nothing_about_the_days_after_the_last_sync(store, cfg):
    _security(store, "AAA"), _hold(store, "AAA")
    _bars_on(store, "AAA", _weekdays(date(2026, 4, 1), date(2026, 4, 15), skip=("2026-04-03",)))     # 16th, 17th not yet
    assert offline.gaps(_ctx(store, cfg, APRIL)) == []


def test_gaps_today_is_never_counted_even_with_a_final_bar_on_it(store, cfg):
    _security(store, "AAA"), _hold(store, "AAA")
    _bars_on(store, "AAA", _weekdays(date(2026, 4, 1), date(2026, 4, 20), skip=("2026-04-03", "2026-04-17")))
    (f,) = offline.gaps(_ctx(store, cfg, datetime(2026, 4, 20, 23, 0, tzinfo=timezone.utc)))
    assert f.evidence["missing"] == ["2026-04-17"]                      # the 17th: before the newest bar (the 20th)


def test_impossible_move_also_covers_a_held_symbol_with_no_valuation_row(store, cfg):
    _security(store, "HELD"), _hold(store, "HELD")
    _bars(store, "HELD", [100, 200])
    assert [f.symbol for f in offline.impossible(_ctx(store, cfg))] == ["HELD"]
    _security(store, "LOOSE")
    _bars(store, "LOOSE", [100, 200])                                   # neither held nor valued: not covered
    assert [f.symbol for f in offline.impossible(_ctx(store, cfg))] == ["HELD"]


def test_gaps_only_cover_held_symbols(store, cfg):
    _security(store, "NOTHELD")
    _bars_on(store, "NOTHELD", ["2026-04-01", "2026-04-15"])
    assert offline.gaps(_ctx(store, cfg, APRIL)) == []


# ---- closes ------------------------------------------------------------------------------------

THU = datetime(2026, 10, 29, 12, 0, tzinfo=timezone.utc)      # 08:00 New York


def _priced_to(store, symbol, last, start="2026-10-01"):
    """Final bars on every weekday from `start` to `last`."""
    _bars_on(store, symbol, _weekdays(date.fromisoformat(start), date.fromisoformat(last)))


def test_a_close_two_trading_days_old_warns(store, cfg):
    _security(store, "AAA"), _hold(store, "AAA")
    _priced_to(store, "AAA", "2026-10-26")                         # Tue and Wed missed (Thu is today)
    (f,) = offline.closes(_ctx(store, cfg, THU))
    assert (f.check_name, f.symbol, f.severity, f.field, f.date_from, f.date_to) ==            ("closes", "AAA", "warn", "close", "2026-10-27", None)
    assert f.evidence == {"newest": "2026-10-26", "missed": ["2026-10-27", "2026-10-28"]}
    assert f.message == ("newest final close is from 2026-10-26, 2 trading days ago (the feed may have stopped)")


def test_yesterdays_close_is_fine(store, cfg):
    _security(store, "AAA"), _security(store, "BBB"), _hold_all(store, "AAA", "BBB")
    _priced_to(store, "AAA", "2026-10-28")
    _priced_to(store, "BBB", "2026-10-27")                         # one missed day: below the warning
    assert offline.closes(_ctx(store, cfg, THU)) == []


def test_five_missed_trading_days_hold_every_value(store, cfg):
    _security(store, "AAA"), _hold(store, "AAA")
    _priced_to(store, "AAA", "2026-10-21")                         # 22, 23, 26, 27, 28 missed
    (f,) = offline.closes(_ctx(store, cfg, THU))
    assert (f.severity, f.field, f.date_from) == ("hold", "*", "2026-10-22")
    assert f.evidence["missed"] == ["2026-10-22", "2026-10-23", "2026-10-26", "2026-10-27", "2026-10-28"]
    assert "5 trading days ago" in f.message
    _security(store, "BBB"), _hold_all(store, "AAA", "BBB")
    _priced_to(store, "BBB", "2026-10-22")                         # four missed: still only a warning
    assert [(g.symbol, g.severity) for g in offline.closes(_ctx(store, cfg, THU))] == [("AAA", "hold"), ("BBB", "warn")]


def test_good_friday_and_the_weekend_are_not_missed_days(store, cfg):
    _security(store, "AAA"), _hold(store, "AAA")
    _priced_to(store, "AAA", "2026-04-02", start="2026-03-02")      # Thursday; Good Friday 04-03 is shut
    assert offline.closes(_ctx(store, cfg, datetime(2026, 4, 6, 12, 0, tzinfo=timezone.utc))) == []


def test_today_is_the_new_york_date(store, cfg):
    _security(store, "AAA"), _hold(store, "AAA")
    _priced_to(store, "AAA", "2026-10-26")
    late = datetime(2026, 10, 29, 2, 0, tzinfo=timezone.utc)       # 22:00 on the 28th in New York: the 28th is today
    assert offline.closes(_ctx(store, cfg, late)) == []            # only the 27th is missed (by UTC date it would be two)


def test_only_a_non_final_bar_is_not_never_priced(store, cfg):
    _security(store, "AAA"), _hold(store, "AAA")
    store.write_prices("AAA", [PriceBar("2026-10-28", 100.0, 100.0)], "test",
                       fetched_at=datetime(2026, 10, 28, 15, 0, tzinfo=timezone.utc))      # before the close
    (f,) = offline.closes(_ctx(store, cfg, THU))
    assert (f.severity, f.field, f.date_from) == ("warn", "close", "2026-10-28")
    assert f.message == "no final closing price yet, only a bar fetched before the close"


def test_held_never_priced_warns_with_a_stable_date(store, cfg):
    store.upsert_security("NEWCO", "NewCo", "stock", first_seen="2026-10-20")
    _hold(store, "NEWCO")
    (f,) = offline.closes(_ctx(store, cfg, THU))
    assert (f.symbol, f.severity, f.field, f.date_from) == ("NEWCO", "warn", "close", "2026-10-20")
    assert f.date_from == store.check_first_seen()["NEWCO"]
    assert f.message == "held, never priced: no price from any provider"
    (g,) = offline.closes(_ctx(store, cfg, THU + timedelta(days=1)))
    assert g.date_from == f.date_from                              # the same finding tomorrow


def test_benchmarks_are_covered_and_money_markets_are_not(store, cfg):
    store.mark_benchmarks(["SPY"])
    _security(store, "CASH", "money_market"), _security(store, "COIN", "crypto")
    _hold_all(store, "CASH", "COIN")                               # held, never priced, but not covered
    (f,) = offline.closes(_ctx(store, cfg, THU))
    assert (f.symbol, f.severity, f.message) == ("SPY", "warn", "benchmark, never priced: no price from any provider")
    _priced_to(store, "SPY", "2026-10-21")
    (f,) = offline.closes(_ctx(store, cfg, THU))
    assert (f.symbol, f.severity) == ("SPY", "hold")


def test_a_symbol_both_held_and_a_benchmark_is_checked_once_with_the_held_wording(store, cfg):
    store.mark_benchmarks(["SPY"])
    _hold(store, "SPY")
    (f,) = offline.closes(_ctx(store, cfg, THU))
    assert f.message == "held, never priced: no price from any provider"


def test_thresholds_come_from_config(store, cfg):
    _security(store, "AAA"), _hold(store, "AAA")
    _priced_to(store, "AAA", "2026-10-26")                         # two missed days
    assert offline.closes(_ctx(store, replace(cfg, check=replace(cfg.check, close_warn_days=3)), THU)) == []
    (f,) = offline.closes(_ctx(store, replace(cfg, check=replace(cfg.check, close_warn_days=1, close_hold_days=2)), THU))
    assert f.severity == "hold"


# ---- recompute ---------------------------------------------------------------------------------

def _full(**kw):
    base = dict(close=100.5, shares=10.0, market_cap=1005.0, revenue_ttm=500.0, fcf_ttm=50.0, eps_ttm=5.0,
                pe=20.1, ps=1005.0 / 500.0, p_fcf=1005.0 / 50.0, dividends_12m=2.0, dividend_yield=2.0 / 100.5)
    base.update(kw)
    return base


def test_recompute_matches_within_1pct_and_holds_beyond(store, cfg):
    for s in ("GOOD", "NEAR", "BAD"):
        _security(store, s)
    _valuation(store, _row("GOOD", **_full()), _row("NEAR", **_full(pe=20.0)), _row("BAD", **_full(pe=25.0)))
    found = offline.recompute(_ctx(store, cfg))
    assert [(f.symbol, f.field, f.severity, f.date_from) for f in found] == [("BAD", "pe", "hold", "2026-10-29")]
    ev = found[0].evidence
    assert ev["stored"] == 25.0 and ev["recomputed"] == pytest.approx(20.1)
    assert ev["inputs"] == {"close": 100.5, "eps_ttm": 5.0}
    assert found[0].check_name == "recompute" and "25" in found[0].message and "20.1" in found[0].message


def test_recompute_checks_each_metric(store, cfg):
    _security(store, "AAA")
    _valuation(store, _row("AAA", **_full(ps=3.0, p_fcf=30.0, market_cap=2000.0, dividend_yield=0.05)))
    found = offline.recompute(_ctx(store, cfg))
    assert sorted(f.field for f in found) == ["dividend_yield", "market_cap", "p_fcf", "ps"]
    assert next(f for f in found if f.field == "market_cap").evidence["inputs"] == {"close": 100.5, "shares": 10.0}


def test_recompute_stored_but_impossible_to_recompute_holds(store, cfg):
    _security(store, "AAA")
    _valuation(store, _row("AAA", **_full(eps_ttm=-1.0, pe=20.1)))       # a pe cannot exist on a loss
    (f,) = offline.recompute(_ctx(store, cfg))
    assert (f.field, f.severity) == ("pe", "hold")
    assert f.evidence["stored"] == 20.1 and f.evidence["recomputed"] is None


def test_recompute_skips_what_is_not_stored_and_rows_that_are_not_ok(store, cfg):
    for s in ("NONE", "STALE"):
        _security(store, s)
    _valuation(store,
               _row("NONE", close=100.0, eps_ttm=5.0, shares=10.0),      # nothing stored: nothing to compare
               _row("STALE", status="stale", pe=99.0, eps_ttm=5.0))
    assert offline.recompute(_ctx(store, cfg)) == []


def test_recompute_tolerance_comes_from_config(store, cfg):
    _security(store, "AAA")
    _valuation(store, _row("AAA", **_full(pe=20.0)))
    assert offline.recompute(_ctx(store, cfg)) == []
    cfg2 = replace(cfg, check=replace(cfg.check, recompute_tolerance=0.001))
    assert [f.field for f in offline.recompute(_ctx(store, cfg2))] == ["pe"]


def test_recompute_reads_no_shaped_view():
    assert "financials_ttm" not in inspect.getsource(offline)


# ---- registration ------------------------------------------------------------------------------

def test_the_four_checks_are_registered():
    assert {"fresh", "impossible", "gaps", "recompute"} <= set(check.CHECKS)
    assert check.CHECKS["fresh"] is offline.fresh


def test_closes_is_registered_after_gaps():
    names = list(check.CHECKS)
    assert names.index("closes") == names.index("gaps") + 1
    assert check.CHECKS["closes"] is offline.closes


def test_the_checks_run_end_to_end_and_record_findings(cfg, store):
    _security(store, "AAA")
    _valuation(store, _row("AAA", ttm_period_end="2025-06-30"))
    store.close()
    rep = check.run_check(cfg, now=NOW, only=["fresh", "impossible", "gaps", "recompute"])
    assert rep.errors == [] and rep.holds == 1
    s = Store.open(cfg.db_path, migrate=False)
    try:
        (r,) = s.query("SELECT check_name, symbol, field, severity FROM check_findings")
        assert tuple(r) == ("fresh", "AAA", "*", "hold")
    finally:
        s.close()


# ---- the calendar ------------------------------------------------------------------------------

def _closed(year):
    return sorted(d.isoformat() for d in nyse.holidays(year))


def test_nyse_holidays_2026():
    assert _closed(2026) == ["2026-01-01", "2026-01-19", "2026-02-16", "2026-04-03", "2026-05-25", "2026-06-19",
                             "2026-07-03", "2026-09-07", "2026-11-26", "2026-12-25"]


def test_nyse_holiday_observance_rules():
    assert "2021-12-31" not in _closed(2021) and "2022-01-01" not in _closed(2022)     # Saturday New Year's: no Friday off
    assert "2021-07-05" in _closed(2021)                                               # Sunday July 4 -> Monday
    assert "2027-12-24" in _closed(2027)                                               # Saturday Christmas -> Friday
    assert "2023-01-02" in _closed(2023)                                               # Sunday New Year's -> Monday
    assert "2021-06-18" not in _closed(2021) and "2022-06-20" in _closed(2022)         # Juneteenth from 2022, Sunday -> Monday
    assert "2024-03-29" in _closed(2024) and "2025-04-18" in _closed(2025)             # Good Friday


def test_trading_days_are_inclusive_and_skip_weekends_and_holidays():
    days = nyse.trading_days(date(2026, 4, 1), date(2026, 4, 7))
    assert days == [date(2026, 4, 1), date(2026, 4, 2), date(2026, 4, 6), date(2026, 4, 7)]
    assert nyse.trading_days(date(2026, 4, 5), date(2026, 4, 4)) == []
