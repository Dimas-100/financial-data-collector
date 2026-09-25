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
