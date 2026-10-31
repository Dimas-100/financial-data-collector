"""The outside checks: a second price source for the newest close, and Yahoo's figures against ours."""
import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

from financial_data_collector import check, config as C
from financial_data_collector.check import CheckContext, outside
from financial_data_collector.derive import valuation as V
from financial_data_collector.http import HttpError
from financial_data_collector.models import AccountRef, PositionRow, PriceBar, Snapshot
from financial_data_collector.store import Store

NOW = datetime(2026, 10, 29, 12, 0, tzinfo=timezone.utc)
DAY = "2026-10-28"
TOKEN = "tk-secret-12345"


class FakeOutside:
    """Yahoo, as a check sees it. quotes: symbol -> Quote | Exception | None; figs likewise."""

    def __init__(self, quotes=None, figs=None):
        self.quotes, self.figs, self.asked = quotes or {}, figs or {}, []

    def quote(self, symbol, on):
        self.asked.append(("quote", symbol, on))
        got = self.quotes.get(symbol)
        if isinstance(got, Exception):
            raise got
        return got

    def figures(self, symbol):
        self.asked.append(("figures", symbol))
        got = self.figs.get(symbol)
        if isinstance(got, Exception):
            raise got
        return got


@pytest.fixture
def cfg(tmp_path: Path):
    C.init_project(tmp_path)
    Store.open(tmp_path / "data" / "warehouse.db").close()
    c = C.load_config(tmp_path)
    c.tiingo_token = TOKEN
    return c


@pytest.fixture
def store(cfg):
    s = Store.open(cfg.db_path, migrate=False)
    yield s
    s.close()


def _held(store, symbol="AAA", close=100.0, source="tiingo", split=1.0, asset_type="stock", day=DAY):
    store.upsert_security(symbol, symbol, asset_type)
    acct = AccountRef(label=f"Account {symbol}", institution="fidelity", account_type="brokerage")
    store.write_snapshot(Snapshot(day, "test", [PositionRow(acct, symbol, symbol, 10.0, close, 10 * close)], []))
    store.write_prices(symbol, [PriceBar(day, close, close, 0.0, split)], source)


def _valuation(store, symbol="AAA", d=DAY, **kw):
    base = dict(symbol=symbol, date=d, close=100.0, shares=None, market_cap=None, revenue_ttm=None,
                net_income_ttm=None, ocf_ttm=None, fcf_ttm=None, eps_ttm=None, pe=None, ps=None, p_fcf=None,
                dividends_12m=None, dividend_yield=None, ttm_period_end="2026-06-30", available_from="2026-08-01",
                status="ok", status_reason=None)
    base.update(kw)
    store.replace_rows("valuation_daily", V.COLUMNS, [tuple(base[c] for c in V.COLUMNS)])


def _ctx(store, cfg, out=None, fetch=None, sleep=None):
    return CheckContext(store=store, cfg=cfg, fetch=fetch, outside=out, now=NOW, sleep=sleep or (lambda s: None))


def _tiingo(close, date=DAY):
    return lambda url, headers=None: json.dumps([{"date": f"{date}T00:00:00.000Z", "close": close}]).encode()


def _prices2(store, cfg, out=None, fetch=None):
    return check.CHECKS["prices2"](_ctx(store, cfg, out or FakeOutside(), fetch))


def _figures(store, cfg, out):
    return check.CHECKS["figures"](_ctx(store, cfg, out))


Q = outside.Quote
F = outside.Figures


# ---- prices2 -----------------------------------------------------------------------------------------

def test_closes_within_half_a_percent_are_fine(store, cfg):
    _held(store, close=100.0)
    assert _prices2(store, cfg, FakeOutside({"AAA": Q(100.49, DAY)})) == []
    assert _prices2(store, cfg, FakeOutside({"AAA": Q(100.0, DAY)})) == []


def test_closes_over_half_a_percent_warn_over_3pct_hold(store, cfg):
    _held(store, close=100.0)
    [w] = _prices2(store, cfg, FakeOutside({"AAA": Q(101.0, DAY)}))
    assert (w.check_name, w.symbol, w.field, w.date_from, w.severity) == ("prices2", "AAA", "close", DAY, "warn")
    assert w.evidence["ours"] == 100.0 and w.evidence["theirs"] == 101.0 and w.evidence["source"] == "tiingo"
    [h] = _prices2(store, cfg, FakeOutside({"AAA": Q(105.0, DAY)}))
    assert (h.field, h.date_from, h.date_to, h.severity) == ("*", DAY, None, "hold")      # a wrong close blanks everything built on it
    assert "100" in h.message and "105" in h.message


def test_compares_the_other_provider_than_the_bar_came_from(store, cfg):
    _held(store, "TII", close=100.0, source="tiingo")
    _held(store, "YFI", close=100.0, source="yfinance")
    yahoo = FakeOutside({"TII": Q(100.0, DAY), "YFI": Q(1.0, DAY)})
    seen = []

    def fetch(url, headers=None):
        seen.append((url, headers))
        return _tiingo(100.0)(url, headers)

    assert _prices2(store, cfg, yahoo, fetch) == []
    assert yahoo.asked == [("quote", "TII", DAY)]                      # the yfinance-sourced bar never asked Yahoo
    [(url, headers)] = seen
    assert url == f"https://api.tiingo.com/tiingo/daily/YFI/prices?startDate={DAY}&endDate={DAY}"
    assert headers["Authorization"] == f"Token {TOKEN}"


def test_a_yfinance_bar_against_a_tiingo_close_that_differs_holds(store, cfg):
    _held(store, close=100.0, source="yfinance")
    [h] = _prices2(store, cfg, fetch=_tiingo(110.0))
    assert (h.severity, h.field, h.evidence["source"]) == ("hold", "*", "yfinance")


def test_no_tiingo_token_is_info_never_a_hold(store, cfg):
    _held(store, close=100.0, source="yfinance")
    cfg.tiingo_token = None
    [f] = _prices2(store, cfg)
    assert f.severity == "info" and "no second source" in f.message


def test_split_day_compares_split_adjusted(store, cfg):
    _held(store, close=50.0, split=2.0)                         # a 2-for-1 on the bar's day
    assert _prices2(store, cfg, FakeOutside({"AAA": Q(100.0, DAY)})) == []        # Yahoo still shows the pre-split price
    assert _prices2(store, cfg, FakeOutside({"AAA": Q(50.1, DAY)})) == []         # or the post-split one
    [h] = _prices2(store, cfg, FakeOutside({"AAA": Q(120.0, DAY)}))               # neither
    assert h.severity == "hold"


def test_split_adjustment_covers_a_reverse_split_and_the_other_side(store, cfg):
    _held(store, close=100.0, split=0.1)                        # a 1-for-10 reverse split
    assert _prices2(store, cfg, FakeOutside({"AAA": Q(10.0, DAY)})) == []
    assert _prices2(store, cfg, FakeOutside({"AAA": Q(100.0, DAY)})) == []


def test_without_a_split_a_factor_of_two_is_a_hold(store, cfg):
    _held(store, close=50.0)
    [h] = _prices2(store, cfg, FakeOutside({"AAA": Q(100.0, DAY)}))
    assert h.severity == "hold"


def test_a_stock_dividend_factor_with_equal_closes_is_not_a_difference(store, cfg):
    _held(store, close=100.0, split=1.05)
    assert _prices2(store, cfg, FakeOutside({"AAA": Q(100.0, DAY)})) == []
    [w] = _prices2(store, cfg, FakeOutside({"AAA": Q(101.0, DAY)}))        # a real gap still shows, unadjusted
    assert (w.severity, w.evidence["split_adjusted"]) == ("warn", False)


def test_a_two_for_one_with_one_side_pre_split_compares_equal(store, cfg):
    _held(store, close=100.0, split=2.0)
    assert _prices2(store, cfg, FakeOutside({"AAA": Q(200.0, DAY)})) == []         # theirs still pre-split
    assert _prices2(store, cfg, FakeOutside({"AAA": Q(50.0, DAY)})) == []          # ours still pre-split


def test_a_zero_close_is_not_compared_and_says_so(store, cfg):
    _held(store, close=100.0)
    [f] = _prices2(store, cfg, FakeOutside({"AAA": Q(0.0, DAY)}))
    assert f.severity == "info" and f.message.startswith("not compared")


def test_money_market_and_crypto_are_not_asked(store, cfg):
    _held(store, "SPAXX", asset_type="money_market")
    _held(store, "BTC", asset_type="crypto")
    out = FakeOutside()
    assert _prices2(store, cfg, out) == [] and out.asked == []


def test_a_held_symbol_without_a_final_bar_is_skipped(store, cfg):
    store.upsert_security("AAA", "AAA", "stock")
    acct = AccountRef(label="Test account", institution="fidelity", account_type="brokerage")
    store.write_snapshot(Snapshot(DAY, "test", [PositionRow(acct, "AAA", "AAA", 1.0, 1.0, 1.0)], []))
    out = FakeOutside()
    assert _prices2(store, cfg, out) == [] and out.asked == []


@pytest.mark.parametrize("answer", [None, RuntimeError("yahoo is down"), Q(100.0, "2026-10-27")])
def test_outside_failure_is_info_never_hold(store, cfg, answer):
    _held(store, close=100.0)
    found = _prices2(store, cfg, FakeOutside({"AAA": answer}))
    assert found and {f.severity for f in found} == {"info"}


def test_an_outage_keeps_the_holds_it_could_not_recheck(store, cfg):
    _held(store, close=100.0)
    [hold] = _prices2(store, cfg, FakeOutside({"AAA": Q(110.0, DAY)}))
    store.check_record("prices2", [hold], "2026-10-28T20:00:00Z")
    found = _prices2(store, cfg, FakeOutside({"AAA": RuntimeError("down")}))
    assert hold in found and any(f.severity == "info" for f in found)
    store.check_record("prices2", found, "2026-10-29T12:00:00Z")
    assert [r["severity"] for r in store.check_open_findings("prices2") if r["severity"] == "hold"] == ["hold"]
    # and a good answer afterwards clears it
    store.check_record("prices2", _prices2(store, cfg, FakeOutside({"AAA": Q(100.0, DAY)})), "2026-10-29T13:00:00Z")
    assert store.check_open_findings("prices2") == []


def test_an_info_never_replaces_a_finding_with_the_same_key(store, cfg):
    _held(store, close=100.0)
    [warn] = _prices2(store, cfg, FakeOutside({"AAA": Q(101.0, DAY)}))
    store.check_record("prices2", [warn], "2026-10-28T20:00:00Z")
    found = _prices2(store, cfg, FakeOutside({"AAA": None}))
    keys = [(f.symbol, f.field, f.date_from) for f in found]
    assert len(keys) == len(set(keys)) and warn in found


def test_the_tiingo_token_never_reaches_a_finding(store, cfg):
    _held(store, close=100.0, source="yfinance")

    def fetch(url, headers=None):
        raise HttpError(0, url, f"boom with Token {TOKEN}")

    [f] = _prices2(store, cfg, fetch=fetch)
    assert f.severity == "info" and TOKEN not in f.message and TOKEN not in json.dumps(f.evidence)


def test_one_symbol_failing_does_not_stop_the_next(store, cfg):
    _held(store, "AAA", close=100.0)
    _held(store, "BBB", close=100.0)
    found = _prices2(store, cfg, FakeOutside({"AAA": RuntimeError("x"), "BBB": Q(110.0, DAY)}))
    assert {(f.symbol, f.severity) for f in found} == {("AAA", "info"), ("BBB", "hold")}


def test_pauses_between_outside_calls(store, cfg):
    _held(store, "AAA")
    _held(store, "BBB")
    slept = []
    check.CHECKS["prices2"](_ctx(store, cfg, FakeOutside({"AAA": Q(100.0, DAY), "BBB": Q(100.0, DAY)}), sleep=slept.append))
    assert len(slept) == 1


# ---- figures -----------------------------------------------------------------------------------------

def test_figure_null_here_but_present_outside_warns(store, cfg):
    _held(store)
    _valuation(store, eps_ttm=None, market_cap=1000.0)
    found = _figures(store, cfg, FakeOutside(figs={"AAA": F(trailing_eps=2.0, market_cap=1000.0, revenue=None, free_cash_flow=None)}))
    [w] = found
    assert (w.check_name, w.symbol, w.field, w.date_from, w.severity) == ("figures", "AAA", "eps_ttm", DAY, "warn")


def test_figure_15pct_apart_warns_40pct_holds_on_that_field(store, cfg):
    _held(store)
    _valuation(store, market_cap=1000.0, revenue_ttm=1000.0, eps_ttm=1.0, fcf_ttm=1000.0)
    found = _figures(store, cfg, FakeOutside(figs={"AAA": F(trailing_eps=1.0, market_cap=1000 / 0.8, revenue=1000 / 0.5,
                                                          free_cash_flow=1000.0)}))
    by = {f.field: f for f in found}
    assert set(by) == {"market_cap", "revenue_ttm", "ps"}      # ps is held with the revenue it is built on
    assert by["market_cap"].severity == "warn"           # ours is 20% below Yahoo's
    assert by["revenue_ttm"].severity == "hold"          # 50% below
    assert by["revenue_ttm"].evidence["ours"] == 1000.0 and by["revenue_ttm"].evidence["theirs"] == 2000.0


def test_free_cash_flow_never_holds_at_any_size(store, cfg):
    _held(store)
    _valuation(store, fcf_ttm=1.0, p_fcf=50.0)
    [f] = _figures(store, cfg, FakeOutside(figs={"AAA": F(None, None, None, 1000.0)}))
    assert (f.field, f.severity) == ("fcf_ttm", "warn")


def test_tiny_eps_only_warns(store, cfg):
    _held(store)
    _valuation(store, eps_ttm=0.05, pe=2000.0)
    [f] = _figures(store, cfg, FakeOutside(figs={"AAA": F(0.20, None, None, None)}))      # 75% apart, but cents
    assert (f.field, f.severity) == ("eps_ttm", "warn")
    found = _figures(store, cfg, FakeOutside(figs={"AAA": F(2.0, None, None, None)}))     # a real EPS: holds
    assert {(f.field, f.severity) for f in found} == {("eps_ttm", "hold"), ("pe", "hold")}


def test_a_figures_hold_holds_the_ratios_built_on_it(store, cfg):
    _held(store)
    _valuation(store, eps_ttm=2.0, pe=50.0, market_cap=1000.0, ps=5.0, p_fcf=20.0, revenue_ttm=200.0)
    found = _figures(store, cfg, FakeOutside(figs={"AAA": F(5.0, 1000.0, None, None)}))
    by = {f.field: f for f in found}
    assert set(by) == {"eps_ttm", "pe"} and by["pe"].severity == "hold" and "built on eps_ttm" in by["pe"].message
    assert (by["pe"].date_from, by["pe"].evidence) == (by["eps_ttm"].date_from, by["eps_ttm"].evidence)
    store.check_record("figures", found, "2026-10-29T12:00:00Z")
    r = store.query("SELECT eps_ttm, pe, market_cap, ps FROM valuation_latest WHERE symbol = 'AAA'")[0]
    assert (r["eps_ttm"], r["pe"]) == (None, None) and (r["market_cap"], r["ps"]) == (1000.0, 5.0)
    found = _figures(store, cfg, FakeOutside(figs={"AAA": F(2.0, 100.0, None, None)}))     # market_cap 10x off
    assert {f.field for f in found} == {"market_cap", "ps", "p_fcf"}


def test_a_zero_figure_outside_is_not_compared_and_says_so(store, cfg):
    _held(store)
    _valuation(store, market_cap=1000.0)
    [f] = _figures(store, cfg, FakeOutside(figs={"AAA": F(None, 0.0, None, None)}))
    assert (f.field, f.severity) == ("market_cap", "info") and f.message.startswith("not compared")


def test_figures_inside_the_warn_band_are_fine(store, cfg):
    _held(store)
    _valuation(store, market_cap=1000.0, eps_ttm=2.0, revenue_ttm=500.0, fcf_ttm=50.0)
    assert _figures(store, cfg, FakeOutside(figs={"AAA": F(2.1, 1040.0, 505.0, 52.0)})) == []


def test_figures_map_to_their_columns(store, cfg):
    _held(store)
    _valuation(store, market_cap=100.0, eps_ttm=100.0, revenue_ttm=100.0, fcf_ttm=100.0)
    found = _figures(store, cfg, FakeOutside(figs={"AAA": F(1.0, 1.0, 1.0, 1.0)}))
    by = {f.field: f for f in found}
    assert {"eps_ttm", "market_cap", "revenue_ttm", "fcf_ttm"} <= set(by)
    assert by["fcf_ttm"].severity == "warn"              # never a hold: Yahoo's free cash flow is defined differently


def test_figures_only_for_stocks_whose_values_are_ok(store, cfg):
    _held(store, "AAA")
    _valuation(store, "AAA", status="stale", status_reason="old", market_cap=1.0)
    _held(store, "FND", asset_type="etf")
    _valuation(store, "FND", market_cap=1.0)
    out = FakeOutside(figs={"AAA": F(None, 500.0, None, None), "FND": F(None, 500.0, None, None)})
    assert _figures(store, cfg, out) == [] and out.asked == []


def test_figures_for_a_symbol_not_held_are_not_asked(store, cfg):
    store.upsert_security("ZZZ", "ZZZ", "stock")
    _valuation(store, "ZZZ", market_cap=1.0)
    out = FakeOutside()
    assert _figures(store, cfg, out) == [] and out.asked == []


@pytest.mark.parametrize("answer", [None, RuntimeError("yahoo is down")])
def test_figures_outside_failure_is_info_and_keeps_open_holds(store, cfg, answer):
    _held(store)
    _valuation(store, market_cap=100.0)
    holds = _figures(store, cfg, FakeOutside(figs={"AAA": F(None, 1000.0, None, None)}))
    assert {f.field for f in holds} == {"market_cap", "ps", "p_fcf"} and {f.severity for f in holds} == {"hold"}
    store.check_record("figures", holds, "2026-10-28T20:00:00Z")
    found = _figures(store, cfg, FakeOutside(figs={"AAA": answer}))
    assert all(h in found for h in holds) and any(f.severity == "info" for f in found)
    assert not [f for f in found if f.severity == "hold" and f not in holds]


def test_figure_error_texts_are_redacted(store, cfg):
    _held(store)
    _valuation(store, market_cap=100.0)
    [f] = _figures(store, cfg, FakeOutside(figs={"AAA": RuntimeError(f"bad Token {TOKEN} at https://user:pw@h/x")}))
    assert TOKEN not in f.message and "pw@" not in f.message


# ---- the source -----------------------------------------------------------------------------------------

def test_the_yahoo_source_has_the_two_calls():
    src = outside.YahooSource()
    assert callable(src.quote) and callable(src.figures)


def _fake_yfinance(monkeypatch, frame=None, info=None):
    import logging
    import sys
    import types
    calls = []

    class Ticker:
        def __init__(self, symbol):
            calls.append(("Ticker", symbol))
            self.info = info

        def history(self, **kw):
            calls.append(("history", kw))
            return frame

    mod = types.ModuleType("yfinance")
    mod.Ticker = Ticker
    monkeypatch.setitem(sys.modules, "yfinance", mod)
    monkeypatch.setattr(logging.getLogger("yfinance"), "level", logging.NOTSET)
    return calls


def test_yahoo_quote_asks_for_one_day_and_reads_the_date_from_a_tz_aware_index(monkeypatch):
    import pandas as pd
    idx = pd.DatetimeIndex(["2026-10-28 00:00:00"], tz="America/New_York")
    frame = pd.DataFrame({"Close": [123.45]}, index=idx)
    calls = _fake_yfinance(monkeypatch, frame=frame)
    assert outside.YahooSource().quote("BRK.B", "2026-10-28") == Q(123.45, "2026-10-28")
    assert ("Ticker", "BRK-B") in calls
    [kw] = [c[1] for c in calls if c[0] == "history"]
    assert kw == {"start": "2026-10-28", "end": "2026-10-29", "auto_adjust": False}        # end is exclusive


def test_yahoo_quote_with_an_empty_frame_is_none(monkeypatch):
    import pandas as pd
    _fake_yfinance(monkeypatch, frame=pd.DataFrame({"Close": []}, index=pd.DatetimeIndex([], tz="America/New_York")))
    assert outside.YahooSource().quote("AAA", "2026-10-28") is None


def test_yahoo_figures_map_the_info_keys_and_drop_junk(monkeypatch):
    _fake_yfinance(monkeypatch, info={"trailingEps": 2.5, "marketCap": 1e9, "totalRevenue": "n/a", "freeCashflow": float("nan")})
    assert outside.YahooSource().figures("AAA") == F(2.5, 1e9, None, None)
    _fake_yfinance(monkeypatch, info={})
    assert outside.YahooSource().figures("AAA") is None


def test_yahoo_calls_silence_the_yfinance_logger(monkeypatch):
    import logging
    import pandas as pd
    _fake_yfinance(monkeypatch, frame=pd.DataFrame({"Close": []}, index=pd.DatetimeIndex([], tz="UTC")), info={})
    outside.YahooSource().quote("AAA", "2026-10-28")
    assert logging.getLogger("yfinance").level == logging.CRITICAL
    logging.getLogger("yfinance").setLevel(logging.NOTSET)
    outside.YahooSource().figures("AAA")
    assert logging.getLogger("yfinance").level == logging.CRITICAL


def test_run_check_builds_the_default_source_only_when_a_check_asks(store, cfg, monkeypatch):
    built = []

    class Stub:
        def quote(self, symbol, on):
            return None

        def figures(self, symbol):
            return None

    monkeypatch.setattr(outside, "default_source", lambda: built.append(1) or Stub())
    check.run_check(cfg, fetch=None, now=NOW, store=store, only=["fresh", "impossible"])
    assert built == []
    _held(store)
    check.run_check(cfg, fetch=None, now=NOW, store=store, only=["prices2"])
    assert built == [1]


def test_the_suite_never_reaches_yahoo():
    # the autouse fixture in conftest swaps default_source for a stub that knows nothing
    src = outside.default_source()
    assert src.quote("AAA", DAY) is None and src.figures("AAA") is None
