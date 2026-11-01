"""The `split` check: a proposal over the cash, with a line that is not whole dollars, to a hold name or to a name with
no target, or without sources is held; a clean one gives nothing; a closed one gives nothing; a bad row is a finding,
never an exception. Bad proposals are built straight through `Store.write_proposal` (it checks only the schema)."""
import json
import re
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path

import pytest

from financial_data_collector import check, config as C
from financial_data_collector.check import CheckContext
from financial_data_collector.check import split as check_split
from financial_data_collector.models import AccountRef, AllocationProposal, CashRow, Snapshot, SplitLine
from financial_data_collector.store import Store

NOW = datetime(2026, 10, 31, 12, 0, tzinfo=timezone.utc)
STAMP = "2026-10-31T12:00:00Z"
SRC = [{"kind": "derived", "label": "cash_balances", "date": "2026-10-30", "ref": "cash_balances"}]
TARGETS = {
    "last_revised": "2026-10-01",
    "_comment": [{"ticker": "NOPE", "target": 1}],
    "brokerage": [
        {"ticker": "VOO", "target": 50},
        {"ticker": "KO", "target": 5, "rebalance": "hold"},
        {"ticker": "JNJ", "target": 5, "rebalance": "contributions"},
        {"ticker": "XOM", "target": 5, "rebalance": "Hold"},
    ],
    "roth": [
        {"ticker": "voo", "target": 40},
        {"ticker": "GLD", "target": 10, "type": "pct_of_brokerage"},
        {"ticker": "AAPL", "target": 5, "rebalance": "hold"},
    ],
    "sleeves": [{"account": "brokerage", "label": "x", "members": ["MSFT"]}],
}


@pytest.fixture
def cfg(tmp_path: Path):
    C.init_project(tmp_path)
    Store.open(tmp_path / "data" / "warehouse.db").close()
    path = tmp_path / "targets.json"
    path.write_text(json.dumps(TARGETS), encoding="utf-8")
    c = C.load_config(tmp_path)
    return replace(c, allocation=replace(c.allocation, targets=path,
                                         accounts={"brokerage": "Sample Brokerage", "roth": "Sample Roth"}))


@pytest.fixture
def store(cfg):
    s = Store.open(cfg.db_path, migrate=False)
    yield s
    s.close()


def _ctx(store, cfg):
    return CheckContext(store=store, cfg=cfg, fetch=None, outside=None, now=NOW)


def _with_targets(cfg, path):
    return replace(cfg, allocation=replace(cfg.allocation, targets=path))


ACCOUNTS = {"brokerage": AccountRef("Sample Brokerage", "brokerage", "fidelity", "brokerage"),
            "roth": AccountRef("Sample Roth", "roth", "fidelity", "roth_ira")}


def _cash_row(store, account: str, amount: float, day: str = "2026-10-30") -> None:
    store.write_snapshot(Snapshot(day, "fidelity_csv", [], [CashRow(ACCOUNTS[account], amount)]))


def _open(store, lines, *, cash=100.0, account="brokerage", cash_row=True) -> int:
    """Write an open proposal, and (unless told not to) the account's cash row it was made from on its cash_date.
    `lines` is [(symbol, dollars)] or [(symbol, dollars, sources)]."""
    if cash_row:
        _cash_row(store, account, cash)
    built, srcs = [], []
    for item in lines:
        built.append(SplitLine(item[0], item[1], 10.0, "usual", 1.0, ()))
        srcs.append(tuple(item[2]) if len(item) > 2 else tuple(SRC))
    p = AllocationProposal(account, "2026-10-30", cash, sum(int(b.dollars) for b in built), 0.0, tuple(built), tuple(srcs), (),
                           tuple(SRC))
    return store.write_proposal(p, created_at=STAMP, checked_at=None, replaces=None)


def _one(store, cfg):
    (f,) = check_split.split(_ctx(store, cfg))
    return f


def test_clean_proposal_has_no_findings(store, cfg):
    _open(store, [("VOO", 60), ("JNJ", 40)])
    assert check_split.split(_ctx(store, cfg)) == []


def test_no_open_proposal_gives_no_finding_and_no_warning_without_a_targets_file(store, cfg):
    assert check_split.split(_ctx(store, _with_targets(cfg, None))) == []


def test_lines_over_the_cash_are_held(store, cfg):
    pid = _open(store, [("VOO", 80), ("JNJ", 40)], cash=100.0)
    f = _one(store, cfg)
    assert (f.check_name, f.symbol, f.field, f.severity) == ("split", "brokerage", f"proposal:{pid}", "hold")
    assert (f.date_from, f.date_to) == ("2026-10-30", None)
    assert "120" in f.message and "100" in f.message
    assert f.evidence["cash"] == 100.0 and f.evidence["lines_total"] == 120


def test_lines_within_half_a_cent_of_the_cash_are_tolerated(store, cfg):
    _open(store, [("VOO", 60), ("JNJ", 40)], cash=99.996)
    assert check_split.split(_ctx(store, cfg)) == []


def test_a_line_that_is_not_a_whole_number_is_held(store, cfg):
    _open(store, [("VOO", 12.5), ("JNJ", 40)])
    f = _one(store, cfg)
    assert f.severity == "hold" and "whole number" in f.message and f.evidence["fractional_lines"] == ["VOO"]


def test_a_line_to_a_hold_name_is_held(store, cfg):
    _open(store, [("VOO", 60), ("KO", 20)])
    f = _one(store, cfg)
    assert f.severity == "hold" and f.evidence["hold_names"] == ["KO"] and "KO" in f.message
    assert "no_target" not in f.evidence       # a hold name is on file: one fault, not two


def test_a_hold_name_holds_only_in_the_account_it_is_filed_under(store, cfg):
    _open(store, [("AAPL", 20)], account="brokerage")          # AAPL is "hold" in roth only: brokerage has no target for it
    assert _one(store, cfg).evidence == {"no_target": ["AAPL"]}


def test_a_line_to_a_name_with_no_target_in_the_account_is_held(store, cfg):
    _open(store, [("VOO", 60), ("SPY", 20)])
    f = _one(store, cfg)
    assert f.severity == "hold" and f.evidence["no_target"] == ["SPY"]


def test_underscore_keys_and_sleeve_members_are_not_targets(store, cfg):
    _open(store, [("NOPE", 10), ("MSFT", 10)])
    assert _one(store, cfg).evidence["no_target"] == ["MSFT", "NOPE"]


def test_a_pct_of_brokerage_entry_counts_under_brokerage_wherever_it_is_filed(store, cfg):
    _open(store, [("GLD", 30)], account="brokerage")           # filed under roth, type pct_of_brokerage
    assert check_split.split(_ctx(store, cfg)) == []


def test_a_pct_of_brokerage_entry_is_known_only_under_brokerage(store, cfg):
    _open(store, [("GLD", 30)], account="roth")                # GLD is pct_of_brokerage, filed under roth
    f = _one(store, cfg)
    assert f.severity == "hold" and f.symbol == "roth" and f.evidence["no_target"] == ["GLD"]


def test_a_pct_of_brokerage_hold_name_holds_under_both_accounts(tmp_path, store, cfg):
    path = tmp_path / "t2.json"
    path.write_text(json.dumps({"roth": [{"ticker": "GLD", "target": 10, "type": "pct_of_brokerage",
                                          "rebalance": "hold"}]}), encoding="utf-8")
    for account in ("roth", "brokerage"):
        with store.conn:
            store.conn.execute("DELETE FROM allocation_lines_raw")
            store.conn.execute("DELETE FROM allocation_proposals_raw")
        _open(store, [("GLD", 30)], account=account)
        f = _one(store, _with_targets(cfg, path))
        assert f.evidence == {"hold_names": ["GLD"]}, account


def test_cash_that_is_not_a_number_holds_the_proposal(store, cfg):
    pid = _open(store, [("VOO", 60)])
    with store.conn:
        store.conn.execute("UPDATE allocation_proposals_raw SET cash = 'lots' WHERE id = ?", (pid,))
    f = _one(store, cfg)
    assert f.severity == "hold" and f.field == f"proposal:{pid}" and "cash is unreadable" in f.message


def test_a_line_to_a_name_with_an_unknown_rebalance_rule_is_held(store, cfg):
    _open(store, [("VOO", 60), ("XOM", 10)])                    # "Hold" is not exactly hold: the split gives it nothing
    f = _one(store, cfg)
    assert f.severity == "hold" and f.evidence == {"unknown_rule": ["XOM"]} and "XOM" in f.message


def test_tickers_are_read_through_canonical(store, cfg):
    _open(store, [("VOO", 30)], account="roth")                # the file says "voo"
    assert check_split.split(_ctx(store, cfg)) == []


def test_a_line_without_sources_is_held(store, cfg):
    for bad in ([], [{"kind": "derived"}], [{"label": "x"}], ["text"], [{"kind": "", "label": "x"}]):
        with store.conn:
            store.conn.execute("DELETE FROM allocation_lines_raw")
            store.conn.execute("DELETE FROM allocation_proposals_raw")
        _open(store, [("VOO", 60), ("JNJ", 40, bad)])
        f = _one(store, cfg)
        assert f.severity == "hold" and f.evidence["unsourced_lines"] == ["JNJ"], bad


def test_sources_that_are_not_a_list_are_a_finding_not_an_exception(store, cfg):
    pid = _open(store, [("VOO", 60)])
    with store.conn:
        store.conn.execute("UPDATE allocation_lines_raw SET sources = ?", ('{"kind": "derived"}',))
    assert _one(store, cfg).evidence["unsourced_lines"] == ["VOO"]
    with store.conn:
        store.conn.execute("UPDATE allocation_lines_raw SET sources = ?", ("not json {",))
    f = _one(store, cfg)
    assert f.field == f"proposal:{pid}" and f.evidence["unsourced_lines"] == ["VOO"]


def test_a_missing_or_unreadable_targets_file_warns_when_a_proposal_is_open(store, cfg, tmp_path):
    pid = _open(store, [("VOO", 60)])
    bad = tmp_path / "bad.json"
    for path, text in ((tmp_path / "gone.json", None), (None, None), (bad, "{not json"), (bad, "[1, 2]")):
        if text is not None:
            bad.write_text(text, encoding="utf-8")
        f = _one(store, _with_targets(cfg, path))
        assert (f.severity, f.field) == ("warn", f"proposal:{pid}") and "cannot check hold names" in f.message
        assert str(tmp_path) not in f.message


def test_without_targets_the_other_rules_still_hold(store, cfg, tmp_path):
    _open(store, [("VOO", 150)], cash=100.0)
    f = _one(store, _with_targets(cfg, tmp_path / "gone.json"))
    assert f.severity == "hold" and "more than the cash" in f.message and "cannot check hold names" in f.message


def test_several_faults_on_one_proposal_are_one_finding(store, cfg):
    _open(store, [("KO", 12.5, []), ("SPY", 200)], cash=100.0)
    f = _one(store, cfg)
    for part in ("more than the cash", "whole number", "marks hold", "no target", "no sources"):
        assert part in f.message
    assert f.severity == "hold"


def test_a_closed_proposal_gets_no_finding(store, cfg):
    pid = _open(store, [("KO", 20)])
    assert len(check_split.split(_ctx(store, cfg))) == 1
    store.close_proposal(pid, closed_at=STAMP, closed_cash=0.0)
    assert check_split.split(_ctx(store, cfg)) == []


def test_split_is_registered_last_and_is_an_engine_check():
    assert list(check.CHECKS)[-1] == "split"
    assert check_split.split is check.CHECKS["split"]


def test_a_held_proposal_shows_held_in_the_view(store, cfg):
    pid = _open(store, [("VOO", 60), ("KO", 20)])
    rep = check.run_check(cfg, now=NOW, store=store, only=["split"], fetch=lambda u, h: b"")
    assert (rep.holds, rep.warns, rep.errors) == (1, 0, [])
    (row,) = store.query("SELECT status, hold_ids FROM allocation_proposals WHERE id = ?", (pid,))
    assert row["status"] == "held" and row["hold_ids"]
    dollars = [r[0] for r in store.query("SELECT dollars FROM allocation_lines WHERE proposal_id = ?", (pid,))]
    assert dollars == [None, None]
    assert store.query("SELECT status FROM allocation_proposals_raw")[0][0] == "open"       # the raw record is untouched
    # the proposal closes: the next run clears the finding
    store.close_proposal(pid, closed_at=STAMP, closed_cash=0.0)
    check.run_check(cfg, now=NOW, store=store, only=["split"], fetch=lambda u, h: b"")
    assert store.check_open_findings("split") == []


def test_check_imports_nothing_from_derive():
    lines = Path(check_split.__file__).read_text(encoding="utf-8").splitlines()
    imports = "\n".join(l for l in lines if l.startswith(("from ", "import ")))
    assert imports and not re.search(r"derive|statements|collectors", imports)


# ---- final fix wave: the proposal's cash against the account's own cash row; numbers no float can hold

def test_a_proposal_whose_cash_is_not_the_accounts_is_held(store, cfg):
    pid = _open(store, [("VOO", 60)], cash=100.0, cash_row=False)
    _cash_row(store, "brokerage", 90.0)
    f = _one(store, cfg)
    assert (f.severity, f.field) == ("hold", f"proposal:{pid}")
    assert "the proposal's cash $100.00 is not the account's $90.00 on 2026-10-30" in f.message
    assert f.evidence["account_cash"] == 90.0
    assert "Sample" not in f.message and "Sample" not in json.dumps(f.evidence)


def test_a_cash_row_within_half_a_cent_agrees(store, cfg):
    _open(store, [("VOO", 60)], cash=100.0, cash_row=False)
    _cash_row(store, "brokerage", 100.004)
    assert check_split.split(_ctx(store, cfg)) == []


def test_a_missing_cash_row_or_an_unmapped_account_warns(store, cfg):
    pid = _open(store, [("VOO", 60)], cash=100.0, cash_row=False)
    _cash_row(store, "brokerage", 100.0, day="2026-10-29")              # not the proposal's day
    f = _one(store, cfg)
    assert (f.severity, f.field) == ("warn", f"proposal:{pid}") and "no cash row" in f.message
    unmapped = replace(cfg, allocation=replace(cfg.allocation, accounts={"roth": "Sample Roth"}))
    f = _one(store, unmapped)
    assert f.severity == "warn" and "not in [allocation.accounts]" in f.message


def test_a_cash_mismatch_joins_the_other_faults_in_one_finding(store, cfg):
    _open(store, [("KO", 20)], cash=100.0, cash_row=False)
    _cash_row(store, "brokerage", 50.0)
    f = _one(store, cfg)
    assert f.severity == "hold" and "marks hold" in f.message and "is not the account's $50.00" in f.message


def test_check_split_cash_reads_one_days_usd_row(store):
    _cash_row(store, "brokerage", 120.0)
    store.write_snapshot(Snapshot("2026-10-30", "fidelity_csv", [], [CashRow(ACCOUNTS["roth"], 7.0, "EUR")]))
    assert store.check_split_cash("Sample Brokerage", "2026-10-30") == 120.0
    assert store.check_split_cash("Sample Brokerage", "2026-10-29") is None
    assert store.check_split_cash("Sample Roth", "2026-10-30") is None          # not dollars
    assert store.check_split_cash("Nobody", "2026-10-30") is None


def test_a_targets_file_too_deep_to_read_warns(store, cfg, tmp_path):
    pid = _open(store, [("VOO", 60)])
    deep = tmp_path / "deep.json"
    deep.write_text('{"brokerage": ' + "[" * 100_000 + "]" * 100_000 + "}", encoding="utf-8")
    f = _one(store, _with_targets(cfg, deep))
    assert (f.severity, f.field) == ("warn", f"proposal:{pid}") and "cannot check hold names" in f.message


def test_a_target_too_large_for_a_float_is_no_target_and_no_exception(store, cfg, tmp_path):
    big = tmp_path / "big.json"
    big.write_text('{"brokerage": [{"ticker": "VOO", "target": 1' + "0" * 400 + '}, {"ticker": "JNJ", "target": 5}]}',
                   encoding="utf-8")
    _open(store, [("VOO", 60), ("JNJ", 40)])
    f = _one(store, _with_targets(cfg, big))
    assert f.severity == "hold" and f.evidence == {"no_target": ["VOO"]}           # the split's reader skips it too
    assert check_split._finite(10 ** 400) is False


def _week(store, verdict, fraction, lines, cash=100.0) -> int:
    _cash_row(store, "brokerage", cash)
    built = tuple(SplitLine(s, d, 10.0, "usual", 1.0, ()) for s, d in lines)
    p = AllocationProposal("brokerage", "2026-10-30", cash, sum(d for _, d in lines), cash - sum(d for _, d in lines),
                           built, tuple(tuple(SRC) for _ in built), (), tuple(SRC), "brokerage", 5, verdict, fraction)
    return store.write_proposal(p, created_at=STAMP, checked_at=None, replaces=None)


def test_a_half_week_within_half_the_cash_is_clean(store, cfg):
    _week(store, "half", 0.5, [("VOO", 30), ("JNJ", 20)])
    assert check_split.split(_ctx(store, cfg)) == []


def test_lines_over_the_weeks_fraction_of_the_cash_are_held(store, cfg):
    pid = _week(store, "half", 0.5, [("VOO", 40), ("JNJ", 20)])
    f = _one(store, cfg)
    assert (f.field, f.severity) == (f"proposal:{pid}", "hold") and "half of the cash" in f.message
    assert f.evidence["fraction"] == 0.5 and f.evidence["lines_total"] == 60


def test_a_hold_week_with_a_line_is_held(store, cfg):
    _week(store, "hold", 0.0, [("VOO", 10)])
    assert _one(store, cfg).severity == "hold"


@pytest.mark.parametrize("verdict,fraction", [("go", 0.5), ("half", 1.0), ("hold", 0.5), ("maybe", 1.0)])
def test_a_fraction_that_is_not_its_verdicts_is_held(store, cfg, verdict, fraction):
    if verdict == "maybe":                                   # the table refuses an unknown verdict; the raw row is bent
        _week(store, "go", fraction, [("VOO", 10)])
        store.conn.execute("PRAGMA ignore_check_constraints = 1")
        store.conn.execute("UPDATE allocation_proposals_raw SET verdict = 'maybe'")
        store.conn.commit()
    else:
        _week(store, verdict, fraction, [("VOO", 10)])
    f = _one(store, cfg)
    assert f.severity == "hold" and "fraction" in f.message
