import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from financial_data_collector import config as C
from financial_data_collector.collectors import sec_cik, sec_facts
from financial_data_collector.http import HttpError
from financial_data_collector.store import Store

NOW = datetime(2026, 3, 1, tzinfo=timezone.utc)


def _fetcher(fixtures: Path):
    calls = []

    def fetch(url, headers):
        calls.append((url, headers))
        if url == sec_cik.CIK_URL:
            return (fixtures / "sec" / "company_tickers.json").read_bytes()
        if url == sec_facts.FACTS_URL.format(cik="0000000001"):
            return (fixtures / "sec" / "companyfacts_SAMPLE.json").read_bytes()
        raise HttpError(404, url)

    fetch.calls = calls
    return fetch


def test_load_cik_map_caches(fixtures: Path, tmp_path: Path):
    fetch = _fetcher(fixtures)
    cache = tmp_path / "cache" / "company_tickers.json"
    m = sec_cik.load_cik_map(cache, fetch, "Sample Person s@example.com", NOW)
    assert m["AAPL"] == ("0000000001", "Sample Corp") and m["BRK-B"] == ("0000000003", "Sample Holding B")
    assert fetch.calls[0][1]["User-Agent"] == "Sample Person s@example.com"
    assert cache.exists()
    sec_cik.load_cik_map(cache, fetch, "ua", NOW + timedelta(days=6))
    assert len(fetch.calls) == 1                                  # fresh cache, no refetch
    sec_cik.load_cik_map(cache, fetch, "ua", NOW + timedelta(days=8))
    assert len(fetch.calls) == 2                                  # stale cache, refetched
    assert sec_cik.lookup(m, "BRK.B") == ("0000000003", "Sample Holding B")
    assert sec_cik.lookup(m, "VTI") is None


def test_parse_company_facts(fixtures: Path):
    data = json.loads((fixtures / "sec" / "companyfacts_SAMPLE.json").read_text())
    facts = sec_facts.parse_company_facts(data)
    assert len(facts) == 34
    assets = [f for f in facts if f.concept == "Assets"]
    assert all(f.period_start == "" and f.taxonomy == "us-gaap" and f.unit == "USD" for f in assets)
    rev = [f for f in facts if f.concept == "Revenues" and f.frame == "CY2025"][0]
    assert (rev.value, rev.fy, rev.fp, rev.form, rev.filed, rev.accn) == (1100.0, 2025, "FY", "10-K", "2026-02-01", "k25")
    dei = [f for f in facts if f.taxonomy == "dei"]
    assert len(dei) == 1 and dei[0].unit == "shares"
    assert sec_facts.has_operating_facts(facts)
    assert not sec_facts.has_operating_facts(assets)


@pytest.fixture
def project(tmp_path: Path):
    C.init_project(tmp_path)
    (tmp_path / ".env").write_text("SEC_USER_AGENT=Sample Person s@example.com\n")
    cfg = C.load_config(tmp_path)
    store = Store.open(cfg.db_path)
    for sym, kind in (("AAPL", None), ("VTI", "etf"), ("SPAXX", "money_market"), ("ZZZZ", None)):
        store.upsert_security(sym, asset_type=kind, first_seen="2026-01-01")
    yield cfg, store
    store.close()


def test_collect_sec_end_to_end_with_stub_rebuild(project, fixtures: Path):
    cfg, store = project
    fetch = _fetcher(fixtures)
    rebuilt = []

    def rebuild(facts, rules):
        rebuilt.append(len(facts))
        return []

    results = sec_facts.collect_sec(store, cfg, fetch=fetch, now=NOW, rebuild=rebuild, sleep=lambda s: None)
    by = {r.symbol: r for r in results}
    assert by["AAPL"].cik == "0000000001" and by["AAPL"].facts == 34
    assert rebuilt == [34]
    row = store.query("SELECT cik, sec_name, asset_type, last_sec_fetch FROM securities WHERE symbol='AAPL'")[0]
    assert tuple(row)[:3] == ("0000000001", "Sample Corp", "stock") and row[3].startswith("2026-03-01")
    assert store.query("SELECT COUNT(*) FROM sec_facts")[0][0] == 34
    assert store.query("SELECT cik FROM securities WHERE symbol='ZZZZ'")[0][0] is None
    assert "no CIK" in by["ZZZZ"].message
    assert "VTI" not in by and "SPAXX" not in by                  # funds never hit EDGAR
    facts_calls = [u for u, _ in fetch.calls if "companyfacts" in u]
    assert len(facts_calls) == 1
    again = sec_facts.collect_sec(store, cfg, fetch=fetch, now=NOW + timedelta(hours=1), rebuild=rebuild, sleep=lambda s: None)
    assert len([u for u, _ in fetch.calls if "companyfacts" in u]) == 1      # within max_age: no refetch
    assert {r.symbol: r.message for r in again}["AAPL"] == "fresh"


def test_collect_sec_requires_user_agent(project, fixtures: Path):
    cfg, store = project
    cfg.sec_user_agent = None
    with pytest.raises(C.ConfigError):
        sec_facts.collect_sec(store, cfg, fetch=_fetcher(fixtures), now=NOW, rebuild=lambda f, r: [])


def test_collect_sec_flags_fund_with_cik_as_etf(project, fixtures: Path, tmp_path: Path):
    cfg, store = project

    def fetch(url, headers):
        if url == sec_cik.CIK_URL:
            return json.dumps({"0": {"cik_str": 9, "ticker": "AAPL", "title": "Sample Trust"}}).encode()
        return json.dumps({"cik": 9, "facts": {"dei": {"EntityCommonStockSharesOutstanding": {"units": {"shares": [
            {"end": "2025-12-31", "val": 1, "accn": "x", "fy": 2025, "fp": "FY", "form": "N-CSR", "filed": "2026-01-01"}]}}}}}).encode()

    results = sec_facts.collect_sec(store, cfg, fetch=fetch, now=NOW, rebuild=lambda f, r: [], sleep=lambda s: None)
    assert store.query("SELECT asset_type FROM securities WHERE symbol='AAPL'")[0][0] == "etf"
    assert "no operating" in {r.symbol: r.message for r in results}["AAPL"]
