"""Earnings history: report dates from SEC 8-K item 2.02 filings, EPS and surprise from Yahoo, merged by plain rules;
plus one estimate snapshot per company per New York day."""
import json
import logging
import sys
import types
from datetime import date, datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from financial_data_collector import config as C
from financial_data_collector import sync
from financial_data_collector.collectors import earnings, yahoo
from financial_data_collector.http import HttpError
from financial_data_collector.models import EarningsEvent, Estimate, YahooEarning
from financial_data_collector.store import Store

NY = ZoneInfo("America/New_York")
FIXTURES = Path(__file__).parent / "fixtures"
EARN = json.loads((FIXTURES / "sec" / "submissions_EARN.json").read_text(encoding="utf-8"))
CIK = "0000999002"
URL = f"https://data.sec.gov/submissions/CIK{CIK}.json"
A0 = "0000999002-24-000030"
A1 = "0000999002-26-000031"
A2 = "0000999002-26-000018"
TODAY = date(2026, 10, 30)
NOW = datetime(2026, 10, 30, 12, 0, tzinfo=timezone.utc)            # 08:00 in New York
STAMP = "2026-10-30T12:00:00Z"
AGENT = "Sample Person s@example.com"


def _y(day, hour, minute=0, eps=(1.89, 2.02, 6.74)):
    return YahooEarning(datetime(*day, hour, minute, tzinfo=NY), *eps)


# ---- report dates -------------------------------------------------------------------------------------------------

def test_report_dates_are_8ks_with_item_2_02():
    assert earnings.report_dates(EARN["filings"]["recent"]) == [("2026-07-31", "0000999002-26-000031"),
                                                                ("2026-05-01", "0000999002-26-000018")]


def test_report_dates_read_items_whole_and_skip_what_cannot_be_named():
    recent = {
        "form": ["8-K", "8-K", "8-K", "8-K/A", "8-K", "8-K"],
        "accessionNumber": ["0000999002-26-000001", "0000999002-26-000002", "26-000003", "0000999002-26-000004",
                            "0000999002-26-000005", None],
        "filingDate": ["2026-01-05", "2026-01-06", "2026-01-07", "2026-01-08", "not a date", "2026-01-09"],
        "items": ["12.02", " 9.01 , 2.02", "2.02", "2.02", "2.02", "2.02"],
    }
    assert earnings.report_dates(recent) == [("2026-01-06", "0000999002-26-000002")]


def test_report_dates_of_an_empty_block():
    assert earnings.report_dates({}) == []


@pytest.mark.parametrize("hm, want", [((0, 0), None), ((8, 0), "before_open"), ((9, 29), "before_open"),
                                      ((9, 30), "during"), ((15, 59), "during"), ((16, 0), "after_close")])
def test_time_of_day(hm, want):
    assert earnings.time_of_day(datetime(2026, 7, 30, *hm, tzinfo=NY)) == want


def test_time_of_day_is_new_york_time():
    assert earnings.time_of_day(datetime(2026, 7, 30, 20, 30, tzinfo=timezone.utc)) == "after_close"   # 16:30 NY
    assert earnings.time_of_day(datetime(2026, 7, 30, 4, 0, tzinfo=timezone.utc)) is None               # 00:00 NY
    assert earnings.time_of_day(datetime(2026, 7, 30, 0, 0, tzinfo=timezone.utc)) == "after_close"      # 20:00 NY


# ---- merge --------------------------------------------------------------------------------------------------------

def test_same_day_8k_takes_yahoo_eps_and_time():
    (e,) = earnings.merge("SMPL", [("2026-07-30", A1)], [_y((2026, 7, 30), 16)], [], TODAY)
    assert (e.report_date, e.accession, e.date_source, e.time_of_day, e.eps_reported, e.surprise_pct) == \
           ("2026-07-30", A1, "sec", "after_close", 2.02, 6.74)
    assert (e.time_source, e.eps_source, e.eps_expected) == ("yahoo", "yahoo", 1.89)


def test_next_morning_8k_is_one_event_not_two():
    (e,) = earnings.merge("SMPL", [("2026-07-31", A1)], [_y((2026, 7, 30), 16)], [], TODAY)
    assert (e.report_date, e.date_source, e.time_of_day, e.eps_reported) == ("2026-07-31", "sec", None, 2.02)
    assert (e.time_source, e.eps_source) == (None, "yahoo")


def test_the_match_window_is_the_yahoo_day_up_to_three_days_later():
    assert earnings.merge("SMPL", [("2026-08-02", A1)], [_y((2026, 7, 30), 16)], [], TODAY)[0].eps_reported == 2.02
    late = earnings.merge("SMPL", [("2026-08-03", A1)], [_y((2026, 7, 30), 16)], [], TODAY)
    assert [(e.report_date, e.date_source, e.eps_reported) for e in late] == \
           [("2026-07-30", "yahoo", 2.02), ("2026-08-03", "sec", None)]
    before = earnings.merge("SMPL", [("2026-07-29", A1)], [_y((2026, 7, 30), 16)], [], TODAY)
    assert [(e.report_date, e.date_source) for e in before] == [("2026-07-29", "sec"), ("2026-07-30", "yahoo")]


def test_the_nearest_yahoo_row_is_taken_and_the_other_is_dropped():
    ys = [_y((2026, 7, 29), 16, eps=(1.0, 1.1, 10.0)), _y((2026, 7, 31), 8, eps=(1.89, 2.02, 6.74))]
    (e,) = earnings.merge("SMPL", [("2026-07-31", A1)], ys, [], TODAY)
    assert (e.eps_reported, e.time_of_day) == (2.02, "before_open")


def test_yahoo_only_and_sec_only_events():
    events = earnings.merge("SMPL", [("2026-05-01", A2)], [_y((2026, 7, 30), 16)], [], TODAY)
    assert [(e.report_date, e.date_source, e.accession, e.eps_reported, e.time_of_day) for e in events] == [
        ("2026-05-01", "sec", A2, None, None),
        ("2026-07-30", "yahoo", None, 2.02, "after_close"),
    ]
    yahoo_only = events[1]
    assert (yahoo_only.time_source, yahoo_only.eps_source) == ("yahoo", "yahoo")


def test_future_yahoo_rows_are_left_out():
    events = earnings.merge("SMPL", [], [_y((2026, 10, 30), 16), _y((2026, 10, 31), 16, eps=(2.1, None, None))], [], TODAY)
    assert [e.report_date for e in events] == ["2026-10-30"]


def test_old_event_keeps_its_date_and_eps_when_both_lists_have_dropped_it():
    old = EarningsEvent("SMPL", "2024-07-31", None, A0, 1.30, 1.40, 7.7, "sec", None, "yahoo")
    events = earnings.merge("SMPL", [], [], [old], TODAY)
    assert events == [old]


def test_a_stored_sec_row_keeps_its_eps_and_time_when_yahoo_has_nothing_near():
    old = EarningsEvent("SMPL", "2026-07-30", "after_close", A1, 1.89, 2.02, 6.74, "sec", "yahoo", "yahoo")
    assert earnings.merge("SMPL", [("2026-07-30", A1)], [], [old], TODAY) == [old]


def test_the_same_accession_once_and_the_new_date_wins():
    old = EarningsEvent("SMPL", "2026-07-30", None, A1, 1.89, 2.02, 6.74, "sec", None, "yahoo")
    (e,) = earnings.merge("SMPL", [("2026-07-31", A1)], [], [old], TODAY)
    assert (e.report_date, e.accession, e.eps_reported) == ("2026-07-31", A1, 2.02)


def test_two_8ks_on_one_day_are_one_event():
    (e,) = earnings.merge("SMPL", [("2026-07-31", "0000999002-26-000032"), ("2026-07-31", A1)], [], [], TODAY)
    assert e.accession == A1                                       # the first filed


def test_a_stored_yahoo_row_is_absorbed_when_its_8k_appears_a_day_later():
    stored = [EarningsEvent("SMPL", "2026-07-30", "after_close", None, 1.89, 2.02, 6.74, "yahoo", "yahoo", "yahoo")]
    (e,) = earnings.merge("SMPL", [("2026-07-31", A1)], [], stored, TODAY)
    assert (e.report_date, e.accession, e.eps_reported) == ("2026-07-31", A1, 2.02)
    assert (e.date_source, e.time_of_day, e.eps_source) == ("sec", None, "yahoo")


def test_a_stored_yahoo_row_on_the_8k_day_gives_its_time_too():
    stored = [EarningsEvent("SMPL", "2026-07-30", "after_close", None, 1.89, 2.02, 6.74, "yahoo", "yahoo", "yahoo")]
    (e,) = earnings.merge("SMPL", [("2026-07-30", A1)], [], stored, TODAY)
    assert (e.accession, e.time_of_day, e.time_source, e.eps_reported) == (A1, "after_close", "yahoo", 2.02)


def test_a_new_yahoo_row_wins_over_a_stored_one_on_the_same_day():
    stored = [EarningsEvent("SMPL", "2026-07-30", "after_close", None, 1.89, None, None, "yahoo", "yahoo", "yahoo")]
    (e,) = earnings.merge("SMPL", [("2026-07-31", A1)], [_y((2026, 7, 30), 16)], stored, TODAY)
    assert (e.report_date, e.eps_reported, e.surprise_pct) == ("2026-07-31", 2.02, 6.74)
    (y,) = earnings.merge("SMPL", [], [_y((2026, 7, 30), 16)], stored, TODAY)
    assert (y.date_source, y.eps_reported) == ("yahoo", 2.02)


def test_a_stored_yahoo_row_is_kept_when_nothing_replaces_it():
    stored = [EarningsEvent("SMPL", "2024-04-30", "after_close", None, 1.0, 1.1, 10.0, "yahoo", "yahoo", "yahoo")]
    events = earnings.merge("SMPL", [("2026-07-30", A1)], [_y((2026, 7, 30), 16)], stored, TODAY)
    assert [e.report_date for e in events] == ["2024-04-30", "2026-07-30"] and events[0] == stored[0]


def test_a_yahoo_row_with_no_figures_names_no_eps_source():
    (e,) = earnings.merge("SMPL", [("2026-07-30", A1)], [_y((2026, 7, 30), 0, eps=(None, None, None))], [], TODAY)
    assert (e.eps_reported, e.eps_source, e.time_of_day, e.time_source) == (None, None, None, None)


def test_merge_is_sorted_by_date():
    sec = [("2026-07-31", A1), ("2026-05-01", A2)]
    ys = [_y((2026, 7, 30), 16), _y((2026, 1, 28), 16), _y((2026, 4, 30), 16)]
    assert [e.report_date for e in earnings.merge("SMPL", sec, ys, [], TODAY)] == ["2026-01-28", "2026-05-01", "2026-07-31"]


# ---- collecting ---------------------------------------------------------------------------------------------------

class _FakeYahoo:
    """Answers from dicts; a value that is an Exception is raised. Records every call."""

    def __init__(self, dates=None, estimates=None):
        self.dates = dates or {}
        self.ests = estimates or {}
        self.calls: list[tuple[str, str]] = []

    def fund_info(self, symbol):
        return None

    def earnings_dates(self, symbol, limit):
        self.calls.append(("dates", symbol))
        assert limit == earnings.YAHOO_LIMIT
        answer = self.dates.get(symbol, [])
        if isinstance(answer, Exception):
            raise answer
        return answer

    def estimates(self, symbol):
        self.calls.append(("estimates", symbol))
        answer = self.ests.get(symbol, [])
        if isinstance(answer, Exception):
            raise answer
        return answer


class _Fetch:
    def __init__(self, answers: dict):
        self.answers = answers
        self.calls: list[tuple[str, dict]] = []

    def __call__(self, url, headers):
        self.calls.append((url, headers))
        answer = self.answers.get(url)
        if isinstance(answer, Exception):
            raise answer
        if answer is None:
            raise HttpError(404, url)
        return answer


SMPL_DATES = [_y((2026, 7, 30), 16), _y((2026, 4, 30), 16, eps=(1.5, 1.6, 6.67)), _y((2026, 11, 5), 16)]
SMPL_ESTIMATES = [Estimate("0q", 2.1, 1.9, 2.3, 30), Estimate("+1q", 2.2, None, None, 28),
                  Estimate("0y", 8.0, 7.5, 8.4, 40), Estimate("+1y", 9.0, float("nan"), 9.9, 38)]


@pytest.fixture
def cfg(tmp_path: Path):
    C.init_project(tmp_path)
    c = C.load_config(tmp_path)
    c.sec_user_agent = None
    return c


@pytest.fixture
def cfg_with_agent(cfg):
    cfg.sec_user_agent = AGENT
    return cfg


@pytest.fixture
def store(cfg):
    s = Store.open(cfg.db_path)
    s.upsert_security("SMPL", asset_type="stock", first_seen="2026-01-01")
    s.set_security_cik("SMPL", CIK, "Sample Earnings Co")
    yield s
    s.close()


def _collect(store, cfg, fetch, yahoo_client, universe=("SMPL",), now=NOW, sleep=lambda s: None):
    return earnings.collect_earnings(store, list(universe), cfg, fetch=fetch, yahoo=yahoo_client, now=now, sleep=sleep)


def _events(store):
    return [tuple(r) for r in store.query("SELECT report_date, date_source, accession, eps_reported, time_of_day "
                                          "FROM earnings_events WHERE symbol = 'SMPL' ORDER BY report_date")]


def _estimates(store):
    return [tuple(r) for r in store.query("SELECT date, period, eps_avg, eps_low, eps_high, analysts, source, fetched_at "
                                          "FROM estimate_snapshots WHERE symbol = 'SMPL' ORDER BY period")]


def _marked(store, symbol="SMPL"):
    return store.query("SELECT last_earnings_fetch FROM securities WHERE symbol = ?", (symbol,))[0][0]


def test_collect_stores_sic_events_and_estimates(store, cfg_with_agent):
    fetch = _Fetch({URL: json.dumps(EARN).encode()})
    (r,) = _collect(store, cfg_with_agent, fetch, _FakeYahoo({"SMPL": SMPL_DATES}, {"SMPL": SMPL_ESTIMATES}))
    assert store.query("SELECT sic FROM securities WHERE symbol = 'SMPL'")[0][0] == 3571
    assert _events(store) == [("2026-05-01", "sec", A2, 1.6, None), ("2026-07-31", "sec", A1, 2.02, None)]
    assert _estimates(store) == [
        ("2026-10-30", "+1q", 2.2, None, None, 28, "yfinance", STAMP),
        ("2026-10-30", "+1y", 9.0, None, 9.9, 38, "yfinance", STAMP),
        ("2026-10-30", "0q", 2.1, 1.9, 2.3, 30, "yfinance", STAMP),
        ("2026-10-30", "0y", 8.0, 7.5, 8.4, 40, "yfinance", STAMP),
    ]
    assert (r.symbol, r.events, r.estimates, r.failed, r.message) == ("SMPL", 2, 4, False, "")
    assert _marked(store) == "2026-10-30"
    (url, headers), = fetch.calls
    assert url == URL and headers["User-Agent"] == AGENT


def test_without_sec_user_agent_the_yahoo_part_still_runs(store, cfg):
    fetch = _Fetch({URL: json.dumps(EARN).encode()})
    (r,) = _collect(store, cfg, fetch, _FakeYahoo({"SMPL": SMPL_DATES}, {"SMPL": SMPL_ESTIMATES}))
    assert fetch.calls == [] and not r.failed and _marked(store) == "2026-10-30"
    assert _events(store) == [("2026-04-30", "yahoo", None, 1.6, "after_close"),
                              ("2026-07-30", "yahoo", None, 2.02, "after_close")]
    assert len(_estimates(store)) == 4
    assert store.query("SELECT sic FROM securities WHERE symbol = 'SMPL'")[0][0] is None


def test_a_stock_without_a_cik_asks_only_yahoo(store, cfg_with_agent):
    store.upsert_security("NEWCO", asset_type="stock", first_seen="2026-01-01")
    fetch = _Fetch({})
    yahoo_client = _FakeYahoo({"NEWCO": [_y((2026, 7, 30), 16)]})
    (r,) = _collect(store, cfg_with_agent, fetch, yahoo_client, universe=["NEWCO"])
    assert fetch.calls == [] and not r.failed and r.events == 1 and _marked(store, "NEWCO") == "2026-10-30"


def test_one_call_per_symbol_per_new_york_day_and_no_row_change(store, cfg_with_agent):
    fetch = _Fetch({URL: json.dumps(EARN).encode()})
    yahoo_client = _FakeYahoo({"SMPL": SMPL_DATES}, {"SMPL": SMPL_ESTIMATES})
    _collect(store, cfg_with_agent, fetch, yahoo_client)
    before = (_events(store), _estimates(store))
    for hour in (16, 22, 3):                                             # 12:00, 18:00, 23:00 New York
        later = NOW.replace(hour=hour, day=31 if hour == 3 else 30)
        assert _collect(store, cfg_with_agent, fetch, yahoo_client, now=later) == []
    assert len(fetch.calls) == 1 and yahoo_client.calls == [("dates", "SMPL"), ("estimates", "SMPL")]
    assert (_events(store), _estimates(store)) == before


def test_a_same_day_rerun_after_unmarking_changes_no_row(store, cfg_with_agent):
    fetch = _Fetch({URL: json.dumps(EARN).encode()})
    yahoo_client = _FakeYahoo({"SMPL": SMPL_DATES}, {"SMPL": SMPL_ESTIMATES})
    _collect(store, cfg_with_agent, fetch, yahoo_client)
    store.mark_earnings_fetch("SMPL", None)
    (r,) = _collect(store, cfg_with_agent, fetch, yahoo_client, now=NOW.replace(hour=20))
    assert (r.events, r.estimates) == (0, 0)
    assert {row[-1] for row in _estimates(store)} == {STAMP}              # fetched_at untouched


def test_the_next_new_york_day_asks_again_and_adds_a_snapshot(store, cfg_with_agent):
    fetch = _Fetch({URL: json.dumps(EARN).encode()})
    yahoo_client = _FakeYahoo({"SMPL": SMPL_DATES}, {"SMPL": SMPL_ESTIMATES})
    _collect(store, cfg_with_agent, fetch, yahoo_client)
    (r,) = _collect(store, cfg_with_agent, fetch, yahoo_client, now=datetime(2026, 10, 31, 12, tzinfo=timezone.utc))
    assert len(fetch.calls) == 2 and r.events == 0 and r.estimates == 4
    assert sorted({row[0] for row in _estimates(store)}) == ["2026-10-30", "2026-10-31"]


def test_a_failed_sec_fetch_keeps_yahoo_and_is_asked_again(store, cfg_with_agent):
    down = _Fetch({URL: HttpError(503, URL)})
    yahoo_client = _FakeYahoo({"SMPL": SMPL_DATES}, {"SMPL": SMPL_ESTIMATES})
    (r,) = _collect(store, cfg_with_agent, down, yahoo_client)
    assert r.failed and "SEC" in r.message and "503" in r.message and _marked(store) is None
    assert _events(store) == [("2026-04-30", "yahoo", None, 1.6, "after_close"),
                              ("2026-07-30", "yahoo", None, 2.02, "after_close")]
    assert len(_estimates(store)) == 4
    healthy = _Fetch({URL: json.dumps(EARN).encode()})
    (r,) = _collect(store, cfg_with_agent, healthy, yahoo_client, now=NOW.replace(hour=14))
    assert len(healthy.calls) == 1 and not r.failed and _marked(store) == "2026-10-30"
    assert _events(store) == [("2026-05-01", "sec", A2, 1.6, None), ("2026-07-31", "sec", A1, 2.02, None)]


def test_a_failed_yahoo_ask_still_stores_the_sec_dates_and_is_asked_again(store, cfg_with_agent):
    fetch = _Fetch({URL: json.dumps(EARN).encode()})
    (r,) = _collect(store, cfg_with_agent, fetch, _FakeYahoo({"SMPL": OSError("down")}, {"SMPL": OSError("down")}))
    assert r.failed and "OSError" in r.message and _marked(store) is None
    assert _events(store) == [("2026-05-01", "sec", A2, None, None), ("2026-07-31", "sec", A1, None, None)]
    assert _estimates(store) == []


def test_a_failed_write_is_not_marked(store, cfg_with_agent, monkeypatch):
    def broken(*a, **k):
        raise RuntimeError("disk full")
    monkeypatch.setattr(store, "write_estimates", broken)
    fetch = _Fetch({URL: json.dumps(EARN).encode()})
    (r,) = _collect(store, cfg_with_agent, fetch, _FakeYahoo({"SMPL": SMPL_DATES}, {"SMPL": SMPL_ESTIMATES}))
    assert r.failed and "disk full" in r.message and _marked(store) is None


def test_sec_errors_are_redacted(store, cfg_with_agent):
    cfg_with_agent.tiingo_token = "tok-secret-123"
    fetch = _Fetch({URL: RuntimeError(f"refused for {AGENT} with tok-secret-123")})
    (r,) = _collect(store, cfg_with_agent, fetch, _FakeYahoo())
    assert r.failed and AGENT not in r.message and "tok-secret-123" not in r.message and "***" in r.message


def test_yahoo_errors_are_redacted(store, cfg_with_agent):
    cfg_with_agent.tiingo_token = "tok-secret-123"
    fetch = _Fetch({URL: json.dumps(EARN).encode()})
    (r,) = _collect(store, cfg_with_agent, fetch, _FakeYahoo({"SMPL": RuntimeError("GET ?token=tok-secret-123")}))
    assert r.failed and "tok-secret-123" not in r.message


def test_a_cik_shared_by_two_symbols_is_fetched_once(store, cfg_with_agent):
    store.upsert_security("SMPL.B", asset_type="stock", first_seen="2026-01-01")
    store.set_security_cik("SMPL.B", CIK, "Sample Earnings Co")
    fetch = _Fetch({URL: json.dumps(EARN).encode()})
    results = _collect(store, cfg_with_agent, fetch, _FakeYahoo(), universe=["SMPL", "SMPL.B"])
    assert len(fetch.calls) == 1 and [r.events for r in results] == [2, 2]
    assert store.query("SELECT sic FROM securities WHERE symbol = 'SMPL.B'")[0][0] == 3571


def test_a_failed_shared_cik_is_not_fetched_twice_either(store, cfg_with_agent):
    store.upsert_security("SMPL.B", asset_type="stock", first_seen="2026-01-01")
    store.set_security_cik("SMPL.B", CIK, "Sample Earnings Co")
    fetch = _Fetch({URL: HttpError(503, URL)})
    results = _collect(store, cfg_with_agent, fetch, _FakeYahoo(), universe=["SMPL", "SMPL.B"])
    assert len(fetch.calls) == 1 and [r.failed for r in results] == [True, True]


def test_only_stocks_and_foreign_filers_are_asked(store, cfg):
    kinds = {"FORN": "foreign_filer", "SPY": "etf", "VFIAX": "mutual_fund", "SPAXX": "money_market", "ODD": "unknown"}
    for symbol, kind in kinds.items():
        store.upsert_security(symbol, asset_type=kind, first_seen="2026-01-01")
    yahoo_client = _FakeYahoo()
    _collect(store, cfg, _Fetch({}), yahoo_client, universe=["SMPL", *kinds])
    assert [s for kind, s in yahoo_client.calls if kind == "dates"] == ["SMPL", "FORN"]


def test_a_symbol_outside_the_universe_is_not_asked(store, cfg):
    store.upsert_security("OTHER", asset_type="stock", first_seen="2026-01-01")
    yahoo_client = _FakeYahoo()
    _collect(store, cfg, _Fetch({}), yahoo_client, universe=["SMPL"])
    assert {s for _, s in yahoo_client.calls} == {"SMPL"}


def test_sleeps_between_companies_for_sec_and_between_symbols_for_yahoo(store, cfg_with_agent):
    store.upsert_security("SMPL.B", asset_type="stock", first_seen="2026-01-01")
    store.set_security_cik("SMPL.B", CIK, "Sample Earnings Co")
    store.upsert_security("OTHR", asset_type="stock", first_seen="2026-01-01")
    store.set_security_cik("OTHR", "0000999003", "Other Sample Co")
    pauses: list[float] = []
    fetch = _Fetch({URL: json.dumps(EARN).encode()})
    _collect(store, cfg_with_agent, fetch, _FakeYahoo(), universe=["SMPL", "SMPL.B", "OTHR"], sleep=pauses.append)
    assert sorted(pauses) == [yahoo.PAUSE_SECONDS] * 2 + [earnings.PAUSE_SECONDS]
    assert len(fetch.calls) == 2


def test_estimates_drop_an_unknown_period_and_non_numbers(store, cfg):
    ests = [Estimate("0q", float("nan"), float("inf"), 2.3, None), Estimate("-1q", 1.0, 1.0, 1.0, 3),
            Estimate("0y", 8.0, 7.5, 8.4, float("nan"))]
    (r,) = _collect(store, cfg, _Fetch({}), _FakeYahoo(estimates={"SMPL": ests}))
    assert [row[1:6] for row in _estimates(store)] == [("0q", None, None, 2.3, None), ("0y", 8.0, 7.5, 8.4, None)]
    assert r.estimates == 2


@pytest.mark.parametrize("sic, want", [("3571", 3571), (3571, 3571), ("", None), (None, None), ("n/a", None)])
def test_sic_is_an_int_or_none(store, cfg_with_agent, sic, want):
    data = dict(EARN, sic=sic)
    _collect(store, cfg_with_agent, _Fetch({URL: json.dumps(data).encode()}), _FakeYahoo())
    assert store.query("SELECT sic FROM securities WHERE symbol = 'SMPL'")[0][0] == want


# ---- the store ----------------------------------------------------------------------------------------------------

def test_replace_earnings_writes_only_a_changed_set(store):
    a = EarningsEvent("SMPL", "2026-07-31", None, A1, 1.89, 2.02, 6.74, "sec", None, "yahoo")
    b = EarningsEvent("SMPL", "2026-04-30", "after_close", None, 1.5, 1.6, 6.67, "yahoo", "yahoo", "yahoo")
    assert store.replace_earnings("SMPL", [a, b]) == 2
    assert store.replace_earnings("SMPL", [b, a]) == 0
    assert store.earnings_events_of("SMPL") == [b, a]
    changed = EarningsEvent("SMPL", "2026-07-31", None, A1, 1.89, 2.05, 8.47, "sec", None, "yahoo")
    c = EarningsEvent("SMPL", "2026-01-28", None, A2, None, None, None, "sec", None, None)
    assert store.replace_earnings("SMPL", [changed, c]) == 3               # one changed, one removed, one added
    assert store.earnings_events_of("SMPL") == [c, changed]
    assert store.earnings_events_of("OTHER") == []


def test_write_estimates_is_idempotent(store):
    ests = [Estimate("0q", 2.1, 1.9, 2.3, 30)]
    assert store.write_estimates("SMPL", "2026-10-30", ests, STAMP) == 1
    assert store.write_estimates("SMPL", "2026-10-30", ests, "2026-10-30T20:00:00Z") == 0
    assert store.write_estimates("SMPL", "2026-10-30", [Estimate("0q", 2.2, 1.9, 2.3, 30)], "2026-10-30T21:00:00Z") == 1
    assert _estimates(store) == [("2026-10-30", "0q", 2.2, 1.9, 2.3, 30, "yfinance", "2026-10-30T21:00:00Z")]


def test_earnings_tables_and_columns(store):
    cols = [r["name"] for r in store.query("PRAGMA table_info(earnings_events)")]
    assert cols == ["symbol", "report_date", "time_of_day", "accession", "eps_expected", "eps_reported", "surprise_pct",
                    "date_source", "time_source", "eps_source"]
    cols = [r["name"] for r in store.query("PRAGMA table_info(estimate_snapshots)")]
    assert cols == ["symbol", "date", "period", "eps_avg", "eps_low", "eps_high", "analysts", "source", "fetched_at"]
    names = {r["name"] for r in store.query("PRAGMA table_info(securities)")}
    assert {"last_earnings_fetch", "sic"} <= names


# ---- the client ---------------------------------------------------------------------------------------------------

def _fake_yfinance(monkeypatch, *, dates=None, estimate=None, raises=None):
    pd = pytest.importorskip("pandas")
    asked = []

    class _Ticker:
        def __init__(self, symbol):
            asked.append(symbol)

        def get_earnings_dates(self, limit):
            assert logging.getLogger("yfinance").level == logging.CRITICAL
            asked.append(limit)
            if raises:
                raise raises
            return dates(pd) if dates else None

        @property
        def earnings_estimate(self):
            return estimate(pd) if estimate else None

    monkeypatch.setitem(sys.modules, "yfinance", types.SimpleNamespace(Ticker=_Ticker))
    return asked


def test_yfinance_client_reads_earnings_dates(monkeypatch):
    def dates(pd):
        index = pd.DatetimeIndex([pd.Timestamp("2026-10-29 16:00", tz="America/New_York"),
                                  pd.Timestamp("2026-07-30 16:00", tz="America/New_York")], name="Earnings Date")
        return pd.DataFrame({"EPS Estimate": [2.1, 1.89], "Reported EPS": [float("nan"), 2.02],
                             "Surprise(%)": [float("nan"), 6.74]}, index=index)
    asked = _fake_yfinance(monkeypatch, dates=dates)
    rows = yahoo.YfinanceClient().earnings_dates("BRK.B", 24)
    assert asked == ["BRK-B", 24]
    assert rows == [YahooEarning(datetime(2026, 10, 29, 16, tzinfo=NY), 2.1, None, None),
                    YahooEarning(datetime(2026, 7, 30, 16, tzinfo=NY), 1.89, 2.02, 6.74)]
    assert all(r.when.utcoffset() is not None for r in rows)


def test_yfinance_client_answers_no_earnings_dates_with_an_empty_list(monkeypatch):
    _fake_yfinance(monkeypatch)
    assert yahoo.YfinanceClient().earnings_dates("SPY", 24) == []
    _fake_yfinance(monkeypatch, dates=lambda pd: pd.DataFrame())
    assert yahoo.YfinanceClient().earnings_dates("SPY", 24) == []


def test_yfinance_client_reads_estimates(monkeypatch):
    def estimate(pd):
        return pd.DataFrame({"avg": [2.1, 8.0], "low": [1.9, float("nan")], "high": [2.3, 8.4],
                             "numberOfAnalysts": [30, 40], "yearAgoEps": [1.8, 7.0]}, index=["0q", "0y"])
    _fake_yfinance(monkeypatch, estimate=estimate)
    assert yahoo.YfinanceClient().estimates("AAPL") == [Estimate("0q", 2.1, 1.9, 2.3, 30), Estimate("0y", 8.0, None, 8.4, 40)]
    _fake_yfinance(monkeypatch)
    assert yahoo.YfinanceClient().estimates("AAPL") == []


def test_yfinance_client_restores_the_logger_level_after_a_failed_ask(monkeypatch):
    _fake_yfinance(monkeypatch, raises=RuntimeError("boom"))
    logger = logging.getLogger("yfinance")
    logger.setLevel(logging.WARNING)
    with pytest.raises(RuntimeError):
        yahoo.YfinanceClient().earnings_dates("AAPL", 24)
    assert logger.level == logging.WARNING


# ---- in a sync ----------------------------------------------------------------------------------------------------

def _sync_cfg(tmp_path: Path, agent: str | None) -> C.Config:
    C.init_project(tmp_path)
    (tmp_path / "watchlist.txt").write_text("SMPL\n", encoding="utf-8")
    (tmp_path / ".env").write_text(f"SEC_USER_AGENT={agent}\n" if agent else "", encoding="utf-8")
    cfg = C.load_config(tmp_path)
    s = Store.open(cfg.db_path)
    s.upsert_security("SMPL", asset_type="stock", first_seen="2026-01-01")
    s.set_security_cik("SMPL", CIK, "Sample Earnings Co")
    s.close()
    return cfg


def test_the_earnings_step_reports_what_it_did(tmp_path):
    cfg = _sync_cfg(tmp_path, AGENT)
    fetch = _Fetch({URL: json.dumps(EARN).encode()})
    yahoo_client = _FakeYahoo({"SMPL": SMPL_DATES}, {"SMPL": SMPL_ESTIMATES})
    rep = sync.run_sync(cfg, only=["earnings"], fetch=fetch, now=NOW, yahoo=yahoo_client, sleep=lambda s: None)
    step = rep.steps[0]
    assert (step.step, step.status, step.rows) == ("earnings", "ok", 7)          # 2 events + 4 estimates + 1 sector
    assert step.message == "1 companies asked, 2 events changed, 4 estimate rows written, 1 sectors changed"
    again = sync.run_sync(cfg, only=["earnings"], fetch=fetch, now=NOW, yahoo=yahoo_client, sleep=lambda s: None)
    assert again.steps[0].message == "0 companies asked, 0 events changed, 0 estimate rows written, 0 sectors changed"
    assert again.steps[0].status == "skipped"                          # nothing asked says nothing about the network
    assert len(fetch.calls) == 1


def test_the_earnings_step_without_sec_user_agent_says_so_and_is_ok(tmp_path):
    cfg = _sync_cfg(tmp_path, None)
    rep = sync.run_sync(cfg, only=["earnings"], fetch=_Fetch({}), now=NOW, yahoo=_FakeYahoo({"SMPL": SMPL_DATES}),
                        sleep=lambda s: None)
    step = rep.steps[0]
    assert step.status == "ok"
    assert step.message == "SEC part skipped: SEC_USER_AGENT is not set; 1 companies asked, 2 events changed, 0 estimate rows written, 0 sectors changed"


def test_the_earnings_step_names_failures_and_is_an_error_only_when_all_failed(tmp_path):
    cfg = _sync_cfg(tmp_path, AGENT)
    rep = sync.run_sync(cfg, only=["earnings"], fetch=_Fetch({URL: HttpError(503, URL)}), now=NOW,
                        yahoo=_FakeYahoo(), sleep=lambda s: None)
    step = rep.steps[0]
    assert step.status == "error" and "1 failed: SMPL (SEC" in step.message and AGENT not in step.message


def test_steps_include_earnings_after_funds():
    assert sync.STEPS[sync.STEPS.index("funds") + 1] == "earnings" and "earnings" in sync.NETWORK_STEPS
    assert sync.STEPS[sync.STEPS.index("earnings") + 1] == "derive"
    assert sync.STALE_HOURS["earnings"] == 48
