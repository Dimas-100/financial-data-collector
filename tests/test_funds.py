"""Fund snapshots: one valuation row per fund per New York day from Yahoo, a proxy ETF when a fund has none."""
import logging
import sys
import types
from datetime import datetime, timezone
from pathlib import Path

import pytest

from financial_data_collector import config as C
from financial_data_collector import sync
from financial_data_collector.collectors import funds, yahoo
from financial_data_collector.http import HttpError
from financial_data_collector.models import FundSnapshot
from financial_data_collector.store import Store

NOW = datetime(2026, 10, 29, 12, 0, tzinfo=timezone.utc)            # 08:00 in New York
STAMP = "2026-10-29T12:00:00Z"
SPY_INFO = {"trailingPE": 24.0, "forwardPE": None, "yield": 0.0099, "dividendYield": 0.99, "netExpenseRatio": 0.0945}
ELEVEN = ("XLK", "XLC", "XLY", "XLP", "XLE", "XLF", "XLV", "XLI", "XLB", "XLRE", "XLU")


class _FakeYahoo:
    """Answers from a dict; a value that is an Exception is raised. Records every symbol asked."""

    def __init__(self, infos: dict):
        self.infos = infos
        self.calls: list[str] = []

    def fund_info(self, symbol):
        self.calls.append(symbol)
        answer = self.infos.get(symbol)
        if isinstance(answer, Exception):
            raise answer
        return answer


EXAMPLE_ENTRY = '# "XYZFX" = "SPY"'                                  # the commented line the example config has


def _cfg(tmp_path: Path, entry: str | None = None) -> C.Config:
    """The example config, with its commented [fund_proxies] line replaced by `entry`."""
    C.init_project(tmp_path)
    if entry is not None:
        text = (tmp_path / "config.toml").read_text(encoding="utf-8")
        assert EXAMPLE_ENTRY in text
        (tmp_path / "config.toml").write_text(text.replace(EXAMPLE_ENTRY, entry), encoding="utf-8")
    return C.load_config(tmp_path)


@pytest.fixture
def cfg(tmp_path: Path):
    return _cfg(tmp_path)


@pytest.fixture
def cfg_with_proxy(tmp_path: Path):
    return _cfg(tmp_path, '"xyzfx" = "spy"')


@pytest.fixture
def store(cfg):
    s = Store.open(cfg.db_path)
    yield s
    s.close()


def _security(store: Store, symbol: str, asset_type: str) -> None:
    store.upsert_security(symbol, asset_type=asset_type, first_seen="2026-01-01")


def _snapshot(store: Store, symbol: str):
    rows = store.query("SELECT * FROM fund_snapshots WHERE symbol = ?", (symbol,))
    return rows[0] if rows else None


def _collect(store, universe, cfg, yahoo_client, now=NOW):
    return funds.collect_funds(store, universe, cfg, yahoo=yahoo_client, now=now, sleep=lambda s: None)


# ---- the figures --------------------------------------------------------------------------------------------------

def test_snapshot_stores_fractions():
    figs, notes = funds.figures_from_info(SPY_INFO)
    assert figs == {"trailing_pe": 24.0, "forward_pe": None, "dividend_yield": 0.0099,
                    "expense_ratio": pytest.approx(0.000945)}
    assert notes == []


def test_annual_report_expense_ratio_is_already_a_fraction():
    figs, _ = funds.figures_from_info({"trailingPE": 20.0, "annualReportExpenseRatio": 0.0004})
    assert figs["expense_ratio"] == 0.0004


def test_net_expense_ratio_wins_over_the_annual_report_one():
    figs, notes = funds.figures_from_info({"netExpenseRatio": 0.0945, "annualReportExpenseRatio": 0.0009})
    assert figs["expense_ratio"] == pytest.approx(0.000945) and notes == []


def test_expense_ratios_ten_times_apart_are_refused():
    figs, notes = funds.figures_from_info({"trailingPE": 20.0, "netExpenseRatio": 0.0945,
                                           "annualReportExpenseRatio": 0.0945})
    assert figs["expense_ratio"] is None
    assert notes == ["netExpenseRatio 0.0945% disagrees with annualReportExpenseRatio 0.0945"]
    assert funds.figures_from_info({"netExpenseRatio": 0.05, "annualReportExpenseRatio": 0.0049})[0]["expense_ratio"] \
        == pytest.approx(0.0005)                                          # 9.8 times apart: kept


def test_a_yield_and_dividend_yield_that_disagree_are_refused():
    figs, notes = funds.figures_from_info({"trailingPE": 20.0, "yield": 0.99, "dividendYield": 0.99})
    assert figs["dividend_yield"] is None and notes == ["yield 0.99 disagrees with dividendYield 0.99%"]
    assert funds.figures_from_info({"yield": 0.0, "dividendYield": 0.5})[0]["dividend_yield"] is None
    assert funds.figures_from_info({"yield": 0.0, "dividendYield": 0.0})[0]["dividend_yield"] == 0.0
    assert funds.figures_from_info({"yield": 0.0099, "dividendYield": 0.95})[0]["dividend_yield"] == 0.0099


def test_numpy_numbers_are_figures():
    np = pytest.importorskip("numpy")
    figs, _ = funds.figures_from_info({"trailingPE": np.int64(24), "yield": np.float64(0.0099)})
    assert figs["trailing_pe"] == 24.0 and figs["dividend_yield"] == 0.0099


def test_a_yield_in_percent_is_refused_not_stored():
    figs, notes = funds.figures_from_info({"trailingPE": 20.0, "yield": 0.99})
    assert figs["dividend_yield"] is None and notes == ["yield 0.99 out of range"]


def test_dividend_yield_is_never_stored():
    figs, notes = funds.figures_from_info({"trailingPE": 20.0, "dividendYield": 0.99})
    assert figs["dividend_yield"] is None and notes == []


def test_an_expense_ratio_in_percent_is_refused():
    figs, notes = funds.figures_from_info({"annualReportExpenseRatio": 0.5})
    assert figs["expense_ratio"] is None and notes == ["expense ratio 0.5 out of range"]


@pytest.mark.parametrize("bad", [float("nan"), float("inf"), "24.0", None, True])
def test_not_a_finite_number_is_none(bad):
    figs, notes = funds.figures_from_info({"trailingPE": bad, "forwardPE": bad, "yield": bad,
                                           "netExpenseRatio": bad, "annualReportExpenseRatio": bad})
    assert set(figs.values()) == {None} and notes == []


def test_the_yield_range_is_zero_up_to_but_not_including_a_quarter():
    assert funds.figures_from_info({"yield": 0.0})[0]["dividend_yield"] == 0.0
    assert funds.figures_from_info({"yield": 0.249})[0]["dividend_yield"] == 0.249
    assert funds.figures_from_info({"yield": 0.25})[0]["dividend_yield"] is None
    assert funds.figures_from_info({"yield": -0.01})[0]["dividend_yield"] is None


# ---- collecting ---------------------------------------------------------------------------------------------------

def test_own_figures_are_stored_with_source_yfinance(store, cfg):
    _security(store, "SPY", "etf")
    results = _collect(store, ["SPY"], cfg, _FakeYahoo({"SPY": SPY_INFO}))
    row = _snapshot(store, "SPY")
    assert (row["date"], row["source"], row["fetched_at"]) == ("2026-10-29", "yfinance", STAMP)
    assert (row["trailing_pe"], row["forward_pe"], row["dividend_yield"]) == (24.0, None, 0.0099)
    assert results[0].source == "yfinance" and results[0].written == 1 and not results[0].failed


def test_proxy_stands_in_and_says_so(store, cfg_with_proxy):
    _security(store, "XYZFX", "mutual_fund")
    yahoo_client = _FakeYahoo({"XYZFX": {"netExpenseRatio": 0.015}, "SPY": SPY_INFO})
    _collect(store, ["XYZFX"], cfg_with_proxy, yahoo_client)
    row = _snapshot(store, "XYZFX")
    assert row["source"] == "proxy: SPY via yfinance" and row["trailing_pe"] == 24.0
    assert row["dividend_yield"] == 0.0099
    assert row["expense_ratio"] == pytest.approx(0.00015)                # the fund's own, not SPY's
    assert yahoo_client.calls == ["XYZFX", "SPY"]


def test_a_proxy_is_not_asked_when_the_fund_has_its_own_figures(store, cfg_with_proxy):
    _security(store, "XYZFX", "mutual_fund")
    yahoo_client = _FakeYahoo({"XYZFX": {"trailingPE": 18.0, "yield": 0.02}, "SPY": SPY_INFO})
    _collect(store, ["XYZFX"], cfg_with_proxy, yahoo_client)
    assert _snapshot(store, "XYZFX")["source"] == "yfinance" and yahoo_client.calls == ["XYZFX"]


def test_a_proxy_asked_in_the_same_run_is_not_asked_twice(store, cfg_with_proxy):
    _security(store, "SPY", "etf")
    _security(store, "XYZFX", "mutual_fund")
    yahoo_client = _FakeYahoo({"XYZFX": {}, "SPY": SPY_INFO})
    _collect(store, ["SPY", "XYZFX"], cfg_with_proxy, yahoo_client)
    assert yahoo_client.calls == ["SPY", "XYZFX"] and _snapshot(store, "XYZFX")["source"] == "proxy: SPY via yfinance"


def test_a_proxy_with_nothing_either_writes_nothing(store, cfg_with_proxy):
    _security(store, "XYZFX", "mutual_fund")
    results = _collect(store, ["XYZFX"], cfg_with_proxy, _FakeYahoo({"XYZFX": None, "SPY": None}))
    assert results[0].message == "no figures" and results[0].written == 0 and _snapshot(store, "XYZFX") is None


def test_a_failing_proxy_fails_the_fund_and_marks_nothing(store, cfg_with_proxy):
    _security(store, "XYZFX", "mutual_fund")
    results = _collect(store, ["XYZFX"], cfg_with_proxy, _FakeYahoo({"XYZFX": {}, "SPY": RuntimeError("down")}))
    assert results[0].failed and _snapshot(store, "XYZFX") is None
    assert store.query("SELECT last_fund_fetch FROM securities WHERE symbol='XYZFX'")[0][0] is None


def test_only_fund_types_are_asked(store, cfg):
    names = ("AAPL", "SPY", "VFIAX", "QQQ", "SPAXX", "NEW")
    for symbol, kind in zip(names, ("stock", "etf", "mutual_fund", "fund", "money_market", "unknown")):
        _security(store, symbol, kind)
    yahoo_client = _FakeYahoo({s: SPY_INFO for s in names})
    _collect(store, list(names), cfg, yahoo_client)
    assert yahoo_client.calls == ["SPY", "VFIAX", "QQQ"]


def test_a_symbol_outside_the_universe_is_not_asked(store, cfg):
    _security(store, "SPY", "etf")
    _security(store, "QQQ", "etf")
    yahoo_client = _FakeYahoo({"SPY": SPY_INFO, "QQQ": SPY_INFO})
    _collect(store, ["SPY"], cfg, yahoo_client)
    assert yahoo_client.calls == ["SPY"]


def test_one_call_per_symbol_per_new_york_day_and_no_row_change(store, cfg):
    _security(store, "SPY", "etf")
    yahoo_client = _FakeYahoo({"SPY": SPY_INFO})
    for hour in (12, 16, 22):                                            # 08:00, 12:00, 18:00 New York
        _collect(store, ["SPY"], cfg, yahoo_client, now=NOW.replace(hour=hour))
    assert yahoo_client.calls == ["SPY"] and _snapshot(store, "SPY")["fetched_at"] == STAMP


def test_the_next_new_york_day_asks_again(store, cfg):
    _security(store, "SPY", "etf")
    yahoo_client = _FakeYahoo({"SPY": SPY_INFO})
    _collect(store, ["SPY"], cfg, yahoo_client)
    _collect(store, ["SPY"], cfg, yahoo_client, now=NOW.replace(day=30))
    assert yahoo_client.calls == ["SPY", "SPY"]
    assert [r["date"] for r in store.query("SELECT date FROM fund_snapshots ORDER BY date")] == ["2026-10-29", "2026-10-30"]


def test_the_day_is_a_new_york_day_not_a_utc_day(store, cfg):
    _security(store, "SPY", "etf")
    yahoo_client = _FakeYahoo({"SPY": SPY_INFO})
    _collect(store, ["SPY"], cfg, yahoo_client, now=datetime(2026, 10, 29, 23, 0, tzinfo=timezone.utc))   # 19:00 New York
    _collect(store, ["SPY"], cfg, yahoo_client, now=datetime(2026, 10, 30, 2, 0, tzinfo=timezone.utc))    # 22:00 New York
    assert yahoo_client.calls == ["SPY"] and _snapshot(store, "SPY")["date"] == "2026-10-29"


def test_a_failed_ask_is_not_marked_and_is_asked_again(store, cfg):
    _security(store, "SPY", "etf")
    broken = _FakeYahoo({"SPY": ConnectionError("no route")})
    results = _collect(store, ["SPY"], cfg, broken)
    assert results[0].failed and "ConnectionError" in results[0].message and _snapshot(store, "SPY") is None
    healthy = _FakeYahoo({"SPY": SPY_INFO})
    _collect(store, ["SPY"], cfg, healthy)
    assert healthy.calls == ["SPY"] and _snapshot(store, "SPY") is not None


def test_a_failure_message_hides_the_tiingo_token(store, tmp_path):
    cfg = _cfg(tmp_path)
    cfg.tiingo_token = "tok-secret-123"
    _security(store, "SPY", "etf")
    results = _collect(store, ["SPY"], cfg, _FakeYahoo({"SPY": RuntimeError("GET /x?token=tok-secret-123 failed")}))
    assert "tok-secret-123" not in results[0].message and "***" in results[0].message


def test_one_failure_does_not_stop_the_rest(store, cfg):
    _security(store, "SPY", "etf")
    _security(store, "VFIAX", "mutual_fund")
    results = _collect(store, ["SPY", "VFIAX"], cfg, _FakeYahoo({"SPY": ValueError("x"), "VFIAX": SPY_INFO}))
    assert [r.failed for r in results] == [True, False] and _snapshot(store, "VFIAX") is not None


def test_no_figures_writes_nothing_and_is_still_marked(store, cfg):
    _security(store, "SPY", "etf")
    yahoo_client = _FakeYahoo({"SPY": {"longName": "x"}})
    results = _collect(store, ["SPY"], cfg, yahoo_client)
    assert results[0].message == "no figures" and results[0].written == 0 and results[0].source is None
    assert _snapshot(store, "SPY") is None
    _collect(store, ["SPY"], cfg, yahoo_client, now=NOW.replace(hour=16))
    assert yahoo_client.calls == ["SPY"]                                 # marked: not asked again today


def test_no_figures_after_a_day_with_figures_is_not_marked_and_is_asked_again(store, cfg):
    _security(store, "SPY", "etf")
    _collect(store, ["SPY"], cfg, _FakeYahoo({"SPY": SPY_INFO}), now=NOW.replace(day=28))
    blank = _FakeYahoo({"SPY": None})
    (r,) = _collect(store, ["SPY"], cfg, blank)
    assert r.again and not r.failed and r.source is None and r.written == 0
    assert r.message == "no figures today, had them on 2026-10-28; asked again next sync"
    assert store.query("SELECT last_fund_fetch FROM securities WHERE symbol='SPY'")[0][0] == "2026-10-28"
    _collect(store, ["SPY"], cfg, blank, now=NOW.replace(hour=16))
    assert blank.calls == ["SPY", "SPY"]                                   # not marked: asked again
    _collect(store, ["SPY"], cfg, _FakeYahoo({"SPY": SPY_INFO}), now=NOW.replace(hour=18))
    assert _snapshot(store, "SPY") is not None and len(store.query("SELECT 1 FROM fund_snapshots")) == 2


def test_the_stamp_is_utc_whatever_zone_now_is_in(store, cfg):
    from zoneinfo import ZoneInfo
    _security(store, "SPY", "etf")
    _collect(store, ["SPY"], cfg, _FakeYahoo({"SPY": SPY_INFO}), now=NOW.astimezone(ZoneInfo("America/New_York")))
    assert _snapshot(store, "SPY")["fetched_at"] == STAMP


def test_a_failed_write_is_not_marked(store, cfg, monkeypatch):
    def broken(*a, **k):
        raise RuntimeError("disk full")
    monkeypatch.setattr(store, "write_fund_snapshot", broken)
    _security(store, "SPY", "etf")
    with pytest.raises(RuntimeError, match="disk full"):
        _collect(store, ["SPY"], cfg, _FakeYahoo({"SPY": SPY_INFO}))
    assert store.query("SELECT last_fund_fetch FROM securities WHERE symbol='SPY'")[0][0] is None


def test_sleeps_only_between_calls(store, cfg):
    for symbol in ("SPY", "QQQ", "VFIAX"):
        _security(store, symbol, "etf")
    pauses: list[float] = []
    funds.collect_funds(store, ["QQQ", "SPY", "VFIAX"], cfg, yahoo=_FakeYahoo({}), now=NOW, sleep=pauses.append)
    assert pauses == [yahoo.PAUSE_SECONDS, yahoo.PAUSE_SECONDS]


# ---- the store ----------------------------------------------------------------------------------------------------

def test_write_fund_snapshot_returns_zero_for_the_same_values(store):
    snap = FundSnapshot("SPY", "2026-10-29", 24.0, None, 0.0099, 0.000945, "yfinance")
    assert store.write_fund_snapshot(snap, STAMP) == 1
    assert store.write_fund_snapshot(snap, "2026-10-29T20:00:00Z") == 0
    assert _snapshot(store, "SPY")["fetched_at"] == STAMP                # untouched


def test_write_fund_snapshot_updates_a_changed_value_or_source(store):
    store.write_fund_snapshot(FundSnapshot("SPY", "2026-10-29", 24.0, None, 0.0099, None, "yfinance"), STAMP)
    assert store.write_fund_snapshot(FundSnapshot("SPY", "2026-10-29", 25.0, None, 0.0099, None, "yfinance"),
                                     "2026-10-29T20:00:00Z") == 1
    row = _snapshot(store, "SPY")
    assert (row["trailing_pe"], row["fetched_at"]) == (25.0, "2026-10-29T20:00:00Z")
    assert store.write_fund_snapshot(FundSnapshot("SPY", "2026-10-29", 25.0, None, 0.0099, None, "proxy: X via yfinance"),
                                     "2026-10-29T21:00:00Z") == 1
    assert _snapshot(store, "SPY")["source"] == "proxy: X via yfinance"


def test_fund_snapshots_table_and_column(store):
    cols = [r["name"] for r in store.query("PRAGMA table_info(fund_snapshots)")]
    assert cols == ["symbol", "date", "trailing_pe", "forward_pe", "dividend_yield", "expense_ratio", "source", "fetched_at"]
    assert "last_fund_fetch" in [r["name"] for r in store.query("PRAGMA table_info(securities)")]


# ---- config and the client ----------------------------------------------------------------------------------------

def test_fund_proxies_are_read_in_canonical_form(cfg_with_proxy):
    assert cfg_with_proxy.fund_proxies == {"XYZFX": "SPY"}


def test_no_fund_proxies_by_default(cfg):
    assert cfg.fund_proxies == {}


@pytest.mark.parametrize("entry", ["XYZFX = 3", 'XYZFX = ""'])
def test_a_bad_fund_proxies_entry_is_a_config_error(tmp_path, entry):
    with pytest.raises(C.ConfigError, match="fund_proxies"):
        _cfg(tmp_path, entry)


def test_fund_proxies_must_be_a_table(tmp_path):
    (tmp_path / "config.toml").write_text("fund_proxies = 4\n", encoding="utf-8")
    with pytest.raises(C.ConfigError, match="fund_proxies"):
        C.load_config(tmp_path)


def test_default_client_is_a_yfinance_client(monkeypatch):
    monkeypatch.undo()                                                    # the suite's stub is not what is tested here
    assert isinstance(yahoo.default_client(), yahoo.YfinanceClient)


def _yfinance_logger_state():
    logger = logging.getLogger("yfinance")
    return logger.level, logger.propagate, list(logger.handlers)


def test_yfinance_client_restores_the_logger(monkeypatch):
    class _Ticker:
        def __init__(self, symbol):
            self.symbol = symbol

        @property
        def info(self):
            assert logging.getLogger("yfinance").propagate is False             # kept off the console while asking
            raise RuntimeError("boom")

    monkeypatch.setitem(sys.modules, "yfinance", types.SimpleNamespace(Ticker=_Ticker))
    logger = logging.getLogger("yfinance")
    logger.setLevel(logging.WARNING)
    before = _yfinance_logger_state()
    with pytest.raises(RuntimeError, match="boom"):
        yahoo.YfinanceClient().fund_info("BRK.B")
    assert _yfinance_logger_state() == before


def _logging_yfinance(monkeypatch, info):
    """A fake yfinance whose `info` logs an error on the yfinance logger, as yfinance 1.7 does on a failed request
    (hide_exceptions), and then answers `info`."""
    class _Ticker:
        def __init__(self, symbol):
            self.symbol = symbol

        @property
        def info(self):
            logging.getLogger("yfinance").error("401 Client Error: Unauthorized ... Invalid Crumb")
            return info

    monkeypatch.setitem(sys.modules, "yfinance", types.SimpleNamespace(Ticker=_Ticker))


@pytest.mark.parametrize("info", [{}, {"symbol": "SPY"}])
def test_an_error_yfinance_logged_raises_instead_of_reading_as_nothing(monkeypatch, caplog, info):
    _logging_yfinance(monkeypatch, info)
    before = _yfinance_logger_state()
    with caplog.at_level(logging.DEBUG):
        with pytest.raises(RuntimeError, match="Invalid Crumb"):
            yahoo.YfinanceClient().fund_info("SPY")
    assert caplog.records == []                                            # nothing reached the console
    assert _yfinance_logger_state() == before


def test_a_logged_error_fails_the_fund_and_marks_nothing(monkeypatch, store, cfg):
    _logging_yfinance(monkeypatch, {})
    _security(store, "SPY", "etf")
    (r,) = _collect(store, ["SPY"], cfg, yahoo.YfinanceClient())
    assert r.failed and "Invalid Crumb" in r.message and not r.again
    assert store.query("SELECT last_fund_fetch FROM securities WHERE symbol='SPY'")[0][0] is None


def test_a_warning_yfinance_logged_is_not_an_error(monkeypatch):
    class _Ticker:
        def __init__(self, symbol):
            pass

        @property
        def info(self):
            logging.getLogger("yfinance").warning("something odd")
            return {"yield": 0.01}

    monkeypatch.setitem(sys.modules, "yfinance", types.SimpleNamespace(Ticker=_Ticker))
    assert yahoo.YfinanceClient().fund_info("SPY") == {"yield": 0.01}


def test_yfinance_client_returns_the_info_dict_or_none(monkeypatch):
    asked = []

    class _Ticker:
        def __init__(self, symbol):
            asked.append(symbol)
            self.info = {"yield": 0.01} if symbol == "BRK-B" else {}

    monkeypatch.setitem(sys.modules, "yfinance", types.SimpleNamespace(Ticker=_Ticker))
    client = yahoo.YfinanceClient()
    assert client.fund_info("BRK.B") == {"yield": 0.01} and client.fund_info("SPY") is None
    assert asked == ["BRK-B", "SPY"]


# ---- in a sync ----------------------------------------------------------------------------------------------------

def _sync_cfg(tmp_path: Path) -> C.Config:
    _cfg(tmp_path)
    (tmp_path / "watchlist.txt").write_text("SPY\nAAPL\n", encoding="utf-8")
    return C.load_config(tmp_path)


def _no_network(url, headers):
    raise HttpError(404, url)


def test_the_funds_step_writes_snapshots_and_reports_them(tmp_path):
    cfg = _sync_cfg(tmp_path)
    yahoo_client = _FakeYahoo({s: SPY_INFO for s in ("SPY", "XLK")})
    rep = sync.run_sync(cfg, only=["funds"], fetch=_no_network, now=NOW, yahoo=yahoo_client, sleep=lambda s: None)
    step = rep.steps[0]
    assert step.step == "funds" and step.status == "ok" and step.rows == 2
    assert step.message.startswith("2 snapshots written, 10 without figures, 0 failed")
    calls = len(yahoo_client.calls)
    again = sync.run_sync(cfg, only=["funds"], fetch=_no_network, now=NOW, yahoo=yahoo_client, sleep=lambda s: None)
    assert again.steps[0].rows == 0 and len(yahoo_client.calls) == calls          # no second call, no second row


def test_the_funds_step_is_an_error_only_when_every_symbol_failed(tmp_path):
    cfg = _sync_cfg(tmp_path)
    down = _FakeYahoo({s: OSError("down") for s in ("SPY",) + ELEVEN})
    rep = sync.run_sync(cfg, only=["funds"], fetch=_no_network, now=NOW, yahoo=down, sleep=lambda s: None)
    assert rep.steps[0].status == "error" and "12 failed" in rep.steps[0].message and "SPY (OSError" in rep.steps[0].message
    half = _FakeYahoo({"SPY": SPY_INFO, "XLK": OSError("down")})                   # the rest answer None: no figures
    rep = sync.run_sync(cfg, only=["funds"], fetch=_no_network, now=NOW, yahoo=half, sleep=lambda s: None)
    assert rep.steps[0].status == "ok" and "1 failed" in rep.steps[0].message


def test_the_funds_step_names_a_fund_that_went_blank(tmp_path):
    cfg = _sync_cfg(tmp_path)
    sync.run_sync(cfg, only=["funds"], fetch=_no_network, now=NOW.replace(day=28), yahoo=_FakeYahoo({"SPY": SPY_INFO}),
                  sleep=lambda s: None)
    rep = sync.run_sync(cfg, only=["funds"], fetch=_no_network, now=NOW, yahoo=_FakeYahoo({}), sleep=lambda s: None)
    assert "SPY (no figures today, had them on 2026-10-28; asked again next sync)" in rep.steps[0].message


def test_the_funds_step_is_skipped_when_it_asks_no_fund(tmp_path):
    cfg = _sync_cfg(tmp_path)
    yahoo_client = _FakeYahoo({s: SPY_INFO for s in ("SPY",) + ELEVEN})
    sync.run_sync(cfg, only=["funds"], fetch=_no_network, now=NOW, yahoo=yahoo_client, sleep=lambda s: None)
    again = sync.run_sync(cfg, only=["funds"], fetch=_no_network, now=NOW, yahoo=yahoo_client, sleep=lambda s: None)
    assert again.steps[0].status == "skipped" and again.steps[0].message.startswith("0 snapshots written")


def test_a_skipped_funds_step_leaves_the_exit_code_to_the_other_network_steps():
    report = sync.SyncReport("r", [sync.StepResult("prices", "error"), sync.StepResult("sec", "error"),
                                   sync.StepResult("funds", "skipped"), sync.StepResult("earnings", "skipped")])
    assert report.exit_code == 1
    report.steps[2].status = "ok"
    assert report.exit_code == 0


def test_run_sync_uses_the_default_client_when_none_is_given(tmp_path, monkeypatch):
    cfg = _sync_cfg(tmp_path)
    stub = _FakeYahoo({"SPY": SPY_INFO})
    monkeypatch.setattr(yahoo, "default_client", lambda: stub)
    sync.run_sync(cfg, only=["funds"], fetch=_no_network, now=NOW, sleep=lambda s: None)
    assert "SPY" in stub.calls
