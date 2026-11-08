"""The targets file and the fund look-through folder, read the way the sibling reader reads them."""
import json
import math
from pathlib import Path

import pytest

from financial_data_collector.derive import targets as T
from financial_data_collector.models import Cap, Sleeve, TargetEntry, Targets
from financial_data_collector.symbols import canonical, exposure_alias

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
    assert (voo.kind, voo.target, voo.rule, voo.raw_rule) == ("pct_of_account", 30.0, "trades", None)
    assert (voo.low, voo.high) == (22.5, 37.5)                                # no min/max: 0.75x and 1.25x of the percent target
    aapl = _entry(t, "brokerage", "AAPL")
    assert (aapl.low, aapl.high, aapl.rule) == (5.0, 15.0, "contributions")    # its own band
    assert _entry(t, "brokerage", "KO").rule == "hold"
    assert _entry(t, "brokerage", "JNJ").kind == "direct_passive_ratio"
    gld = _entry(t, "roth", "GLD")
    assert gld.kind == "pct_of_brokerage"
    assert t.sleeves == (Sleeve("brokerage", "Defensives", ("KO", "JNJ"), 5.0, 20.0, "contributions"),)
    assert t.caps == (Cap("AAPL", 12.0),)


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
        {"ticker": "AAPL", "target": 10.0, "min": 20.0, "max": 10.0},
        {"ticker": "VOO", "target": 10.0, "type": "pct_of_moon"},
        {"target": 10.0},
        {"ticker": "GLD", "target": 5.0},
    ]}
    t = T.read_targets(_write(tmp_path, doc))
    assert [e.symbol for e in t.entries] == ["GLD"]
    assert len(t.problems) == 6
    for sym in ("KO", "JNJ", "SPY", "AAPL", "VOO"):
        assert any("brokerage" in p and sym in p for p in t.problems), sym
    assert any("inverted band" in p and "AAPL" in p for p in t.problems)


def test_rule_missing_is_trades_and_misspelled_is_unknown(tmp_path):
    doc = {"brokerage": [
        {"ticker": "KO", "target": 10.0},
        {"ticker": "JNJ", "target": 10.0, "rebalance": "Hold"},
        {"ticker": "SPY", "target": 10.0, "rebalance": "contributions"},
    ]}
    t = T.read_targets(_write(tmp_path, doc))
    assert _entry(t, "brokerage", "KO").rule == "trades"
    jnj = _entry(t, "brokerage", "JNJ")
    assert (jnj.rule, jnj.raw_rule) == ("unknown", "Hold")
    assert _entry(t, "brokerage", "SPY").rule == "contributions"
    assert len(t.problems) == 1 and "JNJ" in t.problems[0]


def test_tickers_are_canonical(tmp_path):
    doc = {"brokerage": [{"ticker": "brkb", "target": 10.0}],
           "sleeves": [{"account": "brokerage", "label": "X", "members": ["BRKB", "ko"]}],
           "caps": [{"ticker": "BRKB", "max": 10.0}, {"ticker": "KO"}]}
    t = T.read_targets(_write(tmp_path, doc))
    assert t.entries[0].symbol == "BRK.B"
    assert t.sleeves[0].members == ("BRK.B", "KO")
    assert (t.sleeves[0].low, t.sleeves[0].high, t.sleeves[0].rule) == (None, None, "trades")
    assert t.caps == (Cap("BRK.B", 10.0),)
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


def test_a_number_too_large_for_a_float_is_no_number(tmp_path):
    big = "1" + "0" * 400                                          # a 401-digit integer: float() overflows
    p = tmp_path / "sizing-targets.json"
    p.write_text('{"brokerage": [{"ticker": "VOO", "target": ' + big + '}, {"ticker": "KO", "target": 5, "max": '
                 + big + '}], "caps": [{"ticker": "AAPL", "max": ' + big + '}]}', encoding="utf-8")
    t = T.read_targets(p)
    assert [e.symbol for e in t.entries] == ["KO"]
    assert (_entry(t, "brokerage", "KO").high) == 6.25                          # no usable max: the default band
    assert "brokerage/VOO: no finite target" in t.problems and "caps/AAPL: no finite max" in t.problems
    assert T._num(10 ** 400) is None


def test_deeply_nested_json_is_unreadable(tmp_path):
    p = tmp_path / "sizing-targets.json"
    p.write_text('{"brokerage": ' + "[" * 100_000 + "]" * 100_000 + "}", encoding="utf-8")
    with pytest.raises(ValueError) as e:
        T.read_targets(p)
    assert str(tmp_path) not in str(e.value)


# ---------------------------------------------------------------- exposure aliases (the look-through path only)

def test_exposure_alias_joins_share_classes_and_foreign_listings():
    assert exposure_alias("GOOG") == "GOOGL"
    assert exposure_alias("2330.TW") == "TSM"
    assert exposure_alias("AZN.L") == "AZN"


def test_roche_maps_to_its_adr_not_to_roper():
    """ROP.SW is Roche; ROP on the NYSE is Roper Technologies."""
    assert exposure_alias("ROP.SW") == "RHHBY"
    assert exposure_alias("ROP") == "ROP"


def test_listings_without_a_us_line_stay_unmapped():
    assert exposure_alias("005930.KQ") == "005930.KQ"
    assert exposure_alias("000660.KQ") == "000660.KQ"


def test_canonical_is_not_changed_by_the_exposure_map():
    """Identity is not exposure: a real GOOG position must stay GOOG in the warehouse."""
    assert canonical("GOOG") == "GOOG"
    assert canonical("BRKB") == "BRK.B"
    assert canonical("AZN.L") == "AZN.L"


def _fxaix(folder: Path) -> Path:
    """FXAIX as the fund publishes it: Alphabet in two share classes, one foreign listing, one unmapped one."""
    (folder / "FXAIX.csv").write_text(
        "ticker,name,weight_pct\n"
        "GOOGL,Alphabet Inc Class A,3.06\n"
        "GOOG,Alphabet Inc Class C,2.45\n", encoding="utf-8")
    return folder


def test_look_through_joins_alphabet_into_one_constituent(tmp_path):
    """FXAIX carries GOOGL and GOOG as separate rows; exposure sums them."""
    out = T.read_look_through(_fxaix(tmp_path))
    assert out["FXAIX"]["GOOGL"] == pytest.approx(0.0551)
    assert "GOOG" not in out["FXAIX"]


def test_look_through_reports_coverage_per_fund(tmp_path):
    """Review Focus 2 — the measured sum, not an assumption of 100."""
    out, coverage = T.read_look_through_with_coverage(_fxaix(tmp_path))
    assert out["FXAIX"]["GOOGL"] == pytest.approx(0.0551)
    assert coverage["FXAIX"] == pytest.approx(5.51)


def test_coverage_over_one_hundred_is_reported_not_clamped(tmp_path):
    """Review Focus 2 — a double-counted file must be visible, not quietly made to fit."""
    (tmp_path / "ODD.csv").write_text("ticker,name,weight_pct\nAAPL,Apple Inc,60\nMSFT,Microsoft Corp,55\n",
                                      encoding="utf-8")
    _out, coverage = T.read_look_through_with_coverage(tmp_path)
    assert coverage["ODD"] == pytest.approx(115.0)


def test_coverage_is_empty_for_a_folder_with_nothing_to_read(tmp_path):
    assert T.read_look_through_with_coverage(None) == ({}, {})
    assert T.read_look_through_with_coverage(tmp_path) == ({}, {})


# ---------------------------------------------------------------- the plan's own names, read but never funded

def test_planned_entries_are_read_as_their_own_list(tmp_path):
    """The owner's 13-name plan is seven live rows and six `planned` ones; the panel's lines need all thirteen."""
    t = T.read_targets(_write(tmp_path, {
        "brokerage": [{"ticker": "SCHD", "type": "pct_of_brokerage", "target": 14.0}],
        "planned": [{"ticker": "V", "type": "pct_of_brokerage", "target": 9.0, "label": "V - 9%", "size": "9%"}]}))
    assert [(e.account, e.symbol, e.target) for e in t.planned] == [("planned", "V", 9.0)]
    assert t.problems == ()


def test_a_planned_name_never_reaches_the_entries_the_split_funds():
    """Global constraint: `planned` stays out of the weekly contribution split. An entry there would send real money
    to a name the owner deliberately withheld it from."""
    t = T.read_targets(TARGETS)
    assert "planned" not in {e.account for e in t.entries}
    assert all(e.account in ("brokerage", "roth") for e in t.entries)


def test_a_planned_entry_that_is_only_prose_is_skipped_without_complaint(tmp_path):
    """`planned` carried reader's notes long before it carried percentages, and one is not a defect."""
    t = T.read_targets(_write(tmp_path, {"brokerage": [], "planned": [
        {"label": "Sample later idea", "size": "~3%"},
        {"ticker": "CB", "type": "pct_of_brokerage", "target": 5.0}]}))
    assert [e.symbol for e in t.planned] == ["CB"] and t.problems == ()


def test_the_fixture_file_keeps_its_prose_only_planned_entry_problem_free():
    assert T.read_targets(TARGETS).problems == ()


# ---------------------------------------------------------------- the funds' published sector maps

def test_fund_sectors_are_read_in_this_repo_s_own_sector_names():
    """fund_category_weights.json publishes Yahoo's names; everything else here speaks sectors.SECTORS."""
    got = T.read_fund_sectors(FIX / "fund_holdings")
    assert got["VOO"] == {"Technology": 60.0, "Consumer Staples": 40.0}


def test_fund_sectors_without_a_file_or_a_usable_map():
    assert T.read_fund_sectors(None) == {}
    assert T.read_fund_sectors(FIX / "nope") == {}
