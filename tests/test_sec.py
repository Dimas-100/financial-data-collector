import json
import os
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
    os.utime(cache, (NOW.timestamp(), NOW.timestamp()))   # file clock = test clock
    sec_cik.load_cik_map(cache, fetch, "ua", NOW + timedelta(days=6))
    assert len(fetch.calls) == 1                                  # fresh cache, no refetch
    sec_cik.load_cik_map(cache, fetch, "ua", NOW + timedelta(days=8))
    assert len(fetch.calls) == 2                                  # stale cache, refetched
    assert sec_cik.lookup(m, "BRK.B") == ("0000000003", "Sample Holding B")
    assert sec_cik.lookup(m, "VTI") is None


def test_parse_company_facts(fixtures: Path):
    data = json.loads((fixtures / "sec" / "companyfacts_SAMPLE.json").read_text())
    facts = sec_facts.parse_company_facts(data)
    assert len(facts) == 35
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
    assert by["AAPL"].cik == "0000000001" and by["AAPL"].facts == 35
    assert rebuilt == [35]
    row = store.query("SELECT cik, sec_name, asset_type, last_sec_fetch FROM securities WHERE symbol='AAPL'")[0]
    assert tuple(row)[:3] == ("0000000001", "Sample Corp", "stock") and row[3].startswith("2026-03-01")
    assert store.query("SELECT COUNT(*) FROM sec_facts")[0][0] == 35
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
    # a name with "Trust" and no revenue is typed fund before the "no operating facts" rule can say etf
    assert store.query("SELECT asset_type FROM securities WHERE symbol='AAPL'")[0][0] == "fund"


def test_collect_sec_skips_money_market_and_marks_404_as_etf(project, fixtures: Path):
    cfg, store = project
    store.upsert_security("FDRXX", first_seen="2026-01-01")     # money market by symbol list, asset_type unknown
    store.upsert_security("BRK.B", first_seen="2026-01-01")     # in the CIK map, but SEC has no company facts

    def fetch(url, headers):
        if url == sec_cik.CIK_URL:
            return (fixtures / "sec" / "company_tickers.json").read_bytes()
        raise HttpError(404, url)

    results = sec_facts.collect_sec(store, cfg, fetch=fetch, now=NOW, rebuild=lambda f, r: [], sleep=lambda s: None)
    by = {r.symbol: r for r in results}
    assert "FDRXX" not in by
    assert store.query("SELECT asset_type FROM securities WHERE symbol = ?", ("BRK.B",))[0][0] == "etf"
    assert "404" in by["BRK.B"].message and "etf" in by["BRK.B"].message


def test_collect_sec_isolates_a_company_failure(project, fixtures: Path):
    cfg, store = project
    store.upsert_security("KO", asset_type="stock", first_seen="2026-01-01")

    def fetch(url, headers):
        if url == sec_cik.CIK_URL:
            return (fixtures / "sec" / "company_tickers.json").read_bytes()
        if "companyfacts" in url:
            return (fixtures / "sec" / "companyfacts_SAMPLE.json").read_bytes()
        raise HttpError(404, url)

    seen = []

    def rebuild(facts, rules):
        seen.append(len(facts))
        if len(seen) == 1:
            raise ValueError("bad concept map for this filer")
        return []

    results = sec_facts.collect_sec(store, cfg, fetch=fetch, now=NOW, rebuild=rebuild, sleep=lambda s: None)
    by = {r.symbol: r for r in results}
    assert "failed" in by["AAPL"].message and "ValueError" in by["AAPL"].message
    assert by["KO"].facts == 35 and by["KO"].message == ""
    assert store.query("SELECT COUNT(DISTINCT cik) FROM sec_facts")[0][0] == 2     # raw facts kept for both


def test_collect_sec_reports_progress(project, fixtures: Path):
    cfg, store = project
    seen = []
    sec_facts.collect_sec(store, cfg, fetch=_fetcher(fixtures), now=NOW, rebuild=lambda f, r: [], sleep=lambda s: None,
                          progress=seen.append)
    assert seen[0].startswith("1/") and any(s.endswith(" AAPL") for s in seen)


def test_collect_sec_skips_a_parked_symbol(project, fixtures: Path):
    cfg, store = project
    store.upsert_security("BRK.B", first_seen="2026-01-01")     # would be found in the CIK map
    store.park("BRK.B", "no price from any provider, no CIK, not held")

    def fetch(url, headers):
        if url == sec_cik.CIK_URL:
            return (fixtures / "sec" / "company_tickers.json").read_bytes()
        raise HttpError(404, url)

    results = sec_facts.collect_sec(store, cfg, fetch=fetch, now=NOW, rebuild=lambda f, r: [], sleep=lambda s: None)
    assert "BRK.B" not in {r.symbol for r in results}   # never even looked up
    assert store.query("SELECT cik FROM securities WHERE symbol = 'BRK.B'")[0][0] is None


def _load(fixtures: Path, name: str):
    return sec_facts.parse_company_facts(json.loads((fixtures / "sec" / name).read_text()))


def test_ifrs_only_filer_is_a_foreign_filer(fixtures: Path):
    assert sec_facts.classify_filer(_load(fixtures, "companyfacts_IFRS.json"), "Sample Mining SA") == "foreign_filer"


def test_us_gaap_until_2012_then_20f_is_a_foreign_filer(fixtures: Path):
    # LAPSED: us-gaap facts end 2012 (10-K), ifrs-full facts from 2013 on 20-F
    assert sec_facts.classify_filer(_load(fixtures, "companyfacts_LAPSED.json"), "Sample Lapsed") == "foreign_filer"


def test_trust_that_files_10q_is_a_fund(fixtures: Path):
    assert sec_facts.classify_filer(_load(fixtures, "companyfacts_TRUST.json"), "Sample Bitcoin Trust") == "fund"


def test_ordinary_company_is_none(fixtures: Path):
    assert sec_facts.classify_filer(_load(fixtures, "companyfacts_SAMPLE.json"), "Sample Corp") is None


def test_a_bank_called_trust_corp_with_revenue_stays_a_company(fixtures: Path):
    facts = _load(fixtures, "companyfacts_SAMPLE.json")        # US filer with Revenues in the last 2 years
    assert sec_facts.classify_filer(facts, "Sample Trust Corp") is None
    assert sec_facts.classify_filer(facts, "Sample Growth Fund") is None


def test_a_trust_name_whose_revenue_is_old_is_a_fund(fixtures: Path):
    old = [f for f in _load(fixtures, "companyfacts_SAMPLE.json") if f.period_end < "2024-06-01"]
    old += [f for f in _load(fixtures, "companyfacts_TRUST.json")]
    assert sec_facts.classify_filer(old, "Sample Royalty Trust") == "fund"


def test_collect_sec_sets_foreign_filer_and_stops_shaping_it(project, fixtures: Path):
    cfg, store = project
    base = _fetcher(fixtures)
    calls = []

    def fetch(url, headers):
        if url == sec_facts.FACTS_URL.format(cik="0000000001"):
            calls.append(url)
            return (fixtures / "sec" / "companyfacts_IFRS.json").read_bytes()
        return base(url, headers)

    rebuilt = []
    store.replace_line_items("0000000001", [])
    results = sec_facts.collect_sec(store, cfg, fetch=fetch, now=NOW, rebuild=lambda f, r: rebuilt.append(1) or [],
                                    sleep=lambda s: None)
    assert store.query("SELECT asset_type FROM securities WHERE symbol='AAPL'")[0][0] == "foreign_filer"
    assert rebuilt == []
    assert store.query("SELECT COUNT(*) FROM financial_line_items WHERE cik='0000000001'")[0][0] == 0
    assert "foreign_filer" in {r.symbol: r.message for r in results}["AAPL"]
    assert store.query("SELECT last_sec_fetch FROM securities WHERE symbol='AAPL'")[0][0] is not None
    again = sec_facts.collect_sec(store, cfg, fetch=fetch, now=NOW + timedelta(days=30), rebuild=lambda f, r: [],
                                  sleep=lambda s: None)
    assert len(calls) == 1                                       # a foreign filer is skipped from then on
    assert "AAPL" not in {r.symbol for r in again}


def _one_concept_facts(taxonomy: str, concept: str, form: str, filed: str = "2026-03-01"):
    return sec_facts.parse_company_facts({"facts": {taxonomy: {concept: {"units": {"USD": [
        {"start": "2025-01-01", "end": "2025-12-31", "val": 10, "accn": "a", "fy": 2025, "fp": "FY", "form": form,
         "filed": filed}]}}}}})


def test_a_realty_trust_with_only_lease_revenue_stays_a_company():
    facts = _one_concept_facts("us-gaap", "OperatingLeasesIncomeStatementLeaseRevenue", "10-K")
    assert sec_facts.classify_filer(facts, "Sample Realty Trust") is None


def test_a_bancorp_trust_with_only_interest_income_stays_a_company():
    facts = _one_concept_facts("us-gaap", "InterestAndDividendIncomeOperating", "10-K")
    assert sec_facts.classify_filer(facts, "Sample Bancorp Trust") is None


def test_a_revenue_concept_from_the_concept_map_counts():
    facts = _one_concept_facts("us-gaap", "SalesOfOddThings", "10-K")
    assert sec_facts.classify_filer(facts, "Sample Odd Trust") == "fund"
    assert sec_facts.classify_filer(facts, "Sample Odd Trust", ["SalesOfOddThings"]) is None


def test_ifrs_only_operating_facts_on_a_10k_are_a_foreign_filer():
    facts = _one_concept_facts("ifrs-full", "Revenue", "10-K")
    assert sec_facts.classify_filer(facts, "Sample Holdings") == "foreign_filer"


def test_a_malformed_filed_date_gives_no_fund_verdict_and_no_error():
    facts = _one_concept_facts("us-gaap", "NetIncomeLoss", "10-Q", filed="soon")
    assert sec_facts.classify_filer(facts, "Sample Bitcoin Trust") is None


def test_collect_sec_still_types_a_filer_with_no_operating_facts_as_etf(project, fixtures: Path):
    cfg, store = project

    def fetch(url, headers):
        if url == sec_cik.CIK_URL:
            return json.dumps({"0": {"cik_str": 9, "ticker": "AAPL", "title": "Sample Holdings"}}).encode()
        return json.dumps({"cik": 9, "facts": {"dei": {"EntityCommonStockSharesOutstanding": {"units": {"shares": [
            {"end": "2025-12-31", "val": 1, "accn": "x", "fy": 2025, "fp": "FY", "form": "10-K", "filed": "2026-01-01"}]}}}}}).encode()

    results = sec_facts.collect_sec(store, cfg, fetch=fetch, now=NOW, rebuild=lambda f, r: [], sleep=lambda s: None)
    assert store.query("SELECT asset_type FROM securities WHERE symbol='AAPL'")[0][0] == "etf"
    assert "no operating facts; classified as etf" in {r.symbol: r.message for r in results}["AAPL"]
