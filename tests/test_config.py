from pathlib import Path

import pytest

from financial_data_collector import config as C


def test_init_project_creates_layout(tmp_path: Path):
    created = C.init_project(tmp_path)
    for rel in ("config.toml", ".env", "data", "data/backups", "data/cache", "inbox", "inbox/processed"):
        assert (tmp_path / rel).exists(), rel
    assert "config.toml" in " ".join(created)
    assert C.init_project(tmp_path) == []          # second call creates nothing


def test_load_config_defaults_and_env(tmp_path: Path):
    C.init_project(tmp_path)
    (tmp_path / ".env").write_text("TIINGO_API_TOKEN=abc\nSEC_USER_AGENT=Sample Person sample@example.com\n")
    cfg = C.load_config(tmp_path)
    assert cfg.db_path == tmp_path / "data" / "warehouse.db"
    assert cfg.inbox == tmp_path / "inbox"
    assert cfg.processed_dir == tmp_path / "inbox" / "processed"
    assert cfg.backup_dir == tmp_path / "data" / "backups"
    assert cfg.cache_dir == tmp_path / "data" / "cache"
    assert cfg.lookback_years == 5 and cfg.sec_max_age_hours == 24
    assert cfg.snaptrade_enabled is True
    assert cfg.snaptrade_dir == (tmp_path / ".." / "investing" / "fidelity" / "dashboard-data").resolve()
    assert cfg.tiingo_token == "abc"
    assert cfg.sec_user_agent == "Sample Person sample@example.com"
    assert cfg.classify == {}


def test_load_config_overrides(tmp_path: Path):
    (tmp_path / "config.toml").write_text(
        '[paths]\ndb = "x/y.db"\n[prices]\nlookback_years = 2\n[sec]\nmax_age_hours = 1\n'
        '[sources.snaptrade]\nenabled = false\n[classify]\nVTI = "etf"\n'
    )
    cfg = C.load_config(tmp_path)
    assert cfg.db_path == tmp_path / "x" / "y.db"
    assert cfg.lookback_years == 2 and cfg.sec_max_age_hours == 1
    assert cfg.snaptrade_enabled is False
    assert cfg.classify == {"VTI": "etf"}
    assert cfg.tiingo_token is None and cfg.sec_user_agent is None


def test_load_config_missing_raises(tmp_path: Path):
    with pytest.raises(C.ConfigError):
        C.load_config(tmp_path)


def test_process_env_overrides_dotenv(tmp_path: Path, monkeypatch):
    C.init_project(tmp_path)
    (tmp_path / ".env").write_text("TIINGO_API_TOKEN=fromfile\n")
    monkeypatch.setenv("TIINGO_API_TOKEN", "fromenv")
    assert C.load_config(tmp_path).tiingo_token == "fromenv"


def test_placeholder_user_agent_counts_as_unset(tmp_path: Path):
    C.init_project(tmp_path)                       # .env holds the example placeholder
    assert C.load_config(tmp_path).sec_user_agent is None
