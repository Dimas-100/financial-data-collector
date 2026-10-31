"""The `scores` check: a score with no sources, an unbuilt link, a percentile out of range or a band its percentile
does not give is held; a sound row gives nothing; a bad row is a finding, never an exception."""
import json
import re
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path

import pytest

from financial_data_collector import check, config as C
from financial_data_collector.check import CheckContext
from financial_data_collector.check import engine as check_engine
from financial_data_collector.store import Store

NOW = datetime(2026, 10, 30, 12, 0, tzinfo=timezone.utc)
FILING = {"kind": "filing", "label": "10-Q", "date": "2026-08-01", "ref": "0000999002-26-000011",
          "period": "2026-06-30",
          "url": "https://www.sec.gov/Archives/edgar/data/999002/000099900226000011/0000999002-26-000011-index.htm"}
PRICE = {"kind": "price", "label": "tiingo close", "date": "2026-10-29", "ref": "2026-10-29T21:00:00Z"}


@pytest.fixture
def cfg(tmp_path: Path):
    C.init_project(tmp_path)
    Store.open(tmp_path / "data" / "warehouse.db").close()
    return C.load_config(tmp_path)


@pytest.fixture
def store(cfg):
    s = Store.open(cfg.db_path, migrate=False)
    yield s
    s.close()


def _ctx(store, cfg):
    return CheckContext(store=store, cfg=cfg, fetch=None, outside=None, now=NOW)


def _raw(store, symbol, metric, *, status="ok", sources=None, percentile=None, band=None, value=1.0, d="2026-10-29",
         inputs=""):
    """One valuation_scores_raw row as it is stored; `sources` is the JSON text (a list is dumped)."""
    if sources is None:
        sources = [PRICE]
    if not isinstance(sources, str):
        sources = json.dumps(sources)
    with store.conn:
        store.conn.execute(
            "INSERT OR REPLACE INTO valuation_scores_raw (symbol, date, metric, value, percentile_5y, band, status, "
            "inputs, sources, checked_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
            (symbol, d, metric, value, percentile, band, status, inputs, sources, "2026-10-30T12:05:00Z"))


def test_an_ok_score_with_no_sources_is_held(store, cfg):
    _raw(store, "AAA", "pe", status="ok", sources="[]")
    (f,) = check_engine.scores(_ctx(store, cfg))
    assert (f.check_name, f.symbol, f.field, f.severity, f.date_to) == ("scores", "AAA", "score:pe", "hold", "2026-10-29")
    assert f.date_from == "2026-10-29" and f.message == "no sources"


def test_a_score_that_is_not_ok_may_have_no_sources(store, cfg):
    _raw(store, "AAA", "pe", status="unsupported", sources="[]", value=None)
    assert check_engine.scores(_ctx(store, cfg)) == []


def test_a_filing_url_that_was_not_built_is_held(store, cfg):          # e.g. a url copied from a feed
    urls = ["https://www.sec.gov/cgi-bin/browse-edgar?action=getcompany",
            FILING["url"].replace("https://", "http://"),
            FILING["url"].replace("000099900226000011/", "000099900226000012/"),       # not this source's accession
            None]
    for i, url in enumerate(urls):
        bad = dict(FILING, url=url)
        if url is None:
            del bad["url"]
        _raw(store, f"A{i}", "pe", sources=[PRICE, bad])
    found = check_engine.scores(_ctx(store, cfg))
    assert sorted(f.symbol for f in found) == ["A0", "A1", "A2", "A3"]
    assert {f.message for f in found} == {"a source link was not built"}


def test_a_feed_link_must_be_https(store, cfg):
    feed = {"kind": "feed", "label": "news", "date": None, "ref": None}
    _raw(store, "AAA", "pe", sources=[PRICE, dict(feed, url="http://example.com/a")])
    _raw(store, "BBB", "pe", sources=[PRICE, dict(feed, url="https://example.com/a")])
    assert [f.symbol for f in check_engine.scores(_ctx(store, cfg))] == ["AAA"]


def test_a_band_that_does_not_follow_from_its_percentile_is_held(store, cfg):   # pe 10th percentile with band premium
    _raw(store, "AAA", "pe", percentile=10.0, band="premium")
    (f,) = check_engine.scores(_ctx(store, cfg))
    assert f.message == "band does not follow from its percentile"
    assert f.evidence["percentile_5y"] == 10.0 and f.evidence["band"] == "premium" and f.evidence["expected_band"] == "cheap"


def test_a_band_on_a_metric_that_has_none_is_held(store, cfg):         # off_high_52w with band cheap
    _raw(store, "AAA", "off_high_52w", percentile=10.0, band="cheap")
    (f,) = check_engine.scores(_ctx(store, cfg))
    assert f.field == "score:off_high_52w" and f.evidence["expected_band"] is None


def test_a_band_without_a_percentile_or_on_a_row_that_is_not_ok_is_held(store, cfg):
    _raw(store, "AAA", "pe", band="cheap")
    _raw(store, "BBB", "pe", status="not_enough_history", percentile=10.0, band="cheap")
    assert sorted(f.symbol for f in check_engine.scores(_ctx(store, cfg))) == ["AAA", "BBB"]


def test_an_ok_row_with_a_percentile_and_no_band_is_held_on_a_banded_metric(store, cfg):
    _raw(store, "AAA", "pe", percentile=10.0, band=None)
    _raw(store, "BBB", "implied_growth", percentile=None, band=None)
    assert [f.symbol for f in check_engine.scores(_ctx(store, cfg))] == ["AAA"]


def test_a_percentile_out_of_range_is_held(store, cfg):
    _raw(store, "AAA", "pe", percentile=104.0, band="premium")
    _raw(store, "BBB", "pe", percentile=-1.0, band="cheap")
    found = {f.symbol: f for f in check_engine.scores(_ctx(store, cfg))}
    assert set(found) == {"AAA", "BBB"} and "percentile out of range" in found["AAA"].message


def test_the_band_rule_per_metric_and_edge(store, cfg):
    # 25 and 75 are the edges: strictly under / over. The yield is mirrored: over 75 cheap, under 25 premium.
    rows = [("pe", 24.9, "cheap"), ("pe", 25.0, "usual"), ("pe", 75.0, "usual"), ("pe", 75.1, "premium"),
            ("p_fcf", 90.0, "premium"), ("ps", 5.0, "cheap"), ("ev_ebit", 50.0, "usual"),
            ("dividend_yield", 80.0, "cheap"), ("dividend_yield", 75.0, "usual"), ("dividend_yield", 25.0, "usual"),
            ("dividend_yield", 24.9, "premium"), ("dividend_yield", 50.0, "usual")]
    for i, (metric, p, band) in enumerate(rows):
        _raw(store, f"S{i:02d}", metric, percentile=p, band=band)
    assert check_engine.scores(_ctx(store, cfg)) == []


def test_the_thresholds_come_from_config(store, cfg):
    cfg2 = replace(cfg, engine=replace(cfg.engine, cheap_percentile=10.0, premium_percentile=90.0))
    _raw(store, "AAA", "pe", percentile=20.0, band="usual")
    assert check_engine.scores(_ctx(store, cfg2)) == []
    assert [f.symbol for f in check_engine.scores(_ctx(store, cfg))] == ["AAA"]      # at 25/75 a 20 is cheap


def test_a_sound_row_gives_nothing(store, cfg):
    _raw(store, "AAA", "pe", percentile=60.0, band="usual", sources=[PRICE, FILING])
    _raw(store, "AAA", "off_high_52w")
    _raw(store, "AAA", "ps", status="held", value=None, sources="[]")
    assert check_engine.scores(_ctx(store, cfg)) == []


def test_a_row_that_cannot_be_read_is_a_finding_not_an_exception(store, cfg):
    _raw(store, "A1", "pe", sources="not json {")
    _raw(store, "A2", "pe", sources='{"kind": "price"}')
    _raw(store, "A3", "pe", sources='["text", 4, null]')
    _raw(store, "A4", "pe", sources=json.dumps([{"kind": "filing"}]))
    _raw(store, "A5", "pe", sources=json.dumps([{"kind": "filing", "ref": 5, "url": 7}]))
    found = {f.symbol: f.message for f in check_engine.scores(_ctx(store, cfg))}
    assert found == {"A1": "no sources", "A2": "no sources", "A3": "a source link was not built",
                     "A4": "a source link was not built", "A5": "a source link was not built"}


def test_several_faults_in_one_row_are_one_finding(store, cfg):
    _raw(store, "AAA", "pe", percentile=150.0, band="cheap", sources="[]")
    (f,) = check_engine.scores(_ctx(store, cfg))
    assert "no sources" in f.message and "percentile out of range" in f.message


def test_a_held_score_is_blank_in_the_view(store, cfg):
    _raw(store, "AAA", "pe", percentile=10.0, band="premium", value=12.5)
    _raw(store, "AAA", "ps", percentile=50.0, band="usual", value=3.0)
    rep = check.run_check(cfg, now=NOW, store=store, only=["scores"], fetch=lambda u, h: b"")
    assert (rep.holds, rep.errors) == (1, [])
    assert store.query("SELECT value, status FROM valuation_scores WHERE metric='pe'")[0][:] == (None, "held")
    assert store.query("SELECT value, status FROM valuation_scores WHERE metric='ps'")[0][:] == (3.0, "ok")
    assert store.query("SELECT field FROM check_findings")[0][0] == "score:pe"
    assert store.query("SELECT value FROM valuation_scores_raw WHERE metric='pe'")[0][0] == 12.5     # raw untouched


def test_scores_is_registered_after_the_others_and_is_an_engine_check():
    assert list(check.CHECKS)[-1] == "scores" and check_engine.ENGINE_CHECKS == ("scores",)


def test_the_engine_check_imports_nothing_it_checks():
    lines = Path(check_engine.__file__).read_text(encoding="utf-8").splitlines()
    imports = "\n".join(l for l in lines if l.startswith(("from ", "import ")))
    assert imports and not re.search(r"derive|statements|collectors", imports)
