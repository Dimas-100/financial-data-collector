from pathlib import Path

from financial_data_collector import cli
from financial_data_collector import sync as sync_mod


def test_init_and_status_fresh(tmp_path: Path, capsys):
    assert cli.main(["--root", str(tmp_path), "init"]) == 0
    out = capsys.readouterr().out
    assert "config.toml" in out and (tmp_path / "data" / "warehouse.db").exists()
    assert cli.main(["--root", str(tmp_path), "status"]) == 0
    out = capsys.readouterr().out
    assert "never" in out and "SEC_USER_AGENT" in out


def test_status_without_init(tmp_path: Path, capsys):
    assert cli.main(["--root", str(tmp_path), "status"]) == 2
    assert "fdc init" in capsys.readouterr().err


def test_import_and_query(tmp_path: Path, fixtures: Path, capsys):
    cli.main(["--root", str(tmp_path), "init"])
    rc = cli.main(["--root", str(tmp_path), "import", str(fixtures / "History_SampleBrokerage_2026.csv"), "--account", "Sample Brokerage"])
    assert rc == 0 and "7 rows" in capsys.readouterr().out
    rc = cli.main(["--root", str(tmp_path), "query", "select type, count(*) n from transactions group by 1 order by 1"])
    out = capsys.readouterr().out
    assert rc == 0 and "buy" in out and "contribution" in out
    rc = cli.main(["--root", str(tmp_path), "query", "--csv", "select count(*) n from transactions"])
    assert rc == 0 and capsys.readouterr().out.strip().splitlines() == ["n", "7"]
    rc = cli.main(["--root", str(tmp_path), "query", "delete from transactions"])
    assert rc == 1 and "not allowed" in capsys.readouterr().err


def test_import_directory_and_unknown(tmp_path: Path, fixtures: Path, capsys):
    import shutil

    cli.main(["--root", str(tmp_path), "init"])
    exports = tmp_path / "exports"
    exports.mkdir()
    shutil.copy(fixtures / "Portfolio_Positions_Jan-15-2026.csv", exports)
    shutil.copy(fixtures / "History_SampleBrokerage_2026.csv", exports / "Accounts_History.csv")   # no label anywhere
    (exports / "notes.csv").write_text("a,b\n1,2\n")
    rc = cli.main(["--root", str(tmp_path), "import", str(exports)])
    out = capsys.readouterr().out
    assert rc == 0
    assert "Portfolio_Positions_Jan-15-2026.csv: fidelity_positions, 4 rows" in out
    assert "Accounts_History.csv: skipped" in out and "--account" in out
    assert "notes.csv: skipped" in out and "unrecognized" in out


def test_sync_delegates_and_prints(tmp_path: Path, monkeypatch, capsys):
    cli.main(["--root", str(tmp_path), "init"])
    captured = {}

    def fake_run_sync(cfg, **kw):
        captured.update(kw)
        return sync_mod.SyncReport("r", [sync_mod.StepResult("prices", "ok", 3, "3 symbols updated")])

    monkeypatch.setattr(cli, "run_sync", fake_run_sync)
    rc = cli.main(["--root", str(tmp_path), "sync", "--only", "prices,sec", "--skip", "sec"])
    out = capsys.readouterr().out
    assert rc == 0 and "prices" in out and "ok" in out
    assert captured["only"] == ["prices", "sec"] and captured["skip"] == ["sec"] and captured["dry_run"] is False


def test_sync_dry_run(tmp_path: Path, capsys):
    cli.main(["--root", str(tmp_path), "init"])
    rc = cli.main(["--root", str(tmp_path), "sync", "--dry-run"])
    out = capsys.readouterr().out
    assert rc == 0 and "ingest" in out and "prices" in out and "sec" in out


def test_export_command_writes_three_files(tmp_path: Path, capsys):
    cli.main(["--root", str(tmp_path), "init"])
    out = tmp_path / "cockpit"
    out.mkdir()
    rc = cli.main(["--root", str(tmp_path), "export", "--dir", str(out)])
    assert rc == 0 and sorted(p.name for p in out.iterdir()) == ["dividends.json", "fundamentals.json", "prices.json"]
    assert "written" in capsys.readouterr().out


def test_status_after_a_sync(tmp_path: Path, capsys):
    cli.main(["--root", str(tmp_path), "init"])
    assert cli.main(["--root", str(tmp_path), "sync", "--only", "derive,export"]) == 0
    capsys.readouterr()
    assert cli.main(["--root", str(tmp_path), "status"]) == 0
    out = capsys.readouterr().out
    assert "derive" in out and "export" in out and "holdings_daily" in out


def test_init_prints_next_steps_for_a_stranger(tmp_path: Path, capsys):
    cli.main(["--root", str(tmp_path), "init"])
    out = capsys.readouterr().out
    assert "Next steps" in out and "SEC_USER_AGENT" in out and "inbox" in out and "fdc sync" in out
    assert "SnapTrade" not in out and "investing" not in out          # owner-only sources are not mentioned on a fresh clone


def test_status_renders_a_table_and_hints(tmp_path: Path, capsys):
    cli.main(["--root", str(tmp_path), "init"])
    capsys.readouterr()
    cli.main(["--root", str(tmp_path), "status"])
    out = capsys.readouterr().out
    assert "step" in out and "status" in out and "never" in out
    assert "hint" in out and "SEC_USER_AGENT" in out


def test_config_error_is_one_line_with_a_hint(tmp_path: Path, capsys):
    (tmp_path / "config.toml").write_text("[paths")   # broken TOML
    assert cli.main(["--root", str(tmp_path), "status"]) == 2
    err = capsys.readouterr().err
    assert "error:" in err and "Traceback" not in err
