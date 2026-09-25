import json
from datetime import date
from pathlib import Path

import pytest

from financial_data_collector import config as C
from financial_data_collector.collectors import prices as P
from financial_data_collector.http import HttpError
from financial_data_collector.models import PriceBar
from financial_data_collector.store import Store

TIINGO_AAPL = [
    {"date": "2026-01-02T00:00:00.000Z", "close": 100.0, "adjClose": 99.5, "divCash": 0.0, "splitFactor": 1.0},
    {"date": "2026-01-05T00:00:00.000Z", "close": 102.0, "adjClose": 101.5, "divCash": 0.25, "splitFactor": 1.0},
    {"date": "2026-01-06T00:00:00.000Z", "close": 51.0, "adjClose": 51.0, "divCash": 0.0, "splitFactor": 2.0},
]


def test_next_start():
    assert P.next_start(None, date(2026, 1, 15), 5) == "2021-01-15"
    assert P.next_start("2026-01-10", date(2026, 1, 15), 5) == "2026-01-05"     # re-fetch the trailing window so partial bars get corrected


def test_tiingo_bars_parses_and_sends_token():
    seen = {}

    def fetch(url, headers):
        seen["url"], seen["headers"] = url, headers
        return json.dumps(TIINGO_AAPL).encode()

    bars = P.tiingo_bars("BRK.B", "2026-01-01", "tok", fetch)
    assert "tiingo/daily/BRK-B/prices" in seen["url"] and "startDate=2026-01-01" in seen["url"]
    assert seen["headers"]["Authorization"] == "Token tok"
    assert bars[0] == PriceBar("2026-01-02", 100.0, 99.5, 0.0, 1.0)
    assert bars[2].split_factor == 2.0 and bars[1].dividend == 0.25


@pytest.fixture
def cfg(tmp_path: Path):
    C.init_project(tmp_path)
    (tmp_path / ".env").write_text("TIINGO_API_TOKEN=tok\n")
    return C.load_config(tmp_path)


def test_collect_prices_fallback_and_no_data(cfg, tmp_path: Path):
    store = Store.open(cfg.db_path)
    for s in ("AAPL", "KO", "VTI"):
        store.upsert_security(s, first_seen="2026-01-01")
    store.write_prices("AAPL", [PriceBar("2026-01-01", 99.0, 99.0)], "tiingo")

    def fetch(url, headers):
        if "/AAPL/" in url:
            assert "startDate=2025-12-27" in url          # last stored 2026-01-01 minus the 5-day refresh window
            return json.dumps(TIINGO_AAPL).encode()
        raise HttpError(404, url)

    def yf(symbol, start):
        return [PriceBar("2026-01-02", 60.0, 60.0)] if symbol == "KO" else []

    slept = []
    results = P.collect_prices(store, ["AAPL", "KO", "VTI"], cfg, fetch=fetch, today=date(2026, 1, 15), yf=yf, sleep=slept.append)
    by = {r.symbol: r for r in results}
    assert (by["AAPL"].source, by["AAPL"].rows) == ("tiingo", 3)
    assert (by["KO"].source, by["KO"].rows) == ("yfinance", 1)
    assert by["VTI"].rows == 0 and by["VTI"].source is None and "no bars" in by["VTI"].message
    assert store.query("SELECT COUNT(*) FROM prices WHERE symbol='AAPL'")[0][0] == 4
    assert store.query("SELECT price_source FROM securities WHERE symbol='KO'")[0][0] == "yfinance"
    assert slept and all(s == 0.5 for s in slept)
    store.close()


def test_collect_prices_refetches_trailing_window(cfg):
    store = Store.open(cfg.db_path)
    store.upsert_security("AAPL", first_seen="2026-01-01")
    store.write_prices("AAPL", [PriceBar("2026-01-15", 99.0, 99.0)], "tiingo")
    calls = []
    results = P.collect_prices(store, ["AAPL"], cfg, fetch=lambda u, h: calls.append(u) or b"[]", today=date(2026, 1, 15), yf=lambda s, d: [], sleep=lambda s: None)
    assert len(calls) == 1 and "startDate=2026-01-10" in calls[0]
    assert results[0].message == "no new bars" and results[0].failed is False
    store.close()


def test_collect_prices_without_token_uses_yfinance_only(cfg, tmp_path: Path):
    cfg.tiingo_token = None
    store = Store.open(cfg.db_path)
    store.upsert_security("AAPL", first_seen="2026-01-01")
    calls = []
    results = P.collect_prices(store, ["AAPL"], cfg, fetch=lambda u, h: calls.append(u), today=date(2026, 1, 15), yf=lambda s, d: [PriceBar("2026-01-02", 1.0, 1.0)], sleep=lambda s: None)
    assert calls == [] and results[0].source == "yfinance"
    store.close()


def test_collect_prices_reports_no_new_bars_when_history_exists(cfg):
    store = Store.open(cfg.db_path)
    store.upsert_security("AAPL", first_seen="2026-01-01")
    store.write_prices("AAPL", [PriceBar("2026-01-13", 99.0, 99.0)], "tiingo")
    results = P.collect_prices(store, ["AAPL"], cfg, fetch=lambda u, h: b"[]", today=date(2026, 1, 15), yf=lambda s, d: [], sleep=lambda s: None)
    assert results[0].rows == 0 and results[0].message == "no new bars"
    store.close()


def test_provider_errors_mark_result_failed(cfg):
    store = Store.open(cfg.db_path)
    store.upsert_security("AAPL", first_seen="2026-01-01")

    def boom(url, headers):
        raise HttpError(503, url)

    def yf_boom(symbol, start):
        raise RuntimeError("rate limited")

    r = P.collect_prices(store, ["AAPL"], cfg, fetch=boom, today=date(2026, 1, 15), yf=yf_boom, sleep=lambda s: None)[0]
    assert r.rows == 0 and r.failed is True and "503" in r.message and "rate limited" in r.message
    r2 = P.collect_prices(store, ["AAPL"], cfg, fetch=lambda u, h: b"[]", today=date(2026, 1, 15), yf=lambda s, d: [], sleep=lambda s: None)[0]
    assert r2.failed is False and "no bars" in r2.message
    store.close()


def test_collect_prices_reports_progress(cfg):
    store = Store.open(cfg.db_path)
    for s in ("AAPL", "KO"):
        store.upsert_security(s, first_seen="2026-01-01")
    seen = []
    P.collect_prices(store, ["AAPL", "KO"], cfg, fetch=lambda u, h: b"[]", today=date(2026, 1, 15), yf=lambda s, d: [],
                     sleep=lambda s: None, progress=seen.append)
    assert seen == ["1/2 AAPL", "2/2 KO"]
    store.close()
