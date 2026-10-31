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


def test_connections_min_hours(tmp_path):
    from financial_data_collector import config as C
    C.init_project(tmp_path)
    assert C.load_config(tmp_path).connections_min_hours == 6.0
    (tmp_path / "config.toml").write_text("[connections]\nmin_hours = 0.5\n")
    assert C.load_config(tmp_path).connections_min_hours == 0.5
    (tmp_path / "config.toml").write_text("[connections]\nmin_hours = -1\n")
    try:
        C.load_config(tmp_path)
        assert False
    except C.ConfigError as e:
        assert "min_hours" in str(e)


def test_config_reads_share_classes(tmp_path: Path):
    (tmp_path / "config.toml").write_text('[share_classes]\n"xyz.b" = { per = "XYZ.A", ratio = 1500 }\n')
    cfg = C.load_config(tmp_path)
    assert cfg.share_classes["XYZ.B"] == C.ShareClass("XYZ.A", 1500.0)


def test_config_share_classes_default_empty(tmp_path: Path):
    C.init_project(tmp_path)
    assert C.load_config(tmp_path).share_classes == {}


@pytest.mark.parametrize("entry", ['"XYZ.B" = 1500', '"XYZ.B" = { per = "XYZ.A" }', '"XYZ.B" = { per = "XYZ.A", ratio = 0 }',
                                   '"XYZ.B" = { per = "XYZ.A", ratio = "x" }', '"XYZ.B" = { per = "", ratio = 2 }'])
def test_config_rejects_a_bad_share_class(tmp_path: Path, entry: str):
    (tmp_path / "config.toml").write_text(f"[share_classes]\n{entry}\n")
    with pytest.raises(C.ConfigError):
        C.load_config(tmp_path)


def test_check_settings_default_to_the_specs_values(tmp_path: Path):
    C.init_project(tmp_path)
    cfg = C.load_config(tmp_path)
    assert (cfg.check.fresh_warn_months, cfg.check.fresh_hold_months) == (5, 15)
    assert (cfg.check.recompute_tolerance, cfg.check.max_daily_move) == (0.01, 0.40)
    assert (cfg.check.price_warn, cfg.check.price_hold) == (0.005, 0.03)
    assert (cfg.check.figure_warn, cfg.check.figure_hold) == (0.15, 0.40)
    (tmp_path / "config.toml").write_text("")
    assert C.load_config(tmp_path).check == cfg.check


def test_check_settings_can_be_set(tmp_path: Path):
    (tmp_path / "config.toml").write_text(
        "[check]\nfresh_warn_months = 3\nfresh_hold_months = 12\nrecompute_tolerance = 0.02\nmax_daily_move = 0.5\n")
    c = C.load_config(tmp_path).check
    assert (c.fresh_warn_months, c.fresh_hold_months, c.recompute_tolerance, c.max_daily_move) == (3, 12, 0.02, 0.5)


def test_outside_check_thresholds_can_be_set(tmp_path: Path):
    (tmp_path / "config.toml").write_text("[check]\nprice_warn = 0.01\nprice_hold = 0.05\nfigure_warn = 0.2\nfigure_hold = 0.5\n")
    c = C.load_config(tmp_path).check
    assert (c.price_warn, c.price_hold, c.figure_warn, c.figure_hold) == (0.01, 0.05, 0.2, 0.5)


@pytest.mark.parametrize("body", [
    "fresh_warn_months = 0", "fresh_warn_months = 'five'", "fresh_warn_months = 2.5", "fresh_warn_months = true",
    "fresh_warn_months = 20", "fresh_hold_months = -1", "recompute_tolerance = 0", "recompute_tolerance = 'x'",
    "max_daily_move = -0.1", "max_daily_move = false", "typo_months = 5",
    "price_warn = 0", "price_hold = 'x'", "price_warn = 0.5", "figure_warn = 0.9", "figure_hold = -1",
])
def test_a_bad_check_setting_is_a_config_error(tmp_path: Path, body):
    (tmp_path / "config.toml").write_text(f"[check]\n{body}\n")
    with pytest.raises(C.ConfigError, match=r"\[check\]"):
        C.load_config(tmp_path)


def test_check_must_be_a_table(tmp_path: Path):
    (tmp_path / "config.toml").write_text("check = 5\n")
    with pytest.raises(C.ConfigError, match=r"\[check\]"):
        C.load_config(tmp_path)
