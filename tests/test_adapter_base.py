from pathlib import Path

from financial_data_collector.adapters import base
from financial_data_collector.models import AccountRef, PositionRow


def test_clean_number():
    assert base.clean_number("$1,234.56") == 1234.56
    assert base.clean_number("+$12.00") == 12.0
    assert base.clean_number("-$0.50") == -0.5
    assert base.clean_number("12.5%") == 12.5
    assert base.clean_number("--") is None
    assert base.clean_number("") is None
    assert base.clean_number(None) is None
    assert base.clean_number("n/a") is None


def test_dedupe_key_stable_and_rounded():
    a = base.dedupe_key("Sample Brokerage", "2026-01-02", "buy", "AAPL", 1.00001, -100.00004)
    b = base.dedupe_key("Sample Brokerage", "2026-01-02", "buy", "AAPL", 1.0, -100.0)
    c = base.dedupe_key("Sample Brokerage", "2026-01-02", "buy", "AAPL", 2.0, -100.0)
    assert a == b
    assert a != c
    assert len(a) == 64


def test_dedupe_key_handles_none():
    k = base.dedupe_key("Sample Roth", "2026-01-02", "contribution", None, None, 500.0)
    assert len(k) == 64


def test_sha256_file(tmp_path: Path):
    p = tmp_path / "x.csv"
    p.write_bytes(b"abc")
    assert base.sha256_file(p) == "ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad"


def test_read_text_lines_strips_bom_and_crlf(tmp_path: Path):
    p = tmp_path / "x.csv"
    p.write_bytes("﻿A,B\r\n1,2\r\n".encode("utf-8"))
    assert base.read_text_lines(p) == ["A,B", "1,2", ""]


def test_infer_account_type():
    assert base.infer_account_type("ROTH IRA") == "roth_ira"
    assert base.infer_account_type("Traditional IRA") == "traditional_ira"
    assert base.infer_account_type("Individual - TOD") == "brokerage"
    assert base.infer_account_type("Webull Crypto") == "crypto"


def test_models_are_frozen():
    acct = AccountRef(label="Sample Brokerage")
    row = PositionRow(acct, "AAPL", "APPLE INC", 10, 100.0, 1000.0)
    try:
        row.quantity = 5  # type: ignore[misc]
    except Exception as e:  # FrozenInstanceError
        assert "FrozenInstanceError" in type(e).__name__
    else:
        raise AssertionError("PositionRow should be frozen")
