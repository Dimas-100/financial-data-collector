"""The console's model: steps, sources, runs and the running state, assembled read-only from a project."""
import shutil
from datetime import datetime, timezone
from pathlib import Path

import pytest

from financial_data_collector import config as C
from financial_data_collector import heartbeat as H
from financial_data_collector import sync
from financial_data_collector.console import model as M
from financial_data_collector.console.schedule import Schedule
from financial_data_collector.store import Store

NOW = datetime(2026, 3, 1, 12, 0, tzinfo=timezone.utc)


@pytest.fixture
def project(tmp_path: Path, fixtures: Path):
    C.init_project(tmp_path)
    (tmp_path / "config.toml").write_text('[sources.snaptrade]\nenabled = true\ndir = "snap"\n')
    (tmp_path / ".env").write_text("SEC_USER_AGENT=Sample Person s@example.com\n")
    shutil.copytree(fixtures / "snaptrade", tmp_path / "snap")
    shutil.copy(fixtures / "Portfolio_Positions_Jan-15-2026.csv", tmp_path / "inbox")
    cfg = C.load_config(tmp_path)
    Store.open(cfg.db_path).close()        # as fdc init does: the database exists and is migrated
    return cfg


def _log(cfg, *rows):
    s = Store.open(cfg.db_path, migrate=False)
    try:
        for r in rows:
            s.log_run(*r)
    finally:
        s.close()


def test_helpers():
    assert M.parse_iso("2026-03-01T11:30:00Z") == datetime(2026, 3, 1, 11, 30, tzinfo=timezone.utc)
    assert M.parse_iso("2026-03-01") == datetime(2026, 3, 1, tzinfo=timezone.utc)
    assert M.parse_iso("") is None and M.parse_iso(None) is None
    assert M.seconds_between("2026-03-01T11:30:00Z", "2026-03-01T11:31:05Z") == 65.0
    assert M.fmt_age(None) == "" and M.fmt_age(0.4) == "<1 h ago" and M.fmt_age(6.0) == "6 h ago"
    assert M.fmt_age(47.9) == "48 h ago" and M.fmt_age(80.0) == "3.3 d ago"
    assert M.fmt_duration(42) == "42 s" and M.fmt_duration(185) == "3 min 05 s" and M.fmt_duration(3725) == "1 h 02 min"
    assert M.local_time("2026-03-01T11:30:00Z", tz=timezone.utc) == "03-01 11:30"
    assert M.local_time("", tz=timezone.utc) == "never" and M.local_time(None) == "never"


def test_overview_fresh_project(project):
    ov = M.load_overview(project, now=NOW, heartbeat_read=lambda p: None)
    assert [s.step for s in ov.steps] == list(sync.STEPS)
    assert all(s.status == "never" and not s.stale and s.now == "" for s in ov.steps)
    assert ov.running.state == "idle" and ov.runs == [] and ov.latest_snapshot is None
    assert ov.schedule is None and ov.schedule_reason == ""
    names = [s.name for s in ov.sources]
    assert names == ["inbox files", "broker export folder", "prices", "SEC EDGAR", "watchlist",
                     "research tickers", "-> cockpit feed files"]
    by = {s.name: s for s in ov.sources}
    assert by["inbox files"].state == "1 file waiting" and by["inbox files"].last == "never"
    assert by["broker export folder"].state == "present"
    assert by["prices"].where == "yfinance" and by["prices"].state == "never"
    assert by["SEC EDGAR"].state == "never"
    assert by["watchlist"].state == "missing"
    assert by["research tickers"].state in ("off", "missing")
    assert by["-> cockpit feed files"].state == "off"
    assert ov.universe_size == 0            # nothing ingested yet, no watchlist: nobody to follow


def test_overview_after_a_sync(project, fixtures: Path, monkeypatch):
    rep = sync.run_sync(project, fetch=lambda url, headers: (_ for _ in ()).throw(Exception("offline")),
                        now=NOW, yf=lambda s, d: [], sleep=lambda s: None, only=["ingest", "derive"], origin="console")
    assert [s.status for s in rep.steps] == ["ok", "ok"]
    ov = M.load_overview(project, now=NOW, heartbeat_read=lambda p: None)
    by = {s.step: s for s in ov.steps}
    assert by["ingest"].status == "ok" and by["ingest"].rows > 0 and by["prices"].status == "never"
    assert ov.latest_snapshot is not None
    assert len(ov.runs) == 1
    run = ov.runs[0]
    assert run.origin == "console" and run.statuses == ("", "ok", "", "", "ok", "") and run.errors == 0
    src = {s.name: s for s in ov.sources}
    assert src["inbox files"].state == "empty" and src["inbox files"].last != "never"
    assert "file" in src["broker export folder"].last
    steps = M.load_run_steps(project, run.run_id)
    assert [s.step for s in steps] == ["ingest", "derive"] and steps[0].rows == by["ingest"].rows


def test_stale_steps_and_running_markers(project):
    _log(project,
         ("r1", "prices", "ok", 5, "", "2026-02-20T00:00:00Z", "2026-02-20T00:00:05Z", "scheduled"),
         ("r1", "sec", "ok", 1, "", "2026-02-20T00:00:05Z", "2026-02-20T00:00:06Z", "scheduled"))
    hb = H.Heartbeat("r2", 4242, "console", "2026-03-01T11:58:00Z", "2026-03-01T11:59:50Z", "prices", "3/5",
                     ["connections", "ingest"])
    ov = M.load_overview(project, now=NOW, heartbeat_read=lambda p: hb, alive=lambda pid: True)
    by = {s.step: s for s in ov.steps}
    assert by["prices"].stale and by["prices"].age_hours > 48 and not by["sec"].stale
    assert by["prices"].now == "running" and by["ingest"].now == "done" and by["derive"].now == ""
    assert ov.running.state == "running" and ov.running.elapsed_seconds == 120.0
    gone = M.load_overview(project, now=NOW, heartbeat_read=lambda p: hb, alive=lambda pid: False)
    assert gone.running.state == "stale" and all(s.now == "" for s in gone.steps)


def test_run_summary_marks_absent_steps():
    rows = [
        {"run_id": "a", "step": "prices", "status": "ok", "rows_written": 3, "message": "",
         "started_at": "2026-03-01T10:00:00Z", "finished_at": "2026-03-01T10:00:30Z", "origin": "manual"},
        {"run_id": "b", "step": "ingest", "status": "ok", "rows_written": 1, "message": "",
         "started_at": "2026-03-01T11:00:00Z", "finished_at": "2026-03-01T11:00:01Z", "origin": "scheduled"},
        {"run_id": "b", "step": "prices", "status": "error", "rows_written": 0, "message": "boom",
         "started_at": "2026-03-01T11:00:01Z", "finished_at": "2026-03-01T11:00:02Z", "origin": "scheduled"},
    ]
    runs = M.summarize_runs(rows)
    assert [r.run_id for r in runs] == ["b", "a"]
    assert runs[0].statuses == ("", "ok", "error", "", "", "") and runs[0].errors == 1 and runs[0].rows == 1
    assert runs[0].seconds == 2.0 and runs[1].statuses == ("", "", "ok", "", "", "")


def test_sources_inbox_missing_and_connections(project):
    shutil.rmtree(project.inbox)
    s = Store.open(project.db_path, migrate=False)
    try:
        s.save_connection("simplefin", "fdc-simplefin", "2026-02-01T00:00:00Z")
        s.mark_connection("simplefin", fetched_at="2026-02-28T00:00:00Z", ok=False, error="HTTP 503")
    finally:
        s.close()
    ov = M.load_overview(project, now=NOW, heartbeat_read=lambda p: None,
                         schedule=Schedule("T", "Ready", None, None, None, ("daily 08:30",), None),
                         schedule_reason="")
    by = {s.name: s for s in ov.sources}
    assert by["inbox files"].state == "missing"
    assert by["connection: simplefin"].state == "error: HTTP 503" and by["connection: simplefin"].where == "0 accounts"
    assert ov.schedule.triggers == ("daily 08:30",)
