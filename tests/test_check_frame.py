"""The checker's frame: findings are recorded, repeated, cleared per check, and a broken check hurts no one."""
import json
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from financial_data_collector import check, config as C
from financial_data_collector.check import Finding
from financial_data_collector.config import ConfigError
from financial_data_collector.http import HttpError
from financial_data_collector.store import Store

T0 = datetime(2026, 3, 1, 12, 0, tzinfo=timezone.utc)


def _fetch(url, headers):
    raise HttpError(500, url)


@pytest.fixture
def cfg(tmp_path: Path):
    C.init_project(tmp_path)
    Store.open(tmp_path / "data" / "warehouse.db").close()
    return C.load_config(tmp_path)


def _finding(name="a", symbol="AAA", field="pe", date_from="2026-02-01", severity="hold", message="odd"):
    return Finding(name, symbol, field, date_from, None, severity, message, {"x": 1})


def _rows(cfg, sql="SELECT * FROM check_findings ORDER BY id"):
    s = Store.open(cfg.db_path, migrate=False)
    try:
        return s.query(sql)
    finally:
        s.close()


def _run(cfg, checks, when=T0, **kw):
    check.CHECKS.clear()
    check.CHECKS.update(checks)
    return check.run_check(cfg, fetch=_fetch, now=when, **kw)


@pytest.fixture(autouse=True)
def restore_checks():
    saved = dict(check.CHECKS)
    yield
    check.CHECKS.clear()
    check.CHECKS.update(saved)


def test_a_finding_is_recorded_with_its_evidence(cfg):
    rep = _run(cfg, {"a": lambda ctx: [_finding(), _finding(symbol="BBB", severity="warn")]})
    assert (rep.holds, rep.warns, rep.errors) == (1, 1, [])
    rows = _rows(cfg)
    assert [(r["symbol"], r["severity"], r["cleared_at"]) for r in rows] == [("AAA", "hold", None), ("BBB", "warn", None)]
    assert json.loads(rows[0]["evidence"]) == {"x": 1}
    assert rows[0]["first_seen"] == rows[0]["last_seen"] == "2026-03-01T12:00:00Z"


def test_a_repeat_updates_last_seen_not_a_new_row(cfg):
    _run(cfg, {"a": lambda ctx: [_finding()]})
    _run(cfg, {"a": lambda ctx: [_finding(message="still odd")]}, when=T0 + timedelta(days=1))
    rows = _rows(cfg)
    assert len(rows) == 1
    assert rows[0]["first_seen"] == "2026-03-01T12:00:00Z" and rows[0]["last_seen"] == "2026-03-02T12:00:00Z"
    assert rows[0]["message"] == "still odd" and rows[0]["cleared_at"] is None


def test_a_check_that_finds_nothing_clears_its_open_findings(cfg):
    _run(cfg, {"a": lambda ctx: [_finding("a")], "b": lambda ctx: [_finding("b")]})
    # only a runs and finds nothing: a's finding is cleared, b's is untouched
    _run(cfg, {"a": lambda ctx: [], "b": lambda ctx: [_finding("b")]}, when=T0 + timedelta(days=1), only=["a"])
    by = {r["check_name"]: r for r in _rows(cfg)}
    assert by["a"]["cleared_at"] == "2026-03-02T12:00:00Z"
    assert by["b"]["cleared_at"] is None


def test_a_cleared_finding_that_comes_back_is_reopened(cfg):
    _run(cfg, {"a": lambda ctx: [_finding()]})
    _run(cfg, {"a": lambda ctx: []}, when=T0 + timedelta(days=1))
    _run(cfg, {"a": lambda ctx: [_finding()]}, when=T0 + timedelta(days=2))
    rows = _rows(cfg)
    assert len(rows) == 1 and rows[0]["cleared_at"] is None
    assert rows[0]["first_seen"] == "2026-03-01T12:00:00Z" and rows[0]["last_seen"] == "2026-03-03T12:00:00Z"


def test_a_check_that_raises_leaves_its_findings_untouched(cfg):
    def boom(ctx):
        raise RuntimeError("no network")

    _run(cfg, {"a": lambda ctx: [_finding("a")], "b": lambda ctx: [_finding("b")]})
    rep = _run(cfg, {"a": boom, "b": lambda ctx: []}, when=T0 + timedelta(days=1))
    by = {r["check_name"]: r for r in _rows(cfg)}
    assert by["a"]["cleared_at"] is None and by["a"]["last_seen"] == "2026-03-01T12:00:00Z"
    assert by["b"]["cleared_at"] == "2026-03-02T12:00:00Z"
    assert len(rep.errors) == 1 and rep.errors[0].startswith("a: RuntimeError") and "no network" in rep.errors[0]
    run = _rows(cfg, "SELECT * FROM check_runs ORDER BY run_id DESC")[0]
    assert run["checks_run"] == 1 and "a: RuntimeError" in run["error"]


def test_an_unknown_check_name_is_refused(cfg):
    with pytest.raises(ConfigError, match="nope"):
        _run(cfg, {"a": lambda ctx: []}, only=["nope"])


def test_a_check_is_given_its_context(cfg):
    seen = []
    outside = object()
    check.CHECKS.clear()
    check.CHECKS["a"] = lambda ctx: seen.append(ctx) or []
    check.run_check(cfg, fetch=_fetch, outside=outside, now=T0)
    ctx = seen[0]
    assert ctx.outside is outside and ctx.now == T0 and ctx.fetch is _fetch and ctx.cfg is cfg
    assert hasattr(ctx.store, "check_open_findings")


def test_a_bad_severity_is_refused():
    with pytest.raises(ValueError):
        _finding(severity="loud")


def test_check_run_records_the_derive_it_covered(cfg):
    s = Store.open(cfg.db_path, migrate=False)
    s.log_run("20260301T100000Z", "derive", "ok", 1, "", "2026-03-01T10:00:00Z", "2026-03-01T10:01:00Z")
    s.log_run("20260301T110000Z", "derive", "error", 0, "x", "2026-03-01T11:00:00Z", "2026-03-01T11:01:00Z")
    s.log_run("20260301T110000Z", "export", "ok", 0, "", "2026-03-01T11:02:00Z", "2026-03-01T11:03:00Z")
    s.close()
    rep = _run(cfg, {"a": lambda ctx: [_finding()]})
    run = _rows(cfg, "SELECT * FROM check_runs")[0]
    assert run["run_id"] == rep.run_id == "20260301T120000Z"
    assert run["derive_run_id"] == "20260301T100000Z"
    assert (run["checks_run"], run["holds"], run["warns"], run["error"]) == (1, 1, 0, None)


def test_derive_run_id_is_null_when_no_derive_succeeded(cfg):
    _run(cfg, {})
    assert _rows(cfg, "SELECT * FROM check_runs")[0]["derive_run_id"] is None


def test_check_imports_nothing_it_checks():
    src = Path(check.__file__).parent
    files = list(src.rglob("*.py"))
    assert len(files) >= 2
    text = "\n".join(p.read_text(encoding="utf-8") for p in files)
    banned = r"(derive|statements|collectors)"
    assert not re.search(rf"from\s+\.\.\s*{banned}", text)                       # from ..derive.x import y
    assert not re.search(rf"from\s+\.\.\s+import\s+[^\n]*\b{banned}\b", text)    # from .. import derive
    assert not re.search(r"from\s+\.\.collectors\.(?!http)", text)
    assert not re.search(rf"(from|import)\s+financial_data_collector(\.|\s+import\s+[^\n]*\b){banned}", text)


def test_a_check_that_returns_junk_hurts_no_other_check(cfg):
    _run(cfg, {"a": lambda ctx: [_finding("a")], "b": lambda ctx: [_finding("b")], "c": lambda ctx: [_finding("c")]})
    junk = {
        "a": lambda ctx: [_finding("a", symbol=None)],      # NOT NULL column
        "c": lambda ctx: None,
        "b": lambda ctx: [],
    }
    rep = _run(cfg, junk, when=T0 + timedelta(days=1))
    by = {r["check_name"]: r for r in _rows(cfg)}
    assert by["a"]["cleared_at"] is None and by["a"]["last_seen"] == "2026-03-01T12:00:00Z"
    assert by["c"]["cleared_at"] is None
    assert by["b"]["cleared_at"] == "2026-03-02T12:00:00Z"
    assert sorted(e.split(":")[0] for e in rep.errors) == ["a", "c"]
    run = _rows(cfg, "SELECT * FROM check_runs ORDER BY run_id DESC")[0]
    assert run["checks_run"] == 1 and "a:" in run["error"] and "c:" in run["error"]


def test_unserialisable_evidence_is_that_checks_error_and_records_nothing(cfg):
    good = Finding("a", "AAA", "pe", "2026-02-02", None, "hold", "fine", {})
    bad = Finding("a", "AAA", "pe", "2026-02-03", None, "hold", "x", {1: object(), "k": 2, (1, 2): 3})  # json refuses a tuple key
    rep = _run(cfg, {"a": lambda ctx: [good, bad]})
    assert len(rep.errors) == 1 and rep.errors[0].startswith("a: ")
    assert _rows(cfg) == []                                  # the good one was rolled back with the bad one


def test_a_finding_under_another_checks_name_is_an_error(cfg):
    rep = _run(cfg, {"a": lambda ctx: [_finding("zzz")]})
    assert len(rep.errors) == 1 and "a: ValueError" in rep.errors[0]
    assert _rows(cfg) == []


def test_run_times_come_from_the_injected_clock(cfg):
    _run(cfg, {})
    run = _rows(cfg, "SELECT * FROM check_runs")[0]
    assert run["started_at"] == "2026-03-01T12:00:00Z"
    assert "2026-03-01T12:00:00Z" <= run["finished_at"] < "2026-03-01T12:01:00Z"


def test_the_report_counts_open_holds_across_checks(cfg):
    _run(cfg, {"a": lambda ctx: [_finding("a")], "b": lambda ctx: [_finding("b")]})
    rep = _run(cfg, {"a": lambda ctx: [], "b": lambda ctx: [_finding("b")]}, when=T0 + timedelta(days=1), only=["a"])
    assert rep.holds == 0 and rep.open_holds == 1


def test_open_findings_come_most_recently_seen_first(cfg):
    _run(cfg, {"a": lambda ctx: [_finding("a")]})
    _run(cfg, {"b": lambda ctx: [_finding("b")]}, when=T0 + timedelta(days=1))
    s = Store.open(cfg.db_path, migrate=False)
    names = [r["check_name"] for r in s.check_open_findings()]
    s.close()
    assert names == ["b", "a"]


def test_fdc_check_prints_one_line_of_counts(cfg, capsys):
    from financial_data_collector import cli

    check.CHECKS.clear()
    check.CHECKS["a"] = lambda ctx: [_finding()]
    check.CHECKS["b"] = lambda ctx: []
    rc = cli.main(["--root", str(cfg.root), "check", "--only", "a"])
    out = capsys.readouterr().out
    assert rc == 0 and re.search(r"check: 1 holds found, 1 open, 0 warnings, 0 errors \(run \d{8}T\d{6}Z\)", out), out
    assert [r["check_name"] for r in _rows(cfg)] == ["a"]
