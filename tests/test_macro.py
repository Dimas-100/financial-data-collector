"""The FRED macro feed: parsing, the upsert, the missing-key skip, and a key that never reaches a message."""
import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

from financial_data_collector import config as C
from financial_data_collector import sync
from financial_data_collector.collectors.macro import SERIES, MacroError, collect_macro
from financial_data_collector.http import HttpError
from financial_data_collector.models import MacroObservation
from financial_data_collector.store import Store

NOW = datetime(2026, 10, 25, 12, 0, tzinfo=timezone.utc)
FRED_OK = {"observations": [{"date": "2026-09-01", "value": "324.5"}, {"date": "2026-10-01", "value": "."}]}


@pytest.fixture
def project(tmp_path: Path):
    C.init_project(tmp_path)
    (tmp_path / "config.toml").write_text("[sources.snaptrade]\nenabled = false\n")
    (tmp_path / ".env").write_text("FRED_API_KEY=abc123secret\n")
    return C.load_config(tmp_path)


def _ok_fetch(seen: list | None = None):
    def fetch(url, headers=None):
        if seen is not None:
            seen.append(url)
        return json.dumps(FRED_OK).encode()
    return fetch


def _rows(cfg):
    s = Store.open(cfg.db_path, migrate=False)
    try:
        return [tuple(r) for r in s.conn.execute(
            "SELECT series_id, obs_date, value, fetched_at FROM macro_observations ORDER BY 1, 2")]
    finally:
        s.close()


def test_collect_parses_observations_and_skips_dots():
    urls = []

    def fetch(url):
        urls.append(url)
        return FRED_OK

    got = collect_macro("k", fetch, start="2023-01-01")
    assert sorted(o.series_id for o in got) == sorted(SERIES)
    assert set(SERIES) == {"CPIAUCSL", "CPILFESL", "DFEDTARL", "DFEDTARU", "DGS10", "UNRATE"}
    assert all(o == MacroObservation(o.series_id, "2026-09-01", 324.5) for o in got)   # the "." row is not stored
    assert len(urls) == 6 and all("observation_start=2023-01-01" in u and "api_key=k" in u for u in urls)
    assert all(u.startswith("https://api.stlouisfed.org/fred/series/observations?series_id=") for u in urls)


def test_write_macro_overwrites_a_revision(tmp_path):
    s = Store.open(tmp_path / "w.db")
    try:
        s.write_macro([MacroObservation("CPIAUCSL", "2026-09-01", 324.5)], "2026-10-25T12:00:00Z")
        s.write_macro([MacroObservation("CPIAUCSL", "2026-09-01", 324.6)], "2026-10-26T12:00:00Z")
        rows = [tuple(r) for r in s.conn.execute("SELECT series_id, obs_date, value, fetched_at FROM macro_observations")]
    finally:
        s.close()
    assert rows == [("CPIAUCSL", "2026-09-01", 324.6, "2026-10-26T12:00:00Z")]


def test_macro_step_skips_without_the_fred_key(project, monkeypatch):
    (project.root / ".env").write_text("")
    monkeypatch.delenv("FRED_API_KEY", raising=False)
    cfg = C.load_config(project.root)
    rep = sync.run_sync(cfg, only=["macro"], fetch=_ok_fetch(), now=NOW)
    (step,) = rep.steps
    assert step.step == "macro" and step.status == "skipped"
    assert step.message.startswith("FRED_API_KEY is not set")
    assert rep.exit_code == 0


def test_fred_error_message_never_holds_the_fred_key(project):
    def boom(url, headers=None):
        raise HttpError(500, url)          # its text repeats the whole address, key included

    rep = sync.run_sync(project, only=["macro"], fetch=boom, now=NOW, sleep=lambda s: None)
    (step,) = rep.steps
    assert step.status == "error" and "abc123secret" not in step.message and "HTTP 500" in step.message

    def boom2(url, headers=None):
        raise RuntimeError(f"connection reset talking to {url}")

    rep = sync.run_sync(project, only=["macro"], fetch=boom2, now=NOW, sleep=lambda s: None)
    assert "abc123secret" not in rep.steps[0].message


def test_collect_macro_error_hides_the_key_and_keeps_the_series_that_worked():
    def fetch(url):
        if "series_id=UNRATE" in url:
            raise HttpError(500, url)
        return FRED_OK

    with pytest.raises(MacroError) as e:
        collect_macro("abc123secret", fetch, start="2023-01-01")
    assert "abc123secret" not in str(e.value) and "UNRATE" in str(e.value)
    assert len(e.value.observations) == 5


def test_a_failed_series_does_not_lose_the_others(project):
    def fetch(url, headers=None):
        if "series_id=UNRATE" in url:
            raise HttpError(500, url)
        return json.dumps(FRED_OK).encode()

    rep = sync.run_sync(project, only=["macro"], fetch=fetch, now=NOW, sleep=lambda s: None)
    assert rep.steps[0].status == "ok" and rep.steps[0].rows == 5 and "UNRATE" in rep.steps[0].message
    assert len(_rows(project)) == 5


def test_rerun_with_nothing_new_changes_no_rows(project):
    first = sync.run_sync(project, only=["macro"], fetch=_ok_fetch(), now=NOW)
    assert first.steps[0].status == "ok" and first.steps[0].rows == 6
    before = _rows(project)
    later = datetime(2026, 10, 26, 12, 0, tzinfo=timezone.utc)
    again = sync.run_sync(project, only=["macro"], fetch=_ok_fetch(), now=later)
    assert again.steps[0].rows == 0 and _rows(project) == before       # fetched_at stays: nothing changed


def test_start_is_three_years_back_when_empty_then_400_days(project):
    urls = []
    sync.run_sync(project, only=["macro"], fetch=_ok_fetch(urls), now=NOW)
    assert all("observation_start=2023-10-25" in u for u in urls) and len(urls) == 6
    urls.clear()
    sync.run_sync(project, only=["macro"], fetch=_ok_fetch(urls), now=NOW)
    assert all("observation_start=2025-09-20" in u for u in urls)      # 400 days before 2026-10-25


def test_macro_runs_right_after_prices_and_is_a_network_step():
    assert sync.STEPS.index("macro") == sync.STEPS.index("prices") + 1
    assert "macro" in sync.NETWORK_STEPS and sync.STALE_HOURS["macro"] == 48
