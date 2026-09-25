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


def test_find_snapshot_files(fixtures: Path):
    files = st.find_snapshot_files(fixtures / "snaptrade")
    assert [f.name for f in files] == ["live-positions-2026-01-10.json", "live-positions.json"]
    assert st.find_snapshot_files(fixtures / "nope") == []
