"""The targets file and the fund look-through folder, read the way the sibling reader reads them."""
import json
import math
from pathlib import Path

import pytest

from financial_data_collector.derive import targets as T
from financial_data_collector.models import Cap, Sleeve, TargetEntry, Targets

FIX = Path(__file__).parent / "fixtures" / "investing"
TARGETS = FIX / "config" / "sizing-targets.json"


def _write(tmp_path: Path, doc) -> Path:
    p = tmp_path / "sizing-targets.json"
    p.write_text(json.dumps(doc), encoding="utf-8")
    return p


def _entry(t: Targets, account: str, symbol: str) -> TargetEntry:
    return next(e for e in t.entries if e.account == account and e.symbol == symbol)


def test_reads_the_documented_shape():
    t = T.read_targets(TARGETS)
    assert t is not None and t.problems == ()
    assert t.last_revised == "2026-01-15"
    assert sorted({e.account for e in t.entries}) == ["brokerage", "roth"]      # `planned` and `_comment` are not accounts
    assert len(t.entries) == 6
    voo = _entry(t, "brokerage", "VOO")
    assert (voo.kind, voo.target, voo.rule, voo.raw_rule) == ("pct_of_account", 0.30, "trades", None)
    assert (voo.low, voo.high) == (0.225, 0.375)                                # no min/max: 0.75x and 1.25x
    aapl = _entry(t, "brokerage", "AAPL")
    assert (aapl.low, aapl.high, aapl.rule) == (0.05, 0.15, "contributions")    # its own band
    assert _entry(t, "brokerage", "KO").rule == "hold"
    assert _entry(t, "brokerage", "JNJ").kind == "direct_passive_ratio"
    gld = _entry(t, "roth", "GLD")
    assert gld.kind == "pct_of_brokerage"
    assert t.sleeves == (Sleeve("brokerage", "Defensives", ("KO", "JNJ"), 0.05, 0.20, "contributions"),)
    assert t.caps == (Cap("AAPL", 0.12),)


def test_missing_file_is_none(tmp_path):
    assert T.read_targets(None) is None
    assert T.read_targets(tmp_path / "nope.json") is None


def test_array_or_bad_json_raises_value_error(tmp_path):
    p = tmp_path / "secret-folder-name.json"
    p.write_text("[1, 2]", encoding="utf-8")
    with pytest.raises(ValueError) as e:
        T.read_targets(p)
    assert "secret-folder-name" not in str(e.value) and str(tmp_path) not in str(e.value)
    p.write_text("{not json", encoding="utf-8")
    with pytest.raises(ValueError) as e:
        T.read_targets(p)
    assert str(tmp_path) not in str(e.value)


def test_entry_without_target_or_with_inverted_band_is_skipped_with_a_problem(tmp_path):
    doc = {"brokerage": [
        {"ticker": "KO"},
        {"ticker": "JNJ", "target": "lots"},
        {"ticker": "SPY", "target": float("nan")},
        {"ticker": "AAPL", "target": 0.1, "min": 0.2, "max": 0.1},
        {"ticker": "VOO", "target": 0.1, "type": "pct_of_moon"},
        {"target": 0.1},
        {"ticker": "GLD", "target": 0.05},
    ]}
    t = T.read_targets(_write(tmp_path, doc))
    assert [e.symbol for e in t.entries] == ["GLD"]
    assert len(t.problems) == 6
    for sym in ("KO", "JNJ", "SPY", "AAPL", "VOO"):
        assert any("brokerage" in p and sym in p for p in t.problems), sym
    assert any("inverted band" in p and "AAPL" in p for p in t.problems)


def test_rule_missing_is_trades_and_misspelled_is_unknown(tmp_path):
    doc = {"brokerage": [
        {"ticker": "KO", "target": 0.1},
        {"ticker": "JNJ", "target": 0.1, "rebalance": "Hold"},
        {"ticker": "SPY", "target": 0.1, "rebalance": "contributions"},
    ]}
    t = T.read_targets(_write(tmp_path, doc))
    assert _entry(t, "brokerage", "KO").rule == "trades"
    jnj = _entry(t, "brokerage", "JNJ")
    assert (jnj.rule, jnj.raw_rule) == ("unknown", "Hold")
    assert _entry(t, "brokerage", "SPY").rule == "contributions"
    assert len(t.problems) == 1 and "JNJ" in t.problems[0]


def test_tickers_are_canonical(tmp_path):
    doc = {"brokerage": [{"ticker": "brkb", "target": 0.1}],
           "sleeves": [{"account": "brokerage", "label": "X", "members": ["BRKB", "ko"]}],
           "caps": [{"ticker": "BRKB", "max": 0.1}, {"ticker": "KO"}]}
    t = T.read_targets(_write(tmp_path, doc))
    assert t.entries[0].symbol == "BRK.B"
    assert t.sleeves[0].members == ("BRK.B", "KO")
    assert (t.sleeves[0].low, t.sleeves[0].high, t.sleeves[0].rule) == (None, None, "trades")
    assert t.caps == (Cap("BRK.B", 0.1),)
    assert len(t.problems) == 1 and "KO" in t.problems[0]                       # a cap with no max


def test_file_name_only(tmp_path):
    t = T.read_targets(TARGETS)
    assert t.file_name == "sizing-targets.json"
    assert T.read_targets(_write(tmp_path, {"brokerage": []})).last_revised is None


def test_look_through_reads_weights_as_fractions():
    lt = T.read_look_through(FIX / "fund_holdings")
    assert sorted(lt) == ["VFIAX", "VOO"]                                       # _archive, _OLD.csv and the .json are ignored
    assert lt["VOO"]["AAPL"] == pytest.approx(0.07)
    assert lt["VFIAX"] == {"AAPL": pytest.approx(0.065), "KO": pytest.approx(0.007)}   # BOM read; the non-finite row dropped
    assert all(math.isfinite(w) for f in lt.values() for w in f.values())


def test_look_through_missing_folder_and_empty_file(tmp_path):
    assert T.read_look_through(None) == {}
    assert T.read_look_through(tmp_path / "nope") == {}
    (tmp_path / "EMPTY.csv").write_text("", encoding="utf-8")
    (tmp_path / "ALLBAD.csv").write_text("ticker,name,weight_pct\nAAPL,x,nan\n", encoding="utf-8")
    assert T.read_look_through(tmp_path) == {}
