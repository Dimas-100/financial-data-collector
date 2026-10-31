from pathlib import Path

from financial_data_collector import universe as U
from financial_data_collector.store import Store


def test_read_watchlist(tmp_path: Path):
    p = tmp_path / "watchlist.txt"
    p.write_text("# comment\n brk/b \n\nko\nKO\n")
    assert U.read_watchlist(p) == ["BRK.B", "KO"]
    assert U.read_watchlist(tmp_path / "missing.txt") == []


def test_build_universe(tmp_path: Path):
    s = Store.open(tmp_path / "w.db")
    s.upsert_security("AAPL", asset_type="stock", first_seen="2026-01-01")
    s.upsert_security("SPAXX", asset_type="money_market", first_seen="2026-01-01")
    s.upsert_security("FDRXX", first_seen="2026-01-01")             # money market by symbol list
    s.upsert_security("BTC", asset_type="crypto", first_seen="2026-01-01")
    wl = tmp_path / "watchlist.txt"
    wl.write_text("VTI\n")
    assert U.build_universe(s, wl) == ["AAPL", "VTI"]
    assert s.query("SELECT COUNT(*) FROM securities WHERE symbol='VTI'")[0][0] == 1
    s.close()


def test_classify_overrides_are_applied(tmp_path: Path):
    s = Store.open(tmp_path / "w.db")
    s.upsert_security("VTI", first_seen="2026-01-01")
    s.upsert_security("BTC", first_seen="2026-01-01")
    out = U.build_universe(s, tmp_path / "none.txt", classify={"VTI": "etf", "BTC": "crypto"})
    assert out == ["VTI"]
    assert s.query("SELECT asset_type FROM securities WHERE symbol='VTI'")[0][0] == "etf"
    s.close()


def test_read_investing_tickers(fixtures: Path):
    assert U.read_investing_tickers(fixtures / "investing") == ["AAPL", "BRK.B", "GEV", "KO", "VTI"]   # GEV via scan-history.json
    assert U.read_investing_tickers(fixtures / "nope") == []


def test_build_universe_includes_investing(tmp_path: Path, fixtures: Path):
    s = Store.open(tmp_path / "w.db")
    assert U.build_universe(s, tmp_path / "none.txt", investing_dir=fixtures / "investing") == ["AAPL", "BRK.B", "GEV", "KO", "VTI"]
    assert s.query("SELECT COUNT(*) FROM securities")[0][0] == 5
    s.close()


def test_read_investing_tickers_includes_scan_promoted_names(fixtures: Path):
    # data/scan-history.json names seen at least `promote_count` times join the universe
    assert U.read_investing_tickers(fixtures / "investing", promote_count=3) == ["AAPL", "BRK.B", "GEV", "KO", "VTI"]
    assert U.read_investing_tickers(fixtures / "investing", promote_count=99) == ["AAPL", "BRK.B", "KO", "VTI"]


def test_universe_symbols_is_pure(tmp_path: Path):
    """The same answer as build_universe, without the writes: a watchlist name counts but gets no row, a
    [classify] override applies to the answer but not to the table."""
    s = Store.open(tmp_path / "w.db")
    s.upsert_security("AAPL", asset_type="stock", first_seen="2026-01-01")
    s.upsert_security("SPAXX", asset_type="money_market", first_seen="2026-01-01")
    s.upsert_security("BTC", first_seen="2026-01-01")
    wl = tmp_path / "watchlist.txt"
    wl.write_text("VTI\nFDRXX\n")
    assert U.universe_symbols(s, wl, classify={"BTC": "crypto"}) == ["AAPL", "VTI"]
    assert s.query("SELECT COUNT(*) FROM securities")[0][0] == 3
    assert s.query("SELECT asset_type FROM securities WHERE symbol = 'BTC'")[0][0] == "unknown"
    assert U.build_universe(s, wl, classify={"BTC": "crypto"}) == ["AAPL", "VTI"]
    assert s.query("SELECT COUNT(*) FROM securities")[0][0] == 5
    s.close()


# ---- parking a symbol nothing knows --------------------------------------------------------------------

def _no_bars_runs(cfg, store, symbol, n):
    from datetime import date
    from financial_data_collector.collectors import prices as P

    def fetch(url, headers):
        return b"[]"

    for _ in range(n):
        P.collect_prices(store, [symbol], cfg, fetch=fetch, today=date(2026, 1, 15),
                         yf=lambda s, start: [], sleep=lambda s: None)


def _cfg(tmp_path: Path):
    from financial_data_collector import config as C
    C.init_project(tmp_path)
    (tmp_path / ".env").write_text("TIINGO_API_TOKEN=tok\n")
    return C.load_config(tmp_path)


def test_symbol_with_no_cik_no_bars_no_position_parks_after_three_runs(tmp_path: Path):
    cfg = _cfg(tmp_path)
    store = Store.open(cfg.db_path)
    store.upsert_security("PRIVCO", first_seen="2026-01-01")
    wl = tmp_path / "watchlist.txt"
    wl.write_text("PRIVCO\n")
    _no_bars_runs(cfg, store, "PRIVCO", U.PARK_AFTER - 1)
    assert {r["symbol"]: r for r in store.securities()}["PRIVCO"]["parked_reason"] is None
    assert "PRIVCO" in U.universe_symbols(store, wl)
    _no_bars_runs(cfg, store, "PRIVCO", 1)
    sec = {r["symbol"]: r for r in store.securities()}
    assert sec["PRIVCO"]["parked_reason"] == "no price from any provider, no CIK, not held"
    assert "PRIVCO" not in U.universe_symbols(store, wl)
    assert "PRIVCO" not in U.build_universe(store, wl)   # the watchlist does not bring it back
    store.close()


def test_a_held_or_cik_symbol_is_not_parked(tmp_path: Path):
    from financial_data_collector.models import AccountRef, PositionRow, Snapshot
    cfg = _cfg(tmp_path)
    store = Store.open(cfg.db_path)
    store.upsert_security("HELDCO", first_seen="2026-01-01")
    store.upsert_security("CIKCO", first_seen="2026-01-01")
    store.set_security_cik("CIKCO", "0000000001", "Cik Co")
    acct = AccountRef("Brokerage")
    store.write_snapshot(Snapshot("2026-01-02", "csv", [PositionRow(acct, "HELDCO", None, 1.0, 10.0, 10.0)], []))
    _no_bars_runs(cfg, store, "HELDCO", 4)
    _no_bars_runs(cfg, store, "CIKCO", 4)
    assert all(r["parked_reason"] is None for r in store.securities())
    store.close()


def test_a_parked_symbol_that_appears_in_a_position_is_unparked(tmp_path: Path):
    from financial_data_collector.models import AccountRef, PositionRow, Snapshot
    store = Store.open(tmp_path / "w.db")
    store.upsert_security("PRIVCO", first_seen="2026-01-01")
    store.park("PRIVCO", "no price from any provider, no CIK, not held")
    assert {r["symbol"]: r for r in store.securities()}["PRIVCO"]["parked_reason"] is not None
    store.write_snapshot(Snapshot("2026-01-02", "csv",
                                  [PositionRow(AccountRef("Brokerage"), "PRIVCO", None, 1.0, 10.0, 10.0)], []))
    row = {r["symbol"]: r for r in store.securities()}["PRIVCO"]
    assert row["parked_reason"] is None and row["miss_runs"] == 0
    store.close()


def test_one_good_bar_resets_the_miss_count(tmp_path: Path):
    from datetime import date
    from financial_data_collector.collectors import prices as P
    from financial_data_collector.models import PriceBar
    cfg = _cfg(tmp_path)
    store = Store.open(cfg.db_path)
    store.upsert_security("PRIVCO", first_seen="2026-01-01")
    _no_bars_runs(cfg, store, "PRIVCO", 2)
    assert {r["symbol"]: r for r in store.securities()}["PRIVCO"]["miss_runs"] == 2
    P.collect_prices(store, ["PRIVCO"], cfg, fetch=lambda u, h: b"[]", today=date(2026, 1, 15),
                     yf=lambda s, start: [PriceBar("2026-01-14", 5.0, 5.0)], sleep=lambda s: None)
    assert {r["symbol"]: r for r in store.securities()}["PRIVCO"]["miss_runs"] == 0
    _no_bars_runs(cfg, store, "PRIVCO", 1)   # nothing new now: not a miss, history exists
    assert {r["symbol"]: r for r in store.securities()}["PRIVCO"]["miss_runs"] == 0
    store.close()


def test_a_provider_failure_is_not_a_miss(tmp_path: Path):
    from datetime import date
    from financial_data_collector.collectors import prices as P
    cfg = _cfg(tmp_path)
    store = Store.open(cfg.db_path)
    store.upsert_security("PRIVCO", first_seen="2026-01-01")

    def boom(symbol, start):
        raise RuntimeError("down")

    for _ in range(4):
        P.collect_prices(store, ["PRIVCO"], cfg, fetch=lambda u, h: b"[]", today=date(2026, 1, 15),
                         yf=boom, sleep=lambda s: None)
    row = {r["symbol"]: r for r in store.securities()}["PRIVCO"]
    assert row["miss_runs"] == 0 and row["parked_reason"] is None
    store.close()
