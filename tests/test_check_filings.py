"""The filings check: a 10-Q, 10-K or 20-F that SEC lists but the warehouse's SEC facts lack holds the company."""
import json
from datetime import date, datetime, timezone
from pathlib import Path

import pytest

from financial_data_collector import check, config as C
from financial_data_collector.check import CheckContext, filings
from financial_data_collector.http import HttpError
from financial_data_collector.models import Fact
from financial_data_collector.store import Store

NOW = datetime(2026, 10, 29, 12, 0, tzinfo=timezone.utc)          # 2026-10-29 in New York
CIK = "0000999001"
SAMPLE = json.loads((Path(__file__).parent / "fixtures" / "sec" / "submissions_SAMPLE.json").read_text())
A = "0000999001-26-00000"                                           # a prefix: A + "1" is a well-formed accession
UA = "Test Suite test@example.com"


@pytest.fixture
def cfg(tmp_path: Path):
    C.init_project(tmp_path)
    Store.open(tmp_path / "data" / "warehouse.db").close()
    c = C.load_config(tmp_path)
    c.sec_user_agent = UA
    return c


@pytest.fixture
def store(cfg):
    s = Store.open(cfg.db_path, migrate=False)
    yield s
    s.close()


def _company(store, symbol="SMPL", cik=CIK, asset_type="stock", fetched="2026-10-28T10:00:00Z"):
    store.upsert_security(symbol, symbol, asset_type)
    store.set_security_cik(symbol, cik, symbol)
    if fetched:
        store.mark_sec_fetch(symbol, fetched)


def _facts(store, *accns, cik=CIK):
    store.write_sec_facts(cik, [
        Fact("us-gaap", "Revenues", "USD", "2026-01-01", "2026-03-31", 10.0, 2026, "Q1", "10-Q", "2026-05-06", a)
        for a in accns])


def _run(store, cfg, answers, now=NOW, slept=None):
    """answers: cik -> json-able, or an Exception to raise. Returns (findings, [(url, headers)])."""
    seen: list[tuple[str, dict]] = []

    def fetch(url, headers=None):
        seen.append((url, headers))
        got = answers[next(c for c in answers if c in url)]
        if isinstance(got, Exception):
            raise got
        return json.dumps(got).encode()

    ctx = CheckContext(store=store, cfg=cfg, fetch=fetch, outside=None, now=now,
                       sleep=(slept.append if slept is not None else (lambda s: None)))
    return check.CHECKS["filings"](ctx), seen


def test_a_10q_filed_four_days_ago_without_facts_holds_from_its_filing_date(store, cfg):
    _company(store)
    _facts(store, "0000999001-26-000030", "0000999001-26-000020", "0000999001-24-000010")
    [f] = _run(store, cfg, {CIK: SAMPLE})[0]
    assert (f.check_name, f.symbol, f.field, f.date_from, f.date_to, f.severity) == \
        ("filings", "SMPL", "*", "2026-10-25", None, "hold")
    assert f.evidence == {"form": "10-Q", "accession": "0000999001-26-000040", "filed": "2026-10-25",
                          "url": "https://www.sec.gov/Archives/edgar/data/999001/000099900126000040/"}
    assert "0000999001-26-000040" in f.message


def test_within_grace_days_nothing(store, cfg):
    _company(store)
    _facts(store, "0000999001-26-000030", "0000999001-26-000020", "0000999001-24-000010")
    assert _run(store, cfg, {CIK: SAMPLE}, now=datetime(2026, 10, 28, 12, 0, tzinfo=timezone.utc))[0] == []   # 3 days
    assert len(_run(store, cfg, {CIK: SAMPLE}, now=datetime(2026, 10, 29, 12, 0, tzinfo=timezone.utc))[0]) == 1  # 4 days


def test_missing_rules_by_hand():
    recent = {"form": ["10-Q", "10-K/A", "20-F", "8-K", "10-Q", "10-K", "10-Q"],
              "accessionNumber": [A + "1", A + "2", A + "3", A + "4", A + "5", A + "6", A + "7"],
              "filingDate": ["2026-10-01", "2026-09-01", "2026-08-01", "2026-10-01", "2026-10-01", "2025-09-01", "2026-10-02"],
              "isXBRL": [1, 1, 1, 1, 0, 1, 1]}
    today = date(2026, 10, 29)
    got = filings.missing(recent, {A + "7"}, today)
    # a, b, c are wanted; d is an 8-K; e has no XBRL; f is 423 days old; g has facts
    assert got == [("10-Q", A + "1", "2026-10-01"), ("10-K/A", A + "2", "2026-09-01"), ("20-F", A + "3", "2026-08-01")]
    assert filings.missing(recent, {A + "7"}, today, window_days=500)[-1] == ("10-K", A + "6", "2025-09-01")
    assert filings.missing({}, set(), today) == []                      # an answer with no filings is no finding


def test_8k_and_proxy_forms_are_ignored(store, cfg):
    _company(store)
    _facts(store, "0000999001-26-000040", "0000999001-26-000030", "0000999001-26-000020", "0000999001-24-000010")
    # the 8-K (-000038) and the proxy (-000035) are not in the facts, and are not wanted
    assert _run(store, cfg, {CIK: SAMPLE})[0] == []


def test_a_filing_past_the_400_day_window_is_ignored(store, cfg):
    _company(store)
    _facts(store, "0000999001-26-000040", "0000999001-26-000030", "0000999001-26-000020")
    # the 2024 10-K is not in the facts but is more than 400 days back
    assert _run(store, cfg, {CIK: SAMPLE})[0] == []


def test_the_hold_clears_once_facts_with_that_accession_exist(store, cfg):
    _company(store)
    _facts(store, "0000999001-26-000030", "0000999001-26-000020")
    found, _ = _run(store, cfg, {CIK: SAMPLE})
    assert [f.evidence["accession"] for f in found] == ["0000999001-26-000040"]
    store.check_record("filings", found, "2026-10-29T12:00:00Z")
    assert len(store.check_open_findings("filings")) == 1
    _facts(store, "0000999001-26-000040")
    found, _ = _run(store, cfg, {CIK: SAMPLE})
    assert found == []
    store.check_record("filings", found, "2026-10-30T12:00:00Z")
    assert store.check_open_findings("filings") == []


def test_only_stock_companies_with_a_cik_and_a_fetch_are_asked(store, cfg):
    _company(store, "SMPL")
    _company(store, "ETFX", "0000999002", asset_type="etf")
    _company(store, "NOFE", "0000999003", fetched=None)
    store.upsert_security("NOCK", "NOCK", "stock")
    _, seen = _run(store, cfg, {CIK: SAMPLE})
    assert [u for u, _ in seen] == ["https://data.sec.gov/submissions/CIK0000999001.json"]


def test_uses_the_sec_user_agent_and_pacing(store, cfg):
    _company(store, "SMPL")
    _company(store, "OTHR", "0000999004")
    slept: list[float] = []
    empty = {"filings": {"recent": {}}}
    _, seen = _run(store, cfg, {CIK: SAMPLE, "0000999004": empty}, slept=slept)
    assert len(seen) == 2 and all(h["User-Agent"] == UA for _, h in seen)
    assert all("Accept-Encoding" in h and "Accept" in h for _, h in seen)
    assert slept == [1.0]                                # between the two companies, not before the first


def test_no_user_agent_is_the_checks_error_and_asks_nobody(store, cfg):
    cfg.sec_user_agent = None
    _company(store)
    with pytest.raises(RuntimeError, match="SEC_USER_AGENT is not set"):
        _run(store, cfg, {CIK: SAMPLE})
    rep = check.run_check(cfg, fetch=lambda u, h=None: pytest.fail("fetched"), now=NOW, only=["filings"], store=store)
    assert rep.errors == ["filings: RuntimeError: SEC_USER_AGENT is not set"] and rep.holds == 0


def test_a_fetch_error_is_an_info_finding_and_the_next_company_still_runs(store, cfg):
    _company(store, "AAAA", "0000999005")
    _company(store, "SMPL")
    boom = HttpError(500, f"https://data.sec.gov/submissions/CIK0000999005.json?key={UA}", "HTTP 500")
    found, seen = _run(store, cfg, {"0000999005": boom, CIK: SAMPLE})
    assert len(seen) == 2
    by = {f.symbol: f for f in found}
    assert by["AAAA"].severity == "info" and by["AAAA"].field == "*" and by["AAAA"].date_from == "2026-10-29"
    assert UA not in by["AAAA"].message and "HTTP 500" in by["AAAA"].message
    assert by["SMPL"].severity == "hold"


def test_a_garbled_answer_is_an_info_finding_too(store, cfg):
    _company(store)
    found, _ = _run(store, cfg, {CIK: ValueError("not json")})
    assert [f.severity for f in found] == ["info"]


def test_the_check_is_registered_and_the_sync_sleep_reaches_it(store, cfg):
    assert "filings" in check.CHECKS
    _company(store, "AAAA", "0000999005")
    _company(store, "SMPL")
    slept: list[float] = []
    rep = check.run_check(cfg, fetch=lambda u, h=None: json.dumps(SAMPLE).encode(), now=NOW, only=["filings"],
                          store=store, sleep=slept.append)
    assert rep.errors == [] and slept == [1.0]


def test_a_10k_and_its_amendment_filed_the_same_day_are_one_finding(store, cfg):
    _company(store)
    answer = {"filings": {"recent": {
        "form": ["10-K/A", "10-K"], "accessionNumber": ["0000999001-26-000051", "0000999001-26-000050"],
        "filingDate": ["2026-09-01", "2026-09-01"], "isXBRL": [1, 1]}}}
    found, _ = _run(store, cfg, {CIK: answer})
    assert [(f.date_from, f.evidence["accession"]) for f in found] == [("2026-09-01", "0000999001-26-000051")]
    store.check_record("filings", found, "2026-10-29T12:00:00Z")      # one row per (symbol, field, date_from)


def test_a_failed_fetch_does_not_lift_the_hold_it_could_not_re_examine(store, cfg):
    _company(store)
    _facts(store, "0000999001-26-000030", "0000999001-26-000020")
    found, _ = _run(store, cfg, {CIK: SAMPLE})
    store.check_record("filings", found, "2026-10-29T12:00:00Z")
    [before] = store.check_open_findings("filings")
    found, _ = _run(store, cfg, {CIK: HttpError(503, "https://data.sec.gov/x", "HTTP 503")},
                    now=datetime(2026, 10, 30, 12, 0, tzinfo=timezone.utc))
    assert sorted(f.severity for f in found) == ["hold", "info"]
    store.check_record("filings", found, "2026-10-30T12:00:00Z")
    rows = {r["severity"]: r for r in store.check_open_findings("filings")}
    assert set(rows) == {"hold", "info"}
    h = rows["hold"]
    assert (h["id"], h["first_seen"], h["date_from"], h["message"], h["evidence"]) == \
        (before["id"], before["first_seen"], before["date_from"], before["message"], before["evidence"])


def test_a_garbled_cik_or_accession_is_that_companys_info_or_nothing_never_a_stop(store, cfg):
    _company(store, "AAAA", "not-a-cik")
    _company(store, "SMPL")
    bad = {"filings": {"recent": {"form": ["10-Q", "10-Q", "10-Q"], "accessionNumber": [None, "x-1", 7],
                                  "filingDate": ["2026-09-01"] * 3, "isXBRL": [1, 1, 1]}}}
    found, _ = _run(store, cfg, {"not-a-cik": bad, CIK: SAMPLE})
    by = {f.symbol: f for f in found}
    assert "AAAA" not in by                                       # no filing nameable: nothing, never a hold
    assert by["SMPL"].severity == "hold"
    assert filings.missing(bad["filings"]["recent"], set(), date(2026, 10, 29)) == []


def test_a_garbled_cik_with_a_real_looking_filing_is_an_info_finding_not_a_stop(store, cfg):
    _company(store, "AAAA", "not-a-cik")
    _company(store, "SMPL")
    found, _ = _run(store, cfg, {"not-a-cik": SAMPLE, CIK: SAMPLE})
    by = {f.symbol: f for f in found}
    assert by["AAAA"].severity == "info" and by["SMPL"].severity == "hold"
