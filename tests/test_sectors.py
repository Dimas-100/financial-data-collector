"""Sectors: the sibling feed's, else the SEC industry code's, each with the words that say where it came from."""
import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

from financial_data_collector import config as C
from financial_data_collector import sectors
from financial_data_collector.store import Store

FEED_SOURCE = "feed: classifications.json as of 2026-10-27"


@pytest.mark.parametrize("sic, want", [(3571, "Technology"), (2834, "Health Care"), (6798, "Real Estate"),
    (6022, "Financials"), (1311, "Energy"), (3634, "Consumer Discretionary"), (5912, "Consumer Staples"),
    (7372, "Technology"), (9999, None), (None, None)])
def test_sector_for_sic(sic, want):
    assert sectors.sector_for_sic(sic, sectors.load_sic_ranges()) == want


def test_no_sic_code_has_two_equally_narrow_ranges():
    ranges = sectors.load_sic_ranges()
    for code in range(100, 10000):
        spans = sorted(hi - lo for lo, hi, _ in ranges if lo <= code <= hi)
        assert len(spans) < 2 or spans[0] != spans[1], code


def test_every_seed_sector_is_a_known_one_and_has_a_default_etf():
    ranges = sectors.load_sic_ranges()
    assert ranges
    for lo, hi, sector in ranges:
        assert lo <= hi
        assert sector in sectors.SECTORS and sector in sectors.DEFAULT_SECTOR_ETFS


def test_an_unknown_sector_in_the_seed_is_refused(monkeypatch, tmp_path):
    bad = tmp_path / "sic_sector.csv"
    bad.write_text("sic_from,sic_to,sector,what\n100,199,Gadgets,odd\n", encoding="utf-8")
    monkeypatch.setattr(sectors._migrate, "SEEDS_DIR", tmp_path)
    with pytest.raises(ValueError, match="Gadgets"):
        sectors.load_sic_ranges()


def test_feed_maps_yahoo_names_and_skips_unknown_ones(fixtures):
    found, as_of = sectors.read_feed(fixtures / "investing" / "data" / "classifications.json")
    assert found == {"AAPL": "Technology", "JPM": "Financials", "BRK.B": "Financials"}
    assert as_of == "2026-10-27T19:03:48+00:00"


@pytest.mark.parametrize("content", [None, "not json", "[1, 2]", '{"by_ticker": [1]}'])
def test_missing_or_unreadable_feed_is_empty(tmp_path, content):
    path = tmp_path / "classifications.json"
    if content is not None:
        path.write_text(content, encoding="utf-8")
    assert sectors.read_feed(path) == ({}, None)


@pytest.fixture
def cfg(tmp_path: Path, fixtures: Path):
    C.init_project(tmp_path)
    c = C.load_config(tmp_path)
    c.investing_dir = fixtures / "investing"
    return c


@pytest.fixture
def store(cfg):
    s = Store.open(cfg.db_path)
    yield s
    s.close()


def _sector(store, symbol):
    r = store.query("SELECT sector, sector_source FROM securities WHERE symbol = ?", (symbol,))[0]
    return r["sector"], r["sector_source"]


def test_feed_wins_over_sic_and_says_where_it_came_from(store, cfg):
    store.upsert_security("AAPL", asset_type="stock")
    store.set_sic("AAPL", 6022)
    assert sectors.assign_sectors(store, cfg) == 1
    assert _sector(store, "AAPL") == ("Technology", FEED_SOURCE)


def test_the_feeds_dotted_symbol_matches_a_canonical_one(store, cfg):
    store.upsert_security("BRK.B", asset_type="stock")
    sectors.assign_sectors(store, cfg)
    assert _sector(store, "BRK.B") == ("Financials", FEED_SOURCE)


def test_sic_is_the_fallback_and_says_so(store, cfg):
    store.upsert_security("SMPL", asset_type="stock")
    store.set_sic("SMPL", 2834)
    assert sectors.assign_sectors(store, cfg) == 1
    assert _sector(store, "SMPL") == ("Health Care", "sec: SIC 2834")


def test_a_foreign_filer_gets_a_sector_too(store, cfg):
    store.upsert_security("FORN", asset_type="foreign_filer")
    store.set_sic("FORN", 3571)
    sectors.assign_sectors(store, cfg)
    assert _sector(store, "FORN") == ("Technology", "sec: SIC 3571")


def test_no_feed_and_no_sic_leaves_both_empty(store, cfg):
    store.upsert_security("NONE", asset_type="stock")
    store.set_sic("NONE", 9999)
    assert sectors.assign_sectors(store, cfg) == 0
    assert _sector(store, "NONE") == (None, None)


def test_a_fund_gets_no_sector(store, cfg):
    store.upsert_security("AAPL", asset_type="etf")        # in the feed, but typed a fund
    store.upsert_security("FUND", asset_type="mutual_fund")
    store.set_sic("FUND", 3571)
    assert sectors.assign_sectors(store, cfg) == 0
    assert _sector(store, "AAPL") == (None, None) and _sector(store, "FUND") == (None, None)


def test_a_company_retyped_as_a_fund_loses_its_sector(store, cfg):
    store.upsert_security("AAPL", asset_type="stock")
    sectors.assign_sectors(store, cfg)
    store.set_asset_type("AAPL", "etf")
    assert sectors.assign_sectors(store, cfg) == 1
    assert _sector(store, "AAPL") == (None, None)


def test_assign_twice_changes_nothing_the_second_time(store, cfg):
    store.upsert_security("AAPL", asset_type="stock")
    store.upsert_security("SMPL", asset_type="stock")
    store.set_sic("SMPL", 2834)
    assert sectors.assign_sectors(store, cfg) == 2
    assert sectors.assign_sectors(store, cfg) == 0


def test_missing_feed_file_falls_back_to_sic(store, cfg, tmp_path):
    cfg.investing_dir = tmp_path / "nowhere"
    store.upsert_security("AAPL", asset_type="stock")
    store.set_sic("AAPL", 3571)
    sectors.assign_sectors(store, cfg)
    assert _sector(store, "AAPL") == ("Technology", "sec: SIC 3571")
    cfg.investing_dir = None
    store.set_sic("AAPL", 2834)
    assert sectors.assign_sectors(store, cfg) == 1
    assert _sector(store, "AAPL") == ("Health Care", "sec: SIC 2834")


def test_a_feed_without_a_date_still_names_itself(store, cfg, tmp_path):
    (tmp_path / "data").mkdir(exist_ok=True)
    (tmp_path / "data" / "classifications.json").write_text(
        json.dumps({"by_ticker": {"AAPL": {"sector": "Technology"}}}), encoding="utf-8")
    cfg.investing_dir = tmp_path
    store.upsert_security("AAPL", asset_type="stock")
    sectors.assign_sectors(store, cfg)
    assert _sector(store, "AAPL") == ("Technology", "feed: classifications.json")


class _NoYahoo:
    def earnings_dates(self, symbol, limit):
        return []

    def estimates(self, symbol):
        return []


def _run_earnings_step(store, cfg):
    from financial_data_collector import sync
    return sync._step_earnings(store, cfg, fetch=None, now=datetime(2026, 10, 30, 12, tzinfo=timezone.utc),
                               yahoo=_NoYahoo(), sleep=lambda s: None, progress=lambda d: None)


def test_the_earnings_step_reports_sectors_changed_and_counts_them(store, cfg):
    store.upsert_security("AAPL", asset_type="stock")
    rows, msg, status = _run_earnings_step(store, cfg)
    assert "1 companies asked, 0 events changed, 0 estimate rows written, 1 sectors changed" in msg
    assert rows == 1
    assert _sector(store, "AAPL")[0] == "Technology"


def test_the_sector_pass_runs_even_when_no_company_was_asked(store, cfg):
    store.upsert_security("AAPL", asset_type="stock")
    store.mark_earnings_fetch("AAPL", "2026-10-30")          # asked today already
    rows, msg, status = _run_earnings_step(store, cfg)
    assert "0 companies asked" in msg and msg.endswith(", 1 sectors changed") and rows == 1
    assert status == "skipped"
    assert _sector(store, "AAPL")[0] == "Technology"
