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


# ---------------------------------------------------------------------------------------------------------------------
# The lifecycle (Task 5): split.run opens, replaces and closes proposals as each account's waiting cash moves
# ---------------------------------------------------------------------------------------------------------------------

import dataclasses
from datetime import datetime, timezone

from financial_data_collector import config as C
from financial_data_collector.config import AllocationSettings
from financial_data_collector.derive import split as S

TARGETS_DOC = {
    "last_revised": "2026-10-01",
    "brokerage": [
        {"ticker": "VOO", "type": "pct_of_account", "target": 50.0},
        {"ticker": "AAPL", "type": "pct_of_account", "target": 20.0, "rebalance": "contributions"},
        {"ticker": "KO", "type": "pct_of_account", "target": 30.0, "rebalance": "hold"},
    ],
    "roth": [
        {"ticker": "VFIAX", "type": "pct_of_account", "target": 90.0},
        {"ticker": "GLD", "type": "pct_of_account", "target": 5.0, "rebalance": "sometimes"},
    ],
}
B_POSITIONS = (("VOO", 300.0), ("AAPL", 100.0), ("KO", 100.0))
R_POSITIONS = (("VFIAX", 1000.0),)


def _cfg(tmp_path: Path, doc=TARGETS_DOC, **kw):
    C.init_project(tmp_path)
    cfg = C.load_config(tmp_path)
    targets = tmp_path / "sizing-targets.json"
    if doc is not None:
        targets.write_text(doc if isinstance(doc, str) else json.dumps(doc), encoding="utf-8")
    settings = dict(targets=targets, accounts={"brokerage": "Sample Brokerage", "roth": "Sample Roth"})
    settings.update(kw)
    return dataclasses.replace(cfg, allocation=AllocationSettings(**settings))


def _day(store: Store, day: str, cash: float | None, acct: AccountRef = ACCT, positions=B_POSITIONS) -> None:
    """A full snapshot of one account on `day`: its positions (1 share at the value) and its cash."""
    rows = [PositionRow(acct, s, None, 1.0, v, v) for s, v in positions]
    store.write_snapshot(Snapshot(day, "fidelity_csv", rows, [] if cash is None else [CashRow(acct, cash)]))


def _types(store: Store) -> None:
    for s, t in (("VOO", "etf"), ("VFIAX", "mutual_fund"), ("AAPL", "stock"), ("KO", "stock"), ("GLD", "etf")):
        store.upsert_security(s, None, t, "2026-10-01")
        store.set_asset_type(s, t)


def _score(store: Store, symbol: str, metric: str, band: str | None, status: str = "ok", value: float = 20.0) -> None:
    store.conn.execute(
        "INSERT INTO valuation_scores_raw (symbol, date, metric, value, percentile_5y, band, status, inputs, sources) "
        "VALUES (?, '2026-10-29', ?, ?, 50.0, ?, ?, '', ?)",
        (symbol, metric, value, band, status, json.dumps([_src(f"{symbol} {metric}")])))
    store.conn.commit()


def _at(day: int) -> datetime:
    return datetime(2026, 11, day, 12, 0, tzinfo=timezone.utc)


def _run(store: Store, cfg, day: int) -> "S.SplitReport":
    return S.run(store, cfg, checked_at="2026-11-01T11:00:00Z", now=_at(day))


def _all_rows(store: Store) -> list[tuple]:
    return ([tuple(r) for r in store.query("SELECT * FROM allocation_proposals_raw ORDER BY id")]
            + [tuple(r) for r in store.query("SELECT * FROM allocation_lines_raw ORDER BY proposal_id, symbol")])


@pytest.fixture
def setup(store: Store, tmp_path: Path):
    _types(store)
    for m, b in (("pe", "cheap"), ("p_fcf", "cheap"), ("ev_ebit", "usual")):
        _score(store, "AAPL", m, b)
    _day(store, "2026-10-30", 500.0)
    return store, _cfg(tmp_path)


def test_first_deposit_opens_a_proposal(setup):
    store, cfg = setup
    rep = _run(store, cfg, 2)
    assert (rep.opened, rep.replaced, rep.spent, rep.skipped) == (1, 0, 0, None)
    p = store.open_proposal("brokerage")
    assert (p["cash_date"], p["cash"], p["created_at"], p["checked_at"]) == \
           ("2026-10-30", 500.0, "2026-11-02T12:00:00Z", "2026-11-01T11:00:00Z")
    by = {l["symbol"]: l for l in p["lines"]}
    assert set(by) == {"VOO", "AAPL"}
    assert sum(l["dollars"] for l in p["lines"]) + p["waits"] == pytest.approx(500.0)
    assert p["placed"] == 2 and p["waits"] >= 0
    assert by["AAPL"]["band"] == "cheap" and by["AAPL"]["tilt"] == 1.5
    raw = store.query("SELECT reasons FROM allocation_lines_raw WHERE symbol = 'AAPL'")[0][0]
    assert raw == "missing:ps,cheap_by:pe+p_fcf,quality_unknown,band_top"      # the band's codes come first
    assert by["AAPL"]["reasons"] == ("missing:ps", "cheap_by:pe+p_fcf", "quality_unknown", "band_top")   # round trip
    assert by["VOO"]["band"] == "usual" and "no_scores" in by["VOO"]["reasons"]
    assert by["AAPL"]["sources"] == [
        {"kind": "feed", "label": "sizing-targets.json", "date": "2026-10-01", "ref": "brokerage/AAPL"},
        {"kind": "derived", "label": "valuation_scores pe", "date": "2026-10-29", "ref": "AAPL pe"},
        {"kind": "derived", "label": "valuation_scores p_fcf", "date": "2026-10-29", "ref": "AAPL p_fcf"},
        {"kind": "derived", "label": "valuation_scores ev_ebit", "date": "2026-10-29", "ref": "AAPL ev_ebit"},
        {"kind": "config", "label": "[allocation] tilt.cheap", "date": None, "ref": "1.5"},
    ]
    assert by["VOO"]["sources"] == [
        {"kind": "feed", "label": "sizing-targets.json", "date": "2026-10-01", "ref": "brokerage/VOO"},
        {"kind": "config", "label": "[allocation] tilt.usual", "date": None, "ref": "1.0"},
    ]
    assert p["sources"] == [
        {"kind": "derived", "label": "cash_balances", "date": "2026-10-30", "ref": "brokerage"},
        {"kind": "feed", "label": "sizing-targets.json", "date": "2026-10-01", "ref": "brokerage"},
    ]
    assert {"symbol": "KO", "reason": "hold"} in p["notes"]
    assert store.open_proposal("roth") is None                     # the roth account has no cash row
    assert "roth: no cash row" in rep.notes


def test_a_first_deposit_under_min_new_waits(store: Store, tmp_path: Path):
    _types(store)
    _day(store, "2026-10-30", 24.0)
    rep = _run(store, _cfg(tmp_path), 2)
    assert (rep.opened, rep.replaced, rep.spent) == (0, 0, 0)
    assert _all_rows(store) == []


def test_rerun_same_cash_changes_nothing(setup):
    store, cfg = setup
    _run(store, cfg, 2)
    before = _all_rows(store)
    rep = _run(store, cfg, 3)
    assert (rep.opened, rep.replaced, rep.spent) == (0, 0, 0)
    assert _all_rows(store) == before                              # created_at included: no churn
    _score(store, "VOO", "pe", "premium")                          # new scores alone never rewrite an open proposal
    _run(store, cfg, 4)
    assert _all_rows(store) == before


def test_rise_of_min_new_replaces(setup):
    store, cfg = setup
    _run(store, cfg, 2)
    first = store.open_proposal("brokerage")["id"]
    _day(store, "2026-11-02", 525.0)
    rep = _run(store, cfg, 3)
    assert (rep.opened, rep.replaced, rep.spent) == (0, 1, 0)
    new = store.open_proposal("brokerage")
    assert new["id"] != first and new["cash"] == 525.0 and new["cash_date"] == "2026-11-02"
    old = store.query("SELECT status, closed_at, closed_cash FROM allocation_proposals_raw WHERE id = ?", (first,))[0]
    assert tuple(old) == ("replaced", "2026-11-03T12:00:00Z", 525.0)


def test_rise_under_min_new_keeps_the_open_one(setup):
    store, cfg = setup
    _run(store, cfg, 2)
    before = _all_rows(store)
    _day(store, "2026-11-02", 524.99)
    rep = _run(store, cfg, 3)
    assert (rep.opened, rep.replaced, rep.spent) == (0, 0, 0)
    _day(store, "2026-11-03", 499.01)                              # less than a dollar down: not placed
    rep = _run(store, cfg, 4)
    assert (rep.opened, rep.replaced, rep.spent) == (0, 0, 0)
    assert _all_rows(store) == before


def test_partial_placement_is_spent_and_leftover_joins_the_next(setup):
    store, cfg = setup
    _run(store, cfg, 2)
    first = store.open_proposal("brokerage")["id"]
    _day(store, "2026-11-02", 300.0)
    rep = _run(store, cfg, 3)
    assert (rep.opened, rep.replaced, rep.spent) == (0, 0, 1)
    spent = store.last_spent_proposal("brokerage")
    assert (spent["id"], spent["closed_at"], spent["closed_cash"]) == (first, "2026-11-03T12:00:00Z", 300.0)
    assert store.open_proposal("brokerage") is None
    _day(store, "2026-11-03", 300.0)
    before = _all_rows(store)
    rep = _run(store, cfg, 4)
    assert (rep.opened, rep.replaced, rep.spent) == (0, 0, 0) and _all_rows(store) == before
    _day(store, "2026-11-04", 324.0)                               # 24 above the low: not yet
    assert _run(store, cfg, 5).opened == 0
    _day(store, "2026-11-05", 340.0)
    rep = _run(store, cfg, 6)
    assert (rep.opened, rep.replaced, rep.spent) == (1, 0, 0)
    p = store.open_proposal("brokerage")
    assert p["cash"] == 340.0 and p["cash_date"] == "2026-11-05"   # the leftover 300 joins the new 40


def test_a_dip_after_spending_lowers_the_baseline(setup):
    store, cfg = setup
    _run(store, cfg, 2)
    _day(store, "2026-11-02", 300.0)
    _run(store, cfg, 3)                                            # spent at 300, closed on 2026-11-03
    _day(store, "2026-11-03", 280.0)                               # more placed by hand: the low is now 280
    _day(store, "2026-11-04", 310.0)
    assert _run(store, cfg, 5).opened == 1                         # 310 >= 280 + 25
    assert store.open_proposal("brokerage")["cash"] == 310.0


def test_placed_down_to_zero_then_a_small_deposit(setup):
    store, cfg = setup
    _run(store, cfg, 2)
    _day(store, "2026-11-02", 0.0)
    assert _run(store, cfg, 3).spent == 1
    assert store.last_spent_proposal("brokerage")["closed_cash"] == 0.0
    _day(store, "2026-11-03", 30.0)
    rep = _run(store, cfg, 4)
    assert rep.opened == 1
    p = store.open_proposal("brokerage")
    assert p["cash"] == 30.0 and sum(l["dollars"] for l in p["lines"]) <= 30


def test_no_targets_file_skips_quietly(setup, tmp_path: Path):
    store, cfg = setup
    for alloc in (dataclasses.replace(cfg.allocation, targets=tmp_path / "missing.json"),
                  dataclasses.replace(cfg.allocation, targets=None)):
        rep = S.run(store, dataclasses.replace(cfg, allocation=alloc), checked_at=None, now=_at(2))
        assert rep.skipped == "no targets file" and (rep.opened, rep.replaced, rep.spent) == (0, 0, 0)
    assert _all_rows(store) == []


def test_bad_targets_file_skips_with_reason(store: Store, tmp_path: Path):
    _types(store)
    _day(store, "2026-10-30", 500.0)
    rep = _run(store, _cfg(tmp_path, doc="{ not json"), 2)
    assert rep.skipped == "targets file unreadable: the targets file is not valid JSON (line 1)"
    assert _all_rows(store) == []


def test_no_accounts_skips(setup):
    store, cfg = setup
    cfg = dataclasses.replace(cfg, allocation=dataclasses.replace(cfg.allocation, accounts={}))
    assert _run(store, cfg, 2).skipped == "no accounts in [allocation.accounts]"
    assert _all_rows(store) == []


def test_a_skip_leaves_an_open_proposal_alone(setup, tmp_path: Path):
    store, cfg = setup
    _run(store, cfg, 2)
    before = _all_rows(store)
    _day(store, "2026-11-02", 0.0)
    gone = dataclasses.replace(cfg, allocation=dataclasses.replace(cfg.allocation, targets=tmp_path / "missing.json"))
    assert _run(store, gone, 3).skipped == "no targets file"
    assert _all_rows(store) == before


def test_an_old_account_is_left_out_of_the_household(store: Store, tmp_path: Path):
    _types(store)
    doc = {"last_revised": "2026-10-01",
           "brokerage": [{"ticker": "VOO", "target": 50.0}, {"ticker": "AAPL", "target": 20.0}],
           "caps": [{"ticker": "AAPL", "max": 25.0}]}
    _day(store, "2026-10-30", 500.0)
    _day(store, "2026-10-10", 0.0, ROTH, (("AAPL", 5000.0),))    # a closed account's last snapshot, 20 days older
    cfg = _cfg(tmp_path, doc, accounts={"brokerage": "Sample Brokerage"})
    rep = _run(store, cfg, 2)
    assert rep.opened == 1 and "1 account older than 7 days left out" in rep.notes
    p = store.open_proposal("brokerage")
    assert {"symbol": None, "reason": "1 account older than 7 days left out"} in p["notes"]
    # Counted, its AAPL would fill the cap; left out, AAPL has room (25% of 1000 less the 100 held).
    assert "AAPL" in {l["symbol"] for l in p["lines"]}
    text = json.dumps(_all_rows(store))
    assert "Sample" not in text


def test_a_proposal_account_left_out_of_the_household_still_gets_its_proposal(store: Store, tmp_path: Path):
    _types(store)
    _day(store, "2026-10-30", 0.0)
    _day(store, "2026-10-10", 200.0, ROTH, R_POSITIONS)
    rep = _run(store, _cfg(tmp_path), 2)
    assert rep.opened == 1
    p = store.open_proposal("roth")
    assert p["cash"] == 200.0 and [l["symbol"] for l in p["lines"]] == ["VFIAX"]
    reasons = [n["reason"] for n in p["notes"] if n["symbol"] is None]
    assert "1 account older than 7 days left out" in reasons
    assert "this account is older than 7 days and left out of the household" in reasons
    assert {"symbol": "GLD", "reason": "unknown_rule"} in p["notes"]
    assert any(r.startswith("targets: roth/GLD: rebalance") for r in reasons)    # the file's problems ride along
    assert store.open_proposal("brokerage") is None                # its cash is 0


def test_sources_name_the_file_not_its_path(setup):
    store, cfg = setup
    _run(store, cfg, 2)
    feeds = [s for (src,) in store.query("SELECT sources FROM allocation_proposals_raw UNION ALL "
                                         "SELECT sources FROM allocation_lines_raw")
             for s in json.loads(src) if s["kind"] == "feed"]
    assert feeds and {s["label"] for s in feeds} == {"sizing-targets.json"}
    assert not any("/" in s["label"] or "\\" in s["label"] for s in feeds)
    text = json.dumps(_all_rows(store))
    assert cfg.allocation.targets.parent.name not in text
    assert "Sample Brokerage" not in text and "Sample Roth" not in text


def test_hold_name_never_in_lines(setup):
    store, cfg = setup
    _day(store, "2026-11-02", 5000.0)                              # so much cash every top is reached
    _run(store, cfg, 3)
    p = store.open_proposal("brokerage")
    assert "KO" not in {l["symbol"] for l in p["lines"]}
    assert {"symbol": "KO", "reason": "hold"} in p["notes"]
    assert sum(l["dollars"] for l in p["lines"]) <= 5000.0
    words = json.dumps(_all_rows(store)).lower()
    assert "buy" not in words


def test_fund_symbols_come_from_the_asset_type(setup):
    store, _ = setup
    assert store.split_asset_types(["VOO", "AAPL", "VFIAX", "NOPE"]) == \
           {"VOO": "etf", "AAPL": "stock", "VFIAX": "mutual_fund"}


def test_one_account_named_twice_gets_no_proposal(setup):
    store, cfg = setup
    twice = dataclasses.replace(cfg, allocation=dataclasses.replace(
        cfg.allocation, accounts={"brokerage": "Sample Brokerage", "roth": "Sample Brokerage"}))
    rep = _run(store, twice, 2)
    assert (rep.opened, rep.replaced, rep.spent) == (0, 0, 0)
    assert "brokerage, roth: one account named more than once in [allocation.accounts]; no proposal" in rep.notes
    assert _all_rows(store) == []


# ---- fix round 1: comma-free reasons, the household of fresh investment accounts, filed refs, unreachable proposals

BANK = AccountRef("Sample Bank", "bank", "sample", "checking")


def test_reasons_round_trip_with_a_comma_in_a_sleeve_label(setup, tmp_path: Path):
    store, _ = setup
    doc = dict(TARGETS_DOC, sleeves=[{"account": "brokerage", "label": "Core, index", "members": ["VOO", "AAPL"],
                                      "max": 45.0}])
    cfg = _cfg(tmp_path, doc)
    _run(store, cfg, 2)
    # base 1000; the sleeve's room 450 - 400 held = 50: both lines cut by it.
    lines = store.open_proposal("brokerage")["lines"]
    assert lines and all("sleeve_cut:Core+ index" in l["reasons"] for l in lines)
    for l in lines:
        raw = store.query("SELECT reasons FROM allocation_lines_raw WHERE symbol = ?", (l["symbol"],))[0][0]
        assert tuple(raw.split(",")) == l["reasons"]
        assert not any("," in code for code in l["reasons"])


def _cap_doc(cap: float) -> dict:
    return {"last_revised": "2026-10-01",
            "brokerage": [{"ticker": "VOO", "target": 50.0}, {"ticker": "AAPL", "target": 20.0}],
            "caps": [{"ticker": "AAPL", "max": cap}]}


def test_an_account_with_fresh_positions_and_old_cash_stays_in_the_household(store: Store, tmp_path: Path):
    _types(store)
    _day(store, "2026-10-30", 500.0)
    store.write_snapshot(Snapshot("2026-10-01", "fidelity_csv", [], [CashRow(ROTH, 9000.0)]))   # its cash is old
    _day(store, "2026-10-30", None, ROTH, (("AAPL", 5000.0),))                                   # its positions are not
    rep = _run(store, _cfg(tmp_path, _cap_doc(25.0), accounts={"brokerage": "Sample Brokerage"}), 2)
    assert rep.opened == 1
    note = "1 account's cash row older than 7 days not counted"
    assert note in rep.notes and not any("left out" in n for n in rep.notes)
    p = store.open_proposal("brokerage")
    assert {"symbol": None, "reason": note} in p["notes"]
    # Its AAPL counts toward the cap: 25% of (1000 + 5000) = 1500, already 5100 held -> no room.
    assert "AAPL" not in {l["symbol"] for l in p["lines"]}
    assert {"symbol": "AAPL", "reason": "cap_cut:AAPL"} in p["notes"]


def test_a_cash_only_account_is_neither_counted_nor_noted(store: Store, tmp_path: Path):
    _types(store)
    _day(store, "2026-10-30", 500.0)
    store.write_snapshot(Snapshot("2026-10-30", "sample_bank", [], [CashRow(BANK, 100000.0)]))
    rep = _run(store, _cfg(tmp_path, _cap_doc(10.0), accounts={"brokerage": "Sample Brokerage"}), 2)
    assert rep.opened == 1 and rep.notes == ()
    p = store.open_proposal("brokerage")
    # Counted, the bank's cash would make room under the cap; it is not: 10% of 1000 - 100 held = 0.
    assert "AAPL" not in {l["symbol"] for l in p["lines"]}
    assert {"symbol": "AAPL", "reason": "cap_cut:AAPL"} in p["notes"]
    assert not any(n["symbol"] is None for n in p["notes"])


def test_a_line_cites_the_account_its_entry_is_filed_under(setup, tmp_path: Path):
    store, _ = setup
    store.upsert_security("SPY", None, "etf", "2026-10-01")
    store.set_asset_type("SPY", "etf")
    doc = {"last_revised": "2026-10-01", "brokerage": [{"ticker": "VOO", "target": 50.0}],
           "roth": [{"ticker": "SPY", "type": "pct_of_brokerage", "target": 20.0}]}
    _run(store, _cfg(tmp_path, doc, accounts={"brokerage": "Sample Brokerage"}), 2)
    by = {l["symbol"]: l for l in store.open_proposal("brokerage")["lines"]}
    assert by["SPY"]["sources"][0] == {"kind": "feed", "label": "sizing-targets.json", "date": "2026-10-01",
                                       "ref": "roth/SPY"}
    assert by["VOO"]["sources"][0]["ref"] == "brokerage/VOO"


@pytest.mark.parametrize("accounts, note", [
    ({"roth": "Sample Roth"}, "brokerage: not in [allocation.accounts]; its open proposal stays open"),
    ({"brokerage": "Nobody"}, "brokerage: no cash row; its open proposal stays open"),
    ({"brokerage": "Sample Brokerage", "roth": "Sample Brokerage"},
     "brokerage: named with another on one account; its open proposal stays open"),
])
def test_an_unreachable_open_proposal_stays_open_and_is_noted(setup, accounts, note):
    store, cfg = setup
    _run(store, cfg, 2)
    before = _all_rows(store)
    _day(store, "2026-11-02", 0.0)                                 # would close it, were it reachable
    rep = _run(store, dataclasses.replace(cfg, allocation=dataclasses.replace(cfg.allocation, accounts=accounts)), 3)
    assert (rep.opened, rep.replaced, rep.spent) == (0, 0, 0)
    assert note in rep.notes and not any("Sample" in n or "Nobody" in n for n in rep.notes)
    assert _all_rows(store) == before


def test_an_account_with_no_targets_opens_nothing(store: Store, tmp_path: Path):
    _types(store)
    _day(store, "2026-10-30", 500.0)
    _day(store, "2026-10-30", 400.0, ROTH, R_POSITIONS)
    doc = {"last_revised": "2026-10-01", "roth": [{"ticker": "VOO", "type": "pct_of_brokerage", "target": 50.0}]}
    rep = _run(store, _cfg(tmp_path, doc), 2)
    assert rep.opened == 1 and "no targets for roth" in rep.notes      # its only entry sends money to the brokerage
    assert "no targets for brokerage" not in rep.notes
    assert store.open_proposal("brokerage") is not None and store.open_proposal("roth") is None
    doc = {"last_revised": "2026-10-01", "roth": [{"ticker": "VFIAX", "target": 90.0}]}
    rep = _run(store, _cfg(tmp_path, doc), 3)
    assert "no targets for brokerage; its open proposal stays open" in rep.notes
    assert rep.opened == 1 and store.open_proposal("roth") is not None


def test_open_proposal_keys(store: Store):
    store.write_proposal(_proposal("roth"), created_at=NOW, checked_at=None, replaces=None)
    pid = store.write_proposal(_proposal("brokerage"), created_at=NOW, checked_at=None, replaces=None)
    assert store.open_proposal_keys() == ["brokerage", "roth"]
    store.close_proposal(pid, closed_at=NOW, closed_cash=0.0)
    assert store.open_proposal_keys() == ["roth"]
