import sqlite3
import json
import shutil
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from financial_data_collector import config as C
from financial_data_collector import sync
from financial_data_collector.connections import keys as K
from financial_data_collector.collectors import sec_cik
from financial_data_collector.http import HttpError
from financial_data_collector.store import Store

NOW = datetime(2026, 3, 1, 12, 0, tzinfo=timezone.utc)
TIINGO = [{"date": "2026-01-02T00:00:00.000Z", "close": 1.0, "adjClose": 1.0, "divCash": 0, "splitFactor": 1}]


@pytest.fixture
def project(tmp_path: Path, fixtures: Path):
    C.init_project(tmp_path)
    (tmp_path / "config.toml").write_text('[sources.snaptrade]\nenabled = true\ndir = "snap"\n')
    (tmp_path / ".env").write_text("TIINGO_API_TOKEN=tok\nSEC_USER_AGENT=Sample Person s@example.com\n")
    shutil.copytree(fixtures / "snaptrade", tmp_path / "snap")
    shutil.copy(fixtures / "Portfolio_Positions_Jan-15-2026.csv", tmp_path / "inbox")
    return C.load_config(tmp_path)


def _fetch(fixtures: Path):
    def fetch(url, headers):
        if url == sec_cik.CIK_URL:
            return (fixtures / "sec" / "company_tickers.json").read_bytes()
        if "companyfacts/CIK0000000001" in url:
            return (fixtures / "sec" / "companyfacts_SAMPLE.json").read_bytes()
        if "tiingo" in url and "/AAPL/" in url:
            return json.dumps(TIINGO).encode()
        raise HttpError(404, url)
    return fetch


def _counts(cfg):
    s = Store.open(cfg.db_path, migrate=False)
    try:
        return s.counts()
    finally:
        s.close()


def test_full_sync_then_noop(project, fixtures: Path):
    fetch = _with_submissions(fixtures, {"filings": {"recent": {}}})      # SEC answers the earnings step: no 8-Ks
    rep = sync.run_sync(project, fetch=fetch, now=NOW, yf=lambda s, d: [], sleep=lambda s: None)
    assert [s.step for s in rep.steps] == ["connections", "ingest", "prices", "macro", "sec", "funds", "earnings", "derive", "export", "check", "engine"]
    assert [s.status for s in rep.steps] == ["skipped", "ok", "ok", "skipped", "ok", "ok", "ok", "ok", "skipped", "ok", "ok"], rep.steps   # no connections, no FRED key; export disabled by default
    assert rep.exit_code == 0
    c1 = _counts(project)
    assert c1["position_snapshots"] > 0 and c1["prices"] > 0 and c1["sec_facts"] == 35
    assert c1["financial_line_items"] > 0 and c1["sync_runs"] == 11
    assert c1["valuation_scores_raw"] > 0 and rep.steps[-1].message.endswith("0 moves; split: no targets file; checks: 0 holds, 0 warnings, 0 errors")
    assert "without data" in rep.steps[2].message          # KO, VTI, BRK.B had no bars
    rep2 = sync.run_sync(project, fetch=fetch, now=NOW, yf=lambda s, d: [], sleep=lambda s: None)
    assert rep2.steps[1].rows == 0
    c2 = _counts(project)
    logs = ("sync_runs", "check_runs")                   # each run logs itself; the data rows must not change
    assert {k: v for k, v in c2.items() if k not in logs} == {k: v for k, v in c1.items() if k not in logs}
    assert c2["check_runs"] == c1["check_runs"] + 2          # the check step, then the engine's re-check of its scores
    assert rep2.steps[4].message.startswith("0 fetched")


def test_only_and_skip(project, fixtures: Path):
    rep = sync.run_sync(project, only=["prices"], fetch=_fetch(fixtures), now=NOW, yf=lambda s, d: [], sleep=lambda s: None)
    assert [s.step for s in rep.steps] == ["prices"]
    rep = sync.run_sync(project, skip=["sec"], fetch=_fetch(fixtures), now=NOW, yf=lambda s, d: [], sleep=lambda s: None)
    assert [s.step for s in rep.steps] == ["connections", "ingest", "prices", "macro", "funds", "earnings", "derive", "export", "check", "engine"]


def test_dry_run_touches_nothing(project):
    rep = sync.run_sync(project, dry_run=True)
    assert rep.dry_run and [s.step for s in rep.steps] == ["connections", "ingest", "prices", "macro", "sec", "funds", "earnings", "derive", "export", "check", "engine"]
    assert not project.db_path.exists()


def test_step_failure_is_isolated(project, fixtures: Path):
    (project.snaptrade_dir / "snapshots" / "live-positions-2026-01-11.json").write_text("{not json")
    rep = sync.run_sync(project, fetch=_fetch(fixtures), now=NOW, yf=lambda s, d: [], sleep=lambda s: None)
    by = {s.step: s for s in rep.steps}
    assert by["ingest"].status == "error" and "JSONDecodeError" in by["ingest"].message
    assert by["prices"].status == "ok" and by["sec"].status == "ok"
    assert rep.exit_code == 0


def test_missing_user_agent_skips_sec(project, fixtures: Path):
    project.sec_user_agent = None
    rep = sync.run_sync(project, fetch=_fetch(fixtures), now=NOW, yf=lambda s, d: [], sleep=lambda s: None)
    by = {s.step: s for s in rep.steps}
    assert by["sec"].status == "skipped" and "SEC_USER_AGENT" in by["sec"].message
    assert rep.exit_code == 0


class _YahooDown:
    def fund_info(self, symbol):
        raise OSError("network down")

    def earnings_dates(self, symbol, limit):
        raise OSError("network down")

    def estimates(self, symbol):
        raise OSError("network down")


def test_network_down_exit_code(project):
    def down(url, headers):
        raise HttpError(500, url)
    rep = sync.run_sync(project, fetch=down, now=NOW, yf=lambda s, d: [], yahoo=_YahooDown(), sleep=lambda s: None)
    by = {s.step: s for s in rep.steps}
    assert by["ingest"].status == "ok"
    assert by["prices"].status == "error" and by["sec"].status == "error" and by["funds"].status == "error"
    assert by["earnings"].status == "skipped"         # no company is typed a stock without SEC: nothing to ask
    assert rep.exit_code == 1


def test_quiet_morning_with_dead_symbol_is_ok(project, fixtures: Path):
    sync.run_sync(project, fetch=_fetch(fixtures), now=NOW, yf=lambda s, d: [], sleep=lambda s: None)
    s = Store.open(project.db_path, migrate=False)
    s.upsert_security("DEAD", first_seen="2026-01-01")
    s.close()

    def quiet(url, headers):
        if url == sec_cik.CIK_URL:
            return (fixtures / "sec" / "company_tickers.json").read_bytes()
        if "tiingo" in url:
            return b"[]"
        raise HttpError(404, url)

    rep = sync.run_sync(project, only=["prices"], fetch=quiet, now=NOW, yf=lambda s, d: [], sleep=lambda s: None)
    step = rep.steps[0]
    assert step.status == "ok", step
    assert "no new bars" not in step.message and "DEAD" in step.message


def test_derive_and_export_steps(project, fixtures: Path, tmp_path: Path):
    rep = sync.run_sync(project, fetch=_fetch(fixtures), now=NOW, yf=lambda s, d: [], sleep=lambda s: None)
    by = {s.step: s for s in rep.steps}
    assert by["derive"].status == "ok" and "holdings_daily" in by["derive"].message
    s = Store.open(project.db_path, migrate=False)
    assert s.query("SELECT COUNT(*) FROM holdings_daily")[0][0] > 0
    assert s.query("SELECT COUNT(*) FROM valuation_daily")[0][0] > 0
    assert s.query("SELECT COUNT(*) FROM portfolio_daily_full")[0][0] >= s.query("SELECT COUNT(*) FROM portfolio_daily")[0][0]
    s.close()
    assert by["export"].status == "skipped" and "disabled" in by["export"].message
    out = tmp_path / "cockpit"
    out.mkdir()
    project.export_cockpit = True
    project.export_dir = out
    rep = sync.run_sync(project, only=["export"], fetch=_fetch(fixtures), now=NOW, yf=lambda s, d: [], sleep=lambda s: None)
    assert rep.steps[0].status == "ok" and rep.steps[0].rows == 3
    assert sorted(p.name for p in out.iterdir()) == ["dividends.json", "fundamentals.json", "prices.json"]


def test_derive_reshapes_statements_from_stored_facts(project, fixtures: Path):
    # Line items are a function of the shaping code and the concept map, so derive
    # rebuilds them from sec_facts every run; a company fetched last week with an
    # older shaper must not keep stale rows.
    sync.run_sync(project, fetch=_fetch(fixtures), now=NOW, yf=lambda s, d: [], sleep=lambda s: None)
    s = Store.open(project.db_path, migrate=False)
    s.replace_line_items("0000000001", [])
    assert s.query("SELECT COUNT(*) FROM financial_line_items")[0][0] == 0
    s.close()
    rep = sync.run_sync(project, only=["derive"], fetch=_fetch(fixtures), now=NOW, yf=lambda s, d: [], sleep=lambda s: None)
    assert rep.steps[0].status == "ok" and "statements" in rep.steps[0].message
    s = Store.open(project.db_path, migrate=False)
    assert s.query("SELECT COUNT(*) FROM financial_line_items")[0][0] > 0
    s.close()


def test_derive_isolates_a_company_whose_statements_fail(project, fixtures: Path, monkeypatch):
    sync.run_sync(project, fetch=_fetch(fixtures), now=NOW, yf=lambda s, d: [], sleep=lambda s: None)
    s = Store.open(project.db_path, migrate=False)
    s.write_sec_facts("0000000099", s.facts_for("0000000001")[:3])
    s.close()
    from financial_data_collector import statements as st
    real = st.rebuild

    def flaky(facts, rules):
        if len(facts) == 3:
            raise ValueError("odd filer")
        return real(facts, rules)

    monkeypatch.setattr(sync.statements, "rebuild", flaky)
    rep = sync.run_sync(project, only=["derive"], fetch=_fetch(fixtures), now=NOW, yf=lambda s, d: [], sleep=lambda s: None)
    assert rep.steps[0].status == "ok" and "1 failed" in rep.steps[0].message
    s = Store.open(project.db_path, migrate=False)
    assert s.query("SELECT COUNT(*) FROM financial_line_items WHERE cik='0000000001'")[0][0] > 0
    s.close()


def test_run_sync_reports_step_progress(project, fixtures: Path):
    seen = []
    sync.run_sync(project, fetch=_fetch(fixtures), now=NOW, yf=lambda s, d: [], sleep=lambda s: None,
                  progress=lambda step, detail: seen.append((step, detail)))
    steps = [s for s, _ in seen]
    assert steps[0] == "connections" and "ingest" in steps and "prices" in steps and "sec" in steps and "derive" in steps
    assert any(step == "prices" and detail.endswith(" AAPL") for step, detail in seen)


def test_connections_step_runs_the_connections(project, fixtures: Path, tmp_path: Path):
    from tests.test_simplefin import ACCESS, ANSWER
    import json as _json

    home = K.KeyHome(K.MemoryKeyStore(), K.EnvFile(tmp_path / ".env", environ={"FDC_SIMPLEFIN_ACCESS_URL": ACCESS}))

    def fetch(url, headers):
        if "simplefin" in url:
            return _json.dumps(ANSWER).encode()
        return _fetch(fixtures)(url, headers)
    rep = sync.run_sync(project, only=["connections", "derive"], fetch=fetch, now=NOW, yf=lambda s, d: [],
                        sleep=lambda s: None, keys=home)
    by = {s.step: s for s in rep.steps}
    assert by["connections"].status == "ok" and "simplefin: 3 accounts" in by["connections"].message
    s = Store.open(project.db_path, migrate=False)
    assert s.query("SELECT COUNT(*) FROM accounts WHERE origin = 'simplefin'")[0][0] == 3
    assert s.query("SELECT COUNT(*) FROM cash_daily")[0][0] > 0
    assert s.query("SELECT COUNT(*) FROM reconciliation")[0][0] == 0
    s.close()


def test_a_skipped_connections_step_leaves_the_exit_code_to_the_other_network_steps(project):
    def down(url, headers):
        raise HttpError(500, url)
    rep = sync.run_sync(project, fetch=down, now=NOW, yf=lambda s, d: [], yahoo=_YahooDown(), sleep=lambda s: None)
    by = {s.step: s for s in rep.steps}
    assert by["connections"].status == "skipped" and rep.exit_code == 1


def test_run_origin_is_logged(project, fixtures: Path):
    sync.run_sync(project, fetch=_fetch(fixtures), now=NOW, yf=lambda s, d: [], sleep=lambda s: None,
                  only=["ingest"], origin="console")
    sync.run_sync(project, fetch=_fetch(fixtures), now=NOW, yf=lambda s, d: [], sleep=lambda s: None, only=["ingest"])
    s = Store.open(project.db_path, migrate=False)
    try:
        assert [r[0] for r in s.query("SELECT origin FROM sync_runs ORDER BY id")] == ["console", "manual"]
    finally:
        s.close()
    assert sync.ORIGINS == ("scheduled", "manual", "console")
    assert sync.STALE_HOURS["sec"] == 336


def test_heartbeat_lives_during_the_run_and_not_after(project, fixtures: Path):
    from financial_data_collector import heartbeat as H
    seen: list[tuple[str, str, list[str]]] = []

    def progress(step: str, detail: str) -> None:
        hb = H.read(project.heartbeat_path)
        assert hb is not None, "the heartbeat exists while a step reports progress"
        seen.append((hb.step, hb.detail, list(hb.steps_done)))

    rep = sync.run_sync(project, fetch=_fetch(fixtures), now=NOW, yf=lambda s, d: [], sleep=lambda s: None,
                        only=["ingest", "derive"], origin="console", progress=progress)
    assert [s.status for s in rep.steps] == ["ok", "ok"]
    assert not project.heartbeat_path.exists()
    assert seen[0][0] == "ingest" and seen[0][2] == []
    assert any(step == "derive" and done == ["ingest"] for step, _, done in seen)


def test_dry_run_writes_no_heartbeat(project):
    rep = sync.run_sync(project, dry_run=True)
    assert rep.dry_run and not project.heartbeat_path.exists()


def test_sync_waits_for_another_live_sync(project, fixtures: Path, monkeypatch):
    from financial_data_collector import heartbeat as H
    other = H.Heartbeat("r0", 999999, "scheduled", "2026-03-01T11:59:00Z", "2026-03-01T11:59:50Z", "prices", "3/5")
    project.heartbeat_path.write_text(other.to_json(), encoding="utf-8")
    slept: list[float] = []
    monkeypatch.setattr(H, "pid_alive", lambda pid: pid == 999999 and len(slept) < 2)
    rep = sync.run_sync(project, fetch=_fetch(fixtures), now=NOW, yf=lambda s, d: [], sleep=slept.append,
                        only=["ingest"])
    assert slept == [5.0, 5.0]
    assert rep.steps[0].message.startswith("waited 10 s for another sync; ")
    assert not project.heartbeat_path.exists()


def test_no_heartbeat_is_left_when_the_store_cannot_open(project, monkeypatch):
    def boom(*a, **k):
        raise sqlite3.OperationalError("disk I/O error")

    monkeypatch.setattr(sync.Store, "open", boom)
    with pytest.raises(sqlite3.OperationalError):
        sync.run_sync(project, now=NOW, sleep=lambda s: None, only=["ingest"])
    assert not project.heartbeat_path.exists()


def test_sync_runs_check_last_and_a_failing_check_does_not_fail_the_sync(project, fixtures: Path, monkeypatch):
    from financial_data_collector import check

    def boom(ctx):
        raise RuntimeError("check is broken")

    monkeypatch.setattr(check, "CHECKS", {"boom": boom})
    rep = sync.run_sync(project, fetch=_fetch(fixtures), now=NOW, yf=lambda s, d: [], sleep=lambda s: None)
    last = rep.steps[-2]
    assert last.step == "check" and last.status == "ok" and rep.exit_code == 0
    assert "0 holds found, 0 open, 0 warnings, 1 errors" in last.message and "boom: RuntimeError" in last.message
    eng = rep.steps[-1]                                          # a check run with an error covers no derive
    assert (eng.step, eng.status) == ("engine", "skipped") and eng.message.startswith("waiting for a check run")
    s = Store.open(project.db_path, migrate=False)
    run = s.query("SELECT * FROM check_runs")[0]
    s.close()
    assert run["run_id"].startswith(rep.run_id[:-1]) and run["derive_run_id"] is None     # a run where a check failed covers no derive


def test_check_is_not_a_network_step_and_goes_stale_in_two_days():
    assert "check" not in sync.NETWORK_STEPS and sync.STALE_HOURS["check"] == 48


def test_engine_runs_after_check():
    assert sync.STEPS[-2:] == ("check", "engine")
    assert "engine" not in sync.NETWORK_STEPS and sync.STALE_HOURS["engine"] == 48


def _with_submissions(fixtures: Path, answer):
    inner = _fetch(fixtures)

    def fetch(url, headers):
        if "submissions/CIK0000000001" in url:
            return json.dumps(answer).encode()
        return inner(url, headers)
    return fetch


def test_sync_check_step_holds_a_company_for_a_filing_sec_lists_and_the_warehouse_lacks(project, fixtures: Path):
    answer = {"filings": {"recent": {
        "form": ["10-Q"], "accessionNumber": ["0000000001-26-000007"], "filingDate": ["2026-02-10"], "isXBRL": [1]}}}
    rep = sync.run_sync(project, fetch=_with_submissions(fixtures, answer), now=NOW, yf=lambda s, d: [],
                        sleep=lambda s: None)
    last = rep.steps[-2]
    assert last.step == "check" and last.status == "ok" and rep.exit_code == 0 and "filings:" not in last.message
    s = Store.open(project.db_path, migrate=False)
    rows = s.check_open_findings("filings")
    s.close()
    [hold] = [r for r in rows if r["severity"] == "hold"]
    assert (hold["symbol"], hold["field"], hold["date_from"]) == ("AAPL", "*", "2026-02-10")
    assert json.loads(hold["evidence"])["accession"] == "0000000001-26-000007"


def test_sync_check_step_when_sec_has_no_submissions_is_an_info_finding_and_no_error(project, fixtures: Path):
    rep = sync.run_sync(project, fetch=_fetch(fixtures), now=NOW, yf=lambda s, d: [], sleep=lambda s: None)
    last = rep.steps[-2]
    assert last.status == "ok" and "0 errors" in last.message
    s = Store.open(project.db_path, migrate=False)
    rows = s.check_open_findings("filings")
    run = s.check_last_run()            # the newest FULL run: the engine's re-check runs `scores` alone and is passed over
    s.close()
    assert run["derive_run_id"] == rep.run_id                   # a clean run of every check covers the sync's derive
    assert [(r["symbol"], r["severity"]) for r in rows] == [("AAPL", "info")]


def _trust_fetch(fixtures: Path, bars=None):
    """AAPL's CIK as a company SEC names "... Royalty Trust", whose facts have no revenue: classify_filer calls it
    a fund unless [classify] says otherwise."""
    facts = json.loads((fixtures / "sec" / "companyfacts_SAMPLE.json").read_text())
    facts["facts"]["us-gaap"].pop("Revenues")
    tickers = json.loads((fixtures / "sec" / "company_tickers.json").read_text())
    tickers["0"]["title"] = "Sample Royalty Trust"
    bars = bars or [{"date": "2026-02-20T00:00:00.000Z", "close": 10.0, "adjClose": 10.0, "divCash": 0, "splitFactor": 1}]

    def fetch(url, headers):
        if url == sec_cik.CIK_URL:
            return json.dumps(tickers).encode()
        if "companyfacts/CIK0000000001" in url:
            return json.dumps(facts).encode()
        if "tiingo" in url and "/AAPL/" in url:
            return json.dumps(bars).encode()
        raise HttpError(404, url)
    return fetch


def _aapl(cfg):
    s = Store.open(cfg.db_path, migrate=False)
    try:
        kind = s.query("SELECT asset_type FROM securities WHERE symbol = 'AAPL'")[0][0]
        statuses = {r[0] for r in s.query("SELECT status FROM valuation_daily WHERE symbol = 'AAPL'")}
        items = s.query("SELECT COUNT(*) FROM financial_line_items WHERE cik = '0000000001'")[0][0]
        return kind, statuses, items
    finally:
        s.close()


def test_without_an_override_the_trust_is_typed_a_fund(project, fixtures: Path):
    sync.run_sync(project, only=["ingest"], fetch=_trust_fetch(fixtures), now=NOW, yf=lambda s, d: [], sleep=lambda s: None)
    sync.run_sync(project, only=["prices", "sec", "derive"], fetch=_trust_fetch(fixtures), now=NOW,
                  yf=lambda s, d: [], sleep=lambda s: None)
    kind, statuses, _ = _aapl(project)
    assert kind == "fund" and statuses == {"unsupported"}


def test_a_classify_override_survives_every_sec_refetch(project, fixtures: Path):
    project.classify = {"AAPL": "stock"}
    sync.run_sync(project, only=["ingest"], fetch=_trust_fetch(fixtures), now=NOW, yf=lambda s, d: [], sleep=lambda s: None)
    for days in (0, 2, 4):            # the SEC answer is refetched each time (max age 24 h)
        when = NOW + timedelta(days=days)
        rep = sync.run_sync(project, only=["prices", "sec", "derive"], fetch=_trust_fetch(fixtures), now=when,
                            yf=lambda s, d: [], sleep=lambda s: None)
        assert [s.status for s in rep.steps] == ["ok", "ok", "ok"], rep.steps
        kind, statuses, items = _aapl(project)
        assert kind == "stock", days
        assert statuses and "unsupported" not in statuses and items > 0


def test_derive_does_not_reshape_a_company_the_sec_step_typed_a_foreign_filer(project, fixtures: Path):
    sync.run_sync(project, fetch=_fetch(fixtures), now=NOW, yf=lambda s, d: [], sleep=lambda s: None)
    assert _aapl(project)[2] > 0                         # shaped as a company first

    def ifrs(url, headers):                              # its next filing is IFRS: a foreign filer from now on
        if "companyfacts/CIK0000000001" in url:
            return (fixtures / "sec" / "companyfacts_IFRS.json").read_bytes()
        return _fetch(fixtures)(url, headers)

    later = NOW + timedelta(days=2)
    rep = sync.run_sync(project, only=["sec", "derive"], fetch=ifrs, now=later, yf=lambda s, d: [], sleep=lambda s: None)
    assert [s.status for s in rep.steps] == ["ok", "ok"], rep.steps
    kind, statuses, items = _aapl(project)
    assert kind == "foreign_filer" and items == 0        # its old us-gaap facts are still in sec_facts, but not shaped
    rep = sync.run_sync(project, only=["derive"], fetch=ifrs, now=later, yf=lambda s, d: [], sleep=lambda s: None)
    assert _aapl(project)[2] == 0 and "statements reshaped 0" in rep.steps[0].message


def test_steps_include_funds_after_sec():
    assert sync.STEPS[sync.STEPS.index("sec") + 1] == "funds" and "funds" in sync.NETWORK_STEPS
    assert sync.STALE_HOURS["funds"] == 48


def test_engine_step_rechecks_what_it_wrote_and_does_not_stamp_checked_at(project):
    from financial_data_collector.derive import valuation as V
    from financial_data_collector.models import PriceBar
    covered_at = "2026-03-01T11:30:00Z"
    s = Store.open(project.db_path)
    s.upsert_security("AAA", asset_type="stock", first_seen="2025-01-01")
    day = datetime(2026, 2, 27)
    bars = []
    while len(bars) < 30:
        if day.weekday() < 5:
            bars.append(PriceBar(day.strftime("%Y-%m-%d"), 100.0, 100.0, 0.0, 1.0))
        day -= timedelta(days=1)
    s.write_prices("AAA", bars[::-1], "tiingo", fetched_at=datetime(2026, 2, 28, 12, 0, tzinfo=timezone.utc))
    s.replace_rows("valuation_daily", V.COLUMNS, [tuple(
        {"symbol": "AAA", "date": "2026-02-27", "close": 100.0, "status": "ok"}.get(c) for c in V.COLUMNS)])
    s.log_run("r1", "derive", "ok", 0, "", "2026-03-01T11:00:00Z", "2026-03-01T11:01:00Z", "manual")
    s.check_log_run("c1", started_at=covered_at, finished_at=covered_at, derive_run_id="r1", checks_run=9, holds=0,
                    warns=0, error=None)
    latest = s.query("SELECT checked_at FROM valuation_latest WHERE symbol = 'AAA'")[0][0]
    s.close()
    assert latest == covered_at

    rep = sync.run_sync(project, now=NOW, sleep=lambda s: None, only=["engine"])
    (step,) = rep.steps
    assert step.status == "ok" and step.message.endswith("; checks: 0 holds, 0 warnings, 0 errors"), step.message
    s = Store.open(project.db_path, migrate=False)
    try:
        runs = s.query("SELECT checks_run, derive_run_id FROM check_runs ORDER BY run_id")
        assert sorted(tuple(r) for r in runs) == [(1, None), (9, "r1")]            # the re-check ran `scores` alone, covering no derive
        assert {r[0] for r in s.query("SELECT checked_at FROM valuation_scores_raw")} == {covered_at}
        assert s.query("SELECT checked_at FROM valuation_latest WHERE symbol = 'AAA'")[0][0] == covered_at
        assert s.engine_coverage()["check_run_id"] == "c1"
    finally:
        s.close()


def test_engine_step_skipped_runs_no_check(project):
    s = Store.open(project.db_path)
    s.log_run("r1", "derive", "ok", 0, "", "2026-03-01T11:00:00Z", "2026-03-01T11:01:00Z", "manual")
    s.close()
    (step,) = sync.run_sync(project, now=NOW, sleep=lambda s: None, only=["engine"]).steps
    assert step.status == "skipped" and "checks:" not in step.message
    s = Store.open(project.db_path, migrate=False)
    assert s.query("SELECT COUNT(*) FROM check_runs")[0][0] == 0
    s.close()


def _with_allocation(project, tmp_path: Path, targets: bool = True):
    """The project with an [allocation] table naming an invented targets file and one account."""
    doc = {"last_revised": "2026-10-01", "brokerage": [{"ticker": "VOO", "target": 60.0}]}
    path = tmp_path / "sizing-targets.json"
    if targets:
        path.write_text(json.dumps(doc), encoding="utf-8")
    cfg_path = project.root / "config.toml"
    cfg_path.write_text(cfg_path.read_text(encoding="utf-8") + f'\n[allocation]\ntargets = "{path.as_posix()}"\n'
                        '\n[allocation.accounts]\nbrokerage = "Sample Brokerage"\n', encoding="utf-8")
    return C.load_config(project.root)


def _brokerage_cash(cfg, day: str, amount: float) -> None:
    from financial_data_collector.models import AccountRef, CashRow, PositionRow, Snapshot
    acct = AccountRef("Sample Brokerage", "brokerage", "fidelity", "brokerage")
    s = Store.open(cfg.db_path)
    s.write_snapshot(Snapshot(day, "fidelity_csv", [PositionRow(acct, "VOO", None, 1.0, 400.0, 400.0)],
                              [CashRow(acct, amount)]))
    s.close()


def _proposals(cfg) -> list[tuple]:
    s = Store.open(cfg.db_path, migrate=False)
    try:
        return [tuple(r) for r in s.query("SELECT * FROM allocation_proposals_raw ORDER BY id")]
    finally:
        s.close()


def test_engine_step_skipped_leaves_proposals_untouched(project, tmp_path: Path):
    from financial_data_collector.derive import split
    cfg = _with_allocation(project, tmp_path)
    _brokerage_cash(cfg, "2026-02-26", 500.0)
    s = Store.open(cfg.db_path)
    s.log_run("r1", "derive", "ok", 0, "", "2026-03-01T11:00:00Z", "2026-03-01T11:01:00Z", "manual")
    s.close()
    (step,) = sync.run_sync(cfg, now=NOW, sleep=lambda s: None, only=["engine"]).steps
    assert step.status == "skipped" and "split" not in step.message
    assert _proposals(cfg) == []                                   # no check covers the derive: no proposal opened
    s = Store.open(cfg.db_path)
    split.run(s, cfg, checked_at=None, now=NOW)                    # one open from an earlier, covered run
    s.close()
    before = _proposals(cfg)
    assert len(before) == 1
    _brokerage_cash(cfg, "2026-02-27", 0.0)                        # placed, but the gate is shut
    (step,) = sync.run_sync(cfg, now=NOW, sleep=lambda s: None, only=["engine"]).steps
    assert step.status == "skipped" and _proposals(cfg) == before   # neither closed nor replaced


def _covered_derive(cfg) -> None:
    """One scored stock and a full check run covering the newest derive, so the engine's gate is open."""
    from financial_data_collector.derive import valuation as V
    from financial_data_collector.models import PriceBar
    s = Store.open(cfg.db_path)
    s.upsert_security("AAA", asset_type="stock", first_seen="2025-01-01")
    day = datetime(2026, 2, 27)
    bars = []
    while len(bars) < 30:
        if day.weekday() < 5:
            bars.append(PriceBar(day.strftime("%Y-%m-%d"), 100.0, 100.0, 0.0, 1.0))
        day -= timedelta(days=1)
    s.write_prices("AAA", bars[::-1], "tiingo", fetched_at=datetime(2026, 2, 28, 12, 0, tzinfo=timezone.utc))
    s.replace_rows("valuation_daily", V.COLUMNS, [tuple(
        {"symbol": "AAA", "date": "2026-02-27", "close": 100.0, "status": "ok"}.get(c) for c in V.COLUMNS)])
    s.log_run("r1", "derive", "ok", 0, "", "2026-03-01T11:00:00Z", "2026-03-01T11:01:00Z", "manual")
    s.check_log_run("c1", started_at="2026-03-01T11:30:00Z", finished_at="2026-03-01T11:30:00Z", derive_run_id="r1",
                    checks_run=9, holds=0, warns=0, error=None)
    s.close()


def test_engine_step_message_names_the_split(project, tmp_path: Path):
    cfg = _with_allocation(project, tmp_path)
    _covered_derive(cfg)
    _brokerage_cash(cfg, "2026-02-27", 500.0)
    (step,) = sync.run_sync(cfg, now=NOW, sleep=lambda s: None, only=["engine"]).steps
    assert step.status == "ok" and "; split: 1 new, 0 replaced, 0 spent; checks: " in step.message, step.message
    (row,) = _proposals(cfg)
    s = Store.open(cfg.db_path, migrate=False)
    p = s.open_proposal("brokerage")
    s.close()
    assert p["checked_at"] == "2026-03-01T11:30:00Z" and p["created_at"] == "2026-03-01T12:00:00Z"
    (step,) = sync.run_sync(cfg, now=NOW, sleep=lambda s: None, only=["engine"]).steps
    assert "; split: 0 new, 0 replaced, 0 spent" in step.message and _proposals(cfg) == [row]
    _brokerage_cash(cfg, "2026-02-28", 100.0)
    (step,) = sync.run_sync(cfg, now=NOW, sleep=lambda s: None, only=["engine"]).steps
    assert "; split: 0 new, 0 replaced, 1 spent" in step.message


def test_engine_step_message_says_why_the_split_was_skipped(project, tmp_path: Path):
    cfg = _with_allocation(project, tmp_path, targets=False)
    _covered_derive(cfg)
    (step,) = sync.run_sync(cfg, now=NOW, sleep=lambda s: None, only=["engine"]).steps
    assert step.status == "ok" and "; split: no targets file; checks: " in step.message, step.message
