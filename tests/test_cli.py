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
    assert "investing" not in out                                  # the owner-only source is not mentioned on a fresh clone
    assert "fdc connect" in out


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


import base64
import json

import pytest

from financial_data_collector.connections import keys as K


def _root(tmp_path, monkeypatch):
    cli.main(["--root", str(tmp_path), "init"])
    monkeypatch.setattr(cli, "_open", lambda url: True)
    return str(tmp_path)


def test_connect_snaptrade_prompts_hidden_and_lists_accounts(tmp_path, monkeypatch, capsys):
    from tests.test_snaptrade_connection import _fetch
    root = _root(tmp_path, monkeypatch)
    answers = iter([" synthetic-client \n", "synthetic-consumer-key"])
    monkeypatch.setattr(cli, "_ask", lambda prompt: next(answers))
    monkeypatch.setattr(cli.http, "fetch", _fetch([]))
    rc = cli.main(["--root", root, "connect", "snaptrade"])
    out = capsys.readouterr().out
    assert rc == 0
    assert "Example Brokerage Sample Roth IRA" in out and "roth_ira" in out and "guessed" in out
    assert "synthetic-client" not in out and "synthetic-consumer-key" not in out
    assert "fdc sync" in out
    rc = cli.main(["--root", root, "connections"])
    out = capsys.readouterr().out
    assert rc == 0 and "snaptrade" in out and "2" in out and "synthetic" not in out


def test_connect_snaptrade_refused_key_is_one_line(tmp_path, monkeypatch, capsys):
    from financial_data_collector.http import HttpError
    root = _root(tmp_path, monkeypatch)
    monkeypatch.setattr(cli, "_ask", lambda prompt: "x")

    def refused(url, headers):
        raise HttpError(401, url)
    monkeypatch.setattr(cli.http, "fetch", refused)
    assert cli.main(["--root", root, "connect", "snaptrade"]) == 1
    err = capsys.readouterr().err
    assert "error:" in err and "refused the key" in err and "Traceback" not in err


def test_connect_simplefin_and_disconnect(tmp_path, monkeypatch, capsys):
    from tests.test_simplefin import ACCESS, ANSWER
    root = _root(tmp_path, monkeypatch)
    token = base64.b64encode(b"https://bridge.example.org/simplefin/claim/abc").decode()
    monkeypatch.setattr(cli, "_ask", lambda prompt: token)
    monkeypatch.setattr(cli.http, "post", lambda url, headers: (200, ACCESS.encode()))
    monkeypatch.setattr(cli.http, "fetch", lambda url, headers: json.dumps(ANSWER).encode())
    assert cli.main(["--root", root, "connect", "simplefin"]) == 0
    out = capsys.readouterr().out
    assert "Example Bank Everyday Checking" in out and "needs attention" in out and ACCESS not in out
    assert cli.main(["--root", root, "accounts"]) == 0
    out = capsys.readouterr().out
    assert "Example Bank Cash Rewards Visa" in out and "credit_card" in out and "guessed" in out
    assert cli.main(["--root", root, "accounts", "set", "Example Bank Cash Rewards Visa", "--kind", "credit_card",
                     "--limit", "5000", "--rate", "24.9"]) == 0
    assert cli.main(["--root", root, "accounts"]) == 0
    out = capsys.readouterr().out
    assert "5,000" in out and "24.9" in out
    assert cli.main(["--root", root, "accounts", "set", "Nobody", "--kind", "checking"]) == 1
    with pytest.raises(SystemExit) as e:   # argparse itself refuses a kind that isn't one of KINDS
        cli.main(["--root", root, "accounts", "set", "Example Bank Cash Rewards Visa", "--kind", "spaceship"])
    assert e.value.code == 2
    assert cli.main(["--root", root, "accounts", "set", "Example Bank Cash Rewards Visa", "--rate", "150"]) == 2
    capsys.readouterr()
    assert cli.main(["--root", root, "disconnect", "simplefin"]) == 0
    assert cli.main(["--root", root, "disconnect", "simplefin"]) == 1
    assert cli.main(["--root", root, "connections"]) == 0
    assert "no connections" in capsys.readouterr().out


def test_connect_without_a_key_store_says_where_the_key_went(tmp_path, monkeypatch, capsys, no_real_key_store):
    from tests.test_snaptrade_connection import _fetch
    root = _root(tmp_path, monkeypatch)
    no_real_key_store.broken = True
    monkeypatch.setattr(cli, "_ask", lambda prompt: "v")
    monkeypatch.setattr(cli.http, "fetch", _fetch([]))
    assert cli.main(["--root", root, "connect", "snaptrade"]) == 0
    assert "no key store on this computer: the key is in .env instead" in capsys.readouterr().out
    assert "FDC_SNAPTRADE_CLIENT_ID=v" in (tmp_path / ".env").read_text()


def test_connect_with_user_asks_for_four_values(tmp_path, monkeypatch):
    from tests.test_snaptrade_connection import _fetch
    root = _root(tmp_path, monkeypatch)
    asked = []
    monkeypatch.setattr(cli, "_ask", lambda prompt: asked.append(prompt) or "v")
    monkeypatch.setattr(cli.http, "fetch", _fetch([]))
    assert cli.main(["--root", root, "connect", "snaptrade", "--with-user", "--no-browser"]) == 0
    assert len(asked) == 4


def test_connect_redacts_a_services_own_words(tmp_path, monkeypatch, capsys):
    from tests.test_simplefin import ACCESS, ANSWER
    root = _root(tmp_path, monkeypatch)
    token = base64.b64encode(b"https://bridge.example.org/simplefin/claim/abc").decode()
    monkeypatch.setattr(cli, "_ask", lambda prompt: token)
    monkeypatch.setattr(cli.http, "post", lambda url, headers: (200, ACCESS.encode()))
    leaky = dict(ANSWER, errlist=[{"code": "gen.", "msg": f"see {ACCESS} for details"}])
    monkeypatch.setattr(cli.http, "fetch", lambda url, headers: json.dumps(leaky).encode())
    assert cli.main(["--root", root, "connect", "simplefin"]) == 0
    out = capsys.readouterr().out
    assert "p%40ss" not in out and "user%40x" not in out and "for details" in out
