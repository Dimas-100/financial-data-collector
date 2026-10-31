"""Raw inputs for drop causes: each company's 8-Ks from SEC's submissions, and the news feed's headlines."""
import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

from financial_data_collector import config as C
from financial_data_collector import ingest, sync
from financial_data_collector.collectors import earnings
from financial_data_collector.models import CurrentReport, Headline
from financial_data_collector.store import Store

FIXTURES = Path(__file__).parent / "fixtures"
EARN = json.loads((FIXTURES / "sec" / "submissions_EARN.json").read_text(encoding="utf-8"))
CIK = "0000999002"
URL = f"https://data.sec.gov/submissions/CIK{CIK}.json"
AGENT = "Sample Person s@example.com"
DAY1 = datetime(2026, 10, 30, 12, 0, tzinfo=timezone.utc)
DAY2 = datetime(2026, 10, 31, 12, 0, tzinfo=timezone.utc)


class _Yahoo:
    def fund_info(self, symbol):
        return None

    def earnings_dates(self, symbol, limit):
        return []

    def estimates(self, symbol):
        return []


@pytest.fixture
def cfg(tmp_path: Path):
    C.init_project(tmp_path)
    c = C.load_config(tmp_path)
    c.sec_user_agent = AGENT
    c.investing_dir = FIXTURES / "investing"
    return c


@pytest.fixture
def store(cfg):
    s = Store.open(cfg.db_path)
    s.upsert_security("SMPL", asset_type="stock", first_seen="2026-01-01")
    s.set_security_cik("SMPL", CIK, "Sample Earnings Co")
    yield s
    s.close()


def _fetch(url, headers):
    assert url == URL
    return json.dumps(EARN).encode()


# ---- 8-Ks -----------------------------------------------------------------------------------------------------------

def test_current_reports_reads_8ks_with_their_items():
    reps = earnings.current_reports(CIK, EARN["filings"]["recent"])
    assert {(r.form, r.items) for r in reps} >= {("8-K", ("2.02", "9.01")), ("8-K", ("5.07",))}
    assert all(r.form in ("8-K", "8-K/A") for r in reps)
    assert all(r.cik == CIK for r in reps)


def test_current_reports_keeps_amendments_and_skips_what_cannot_be_named():
    recent = {
        "form": ["8-K/A", "8-K", "8-K", "10-Q", "8-K", "8-K"],
        "accessionNumber": ["0000999002-26-000001", "26-000003", "0000999002-26-000004", "0000999002-26-000005",
                            "0000999002-26-000006", None],
        "filingDate": ["2026-01-05", "2026-01-07", "not a date", "2026-01-08", "2026-01-09", "2026-01-10"],
        "items": [" 2.02 , ,9.01", "2.02", "2.02", "2.02", "", "2.02"],
    }
    assert earnings.current_reports(CIK, recent) == [
        CurrentReport(CIK, "0000999002-26-000001", "8-K/A", "2026-01-05", ("2.02", "9.01")),
        CurrentReport(CIK, "0000999002-26-000006", "8-K", "2026-01-09", ()),
    ]
    assert earnings.current_reports(CIK, {}) == []


def test_earnings_step_stores_8ks_once(store, cfg):
    def collect(now):
        return earnings.collect_earnings(store, ["SMPL"], cfg, fetch=_fetch, yahoo=_Yahoo(), now=now,
                                         sleep=lambda s: None)
    (first,) = collect(DAY1)
    (second,) = collect(DAY2)
    assert first.reports == 3 and second.reports == 0
    stored = store.current_reports_of(CIK, "2026-01-01", "2026-12-31")
    assert [r.filed for r in stored] == sorted(r.filed for r in stored) and len(stored) == 3
    assert {r.items for r in stored} == {("2.02", "9.01"), ("5.07",)}
    again = earnings.current_reports(CIK, EARN["filings"]["recent"])
    assert store.write_current_reports(again) == 0                  # the same answer written again
    assert [r.filed for r in store.current_reports_of(CIK, "2026-06-01", "2026-07-31")] == ["2026-06-10", "2026-07-31"]


def test_a_filing_is_kept_as_first_stored(store):
    old = CurrentReport(CIK, "0000999002-26-000001", "8-K", "2026-01-05", ("2.02",))
    assert store.write_current_reports([old]) == 1
    assert store.write_current_reports([CurrentReport(CIK, old.accession, "8-K", "2026-01-05", ("5.07",))]) == 0
    assert store.current_reports_of(CIK, "2026-01-01", "2026-01-31") == [old]


def test_the_earnings_step_message_counts_new_8ks(tmp_path):
    C.init_project(tmp_path)
    (tmp_path / "watchlist.txt").write_text("SMPL\n", encoding="utf-8")
    (tmp_path / ".env").write_text(f"SEC_USER_AGENT={AGENT}\n", encoding="utf-8")
    cfg = C.load_config(tmp_path)
    s = Store.open(cfg.db_path)
    s.upsert_security("SMPL", asset_type="stock", first_seen="2026-01-01")
    s.set_security_cik("SMPL", CIK, "Sample Earnings Co")
    s.close()
    rep = sync.run_sync(cfg, only=["earnings"], fetch=_fetch, now=DAY1, yahoo=_Yahoo(), sleep=lambda s: None)
    step = rep.steps[0]
    assert ", 3 8-Ks new" in step.message and step.rows >= 3


# ---- headlines ------------------------------------------------------------------------------------------------------

def test_read_news_keeps_headline_outlet_date_and_a_safe_link(fixtures):
    rows, note = ingest.read_news(fixtures / "investing" / "data" / "news.json")
    assert note is None and [r.symbol for r in rows] == ["AAPL", "AAPL"]
    assert [r.url for r in rows].count(None) == 1           # the javascript: link is dropped
    assert rows[0] == Headline("AAPL", "2026-10-27", "Sample Phone Maker opens a new campus", "Sample Wire",
                               "https://news.example.com/campus", "2026-10-27T14:30:00+00:00",
                               "2026-10-28T12:00:00+00:00")
    assert not hasattr(rows[0], "description") and not hasattr(rows[0], "tags")


def test_read_news_skips_items_without_a_date_or_title_and_canonicalises(tmp_path):
    path = tmp_path / "news.json"
    path.write_text(json.dumps({"as_of": "2026-10-28", "by_ticker": {
        "brk-b": [{"date": "2026-10-27", "title": "Kept"}, {"title": "no date"}, {"date": "2026-10-27"},
                  "not an item", {"date": "2026-10-27", "title": ""}],
        "BAD": "not a list"}}), encoding="utf-8")
    rows, note = ingest.read_news(path)
    assert note is None and [(r.symbol, r.title, r.outlet, r.url, r.published) for r in rows] == [
        ("BRK.B", "Kept", None, None, None)]


def test_read_news_of_a_missing_file_is_quiet(tmp_path):
    assert ingest.read_news(tmp_path / "news.json") == ([], None)


def test_ingest_news_is_idempotent(store, cfg):
    assert ingest.ingest_news(store, cfg) == (2, None)
    before = [tuple(r) for r in store.query("SELECT * FROM news_headlines ORDER BY date")]
    assert len(before) == 2 and {r["feed_as_of"] for r in store.query("SELECT * FROM news_headlines")} == {
        "2026-10-28T12:00:00+00:00"}
    assert ingest.ingest_news(store, cfg) == (0, None)
    assert [tuple(r) for r in store.query("SELECT * FROM news_headlines ORDER BY date")] == before
    columns = [c[1] for c in store.query("PRAGMA table_info(news_headlines)")]
    assert "description" not in columns and "tags" not in columns
    got = store.headlines_of("AAPL", "2026-10-28", "2026-10-28")
    assert [h.title for h in got] == ["Sample Phone Maker trims its outlook"] and got[0].url is None
    assert len(store.headlines_of("AAPL", "2026-10-01", "2026-10-31")) == 2


def test_missing_news_feed_is_quiet(store, cfg, tmp_path):
    cfg.investing_dir = None
    assert ingest.ingest_news(store, cfg) == (0, None)
    cfg.investing_dir = tmp_path / "nowhere"
    assert ingest.ingest_news(store, cfg) == (0, None)


def test_unreadable_news_feed_is_a_note_not_an_error(store, cfg, tmp_path):
    (tmp_path / "inv" / "data").mkdir(parents=True)
    (tmp_path / "inv" / "data" / "news.json").write_text("{not json", encoding="utf-8")
    cfg.investing_dir = tmp_path / "inv"
    rows, note = ingest.ingest_news(store, cfg)
    assert rows == 0 and note and "news.json" in note
    assert store.counts()["news_headlines"] == 0


def test_ingest_step_message_counts_headlines(tmp_path):
    C.init_project(tmp_path)
    cfg = C.load_config(tmp_path)
    cfg.investing_dir = FIXTURES / "investing"
    rep = sync.run_sync(cfg, only=["ingest"], fetch=_fetch, now=DAY1, yahoo=_Yahoo(), sleep=lambda s: None)
    step = rep.steps[0]
    assert step.status == "ok" and "2 headlines new" in step.message
    again = sync.run_sync(cfg, only=["ingest"], fetch=_fetch, now=DAY2, yahoo=_Yahoo(), sleep=lambda s: None)
    assert "0 headlines new" in again.steps[0].message and again.steps[0].rows == 0


def test_no_news_feed_leaves_the_ingest_message_alone(tmp_path):
    C.init_project(tmp_path)
    cfg = C.load_config(tmp_path)
    cfg.investing_dir = tmp_path / "nowhere"
    rep = sync.run_sync(cfg, only=["ingest"], fetch=_fetch, now=DAY1, yahoo=_Yahoo(), sleep=lambda s: None)
    assert "headlines" not in rep.steps[0].message
