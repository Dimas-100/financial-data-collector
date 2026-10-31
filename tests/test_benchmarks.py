"""Benchmarks: SPY and the sector funds, collected like any symbol, marked, and kept out of the export."""
import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

from financial_data_collector import config as C
from financial_data_collector import sectors, sync
from financial_data_collector.collectors import sec_cik
from financial_data_collector.http import HttpError
from financial_data_collector.models import AccountRef, PositionRow, PriceBar, Snapshot
from financial_data_collector.store import Store
from financial_data_collector.universe import build_universe, universe_symbols

NOW = datetime(2026, 3, 1, 12, 0, tzinfo=timezone.utc)
ELEVEN = ("SPY", "XLC", "XLY", "XLP", "XLE", "XLF", "XLV", "XLI", "XLB", "XLRE", "XLK", "XLU")


def _cfg(tmp_path: Path, extra: str = "") -> C.Config:
    C.init_project(tmp_path)
    if extra:                                  # the example config already has a [benchmarks] table: replace the file
        (tmp_path / "config.toml").write_text(extra, encoding="utf-8")
    return C.load_config(tmp_path)


@pytest.fixture
def cfg(tmp_path: Path):
    return _cfg(tmp_path)


@pytest.fixture
def store(cfg):
    s = Store.open(cfg.db_path)
    yield s
    s.close()


def _hold(store: Store, symbol: str) -> None:
    acct = AccountRef(label="Test account", institution="fidelity", account_type="brokerage")
    store.write_snapshot(Snapshot("2026-02-27", "test", [PositionRow(acct, symbol, symbol, 1.0, 10.0, 10.0)], []))


def _benchmark(store: Store, symbol: str) -> int:
    return {r["symbol"]: r["benchmark"] for r in store.securities()}[symbol]


def _type(store: Store, symbol: str) -> str:
    return {r["symbol"]: r["asset_type"] for r in store.securities()}[symbol]


# ---- config ------------------------------------------------------------------------------------------

def test_default_benchmarks_are_spy_and_the_eleven_sector_spdrs(tmp_path):
    cfg = _cfg(tmp_path)                       # init_project + load_config, no [benchmarks] table
    assert cfg.benchmarks.symbols == ELEVEN


def test_the_sector_list_has_eleven_names_and_a_fund_for_each():
    assert len(sectors.SECTORS) == 11 and set(sectors.DEFAULT_SECTOR_ETFS) == set(sectors.SECTORS)


def test_benchmarks_table_replaces_the_sector_list(tmp_path):
    cfg = _cfg(tmp_path, '[benchmarks]\nmarket = "vti"\nsectors = { "Technology" = "VGT" }\n')
    assert cfg.benchmarks.symbols == ("VTI", "VGT")


def test_a_market_only_table_keeps_the_default_sectors(tmp_path):
    cfg = _cfg(tmp_path, '[benchmarks]\nmarket = "VTI"\n')
    assert cfg.benchmarks.symbols == ("VTI",) + ELEVEN[1:]


def test_a_symbol_named_twice_is_listed_once(tmp_path):
    cfg = _cfg(tmp_path, '[benchmarks]\nmarket = "XLK"\n')
    assert cfg.benchmarks.symbols.count("XLK") == 1 and cfg.benchmarks.symbols[0] == "XLK"


def test_unknown_sector_name_is_a_config_error(tmp_path):
    with pytest.raises(C.ConfigError, match="Tech"):
        _cfg(tmp_path, '[benchmarks]\nsectors = { "Tech" = "XLK" }\n')


@pytest.mark.parametrize("body", ['market = ""', "market = 5", 'sectors = { "Energy" = "" }', 'sectors = { "Energy" = 3 }',
                                  'sectors = "XLE"'])
def test_a_bad_benchmarks_setting_is_a_config_error(tmp_path, body):
    with pytest.raises(C.ConfigError, match="benchmarks"):
        _cfg(tmp_path, f"[benchmarks]\n{body}\n")


def test_benchmarks_must_be_a_table(tmp_path):
    with pytest.raises(C.ConfigError, match="benchmarks"):
        _cfg(tmp_path, 'benchmarks = "SPY"\n')


# ---- the store ---------------------------------------------------------------------------------------

def test_mark_benchmarks_registers_missing_symbols_as_etfs(store):
    store.mark_benchmarks(["SPY", "XLK"])
    assert (_benchmark(store, "SPY"), _type(store, "SPY")) == (1, "etf")
    assert (_benchmark(store, "XLK"), _type(store, "XLK")) == (1, "etf")


def test_removing_a_benchmark_from_config_unmarks_it(store):
    store.mark_benchmarks(["SPY", "XLK"])
    store.mark_benchmarks(["SPY"])
    assert {r["symbol"]: r["benchmark"] for r in store.securities()}["XLK"] == 0


def test_unmarking_leaves_every_other_row_unmarked_too(store):
    store.upsert_security("AAA", "AAA", "stock")
    store.mark_benchmarks(["SPY"])
    assert _benchmark(store, "AAA") == 0 and _benchmark(store, "SPY") == 1


def test_a_benchmark_keeps_a_known_asset_type(store):
    store.upsert_security("SPY", "S&P 500 ETF", "mutual_fund")
    store.mark_benchmarks(["SPY"])
    assert _type(store, "SPY") == "mutual_fund"


def test_a_benchmark_of_unknown_type_becomes_an_etf(store):
    store.upsert_security("XLK", first_seen="2026-01-01")
    assert _type(store, "XLK") == "unknown"
    store.mark_benchmarks(["XLK"])
    assert _type(store, "XLK") == "etf"


def test_held_symbols_are_the_latest_positions(store):
    _hold(store, "SPY")
    store.mark_benchmarks(["SPY", "XLK"])
    assert store.held_symbols() == {"SPY"} and store.check_held_symbols() == ["SPY"]
    assert store.check_benchmark_symbols() == ["SPY", "XLK"]


def test_a_benchmark_is_never_parked(store):
    store.upsert_security("AAA", "AAA", "stock")
    store.upsert_security("XLK", first_seen="2026-01-01")
    assert store.is_parkable("XLK") and store.is_parkable("AAA")
    store.mark_benchmarks(["XLK"])
    assert not store.is_parkable("XLK") and store.is_parkable("AAA")


# ---- the universe ------------------------------------------------------------------------------------

def test_a_benchmark_is_not_exported_unless_held_or_listed(store, cfg):
    store.mark_benchmarks(["SPY", "XLK"])
    _hold(store, "SPY")                                                      # the owner holds SPY
    assert "SPY" in universe_symbols(store, cfg.watchlist, include_benchmarks=False)
    assert "XLK" not in universe_symbols(store, cfg.watchlist, include_benchmarks=False)
    assert "XLK" in universe_symbols(store, cfg.watchlist)
    assert store.held_symbols() == {"SPY"} and store.check_held_symbols() == ["SPY"]


def test_a_listed_benchmark_stays_in_the_export_universe(store, cfg):
    store.mark_benchmarks(["XLK"])
    cfg.watchlist.write_text("XLK\n")
    assert build_universe(store, cfg.watchlist, include_benchmarks=False) == ["XLK"]


def test_the_export_universe_does_not_mark_benchmarks(store, cfg):
    assert build_universe(store, cfg.watchlist, include_benchmarks=False) == []
    assert store.securities() == []


# ---- the sync ----------------------------------------------------------------------------------------

def _bars_for_any(symbol, start):
    return [PriceBar("2026-02-26", 10.0, 10.0, 0.0, 1.0), PriceBar("2026-02-27", 11.0, 11.0, 0.0, 1.0)]


def _fetch(fixtures: Path):
    def fetch(url, headers):
        if url == sec_cik.CIK_URL:
            return (fixtures / "sec" / "company_tickers.json").read_bytes()
        raise HttpError(404, url)
    return fetch


@pytest.fixture
def project(tmp_path: Path, fixtures: Path):
    C.init_project(tmp_path)
    (tmp_path / ".env").write_text("SEC_USER_AGENT=Sample Person s@example.com\n")     # no Tiingo token: yfinance bars
    return C.load_config(tmp_path)


def test_prices_step_registers_benchmarks_as_etfs_and_collects_them(project, fixtures):
    sync.run_sync(project, only=["prices"], fetch=_fetch(fixtures), now=NOW, yf=_bars_for_any, sleep=lambda s: None)
    s = Store.open(project.db_path, migrate=False)
    try:
        rows = {r["symbol"]: r for r in s.securities()}
        assert rows["SPY"]["benchmark"] == 1 and rows["SPY"]["asset_type"] == "etf"
        assert s.last_price_date("XLK") is not None
        assert {sym for sym, r in rows.items() if r["benchmark"]} == set(ELEVEN)
    finally:
        s.close()


def test_a_benchmark_the_provider_has_no_bars_for_is_not_parked(project, fixtures):
    for _ in range(4):
        sync.run_sync(project, only=["prices"], fetch=_fetch(fixtures), now=NOW, yf=lambda s, d: [], sleep=lambda s: None)
    s = Store.open(project.db_path, migrate=False)
    try:
        assert all(r["parked_reason"] is None for r in s.securities())
    finally:
        s.close()


def test_the_export_leaves_out_a_benchmark_that_is_not_held(project, fixtures, tmp_path):
    sync.run_sync(project, only=["prices"], fetch=_fetch(fixtures), now=NOW, yf=_bars_for_any, sleep=lambda s: None)
    out = tmp_path / "cockpit"
    out.mkdir()
    project.export_cockpit, project.export_dir = True, out
    sync.run_sync(project, only=["export"], fetch=_fetch(fixtures), now=NOW, yf=_bars_for_any, sleep=lambda s: None)
    prices = json.loads((out / "prices.json").read_text())
    assert "XLK" not in prices["by_ticker"] and "XLK" not in prices["misses"]
    assert "SPY" not in prices["by_ticker"]
