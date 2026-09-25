import json
import shutil
from datetime import datetime, timezone
from pathlib import Path

import pytest

from financial_data_collector import config as C
from financial_data_collector import sync
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
    rep = sync.run_sync(project, fetch=_fetch(fixtures), now=NOW, yf=lambda s, d: [], sleep=lambda s: None)
    assert [s.step for s in rep.steps] == ["ingest", "prices", "sec"]
    assert all(s.status == "ok" for s in rep.steps), rep.steps
    assert rep.exit_code == 0
    c1 = _counts(project)
    assert c1["position_snapshots"] > 0 and c1["prices"] > 0 and c1["sec_facts"] == 35
    assert c1["financial_line_items"] > 0 and c1["sync_runs"] == 3
    assert "without data" in rep.steps[1].message          # KO, VTI, BRK.B had no bars
    rep2 = sync.run_sync(project, fetch=_fetch(fixtures), now=NOW, yf=lambda s, d: [], sleep=lambda s: None)
    assert rep2.steps[0].rows == 0
    c2 = _counts(project)
    assert {k: v for k, v in c2.items() if k != "sync_runs"} == {k: v for k, v in c1.items() if k != "sync_runs"}
    assert rep2.steps[2].message.startswith("0 fetched")


def test_only_and_skip(project, fixtures: Path):
    rep = sync.run_sync(project, only=["prices"], fetch=_fetch(fixtures), now=NOW, yf=lambda s, d: [], sleep=lambda s: None)
    assert [s.step for s in rep.steps] == ["prices"]
    rep = sync.run_sync(project, skip=["sec"], fetch=_fetch(fixtures), now=NOW, yf=lambda s, d: [], sleep=lambda s: None)
    assert [s.step for s in rep.steps] == ["ingest", "prices"]


def test_dry_run_touches_nothing(project):
    rep = sync.run_sync(project, dry_run=True)
    assert rep.dry_run and [s.step for s in rep.steps] == ["ingest", "prices", "sec"]
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


def test_network_down_exit_code(project):
    def down(url, headers):
        raise HttpError(500, url)
    rep = sync.run_sync(project, fetch=down, now=NOW, yf=lambda s, d: [], sleep=lambda s: None)
    by = {s.step: s for s in rep.steps}
    assert by["ingest"].status == "ok"
    assert by["prices"].status == "error" and by["sec"].status == "error"
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
