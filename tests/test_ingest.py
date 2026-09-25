import shutil
from pathlib import Path

import pytest

from financial_data_collector import config as C
from financial_data_collector import ingest
from financial_data_collector.store import Store

POS = "Portfolio_Positions_Jan-15-2026.csv"
HIST = "History_SampleBrokerage_2026.csv"


@pytest.fixture
def project(tmp_path: Path, fixtures: Path):
    C.init_project(tmp_path)
    (tmp_path / "config.toml").write_text(
        '[sources.snaptrade]\nenabled = true\ndir = "snap"\n'
    )
    shutil.copytree(fixtures / "snaptrade", tmp_path / "snap")
    cfg = C.load_config(tmp_path)
    store = Store.open(cfg.db_path)
    yield cfg, store
    store.close()


def test_classify_file(fixtures: Path, tmp_path: Path):
    assert ingest.classify_file(fixtures / POS) == "fidelity_positions"
    assert ingest.classify_file(fixtures / HIST) == "fidelity_history"
    other = tmp_path / "other.csv"
    other.write_text("a,b\n1,2\n")
    assert ingest.classify_file(other) is None


def test_ingest_inbox_routes_and_is_idempotent(project, fixtures: Path):
    cfg, store = project
    shutil.copy(fixtures / POS, cfg.inbox / POS)
    shutil.copy(fixtures / HIST, cfg.inbox / HIST)
    (cfg.inbox / "junk.csv").write_text("a,b\n1,2\n")
    s1 = ingest.ingest_inbox(store, cfg)
    kinds = {Path(r.path).name: (r.kind, r.rows, r.skipped) for r in s1.results}
    assert kinds[POS] == ("fidelity_positions", 4, None)     # 3 positions + 1 cash row
    assert kinds[HIST] == ("fidelity_history", 7, None)
    assert kinds["junk.csv"][0] is None and "unrecognized" in kinds["junk.csv"][2]
    assert s1.rows == 11
    assert (cfg.processed_dir / POS).exists() and (cfg.processed_dir / HIST).exists()
    assert (cfg.inbox / "junk.csv").exists()                   # unrecognized files stay put
    before = store.counts()
    shutil.copy(fixtures / POS, cfg.inbox / POS)               # same bytes again
    s2 = ingest.ingest_inbox(store, cfg)
    assert s2.rows == 0
    assert any("already ingested" in (r.skipped or "") for r in s2.results)
    assert store.counts() == before


def test_history_without_account_is_skipped_with_hint(project, fixtures: Path):
    cfg, store = project
    shutil.copy(fixtures / HIST, cfg.inbox / "Accounts_History.csv")
    s = ingest.ingest_inbox(store, cfg)
    assert s.rows == 0
    assert "--account" in s.results[0].skipped
    assert (cfg.inbox / "Accounts_History.csv").exists()


def test_ingest_file_with_account(project, fixtures: Path):
    cfg, store = project
    r = ingest.ingest_file(store, fixtures / HIST, account="Sample Brokerage")
    assert r.rows == 7 and r.skipped is None
    assert store.query("SELECT label FROM accounts")[0][0] == "Sample Brokerage"


def test_ingest_snaptrade(project):
    cfg, store = project
    s = ingest.ingest_snaptrade(store, cfg)
    assert s.rows > 0
    dates = [r[0] for r in store.query("SELECT DISTINCT as_of_date FROM position_snapshots ORDER BY 1")]
    assert dates == ["2026-01-10", "2026-01-16"]
    assert store.query("SELECT COUNT(*) FROM transactions")[0][0] == 6
    inst = {r[0]: r[1] for r in store.query("SELECT label, institution FROM accounts")}
    assert inst["Webull Sample Cash"] == "webull" and inst["Sample Roth"] == "fidelity"
    again = ingest.ingest_snaptrade(store, cfg)
    assert again.rows == 0


def test_ingest_snaptrade_disabled_or_missing(project, tmp_path: Path):
    cfg, store = project
    cfg.snaptrade_enabled = False
    assert ingest.ingest_snaptrade(store, cfg).rows == 0
    cfg.snaptrade_enabled = True
    cfg.snaptrade_dir = tmp_path / "missing"
    s = ingest.ingest_snaptrade(store, cfg)
    assert s.rows == 0 and any("missing" in m for m in s.messages)
