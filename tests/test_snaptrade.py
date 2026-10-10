import json
from pathlib import Path

from financial_data_collector.adapters import snaptrade as st


def test_parse_positions(fixtures: Path):
    snap = st.parse_positions(fixtures / "snaptrade" / "live-positions.json")
    assert snap.as_of_date == "2026-01-16"           # midnight UTC stays that UTC date
    assert snap.fetched_at == "2026-01-16T00:00:00.000Z"
    assert snap.source == "snaptrade"
    assert [(p.account.label, p.symbol) for p in snap.positions] == [
        ("Sample Brokerage", "AAPL"), ("Webull Sample Cash", "KO"), ("Sample Roth", "VTI")]
    aapl = snap.positions[0]
    assert (aapl.quantity, aapl.price, aapl.market_value) == (10.0, 101.0, 1010.0)
    assert (aapl.avg_cost, aapl.cost_basis_total, aapl.unrealized_pnl) == (90.0, 900.0, 110.0)
    assert aapl.account.institution == "fidelity" and aapl.account.slug == "brokerage"
    ko = snap.positions[1]
    assert ko.account.institution == "webull" and ko.account.account_type == "brokerage"
    assert [(c.account.label, c.amount) for c in snap.cash] == [
        ("Sample Brokerage", 25.5), ("Webull Sample Cash", 10.0), ("Sample Roth", 0.2)]


def test_local_time_never_used(fixtures: Path, tmp_path: Path):
    data = json.loads((fixtures / "snaptrade" / "live-positions.json").read_text())
    data["fetchedAt"] = "2026-03-01T03:30:00.000Z"
    p = tmp_path / "live-positions.json"
    p.write_text(json.dumps(data))
    assert st.parse_positions(p).as_of_date == "2026-03-01"


def test_parse_activity(fixtures: Path):
    rows = st.parse_activity(fixtures / "snaptrade" / "live-activity.json")
    assert [r.type for r in rows] == ["buy", "dividend", "reinvest", "contribution", "sell", "fee"]
    buy = rows[0]
    assert (buy.trade_date, buy.settlement_date, buy.symbol) == ("2026-01-05", "2026-01-07", "AAPL")
    assert (buy.units, buy.price, buy.amount, buy.fee) == (10.0, 100.0, -1000.0, 0.0)
    assert buy.source == "snaptrade" and buy.description == "BUY"
    div = rows[1]
    assert div.units is None and div.price is None and div.amount == 9.8
    contrib = rows[3]
    assert contrib.symbol is None and contrib.account.label == "Sample Roth"
    assert contrib.account.account_type == "roth_ira"
    sell = rows[4]
    assert sell.symbol == "BRK.B" and sell.account.institution == "webull"


def _activity(tmp_path: Path, records: list[tuple[str, str, float, str | None]]) -> Path:
    p = tmp_path / "live-activity.json"
    p.write_text(json.dumps({"fetchedAt": "2026-10-10T16:00:00.000Z", "activities": [
        {"tradeDate": stamp, "settlementDate": stamp, "type": kind, "symbol": symbol, "units": 0.39 if symbol else 0,
         "price": 0, "amount": amount, "fee": 0, "accountLabel": "Webull Sample Cash"}
        for stamp, kind, amount, symbol in records]}))
    return p


def test_a_deposit_snaptrade_sends_twice_is_one_row(tmp_path: Path):
    # SnapTrade re-emits a Webull deposit with the same stamp to the second: once without fractional seconds, once
    # with. Nobody makes two deposits in one second, so that pair is one deposit.
    rows = st.parse_activity(_activity(tmp_path, [
        ("2026-10-07T12:15:02Z", "CONTRIBUTION", 100.0, None),
        ("2026-10-07T12:15:02.324000Z", "CONTRIBUTION", 100.0, None),
    ]))
    assert [(r.trade_date, r.type, r.amount) for r in rows] == [("2026-10-07", "contribution", 100.0)]


def test_repeats_at_different_seconds_or_with_the_same_stamp_stay_separate(tmp_path: Path):
    # Three $100 deposits at different times are three deposits; two buys the broker stamps identically (Fidelity's
    # midnight stamps) can't be told apart here and both stay, as before.
    rows = st.parse_activity(_activity(tmp_path, [
        ("2026-08-13T19:55:04.623000Z", "CONTRIBUTION", 100.0, None),
        ("2026-08-13T18:00:02.668000Z", "CONTRIBUTION", 100.0, None),
        ("2026-08-13T16:20:01.999000Z", "CONTRIBUTION", 100.0, None),
        ("2026-06-24T04:00:00Z", "BUY", -100.0, "FXAIX"),
        ("2026-06-24T04:00:00Z", "BUY", -100.0, "FXAIX"),
    ]))
    assert [(r.trade_date, r.type) for r in rows] == [("2026-08-13", "contribution")] * 3 + [("2026-06-24", "buy")] * 2


def test_find_snapshot_files(fixtures: Path):
    files = st.find_snapshot_files(fixtures / "snaptrade")
    assert [f.name for f in files] == ["live-positions-2026-01-10.json", "live-positions.json"]
    assert st.find_snapshot_files(fixtures / "nope") == []
