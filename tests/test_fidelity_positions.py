import shutil
from pathlib import Path

from financial_data_collector.adapters import fidelity_positions as fp
from financial_data_collector.adapters.base import read_text_lines

FIX = "Portfolio_Positions_Jan-15-2026.csv"


def test_detect(fixtures: Path):
    assert fp.detect(read_text_lines(fixtures / FIX))
    assert not fp.detect(["Run Date,Action,Symbol,Description,Type"])
    assert not fp.detect([])


def test_parse_positions_cash_and_dates(fixtures: Path):
    snap = fp.parse(fixtures / FIX)
    assert snap.as_of_date == "2026-01-15"
    assert snap.source == "fidelity_csv"
    assert [p.symbol for p in snap.positions] == ["AAPL", "KO", "VTI"]
    aapl = snap.positions[0]
    assert aapl.account.label == "Sample Brokerage"
    assert aapl.account.institution == "fidelity"
    assert aapl.account.account_type == "brokerage"
    assert (aapl.quantity, aapl.price, aapl.market_value) == (10.0, 100.0, 1000.0)
    assert (aapl.cost_basis_total, aapl.avg_cost, aapl.unrealized_pnl) == (900.0, 90.0, 100.0)
    ko = snap.positions[1]
    assert ko.cost_basis_total is None and ko.unrealized_pnl is None
    vti = snap.positions[2]
    assert vti.account.label == "Sample Roth" and vti.account.account_type == "roth_ira"
    assert vti.account.slug == "roth"
    assert len(snap.cash) == 1
    assert snap.cash[0].account.label == "Sample Brokerage" and snap.cash[0].amount == 25.5
    assert snap.warnings == []


def test_account_numbers_never_leak(fixtures: Path):
    snap = fp.parse(fixtures / FIX)
    assert "Z12345678" not in repr(snap) and "Z87654321" not in repr(snap)


def test_renamed_file_dates_from_footer(fixtures: Path, tmp_path: Path):
    dst = tmp_path / "Portfolio_Positions (1).csv"
    shutil.copy(fixtures / FIX, dst)
    snap = fp.parse(dst)
    assert snap.as_of_date == "2026-01-15"
    assert snap.warnings == []


def test_no_date_anywhere_falls_back_to_mtime_with_warning(fixtures: Path, tmp_path: Path):
    lines = read_text_lines(fixtures / FIX)
    body = [l for l in lines if not l.startswith('"Date downloaded')]
    dst = tmp_path / "positions.csv"
    dst.write_text("\n".join(body), encoding="utf-8")
    snap = fp.parse(dst)
    assert len(snap.as_of_date) == 10
    assert any("mtime" in w for w in snap.warnings)


def test_bom_and_crlf(fixtures: Path, tmp_path: Path):
    raw = (fixtures / FIX).read_text(encoding="utf-8").replace("\n", "\r\n")
    dst = tmp_path / "Portfolio_Positions_Jan-15-2026.csv"
    dst.write_bytes(("﻿" + raw).encode("utf-8"))
    snap = fp.parse(dst)
    assert len(snap.positions) == 3
