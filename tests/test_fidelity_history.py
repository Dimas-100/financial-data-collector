import shutil
from pathlib import Path

import pytest

from financial_data_collector.adapters import fidelity_history as fh
from financial_data_collector.adapters.base import read_text_lines

FIX = "History_SampleBrokerage_2026.csv"


def test_detect(fixtures: Path):
    assert fh.detect(read_text_lines(fixtures / FIX))
    assert not fh.detect(["Account Number,Account Name,Symbol"])


@pytest.mark.parametrize(
    "action, want",
    [
        ("YOU BOUGHT APPLE INC (AAPL) (Cash)", "buy"),
        ("YOU SOLD VTI (Cash)", "sell"),
        ("DIVIDEND RECEIVED KO (Cash)", "dividend"),
        ("REINVESTMENT KO (Cash)", "reinvest"),
        ("ELECTRONIC FUNDS TRANSFER RECEIVED (Cash)", "contribution"),
        ("CONTRIBUTION (Cash)", "contribution"),
        ("DIRECT DEPOSIT PAYROLL (Cash)", "contribution"),
        ("ELECTRONIC FUNDS TRANSFER PAID (Cash)", "withdrawal"),
        ("DISTRIBUTION (Cash)", "withdrawal"),
        ("INTEREST EARNED SPAXX", "interest"),
        ("ADVISOR FEE", "fee"),
        ("FEE CHARGED", "fee"),
        ("TRANSFERRED FROM VS X12", "transfer"),
        ("JOURNALED SHARES (Cash)", "other"),
        ("EXCHANGE FIDELITY 500 INDEX FUND (FXAIX) (Cash)", "other"),
        ("", "other"),
    ],
)
def test_classify_action(action, want):
    assert fh.classify_action(action) == want


def test_parse_with_explicit_account(fixtures: Path):
    rows = fh.parse(fixtures / FIX, account="Sample Brokerage")
    assert len(rows) == 7
    assert all(r.account.label == "Sample Brokerage" for r in rows)
    assert all(r.source == "fidelity_csv" for r in rows)
    assert [r.type for r in rows] == [
        "buy", "dividend", "reinvest", "contribution", "sell", "withdrawal", "other"]
    buy = rows[0]
    assert (buy.trade_date, buy.settlement_date) == ("2026-01-05", "2026-01-07")
    assert (buy.symbol, buy.units, buy.price, buy.amount, buy.fee) == ("AAPL", 10.0, 100.0, -1000.0, 0.0)
    assert buy.description.startswith("YOU BOUGHT")
    div = rows[1]
    assert div.units is None and div.amount == 9.8 and div.fee is None
    contrib = rows[3]
    assert contrib.symbol is None and contrib.amount == 500.0
    sell = rows[4]
    assert sell.units == -2.0 and sell.fee == 0.01
    journal = rows[6]
    assert journal.amount is None and journal.units == 3.0 and journal.symbol == "KO"


def test_account_from_filename(fixtures: Path):
    assert fh.account_from_filename(fixtures / FIX) == "SampleBrokerage"
    assert fh.account_from_filename(Path("History.csv")) is None
    assert fh.account_from_filename(Path("Accounts_History.csv")) is None


def test_parse_falls_back_to_filename(fixtures: Path):
    rows = fh.parse(fixtures / FIX)
    assert rows[0].account.label == "SampleBrokerage"


def test_parse_without_any_account_raises(fixtures: Path, tmp_path: Path):
    dst = tmp_path / "Accounts_History.csv"
    shutil.copy(fixtures / FIX, dst)
    with pytest.raises(fh.AccountUnknown):
        fh.parse(dst)


def test_account_column_wins(fixtures: Path, tmp_path: Path):
    lines = read_text_lines(fixtures / FIX)
    out = []
    for line in lines:
        if line.startswith("Run Date,"):
            out.append("Account," + line)
        elif line.startswith(" 0"):
            out.append("Sample Roth," + line)
        else:
            out.append(line)
    dst = tmp_path / "Accounts_History.csv"
    dst.write_text("\n".join(out), encoding="utf-8")
    rows = fh.parse(dst)
    assert {r.account.label for r in rows} == {"Sample Roth"}
    assert rows[0].account.account_type == "roth_ira"


def test_exchange_rows_follow_the_sign_of_quantity():
    assert fh.classify_action("EXCHANGE FIDELITY 500 INDEX FUND (FXAIX) (Cash)", units=3.5) == "buy"
    assert fh.classify_action("EXCHANGE FIDELITY 500 INDEX FUND (FXAIX) (Cash)", units=-3.5) == "sell"


def test_account_numbers_scrubbed_from_description(fixtures: Path, tmp_path: Path):
    lines = read_text_lines(fixtures / FIX)
    lines.insert(4, " 01/21/2026, TRANSFERRED FROM VS Z12345678, , , Cash, , , , , , 50.00, 1349.99, ")
    dst = tmp_path / "History_SampleBrokerage_2026.csv"
    dst.write_text("\n".join(lines), encoding="utf-8")
    rows = fh.parse(dst)
    moved = [r for r in rows if r.type == "transfer"][0]
    assert "Z12345678" not in moved.description and "<acct>" in moved.description


def test_zero_quantity_is_stored_as_null(fixtures: Path, tmp_path: Path):
    lines = read_text_lines(fixtures / FIX)
    lines.insert(4, " 01/22/2026, DIVIDEND RECEIVED APPLE INC (AAPL) (Cash), AAPL, APPLE INC, Cash, , 0, , , , 2.50, 1352.49, ")
    dst = tmp_path / "History_SampleBrokerage_2026.csv"
    dst.write_text("\n".join(lines), encoding="utf-8")
    row = [r for r in fh.parse(dst) if r.trade_date == "2026-01-22"][0]
    assert row.units is None and row.amount == 2.5     # matches the SnapTrade adapter, so cross-source dedupe keys agree
