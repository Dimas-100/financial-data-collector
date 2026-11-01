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
    return AllocationProposal(account_key, "2026-10-30", cash, sum(l.dollars for l in lines), cash - sum(l.dollars for l in lines),
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
           ("brokerage", "2026-10-30", 100.0, 70, 30.0)
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
    assert p["placed"] == 475 and p["waits"] >= 0                 # the dollars placed, not the number of lines
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
        {"kind": "config", "label": "[allocation] go_from", "date": None, "ref": "7"},
        {"kind": "config", "label": "[allocation] half_from", "date": None, "ref": "4"},
        {"kind": "config", "label": "[allocation] max_wait_weeks", "date": None, "ref": "4"},
    ]
    # The week: VOO 200 short (no scores: not known) and AAPL 100 short and cheap, a third of the gap: 7 + 1.
    assert (p["account_type"], p["verdict"], p["fraction"], p["weeks_waited"], p["forced"], p["waiting_since"]) == \
           ("brokerage", "go", 1.0, 0, None, None)
    assert p["score"] == 7 + 1 and [x["code"] for x in p["parts"]] == ["base", "cheap", "not_known"]
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


# ---- fix round 1, item 7: an account's cash older than its positions decides nothing

def test_cash_older_than_positions_keeps_an_open_proposal_open(setup):
    store, cfg = setup
    _run(store, cfg, 2)
    before = _all_rows(store)
    _day(store, "2026-11-02", None)                                # new positions, no new cash row: cash still 10-30
    store.write_snapshot(Snapshot("2026-10-30", "fidelity_csv",
                                  [PositionRow(ACCT, s, None, 1.0, v, v) for s, v in B_POSITIONS], [CashRow(ACCT, 0.0)]))
    rep = _run(store, cfg, 3)                                      # 0 would be spent, were the row as new as the positions
    assert (rep.opened, rep.replaced, rep.spent) == (0, 0, 0)
    assert "brokerage: cash row older than its positions; its open proposal stays open" in rep.notes
    assert not any("Sample" in n for n in rep.notes)
    assert store.open_proposal("brokerage")["id"] == before[0][0]
    assert store.query("SELECT COUNT(*) FROM allocation_proposals_raw")[0][0] == 1


def test_cash_older_than_positions_opens_nothing(store: Store, tmp_path: Path):
    _types(store)
    _day(store, "2026-10-29", 500.0)
    _day(store, "2026-10-30", None)                                # positions a day newer than the cash
    rep = _run(store, _cfg(tmp_path), 2)
    assert (rep.opened, rep.replaced, rep.spent) == (0, 0, 0)
    assert "brokerage: cash row older than its positions" in rep.notes
    assert _all_rows(store) == []


def test_an_account_without_positions_decides_on_any_cash_row(store: Store, tmp_path: Path):
    _types(store)
    store.write_snapshot(Snapshot("2026-10-20", "fidelity_csv", [], [CashRow(ACCT, 500.0)]))
    rep = _run(store, _cfg(tmp_path), 2)
    assert rep.opened == 1 and not any("older than its positions" in n for n in rep.notes)


# ---- final fix wave: caps across accounts, untyped names, placed in dollars

def test_a_household_cap_holds_across_two_accounts_in_one_run(store: Store, tmp_path: Path):
    # Each account holds 100 of AAPL and 1000 of a fund, with 1000 waiting; household 4200, cap AAPL 12% = 504, so
    # 304 of room for both accounts together. The brokerage is planned first; the roth's lines then count its open ones.
    _types(store)
    doc = {"last_revised": "2026-10-01",
           "brokerage": [{"ticker": "AAPL", "target": 30.0}, {"ticker": "VOO", "target": 70.0}],
           "roth": [{"ticker": "AAPL", "target": 30.0}, {"ticker": "VFIAX", "target": 70.0}],
           "caps": [{"ticker": "AAPL", "max": 12.0}]}
    _day(store, "2026-10-30", 1000.0, ACCT, (("VOO", 1000.0), ("AAPL", 100.0)))
    _day(store, "2026-10-30", 1000.0, ROTH, (("VFIAX", 1000.0), ("AAPL", 100.0)))
    rep = _run(store, _cfg(tmp_path, doc), 2)
    assert rep.opened == 2
    aapl = [l["dollars"] for k in ("brokerage", "roth") for l in store.open_proposal(k)["lines"] if l["symbol"] == "AAPL"]
    assert sum(aapl) <= 304
    assert {"symbol": "AAPL", "reason": "cap_cut:AAPL"} in store.open_proposal("roth")["notes"]
    # A replacement of the brokerage's proposal counts the roth's open lines, not its own old ones.
    _day(store, "2026-11-02", 1100.0, ACCT, (("VOO", 1000.0), ("AAPL", 100.0)))
    rep = _run(store, _cfg(tmp_path, doc), 3)
    assert rep.replaced == 1
    aapl = [l["dollars"] for k in ("brokerage", "roth") for l in store.open_proposal(k)["lines"] if l["symbol"] == "AAPL"]
    assert sum(aapl) <= 0.12 * 4300 - 200 + 1e-9
    assert aapl[0] >= 300                       # its own old 304 would have left it 12 of room


def test_an_untyped_symbol_with_a_look_through_file_is_a_fund(store: Store, tmp_path: Path):
    # SPY has no asset type but a look-through file (AAPL 50%): it is a fund, so the AAPL cap cuts its line through the
    # file (0.5 of each dollar) and its band is read from the fund metrics (pe, dividend_yield).
    _types(store)
    funds = tmp_path / "fund_holdings"
    funds.mkdir()
    (funds / "SPY.csv").write_text("ticker,name,weight_pct\nAAPL,Apple,50\n", encoding="utf-8")
    for m in ("pe", "dividend_yield"):
        _score(store, "SPY", m, "cheap")
    doc = {"last_revised": "2026-10-01", "brokerage": [{"ticker": "SPY", "target": 60.0}],
           "caps": [{"ticker": "AAPL", "max": 10.0}]}
    _day(store, "2026-10-30", 500.0, ACCT, (("KO", 400.0), ("AAPL", 80.0)))
    rep = _run(store, _cfg(tmp_path, doc, fund_holdings=funds, accounts={"brokerage": "Sample Brokerage"}), 2)
    assert rep.opened == 1
    (line,) = store.open_proposal("brokerage")["lines"]
    # household 980: cap 98, AAPL held 80 -> room 18 -> SPY at most 36.
    assert line["symbol"] == "SPY" and line["dollars"] == 36
    assert line["band"] == "cheap" and "cheap_by:pe+dividend_yield" in line["reasons"]
    assert "type_unknown" not in line["reasons"] and "look_through_assumed" not in line["reasons"]


def test_an_untyped_symbol_without_a_file_is_capped_at_the_assumed_weight(store: Store, tmp_path: Path):
    # SPY: no type, no file. VOO's file names AAPL at 10%: SPY is taken at 10% for an AAPL cap and says type_unknown.
    _types(store)
    funds = tmp_path / "fund_holdings"
    funds.mkdir()
    (funds / "VOO.csv").write_text("ticker,name,weight_pct\nAAPL,Apple,10\n", encoding="utf-8")
    doc = {"last_revised": "2026-10-01", "brokerage": [{"ticker": "SPY", "target": 60.0}],
           "caps": [{"ticker": "AAPL", "max": 10.0}]}
    _day(store, "2026-10-30", 500.0, ACCT, (("KO", 400.0), ("AAPL", 80.0)))
    rep = _run(store, _cfg(tmp_path, doc, fund_holdings=funds, accounts={"brokerage": "Sample Brokerage"}), 2)
    assert rep.opened == 1
    (line,) = store.open_proposal("brokerage")["lines"]
    # household 980: cap 98, AAPL held 80 -> room 18 -> SPY at most 180.
    assert line["symbol"] == "SPY" and line["dollars"] == 180
    assert "type_unknown" in line["reasons"] and "look_through_assumed" in line["reasons"]


def test_placed_is_the_dollars_placed(setup):
    store, cfg = setup
    _run(store, cfg, 2)
    p = store.open_proposal("brokerage")
    assert p["placed"] == sum(l["dollars"] for l in p["lines"]) and p["placed"] > len(p["lines"])
    assert round(p["placed"] + p["waits"], 2) == p["cash"]


# ---- final fix wave: a placement a deposit hides, and a replacement in another account

from financial_data_collector.models import TransactionRow


def _buy(store: Store, day: str, symbol: str = "AAPL", acct: AccountRef = ACCT, kind: str = "buy") -> None:
    store.write_transactions([TransactionRow(acct, day, kind, symbol, 1.0, 100.0, -100.0, None, kind.upper(),
                                             "fidelity_csv")])


def test_split_buys_between_counts_buys_of_holdings_only(store: Store):
    _buy(store, "2026-10-30")
    _buy(store, "2026-11-02", "VOO")
    _buy(store, "2026-11-02", "SPAXX")                             # the money market's sweep is not a placement
    _buy(store, "2026-11-02", "KO", kind="dividend")
    _buy(store, "2026-11-02", "VFIAX", ROTH)
    assert store.split_buys_between("Sample Brokerage", "2026-10-29", "2026-11-02") == 2
    assert store.split_buys_between("Sample Brokerage", "2026-10-30", "2026-11-02") == 1   # the first day does not count
    assert store.split_buys_between("Sample Brokerage", "2026-10-29", "2026-11-01") == 1   # the last day does
    assert store.split_buys_between("Sample Brokerage", "2026-11-02", "2026-11-09") == 0
    assert store.split_buys_between("Sample Roth", "2026-10-30", "2026-11-02") == 1
    assert store.split_buys_between("Nobody", "2026-01-01", "2026-12-31") == 0


def test_a_placement_hidden_by_a_deposit_is_spent_and_a_new_proposal_opens(setup):
    store, cfg = setup
    _run(store, cfg, 2)                                            # 500 waiting on 10-30
    first = store.open_proposal("brokerage")["id"]
    _buy(store, "2026-11-02")                                      # 300 placed, 300 deposited: the cash still reads 500
    _day(store, "2026-11-03", 500.0, positions=(("VOO", 500.0), ("AAPL", 200.0), ("KO", 100.0)))
    rep = _run(store, cfg, 3)
    assert (rep.opened, rep.replaced, rep.spent) == (1, 0, 1)
    old = store.query("SELECT status, closed_cash FROM allocation_proposals_raw WHERE id = ?", (first,))[0]
    assert tuple(old) == ("spent", 500.0)
    new = store.open_proposal("brokerage")
    assert new["id"] != first and (new["cash"], new["cash_date"]) == (500.0, "2026-11-03")
    before = _all_rows(store)
    rep = _run(store, cfg, 4)                                      # the same buy never closes the new one
    assert (rep.opened, rep.replaced, rep.spent) == (0, 0, 0) and _all_rows(store) == before


def test_a_buy_spends_even_under_a_dollar_down_and_opens_only_from_min_new(store: Store, tmp_path: Path):
    _types(store)
    _day(store, "2026-10-30", 500.0)
    cfg = _cfg(tmp_path)
    _run(store, cfg, 2)
    _buy(store, "2026-11-02")
    _day(store, "2026-11-03", 499.5)                               # less than a dollar down, but a buy says placed
    rep = _run(store, cfg, 3)
    assert (rep.opened, rep.replaced, rep.spent) == (1, 0, 1)      # 499.5 >= min_new: a new one for all of it
    assert store.open_proposal("brokerage")["cash"] == 499.5
    # A proposal of 25.50; a buy, and 24.90 left: spent, and nothing new under min_new.
    with store.conn:
        store.conn.execute("DELETE FROM allocation_lines_raw")
        store.conn.execute("DELETE FROM allocation_proposals_raw")
    _day(store, "2026-11-04", 25.5)
    assert _run(store, cfg, 5).opened == 1
    _buy(store, "2026-11-05")
    _day(store, "2026-11-06", 24.9)
    rep = _run(store, cfg, 7)
    assert (rep.opened, rep.replaced, rep.spent) == (0, 0, 1) and store.open_proposal("brokerage") is None


def test_a_buy_on_the_proposals_own_cash_row_changes_nothing(setup):
    store, cfg = setup
    _buy(store, "2026-10-30")                                      # already in the cash the proposal was made from
    _run(store, cfg, 2)
    before = _all_rows(store)
    for day in (3, 4):
        rep = _run(store, cfg, day)
        assert (rep.opened, rep.replaced, rep.spent) == (0, 0, 0)
    assert _all_rows(store) == before
    # A newer cash row with the same cash and no new buy: still nothing (the review's probe, case 1).
    _day(store, "2026-10-31", 500.0)
    _day(store, "2026-11-01", 500.0)
    for day in (5, 6):
        rep = _run(store, cfg, day)
        assert (rep.opened, rep.replaced, rep.spent) == (0, 0, 0)
    assert _all_rows(store) == before


def test_a_proposal_the_buy_rule_opened_is_not_closed_by_the_same_buy(setup):
    # The review's probe, case 2: the buy is on the newer row's own day; the reissued proposal is made from that row.
    store, cfg = setup
    _run(store, cfg, 2)
    _buy(store, "2026-11-03")
    _day(store, "2026-11-03", 500.0)
    rep = _run(store, cfg, 3)
    assert (rep.opened, rep.replaced, rep.spent) == (1, 0, 1)
    before = _all_rows(store)
    _day(store, "2026-11-04", 500.0)                               # a newer row, no new buy
    rep = _run(store, cfg, 4)
    assert (rep.opened, rep.replaced, rep.spent) == (0, 0, 0) and _all_rows(store) == before


def test_a_buy_the_day_after_the_cash_date_spends_and_reissues(setup):
    store, cfg = setup
    _run(store, cfg, 2)                                            # made from the 10-30 row
    first = store.open_proposal("brokerage")["id"]
    _buy(store, "2026-10-31")
    _day(store, "2026-10-31", 500.0)
    rep = _run(store, cfg, 3)
    assert (rep.opened, rep.replaced, rep.spent) == (1, 0, 1)
    new = store.open_proposal("brokerage")
    assert new["id"] != first and new["cash_date"] == "2026-10-31"


def test_a_buy_newer_than_the_cash_row_waits_for_its_row_and_closes_one_proposal(setup):
    store, cfg = setup
    _run(store, cfg, 2)                                            # made from the 10-30 row
    _buy(store, "2026-11-04")                                      # the transactions feed runs ahead of the balances
    _day(store, "2026-11-03", 500.0)
    rep = _run(store, cfg, 3)
    assert (rep.opened, rep.replaced, rep.spent) == (0, 0, 0)      # not yet in any cash row: nothing to say
    _day(store, "2026-11-04", 500.0)
    rep = _run(store, cfg, 4)
    assert (rep.opened, rep.replaced, rep.spent) == (1, 0, 1)
    before = _all_rows(store)
    _day(store, "2026-11-05", 500.0)
    rep = _run(store, cfg, 5)
    assert (rep.opened, rep.replaced, rep.spent) == (0, 0, 0) and _all_rows(store) == before


def test_a_money_market_buy_is_not_a_placement(setup):
    store, cfg = setup
    _run(store, cfg, 2)
    _buy(store, "2026-11-02", "SPAXX")
    _day(store, "2026-11-03", 500.0)
    rep = _run(store, cfg, 3)
    assert (rep.opened, rep.replaced, rep.spent) == (0, 0, 0)


def test_replacing_another_accounts_proposal_is_refused(store: Store):
    pid = store.write_proposal(_proposal("brokerage"), created_at=NOW, checked_at=None, replaces=None)
    with pytest.raises(ValueError):
        store.write_proposal(_proposal("roth"), created_at=NOW, checked_at=None, replaces=pid)
    assert store.open_proposal("brokerage")["id"] == pid and store.open_proposal("roth") is None
    assert store.query("SELECT COUNT(*) FROM allocation_proposals_raw")[0][0] == 1


# ---------------------------------------------------------------------------------------------------------------------
# The week score in the lifecycle (spec addendum 2026-10-31): the fraction of the cash split, the wait, the guards
# ---------------------------------------------------------------------------------------------------------------------

def _premium(store: Store, symbol: str = "VOO") -> None:
    for m in ("pe", "dividend_yield"):             # VOO is a fund: its primary metrics
        _score(store, symbol, m, "premium")


def _week(store: Store, key: str = "brokerage") -> tuple:
    p = store.open_proposal(key)
    return p["score"], p["verdict"], p["fraction"], p["weeks_waited"], p["forced"], p["waiting_since"]


@pytest.fixture
def held_back(store: Store, tmp_path: Path):
    """VOO premium (200 of the 300 short), AAPL without scores: 7 - 4 = 3, hold on."""
    _types(store)
    _premium(store)
    _day(store, "2026-10-30", 500.0)
    return store, _cfg(tmp_path)


def _half(store: Store, tmp_path: Path):
    """VOO premium, AAPL cheap: 7 + 1 - 4 = 4, half."""
    _types(store)
    _premium(store)
    for m in ("pe", "p_fcf"):
        _score(store, "AAPL", m, "cheap")
    _day(store, "2026-10-30", 500.0)
    return _cfg(tmp_path)


def test_a_premium_week_holds_on_and_everything_waits(held_back):
    store, cfg = held_back
    _run(store, cfg, 2)
    p = store.open_proposal("brokerage")
    assert _week(store) == (3, "hold", 0.0, 0, None, "2026-10-30")
    assert (p["placed"], p["waits"], p["lines"]) == (0, 500.0, [])
    assert {"symbol": "VOO", "reason": "waits_this_week"} in p["notes"]
    assert {"symbol": "KO", "reason": "hold"} in p["notes"]
    assert [x["code"] for x in p["parts"]] == ["base", "premium", "not_known"]
    assert p["parts"][1]["text"] == "Premium to its own past: VOO (67% of what is short): -4."


def test_a_half_week_splits_half_the_cash(store: Store, tmp_path: Path):
    _run(store, _half(store, tmp_path), 2)
    p = store.open_proposal("brokerage")
    assert _week(store)[:3] == (4, "half", 0.5)
    assert 0 < p["placed"] <= 250 and p["waits"] == pytest.approx(500.0 - p["placed"])


def test_a_held_back_proposal_is_scored_again_each_week_and_goes_after_four(held_back):
    store, cfg = held_back
    _run(store, cfg, 2)
    _day(store, "2026-11-03", 500.0)                                        # 4 days on: nothing
    before = _all_rows(store)
    _run(store, cfg, 3)
    assert _all_rows(store) == before
    for n, day in enumerate(("2026-11-06", "2026-11-13", "2026-11-20"), start=1):
        _day(store, day, 500.0)
        rep = _run(store, cfg, int(day[-2:]))
        assert rep.replaced == 1 and _week(store) == (3, "hold", 0.0, n, None, "2026-10-30")
    _day(store, "2026-11-27", 500.0)
    _run(store, cfg, 27)
    p = store.open_proposal("brokerage")
    assert _week(store) == (3, "go", 1.0, 4, "waited", None)
    assert p["placed"] > 0 and p["parts"][-1]["code"] == "forced_waited"
    assert store.query("SELECT COUNT(*) FROM allocation_proposals_raw WHERE status = 'replaced'")[0][0] == 4


def test_a_go_proposal_is_never_scored_again_for_the_same_cash(setup):
    store, cfg = setup
    _run(store, cfg, 2)
    before = _all_rows(store)
    _day(store, "2026-11-09", 500.0)
    _run(store, cfg, 9)
    assert _all_rows(store) == before


def test_a_deposit_over_held_back_money_keeps_its_wait(held_back):
    store, cfg = held_back
    _run(store, cfg, 2)
    _day(store, "2026-11-08", 600.0)
    _run(store, cfg, 8)
    assert _week(store) == (3, "hold", 0.0, 1, None, "2026-10-30")
    store.conn.execute("UPDATE valuation_scores_raw SET band = 'usual' WHERE symbol = 'VOO'")   # VOO usual: it goes
    store.conn.commit()
    _day(store, "2026-11-10", 700.0)
    _run(store, cfg, 10)
    assert _week(store) == (7, "go", 1.0, 1, None, None)


def test_a_spent_half_leaves_its_wait_to_the_next_deposit(store: Store, tmp_path: Path):
    cfg = _half(store, tmp_path)
    _run(store, cfg, 2)
    _day(store, "2026-11-03", 260.0)                                        # he placed about half: spent, 260 waits
    assert _run(store, cfg, 3).spent == 1
    _day(store, "2026-11-10", 400.0)
    _run(store, cfg, 10)
    assert _week(store)[3:] == (1, None, "2026-10-30")


def test_a_wait_that_left_under_min_new_starts_again(store: Store, tmp_path: Path):
    cfg = _half(store, tmp_path)
    _run(store, cfg, 2)
    _day(store, "2026-11-03", 10.0)                                         # placed nearly all of it
    _run(store, cfg, 3)
    _day(store, "2026-11-10", 300.0)
    _run(store, cfg, 10)
    assert _week(store)[3:] == (0, None, "2026-11-10")


def _move(store: Store, symbol: str, day: str, move: float, first: str) -> None:
    causes = json.dumps([{"kind": first, "text": "no cause found" if first == "none" else "x", "sources": []}])
    store.conn.execute("INSERT INTO price_moves (symbol, date, span, start_date, move, triggers, causes, sources) "
                       "VALUES (?, ?, '1d', ?, ?, 'drop_day', ?, '[]')", (symbol, day, day, move, causes))
    store.conn.commit()


def test_an_unexplained_drop_lowers_the_score_and_is_cited(setup):
    store, cfg = setup
    _move(store, "VOO", "2026-10-28", -0.05, "none")
    _move(store, "AAPL", "2026-10-29", -0.06, "earnings")                  # explained: not counted
    _run(store, cfg, 2)
    p = store.open_proposal("brokerage")
    assert p["score"] == 7 + 1 - 1
    [drop] = [x for x in p["parts"] if x["code"] == "drop_no_cause"]
    assert drop == {"sign": "down", "code": "drop_no_cause", "symbol": "VOO",
                    "text": "VOO fell 5.0% on Oct 28 with no cause found: -1."}
    assert {"kind": "derived", "label": "price_moves", "date": "2026-10-28", "ref": "VOO"} in p["sources"]


def test_proposals_made_before_the_score_read_as_none(store: Store):
    store.write_proposal(_proposal(), created_at=NOW, checked_at=None, replaces=None)
    r = store.query("SELECT score, verdict, fraction, parts, weeks_waited, forced, account_type "
                    "FROM allocation_proposals")[0]
    assert tuple(r) == (None, None, None, "[]", None, None, None)


# ---------------------------------------------------------------------------------------------------------------------
# holding_bands and allocation_targets: rebuilt by every split run, read by the window's "This week" page
# ---------------------------------------------------------------------------------------------------------------------

def _bands(store: Store) -> dict[str, dict]:
    return {r["symbol"]: dict(r) for r in store.query("SELECT * FROM holding_bands")}


def _targets(store: Store) -> dict[tuple[str, str], dict]:
    return {(r["account_key"], r["symbol"]): dict(r) for r in store.query("SELECT * FROM allocation_targets")}


def test_holding_bands_cover_the_configured_accounts_holdings(setup):
    store, cfg = setup
    _day(store, "2026-10-30", 100.0, acct=ROTH, positions=R_POSITIONS + (("SPAXX", 50.0),))
    other = AccountRef("Sample Other", "other", "fidelity", "brokerage")
    _day(store, "2026-10-30", None, acct=other, positions=(("XOM", 10.0),))
    _run(store, cfg, 2)
    b = _bands(store)
    assert set(b) == {"AAPL", "KO", "VOO", "VFIAX"}                          # no money market, no other account
    assert (b["AAPL"]["band"], b["AAPL"]["date"], b["AAPL"]["checked_at"], b["AAPL"]["hold_ids"]) == \
           ("cheap", "2026-10-29", "2026-11-01T11:00:00Z", None)
    assert b["AAPL"]["reasons"] == "missing:ps,cheap_by:pe+p_fcf,quality_unknown"
    assert json.loads(b["AAPL"]["sources"])[0] == \
           {"kind": "derived", "label": "valuation_scores pe", "date": "2026-10-29", "ref": "AAPL pe"}
    assert (b["VOO"]["band"], b["VOO"]["reasons"], b["VOO"]["date"]) == ("usual", "no_scores", None)


def test_a_hold_on_a_deciding_score_blanks_the_band(setup):
    store, cfg = setup
    _run(store, cfg, 2)
    store.check_record("scores", [Finding("scores", "AAPL", "score:pe", "2026-10-29", None, "hold", "bad")], NOW)
    b = _bands(store)["AAPL"]
    assert b["band"] is None and b["hold_ids"] is not None
    assert store.query("SELECT band FROM holding_bands_raw WHERE symbol = 'AAPL'")[0][0] == "cheap"   # raw kept


def test_a_hold_on_a_score_that_did_not_decide_leaves_the_band(setup):
    store, cfg = setup
    _score(store, "AAPL", "implied_growth", None)
    _run(store, cfg, 2)
    store.check_record("scores", [Finding("scores", "AAPL", "score:implied_growth", "2026-10-29", None, "hold",
                                          "bad")], NOW)
    assert _bands(store)["AAPL"]["band"] == "cheap"


def test_bands_and_targets_are_rebuilt_without_churn(setup):
    store, cfg = setup
    _run(store, cfg, 2)

    def snap():
        return ([tuple(r) for r in store.query("SELECT rowid, * FROM holding_bands_raw ORDER BY symbol")],
                [tuple(r) for r in store.query("SELECT rowid, * FROM allocation_targets_raw ORDER BY symbol")])

    before = snap()
    _run(store, cfg, 3)
    assert snap() == before
    _day(store, "2026-10-31", 500.0, positions=(("VOO", 300.0), ("AAPL", 100.0)))     # KO sold
    _run(store, cfg, 4)
    assert "KO" not in _bands(store)


def test_allocation_targets_give_plan_splits_gap_either_way(setup):
    store, cfg = setup
    _run(store, cfg, 2)
    t = _targets(store)
    assert set(t) == {("brokerage", "VOO"), ("brokerage", "AAPL"), ("brokerage", "KO")}   # roth: no positions, no cash
    voo, aapl, ko = t[("brokerage", "VOO")], t[("brokerage", "AAPL")], t[("brokerage", "KO")]
    # base 1000: VOO 50% = 500 vs 300 held, top 62.5% = 625; AAPL 20% = 200 vs 100; KO 30% = 300 vs 100.
    assert (voo["account_type"], voo["rule"], voo["kind"], voo["target"], voo["held_value"]) == \
           ("brokerage", "trades", "pct_of_account", 50.0, 300.0)
    assert (voo["gap"], voo["side"], voo["top"], voo["date"], voo["note"]) == (200.0, "short", 325.0, "2026-10-30", None)
    assert (aapl["rule"], aapl["gap"], aapl["side"]) == ("contributions", 100.0, "short")
    assert (ko["rule"], ko["gap"], ko["side"]) == ("hold", 200.0, "short")
    assert json.loads(voo["sources"]) == [
        {"kind": "feed", "label": "sizing-targets.json", "date": "2026-10-01", "ref": "brokerage/VOO"},
        {"kind": "derived", "label": "positions_latest", "date": "2026-10-30", "ref": "brokerage"},
        {"kind": "derived", "label": "cash_balances", "date": "2026-10-30", "ref": "brokerage"},
    ]
    _day(store, "2026-10-31", 0.0, positions=(("VOO", 900.0), ("AAPL", 100.0)))        # VOO 90% of 1000: over
    _run(store, cfg, 3)
    voo = _targets(store)[("brokerage", "VOO")]
    assert (voo["gap"], voo["side"], voo["top"]) == (400.0, "over", -275.0)


def test_targets_and_bands_are_published_even_when_no_proposal_opens(store: Store, tmp_path: Path):
    _types(store)
    _day(store, "2026-10-30", 5.0)                                          # under min_new: no proposal
    _run(store, _cfg(tmp_path), 2)
    assert store.open_proposal("brokerage") is None
    assert len(_targets(store)) == 3 and set(_bands(store)) == {"AAPL", "KO", "VOO"}
    (tmp_path / "sizing-targets.json").unlink()
    rep = _run(store, _cfg(tmp_path, doc=None), 3)                          # the targets file is gone
    assert rep.skipped == "no targets file" and _targets(store) == {} and set(_bands(store)) == {"AAPL", "KO", "VOO"}


def test_a_ratio_entry_without_look_through_has_no_gap(store: Store, tmp_path: Path):
    _types(store)
    _day(store, "2026-10-30", 500.0)
    doc = dict(TARGETS_DOC, brokerage=TARGETS_DOC["brokerage"] + [
        {"ticker": "AMZN", "type": "direct_passive_ratio", "target": 1.75}])
    _run(store, _cfg(tmp_path, doc=doc), 2)
    amzn = _targets(store)[("brokerage", "AMZN")]
    assert (amzn["kind"], amzn["target"], amzn["gap"], amzn["side"], amzn["top"], amzn["note"]) == \
           ("direct_passive_ratio", 1.75, None, None, None, "no_look_through")
