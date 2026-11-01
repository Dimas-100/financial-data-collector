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
    assert (cfg.check.close_warn_days, cfg.check.close_hold_days) == (2, 5)
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


def test_close_day_settings_can_be_set(tmp_path: Path):
    (tmp_path / "config.toml").write_text("[check]\nclose_warn_days = 3\nclose_hold_days = 3\n")
    c = C.load_config(tmp_path).check
    assert (c.close_warn_days, c.close_hold_days) == (3, 3)


def test_close_warn_days_must_not_exceed_close_hold_days(tmp_path: Path):
    (tmp_path / "config.toml").write_text("[check]\nclose_warn_days = 6\n")
    with pytest.raises(C.ConfigError, match=r"\[check\] close_warn_days must not be more than close_hold_days"):
        C.load_config(tmp_path)


@pytest.mark.parametrize("body", [
    "fresh_warn_months = 0", "fresh_warn_months = 'five'", "fresh_warn_months = 2.5", "fresh_warn_months = true",
    "fresh_warn_months = 20", "fresh_hold_months = -1", "recompute_tolerance = 0", "recompute_tolerance = 'x'",
    "max_daily_move = -0.1", "max_daily_move = false", "typo_months = 5",
    "price_warn = 0", "price_hold = 'x'", "price_warn = 0.5", "figure_warn = 0.9", "figure_hold = -1",
    "close_warn_days = 0", "close_hold_days = 'five'", "close_warn_days = 1.5", "close_hold_days = true",
])
def test_a_bad_check_setting_is_a_config_error(tmp_path: Path, body):
    (tmp_path / "config.toml").write_text(f"[check]\n{body}\n")
    with pytest.raises(C.ConfigError, match=r"\[check\]"):
        C.load_config(tmp_path)


def test_check_must_be_a_table(tmp_path: Path):
    (tmp_path / "config.toml").write_text("check = 5\n")
    with pytest.raises(C.ConfigError, match=r"\[check\]"):
        C.load_config(tmp_path)


def _engine_cfg(tmp_path: Path, extra: str = "") -> C.Config:
    C.init_project(tmp_path)
    if extra:
        (tmp_path / "config.toml").write_text(extra, encoding="utf-8")
    return C.load_config(tmp_path)


def test_engine_defaults_are_the_spec_values(tmp_path: Path):
    cfg = _engine_cfg(tmp_path)
    assert (cfg.engine.drop_day, cfg.engine.drop_week, cfg.engine.excess_sd) == (0.04, 0.08, 2.5)
    assert (cfg.engine.cheap_percentile, cfg.engine.premium_percentile, cfg.engine.history_years) == (25.0, 75.0, 5)
    assert (cfg.engine.min_history_days, cfg.engine.moves_days) == (365, 365)
    assert (cfg.engine.discount_rate, cfg.engine.terminal_growth) == (0.09, 0.025)
    assert C.EngineSettings() == cfg.engine


def test_engine_can_be_set(tmp_path: Path):
    cfg = _engine_cfg(tmp_path, "[engine]\nhistory_years = 3\ndrop_day = 0.05\ncheap_percentile = 20\n")
    assert (cfg.engine.history_years, cfg.engine.drop_day, cfg.engine.cheap_percentile) == (3, 0.05, 20.0)


def test_engine_cheap_must_be_below_premium(tmp_path: Path):
    with pytest.raises(C.ConfigError, match="cheap_percentile"):
        _engine_cfg(tmp_path, "[engine]\ncheap_percentile = 80\n")


def test_engine_premium_must_be_below_100(tmp_path: Path):
    with pytest.raises(C.ConfigError, match="premium_percentile"):
        _engine_cfg(tmp_path, "[engine]\npremium_percentile = 100\n")


def test_engine_terminal_growth_must_be_below_the_discount_rate(tmp_path: Path):
    with pytest.raises(C.ConfigError, match="terminal_growth"):
        _engine_cfg(tmp_path, "[engine]\nterminal_growth = 0.09\n")


def test_engine_unknown_setting_is_named(tmp_path: Path):
    with pytest.raises(C.ConfigError, match="no_such_thing"):
        _engine_cfg(tmp_path, "[engine]\nno_such_thing = 1\n")


@pytest.mark.parametrize("line", ["history_years = 0", "min_history_days = 1.5", "moves_days = true",
                                  "drop_day = 0", "excess_sd = -1", "discount_rate = \"x\""])
def test_engine_bad_values_are_refused(tmp_path: Path, line: str):
    with pytest.raises(C.ConfigError, match=line.split(" ")[0]):
        _engine_cfg(tmp_path, f"[engine]\n{line}\n")


@pytest.mark.parametrize("lines, name", [("cheap_percentile = 0.25\npremium_percentile = 0.75", "cheap_percentile"),
                                         ("cheap_percentile = 0.5", "cheap_percentile"),
                                         ("cheap_percentile = 0.2\npremium_percentile = 0.8", "cheap_percentile"),
                                         ("cheap_percentile = 1\npremium_percentile = 0.9", "premium_percentile")])
def test_engine_percentiles_are_percent_not_fractions(tmp_path: Path, lines: str, name: str):       # R17 (M4)
    with pytest.raises(C.ConfigError, match=rf"\[engine\] {name} must be a percentile such as 25, not 0\.25"):
        _engine_cfg(tmp_path, f"[engine]\n{lines}\n")


def test_engine_a_percentile_of_one_is_allowed(tmp_path: Path):
    assert _engine_cfg(tmp_path, "[engine]\ncheap_percentile = 1\n").engine.cheap_percentile == 1.0


@pytest.mark.parametrize("line", ["drop_day = 1", "drop_week = 4", "discount_rate = 9",
                                  "terminal_growth = 1.0\ndiscount_rate = 0.5"])
def test_engine_fractions_must_be_below_one(tmp_path: Path, line: str):                         # R17 (M4)
    name = line.split(" ")[0]
    with pytest.raises(C.ConfigError, match=rf"\[engine\] {name} must be below 1"):
        _engine_cfg(tmp_path, f"[engine]\n{line}\n")


@pytest.mark.parametrize("line", ["excess_sd = inf", "drop_day = nan", "premium_percentile = inf",
                                  "cheap_percentile = nan", "discount_rate = +inf"])
def test_engine_numbers_must_be_finite(tmp_path: Path, line: str):                              # R17 (M4)
    name = line.split(" ")[0]
    with pytest.raises(C.ConfigError, match=rf"\[engine\] {name} must be a finite number above 0"):
        _engine_cfg(tmp_path, f"[engine]\n{line}\n")


def _alloc_cfg(tmp_path: Path, body: str) -> C.Config:
    (tmp_path / "config.toml").write_text(body, encoding="utf-8")
    return C.load_config(tmp_path)


def test_allocation_defaults_are_the_spec_values(tmp_path: Path):
    cfg = _alloc_cfg(tmp_path, "[paths]\n")                 # no [allocation] table: no file is ever read by accident
    a = cfg.allocation
    assert a == C.AllocationSettings()
    assert a.targets is None and a.fund_holdings is None
    assert a.accounts == {}
    assert (a.min_new, a.min_line, a.household_days) == (25.0, 10, 7)
    assert a.tilt == {"cheap": 1.5, "usual": 1.0, "premium": 0.5}
    assert (a.go_from, a.half_from, a.max_wait_weeks) == (7, 4, 4)            # the owner's week-score rules


def test_allocation_can_be_set_and_paths_resolve_against_the_config_folder(tmp_path: Path):
    cfg = _alloc_cfg(tmp_path, '[allocation]\ntargets = "t/x.json"\nfund_holdings = "f"\nmin_new = 50\nmin_line = 5\n'
                               'household_days = 3\ntilt = { cheap = 2, premium = 0.25 }\n'
                               'go_from = 8\nhalf_from = 5\nmax_wait_weeks = 3\n'
                               '[allocation.accounts]\nbrokerage = "Sample Brokerage"\nroth = "Sample Roth"\n')
    a = cfg.allocation
    assert a.targets == (tmp_path / "t" / "x.json").resolve()
    assert a.fund_holdings == (tmp_path / "f").resolve()
    assert (a.min_new, a.min_line, a.household_days) == (50.0, 5, 3)
    assert a.tilt == {"cheap": 2.0, "usual": 1.0, "premium": 0.25}              # unnamed bands keep their default
    assert a.accounts == {"brokerage": "Sample Brokerage", "roth": "Sample Roth"}
    assert (a.go_from, a.half_from, a.max_wait_weeks) == (8, 5, 3)


def test_allocation_unknown_setting_is_named(tmp_path: Path):
    with pytest.raises(C.ConfigError, match=r"\[allocation\] has no setting no_such_thing"):
        _alloc_cfg(tmp_path, "[allocation]\nno_such_thing = 1\n")


@pytest.mark.parametrize("line", [
    "min_new = 0", "min_new = -5", "min_new = 'x'", "min_new = true", "min_new = nan", "min_new = inf",
    "min_line = 0", "min_line = 2.5", "min_line = 'x'", "min_line = true",
    "household_days = 0", "household_days = 1.5", "household_days = false",
    "targets = 5", "targets = ''", "fund_holdings = ['a']",
    "tilt = 5", "tilt = { cheap = 0 }", "tilt = { cheap = -1 }", "tilt = { cheap = 'x' }", "tilt = { cheap = nan }",
    "tilt = { cheap = inf }", "tilt = { risky = 1 }",
    "accounts = 'x'",
    "go_from = 11", "go_from = -1", "go_from = 6.5", "go_from = true", "half_from = 'x'", "half_from = 11",
    "max_wait_weeks = 0", "max_wait_weeks = 53", "max_wait_weeks = 1.5", "go_from = 3",   # 3 is under half_from's 4
])
def test_allocation_bad_values_are_refused(tmp_path: Path, line: str):
    with pytest.raises(C.ConfigError, match=r"\[allocation\]"):
        _alloc_cfg(tmp_path, f"[allocation]\n{line}\n")


def test_allocation_accounts_must_be_strings(tmp_path: Path):
    with pytest.raises(C.ConfigError, match=r"\[allocation\.accounts\]"):
        _alloc_cfg(tmp_path, "[allocation.accounts]\nbrokerage = 5\n")
    with pytest.raises(C.ConfigError, match=r"\[allocation\.accounts\]"):
        _alloc_cfg(tmp_path, '[allocation.accounts]\nbrokerage = ""\n')


def test_the_example_config_parses_and_names_the_sibling_files(tmp_path: Path):
    C.init_project(tmp_path)
    a = C.load_config(tmp_path).allocation
    assert a.targets == (tmp_path / "../investing/config/sizing-targets.json").resolve()
    assert a.fund_holdings == (tmp_path / "../investing/fund_holdings").resolve()
    assert a.accounts == {}
