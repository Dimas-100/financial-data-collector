"""The weekly split's storage (plan 4, Task 4): the proposal tables, held through their views, and the Store reads
the split uses."""
import json
import sqlite3
from pathlib import Path

import pytest

from financial_data_collector.check.findings import Finding
from financial_data_collector.models import (
    AccountRef, AllocationProposal, CashRow, PositionRow, Snapshot, SplitLine,
)
from financial_data_collector.store import Store

ACCT = AccountRef("Sample Brokerage", "brokerage", "fidelity", "brokerage")
ROTH = AccountRef("Sample Roth", "roth", "fidelity", "roth_ira")
NOW = "2026-10-31T12:00:00Z"


@pytest.fixture
def store(tmp_path: Path):
    s = Store.open(tmp_path / "w.db")
    yield s
    s.close()


def _src(label: str) -> dict:
    return {"kind": "derived", "label": label, "date": "2026-10-30", "ref": label}


def _proposal(account_key: str = "brokerage", cash: float = 100.0, symbols=("VOO", "KO")) -> AllocationProposal:
    lines = tuple(SplitLine(s, 40 - 10 * i, 55.5, "cheap" if i == 0 else "usual", 1.5 if i == 0 else 1.0,
                            ("cheap_by:pe", "band_top") if i == 0 else ()) for i, s in enumerate(symbols))
    return AllocationProposal(account_key, "2026-10-30", cash, len(lines), cash - sum(l.dollars for l in lines),
                              lines, tuple((_src(f"line {l.symbol}"),) for l in lines),
                              ({"code": "under_min", "symbol": "JNJ"},), (_src("cash_balances"),))


def _hold(store: Store, proposal_id: int, account_key: str = "brokerage") -> None:
    store.check_record("split", [Finding("split", account_key, f"proposal:{proposal_id}", "2026-10-30", None, "hold",
                                         "a line is wrong")], NOW)


def test_write_and_read_an_open_proposal(store: Store):
    p = _proposal()
    pid = store.write_proposal(p, created_at=NOW, checked_at="2026-10-31T11:00:00Z", replaces=None)
    got = store.open_proposal("brokerage")
    assert got["id"] == pid and got["status"] == "open" and got["closed_at"] is None
    assert (got["account_key"], got["cash_date"], got["cash"], got["placed"], got["waits"]) == \
           ("brokerage", "2026-10-30", 100.0, 2, 30.0)
    assert got["created_at"] == NOW and got["checked_at"] == "2026-10-31T11:00:00Z"
    assert got["notes"] == [{"code": "under_min", "symbol": "JNJ"}]
    assert got["sources"] == [_src("cash_balances")]
    assert [l["symbol"] for l in got["lines"]] == ["VOO", "KO"]
    first = got["lines"][0]
    assert (first["dollars"], first["target_gap"], first["band"], first["tilt"]) == (40, 55.5, "cheap", 1.5)
    assert list(first["reasons"]) == ["cheap_by:pe", "band_top"]
    assert first["sources"] == [_src("line VOO")]
    assert list(got["lines"][1]["reasons"]) == []
    assert store.open_proposal("roth") is None
    raw = store.query("SELECT notes, sources FROM allocation_proposals_raw")[0]
    assert raw["notes"] == json.dumps([{"code": "under_min", "symbol": "JNJ"}], sort_keys=True)


def test_one_open_proposal_per_account(store: Store):
    store.write_proposal(_proposal(), created_at=NOW, checked_at=None, replaces=None)
    with pytest.raises(sqlite3.IntegrityError):
        store.write_proposal(_proposal(), created_at=NOW, checked_at=None, replaces=None)
    assert store.query("SELECT COUNT(*) FROM allocation_proposals_raw")[0][0] == 1       # nothing half-written
    assert store.query("SELECT COUNT(*) FROM allocation_lines_raw")[0][0] == 2
    store.write_proposal(_proposal("roth"), created_at=NOW, checked_at=None, replaces=None)   # another account is fine


def test_zero_dollars_is_refused_by_the_table(store: Store):
    bad = AllocationProposal("brokerage", "2026-10-30", 100.0, 1, 100.0,
                             (SplitLine("VOO", 0, 10.0, "usual", 1.0, ()),), ((),), (), ())
    with pytest.raises(sqlite3.IntegrityError):
        store.write_proposal(bad, created_at=NOW, checked_at=None, replaces=None)
    assert store.query("SELECT COUNT(*) FROM allocation_proposals_raw")[0][0] == 0


def test_replace_closes_the_old_one(store: Store):
    old = store.write_proposal(_proposal(cash=100.0), created_at="2026-10-24T12:00:00Z", checked_at=None, replaces=None)
    new = store.write_proposal(_proposal(cash=160.0), created_at=NOW, checked_at=None, replaces=old)
    assert new != old
    r = store.query("SELECT status, closed_at, closed_cash FROM allocation_proposals_raw WHERE id = ?", (old,))[0]
    assert tuple(r) == ("replaced", NOW, 160.0)
    assert store.open_proposal("brokerage")["id"] == new
    assert store.last_spent_proposal("brokerage") is None
    assert store.query("SELECT COUNT(*) FROM allocation_lines_raw WHERE proposal_id = ?", (old,))[0][0] == 2


def test_close_marks_spent(store: Store):
    pid = store.write_proposal(_proposal(), created_at=NOW, checked_at=None, replaces=None)
    store.close_proposal(pid, closed_at="2026-11-02T12:00:00Z", closed_cash=3.0)
    assert store.open_proposal("brokerage") is None
    got = store.last_spent_proposal("brokerage")
    assert (got["id"], got["status"], got["closed_at"], got["closed_cash"]) == (pid, "spent", "2026-11-02T12:00:00Z", 3.0)
    assert len(got["lines"]) == 2
    nxt = store.write_proposal(_proposal(), created_at="2026-11-09T12:00:00Z", checked_at=None, replaces=None)
    store.close_proposal(nxt, closed_at="2026-11-10T12:00:00Z", closed_cash=0.0)
    assert store.last_spent_proposal("brokerage")["id"] == nxt                        # the newest by closed_at
    assert store.last_spent_proposal("roth") is None


def test_replacing_or_closing_what_is_not_open_is_refused(store: Store):
    pid = store.write_proposal(_proposal(), created_at=NOW, checked_at=None, replaces=None)
    store.close_proposal(pid, closed_at=NOW, closed_cash=0.0)
    with pytest.raises(ValueError):
        store.close_proposal(pid, closed_at=NOW, closed_cash=0.0)
    with pytest.raises(ValueError):
        store.write_proposal(_proposal(), created_at=NOW, checked_at=None, replaces=pid)
    assert store.open_proposal("brokerage") is None                                   # the refused write left nothing


def test_view_holds_a_proposal(store: Store):
    pid = store.write_proposal(_proposal(), created_at=NOW, checked_at=None, replaces=None)
    other = store.write_proposal(_proposal("roth"), created_at=NOW, checked_at=None, replaces=None)
    plain = store.query("SELECT status, hold_ids FROM allocation_proposals WHERE id = ?", (pid,))[0]
    assert tuple(plain) == ("open", None)
    _hold(store, pid)
    v = store.query("SELECT status, hold_ids, cash FROM allocation_proposals WHERE id = ?", (pid,))[0]
    assert v["status"] == "held" and v["hold_ids"] and v["cash"] == 100.0
    lines = store.query("SELECT dollars, held FROM allocation_lines WHERE proposal_id = ?", (pid,))
    assert [(l["dollars"], l["held"]) for l in lines] == [(None, 1), (None, 1)]
    assert [tuple(l) for l in store.query(
        "SELECT dollars, held FROM allocation_lines WHERE proposal_id = ? ORDER BY dollars DESC", (other,))] == [(40, 0), (30, 0)]
    assert store.query("SELECT status FROM allocation_proposals WHERE id = ?", (other,))[0][0] == "open"
    assert store.query("SELECT status FROM allocation_proposals_raw WHERE id = ?", (pid,))[0][0] == "open"   # raw untouched
    assert store.query("SELECT dollars FROM allocation_lines_raw WHERE proposal_id = ? ORDER BY dollars DESC", (pid,))[0][0] == 40
    store.check_record("split", [], "2026-10-31T13:00:00Z")                          # cleared
    assert tuple(store.query("SELECT status, hold_ids FROM allocation_proposals WHERE id = ?", (pid,))[0]) == ("open", None)
    assert tuple(store.query("SELECT dollars, held FROM allocation_lines WHERE proposal_id = ? ORDER BY dollars DESC",
                             (pid,))[0]) == (40, 0)


def test_a_closed_proposal_is_never_shown_held(store: Store):
    pid = store.write_proposal(_proposal(), created_at=NOW, checked_at=None, replaces=None)
    store.close_proposal(pid, closed_at=NOW, closed_cash=0.0)
    _hold(store, pid)
    assert store.query("SELECT status FROM allocation_proposals WHERE id = ?", (pid,))[0][0] == "spent"
    assert store.query("SELECT held FROM allocation_lines WHERE proposal_id = ?", (pid,))[0][0] == 0


def test_check_split_rows_lists_open_raw_proposals(store: Store):
    a = store.write_proposal(_proposal(), created_at=NOW, checked_at=None, replaces=None)
    b = store.write_proposal(_proposal("roth"), created_at=NOW, checked_at=None, replaces=None)
    store.close_proposal(b, closed_at=NOW, closed_cash=0.0)
    _hold(store, a)                                                                  # raw rows, holds or not
    rows = store.check_split_rows()
    assert [r["id"] for r in rows] == [a]
    r = rows[0]
    assert (r["account_key"], r["cash_date"], r["cash"]) == ("brokerage", "2026-10-30", 100.0)
    assert [(l["symbol"], l["dollars"]) for l in r["lines"]] == [("VOO", 40), ("KO", 30)]
    assert r["lines"][0]["sources"] == [_src("line VOO")]


def _cash(store: Store, acct: AccountRef, date: str, amount: float, currency: str = "USD") -> None:
    store.write_snapshot(Snapshot(date, "fidelity_csv", [], [CashRow(acct, amount, currency)]))


def test_split_cash_low_since(store: Store):
    for d, amt in (("2026-10-01", 500.0), ("2026-10-08", 40.0), ("2026-10-15", 90.0), ("2026-10-22", 120.0)):
        _cash(store, ACCT, d, amt)
    _cash(store, ACCT, "2026-10-29", 7.0, "EUR")                                   # not dollars
    _cash(store, ROTH, "2026-10-22", 10.0)
    assert store.split_cash_low_since("Sample Brokerage", "2026-10-09") == 90.0
    assert store.split_cash_low_since("Sample Brokerage", "2026-10-08") == 40.0     # inclusive of the day itself
    assert store.split_cash_low_since("Sample Brokerage", "2026-10-23") is None
    assert store.split_cash_low_since("Nobody", "2026-01-01") is None
    assert store.split_cash() == {"Sample Brokerage": ("2026-10-22", 120.0), "Sample Roth": ("2026-10-22", 10.0)}


def test_split_positions(store: Store):
    store.write_snapshot(Snapshot("2026-10-30", "fidelity_csv",
                                  [PositionRow(ACCT, "VOO", "VANGUARD S&P 500", 2.0, 500.0, 1000.0)], []))
    store.upsert_security("VOO", None, "etf", "2026-10-30")
    rows = store.split_positions()
    assert [(r["account"], r["as_of_date"], r["symbol"], r["market_value"]) for r in rows] == \
           [("Sample Brokerage", "2026-10-30", "VOO", 1000.0)]
    assert set(rows[0]) == {"account", "as_of_date", "symbol", "market_value", "asset_type"}


def test_split_scores_come_from_the_view(store: Store):
    store.conn.execute(
        "INSERT INTO valuation_scores_raw (symbol, date, metric, value, percentile_5y, band, status, inputs, sources) "
        "VALUES ('AAPL','2026-10-30','pe',25.0,10.0,'cheap','ok',',pe,close,', ?)", (json.dumps([_src("x")]),))
    store.conn.commit()
    got = store.split_scores(["AAPL", "KO"])
    assert got["KO"] == []
    (row,) = got["AAPL"]
    assert (row["symbol"], row["metric"], row["band"], row["status"], row["inputs"]) == ("AAPL", "pe", "cheap", "ok", ",pe,close,")
    assert row["sources"] == [_src("x")] and row["percentile_5y"] == 10.0
    assert {"date", "value", "status_reason", "checked_at"} <= set(row)
    store.check_record("closes", [Finding("closes", "AAPL", "pe", "2026-10-01", None, "hold", "bad")], NOW)
    assert store.split_scores(["AAPL"])["AAPL"][0]["band"] is None                    # holds applied


def test_counts_include_the_new_tables(store: Store):
    c = store.counts()
    assert c["allocation_proposals_raw"] == 0 and c["allocation_lines_raw"] == 0
    store.write_proposal(_proposal(), created_at=NOW, checked_at=None, replaces=None)
    c = store.counts()
    assert c["allocation_proposals_raw"] == 1 and c["allocation_lines_raw"] == 2
