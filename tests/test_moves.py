"""Drops and their causes (`price_moves`): a day down 4%, a week down 8%, or a day 2.5 of the symbol's own standard
deviations below the market, each with the causes found in sourced facts, in the spec's order, or "no cause found"."""
import json
from datetime import date, timedelta

import pytest

from financial_data_collector import sync
from financial_data_collector.check.findings import Finding
from financial_data_collector.config import EngineSettings
from financial_data_collector.derive import engine, moves, sources
from financial_data_collector.derive import valuation as V
from financial_data_collector.models import CurrentReport, EarningsEvent, FundSnapshot, Headline, PriceMove
from tests.test_engine import END, _cover, _weekday_bars, _weekdays, cfg, store  # noqa: F401  (fixtures)
from tests.test_scores import BASE, _quarters, _insider

CIK = "0000999002"
ACCN_8K = "0000999002-26-000077"
ACCN_ER = "0000999002-26-000076"
S = EngineSettings()
SINCE = "2025-10-29"
FETCHED_S = "2026-10-30T12:00:00Z"
CHECKED = "2026-10-30T12:05:00Z"
NONE = {"kind": "none", "text": "no cause found", "sources": []}


def _series(closes, end: str = END) -> list[tuple]:
    """Held adjusted bars as the engine passes them: (date, adjusted close, 0.0, 1.0, source, fetched_at)."""
    return [(d, float(c), 0.0, 1.0, "tiingo", FETCHED_S) for d, c in zip(_weekdays(end, len(closes)), closes)]


def _move(start: str, day: str, move: float = -0.05, market: float | None = -0.005, sector: float | None = None,
          sector_symbol: str | None = None) -> PriceMove:
    return PriceMove("AAPL", day, "1d", start, move, "SPY", market, sector_symbol, sector, None, ("drop_day",), (),
                     ())


def _event(day: str, time_of_day: str | None, accession: str | None = None, eps=(1.89, 2.02, 6.9)) -> EarningsEvent:
    exp, rep, surprise = eps
    return EarningsEvent("AAPL", day, time_of_day, accession, exp, rep, surprise, "sec" if accession else "yahoo",
                         "yahoo", "yahoo" if rep is not None else None)


def _report(filed: str, items: tuple[str, ...], accession: str = ACCN_8K) -> CurrentReport:
    return CurrentReport(CIK, accession, "8-K", filed, items)


def _headline(day: str, title: str, outlet: str | None = "Example Wire", url: str | None = None,
              as_of: str = "2026-10-30") -> Headline:
    return Headline("AAPL", day, title, outlet, url, None, as_of)


def _rows(store, symbol: str = "AAPL") -> list[dict]:
    return [dict(r) for r in store.query("SELECT * FROM price_moves WHERE symbol = ? ORDER BY date, span", (symbol,))]


def _days_before(day: str, n: int) -> str:
    return (date.fromisoformat(day) - timedelta(days=n)).isoformat()


# ---- finding drops --------------------------------------------------------------------------------------------------

def test_a_split_and_an_ex_dividend_day_are_not_drops(store, cfg):        # Review Focus 1
    over = {i: 100.0 for i in range(-49, 0)}
    over[-50] = (100.0, 0.0, 4.0)                                         # 400 -> 100: a 4-for-1 split
    over[-20] = (96.0, 4.0, 1.0)                                          # 100 -> 96: ex-dividend 4.0
    over.update({i: 96.0 for i in range(-19, 0)})
    _weekday_bars(store, "AAPL", close=400.0, overrides=over)
    _weekday_bars(store, "SPY", asset_type="etf")
    store.mark_benchmarks(["SPY"])
    _cover(store)
    rep = engine.run(store, cfg)
    assert rep.moves == 0 and _rows(store) == []


def test_a_held_split_day_is_still_not_a_drop(store, cfg):                # R5: adjust over every bar, then drop
    over = {i: 100.0 for i in range(-49, 0)}
    over[-50] = (100.0, 0.0, 4.0)
    dates = _weekday_bars(store, "AAPL", close=400.0, overrides=over)
    store.check_record("impossible", [Finding("impossible", "AAPL", "*", dates[-50], dates[-50], "hold", "held")],
                       FETCHED_S)
    _cover(store)
    engine.run(store, cfg)
    assert _rows(store) == []


def test_a_day_down_four_percent_is_recorded_with_its_market_move():
    (m,) = moves.find_moves("AAA", _series([100, 95]), ("SPY", _series([500, 497.5])), None, SINCE, S)
    assert (m.span, m.move, m.market_move, m.triggers) == ("1d", pytest.approx(-0.05), pytest.approx(-0.005),
                                                          ("drop_day",))
    d0, d1 = _weekdays(END, 2)
    assert (m.date, m.start_date, m.market_symbol, m.sector_symbol, m.sector_move) == (d1, d0, "SPY", None, None)
    assert m.excess_sd is None and m.causes == ()                         # too little history for a deviation
    kinds = [(s["kind"], s["date"]) for s in m.sources]
    assert kinds == [("price", d0), ("price", d1), ("price", d0), ("price", d1), ("config", None)]
    assert m.sources[-1] == {"kind": "config", "label": "[engine] drop_day", "date": None, "ref": "0.04"}


def test_a_day_down_less_than_four_percent_is_not_recorded():
    assert moves.find_moves("AAA", _series([100, 96.5]), ("SPY", _series([500, 500])), None, SINCE, S) == []


def test_a_benchmark_without_both_bars_has_no_move():
    (m,) = moves.find_moves("AAA", _series([100, 95]), ("SPY", _series([500])), ("XLK", []), SINCE, S)
    assert (m.market_symbol, m.market_move, m.sector_symbol, m.sector_move) == ("SPY", None, "XLK", None)
    assert [s["kind"] for s in m.sources] == ["price", "price", "config"]


def _alternating(n: int, last: float) -> list[float]:
    """100, then n days alternating +0.5% / -0.5%, then a day of `last`."""
    out = [100.0]
    for i in range(n):
        out.append(out[-1] * (1.005 if i % 2 == 0 else 0.995))
    out.append(out[-1] * (1 + last))
    return out


def test_an_excess_move_beyond_the_symbols_own_spread_is_recorded():
    closes = _alternating(252, -0.02)
    (m,) = moves.find_moves("AAA", _series(closes), ("SPY", _series([500.0] * len(closes))), None, SINCE, S)
    assert m.triggers == ("excess",) and m.span == "1d" and m.date == END
    assert m.move == pytest.approx(-0.02) and m.market_move == pytest.approx(0.0)
    assert m.excess_sd == pytest.approx(-4.0, abs=0.05)
    labels = [s["label"] for s in m.sources if s["kind"] == "config"]
    assert labels == ["[engine] excess_sd"]


def test_excess_needs_120_earlier_moves_with_the_market():
    closes = _alternating(119, -0.02)
    assert moves.find_moves("AAA", _series(closes), ("SPY", _series([500.0] * len(closes))), None, SINCE, S) == []
    closes = _alternating(120, -0.02)
    assert len(moves.find_moves("AAA", _series(closes), ("SPY", _series([500.0] * len(closes))), None, SINCE, S)) == 1


def test_a_drop_day_also_stores_its_excess_sd():
    closes = _alternating(252, -0.05)
    (m,) = moves.find_moves("AAA", _series(closes), ("SPY", _series([500.0] * len(closes))), None, SINCE, S)
    assert m.triggers == ("drop_day", "excess") and m.excess_sd == pytest.approx(-10.0, abs=0.1)


def test_a_five_day_slide_is_recorded_once():
    closes = [100.0] * 10
    for _ in range(6):
        closes.append(closes[-1] * 0.975)
    found = moves.find_moves("AAA", _series(closes), None, None, SINCE, S)
    (m,) = found
    dates = _weekdays(END, len(closes))
    assert m.span == "5d" and m.triggers == ("drop_week",) and m.excess_sd is None
    assert (m.date, m.start_date) == (dates[13], dates[8])                # the 4th day down, against 5 bars back
    assert m.move == pytest.approx(0.975 ** 4 - 1)
    assert m.sources[-1]["label"] == "[engine] drop_week"


def test_a_second_slide_after_a_recovery_is_recorded_again():
    closes = [100.0] * 6 + [90.0] + [100.0] * 6 + [90.0]
    found = moves.find_moves("AAA", _series(closes), None, None, SINCE, S)
    assert [(m.span, m.triggers) for m in found] == [("1d", ("drop_day",)), ("5d", ("drop_week",))] * 2


def test_moves_older_than_moves_days_are_not_recorded():
    closes = [100.0] * 300
    closes[20] = 90.0                                                     # a drop about 400 days back
    closes[-1] = 90.0
    found = moves.find_moves("AAA", _series(closes), None, None, SINCE, S)
    assert {m.date for m in found} == {END}


# ---- causes ---------------------------------------------------------------------------------------------------------

def test_after_close_report_the_evening_before_is_the_cause():            # Review Focus 4
    m = moves.add_causes(_move("2026-10-28", "2026-10-29"), [_event("2026-10-28", "after_close")], [], [], CIK, None)
    assert m.causes[0]["kind"] == "earnings" and "after the close" in m.causes[0]["text"]
    assert m.causes[0]["text"] == "reported earnings on 2026-10-28 after the close: EPS 2.02 vs 1.89 expected (+6.9%)"


def test_before_open_report_on_the_day_before_is_not():                   # Review Focus 4
    m = moves.add_causes(_move("2026-10-28", "2026-10-29"), [_event("2026-10-28", "before_open")], [], [], CIK, None)
    assert m.causes == ({"kind": "none", "text": "no cause found", "sources": []},)


def test_a_report_of_unknown_time_the_day_before_is_a_cause():
    m = moves.add_causes(_move("2026-10-28", "2026-10-29"), [_event("2026-10-28", None, eps=(None, None, None))],
                         [], [], CIK, None)
    assert m.causes[0]["text"] == "reported earnings on 2026-10-28"


@pytest.mark.parametrize("tod, words", [("before_open", " before the open"), ("during", " during the day"),
                                        (None, "")])
def test_earnings_text_names_the_time_of_day(tod, words):
    m = moves.add_causes(_move("2026-10-28", "2026-10-29"), [_event("2026-10-29", tod)], [], [], CIK, None)
    assert m.causes[0]["text"] == f"reported earnings on 2026-10-29{words}: EPS 2.02 vs 1.89 expected (+6.9%)"


def test_an_earnings_cause_links_its_8k():
    m = moves.add_causes(_move("2026-10-28", "2026-10-29"), [_event("2026-10-29", "before_open", ACCN_ER)], [], [],
                         CIK, None)
    src = m.causes[0]["sources"]
    assert src[0]["kind"] == "filing" and src[0]["url"] == sources.filing_url(CIK, ACCN_ER)


def test_causes_come_in_the_spec_order():
    m = moves.add_causes(
        _move("2026-10-28", "2026-10-29", move=-0.06, market=-0.05, sector=-0.045, sector_symbol="XLK"),
        [_event("2026-10-28", "after_close")], [_report("2026-10-29", ("5.02", "9.01"))],
        [_headline("2026-10-29", "Sample Earnings Co names a new finance chief")], CIK, "Technology")
    assert [c["kind"] for c in m.causes] == ["earnings", "filing", "market", "sector", "news"]
    assert m.causes[1]["sources"][0]["url"].startswith("https://www.sec.gov/Archives/edgar/data/999002/")
    assert m.causes[1]["text"] == "filed an 8-K on 2026-10-29: a director or officer change"
    assert m.causes[2]["text"] == "the market fell 5.0% (SPY)"
    assert m.causes[3]["text"] == "its sector fell 4.5% (XLK, Technology)"
    assert m.causes[4]["text"] == "Sample Earnings Co names a new finance chief (Example Wire, 2026-10-29)"
    assert m.causes[4]["sources"] == [{"kind": "feed", "label": "news.json: Example Wire", "date": "2026-10-30",
                                       "ref": "Sample Earnings Co names a new finance chief"}]


def test_a_benchmark_that_fell_less_than_two_thirds_is_not_a_cause():
    m = moves.add_causes(_move("2026-10-28", "2026-10-29", move=-0.06, market=-0.039, sector=-0.04,
                               sector_symbol="XLK"), [], [], [], CIK, None)
    assert [c["kind"] for c in m.causes] == ["sector"]
    assert m.causes[0]["text"] == "its sector fell 4.0% (XLK)"


def test_an_8k_with_only_2_02_and_9_01_is_not_a_filing_cause():
    m = moves.add_causes(_move("2026-10-28", "2026-10-29"), [], [_report("2026-10-29", ("2.02", "9.01"))], [], CIK,
                         None)
    assert m.causes == (NONE,)


def test_8k_items_are_named_and_unknown_items_shown_by_number():
    m = moves.add_causes(_move("2026-10-28", "2026-10-29"), [],
                         [_report("2026-10-28", ("2.02", "5.02", "8.01", "9.99", "9.01"))], [], CIK, None)
    assert m.causes[0]["text"] == "filed an 8-K on 2026-10-28: a director or officer change; other events; item 9.99"
    assert moves.ITEM_LABELS["4.02"] == "earlier statements no longer to be relied on" and len(moves.ITEM_LABELS) == 16


def test_filings_and_news_outside_the_span_are_not_causes():
    m = moves.add_causes(_move("2026-10-28", "2026-10-29"), [_event("2026-10-27", "after_close")],
                         [_report("2026-10-27", ("5.02",)), _report("2026-10-30", ("5.02",))],
                         [_headline("2026-10-27", "Old news"), _headline("2026-10-30", "Later news")], CIK, None)
    assert m.causes == (NONE,)


def test_news_is_newest_first_and_at_most_three():
    heads = [_headline("2026-10-28", "First"), _headline("2026-10-29", "Second", outlet=None),
             _headline("2026-10-29", "Third", url="https://example.invalid/a"), _headline("2026-10-28", "Fourth")]
    m = moves.add_causes(_move("2026-10-28", "2026-10-29"), [], [], heads, CIK, None)
    assert [c["text"] for c in m.causes] == ["Second (2026-10-29)", "Third (Example Wire, 2026-10-29)",
                                             "First (Example Wire, 2026-10-28)"]
    assert m.causes[0]["sources"][0]["label"] == "news.json"
    assert m.causes[1]["sources"][0]["url"] == "https://example.invalid/a"


def test_add_causes_keeps_everything_else():
    before = _move("2026-10-28", "2026-10-29")
    after = moves.add_causes(before, [], [], [], None, None)
    assert after.causes == (NONE,)
    assert (after.symbol, after.move, after.sources, after.triggers) == (before.symbol, before.move, before.sources,
                                                                         before.triggers)


# ---- the engine -----------------------------------------------------------------------------------------------------

def _drop(store, symbol: str = "AAPL", n: int = 260, **kw) -> list[str]:
    """n weekday bars at 100 ending END, the last one 91: down 9% on the day and over the week."""
    return _weekday_bars(store, symbol, n=n, overrides={-1: 91.0}, **kw)


def test_engine_writes_moves_with_checked_at(store, cfg):
    dates = _drop(store)
    _weekday_bars(store, "SPY", asset_type="etf", overrides={-1: 99.0})
    store.mark_benchmarks(["SPY"])
    _cover(store)
    rep = engine.run(store, cfg)
    assert rep.moves == 2                                               # the day and the week
    rows = _rows(store)
    assert [(r["span"], r["date"], r["start_date"]) for r in rows] == [("1d", END, dates[-2]), ("5d", END, dates[-6])]
    r = rows[0]
    assert (r["move"], r["market_symbol"], r["market_move"]) == (pytest.approx(-0.09), "SPY", pytest.approx(-0.01))
    assert (r["triggers"], r["checked_at"], r["sector_symbol"]) == ("drop_day", CHECKED, None)
    assert json.loads(r["causes"]) == [NONE]
    assert [s["kind"] for s in json.loads(r["sources"])] == ["price"] * 4 + ["config"]
    assert rows[1]["triggers"] == "drop_week" and rows[1]["excess_sd"] is None


def test_engine_moves_with_a_sector_and_causes(store, cfg):
    _drop(store)
    store.set_security_cik("AAPL", CIK, "Sample Earnings Co")
    store.set_sector("AAPL", "Technology", "test")
    _weekday_bars(store, "SPY", asset_type="etf", overrides={-1: 99.0})
    _weekday_bars(store, "XLK", asset_type="etf", overrides={-1: 93.0})
    store.mark_benchmarks(["SPY", "XLK"])
    store.replace_earnings("AAPL", [_event("2026-10-28", "after_close", ACCN_ER)])
    _cover(store)
    engine.run(store, cfg)
    r = _rows(store)[0]
    assert (r["sector_symbol"], r["sector_move"]) == ("XLK", pytest.approx(-0.07))
    assert [c["kind"] for c in json.loads(r["causes"])] == ["earnings", "sector"]
    assert json.loads(r["causes"])[1]["text"] == "its sector fell 7.0% (XLK, Technology)"


def test_news_older_than_the_feed_still_explains_an_old_move(store, cfg):
    dates = _weekday_bars(store, "AAPL", overrides={-40: 95.0, **{i: 95.0 for i in range(-39, 0)}})
    store.write_headlines([_headline(dates[-40], "Sample Earnings Co cuts its outlook", as_of=dates[-38])],
                          "2026-09-03T12:00:00Z")                         # stored weeks ago; today's feed lacks it
    _cover(store)
    engine.run(store, cfg)
    (r,) = [r for r in _rows(store) if r["span"] == "1d"]
    (c,) = json.loads(r["causes"])
    assert c["kind"] == "news" and c["text"] == f"Sample Earnings Co cuts its outlook (Example Wire, {dates[-40]})"
    assert c["sources"][0]["date"] == dates[-38]


def test_engine_leaves_out_moves_older_than_moves_days(store, cfg):
    _weekday_bars(store, "AAPL", n=400, overrides={-380: 90.0, **{i: 90.0 for i in range(-379, 0)}})
    _cover(store)
    assert engine.run(store, cfg).moves == 0 and _rows(store) == []


def test_a_held_close_is_left_out_of_the_moves(store, cfg):
    dates = _drop(store)
    store.check_record("prices2", [Finding("prices2", "AAPL", "*", END, END, "hold", "two sources disagree")],
                       FETCHED_S)
    _cover(store)
    engine.run(store, cfg)
    assert _rows(store) == [] and dates[-1] == END


def test_a_second_run_rewrites_no_move(store, cfg):
    _drop(store)
    _weekday_bars(store, "SPY", asset_type="etf", overrides={-1: 99.0})
    store.mark_benchmarks(["SPY"])
    _cover(store)
    engine.run(store, cfg)
    first = [tuple(r) for r in store.query("SELECT * FROM price_moves ORDER BY symbol, date, span")]
    before = store.conn.total_changes
    engine.run(store, cfg)
    assert [tuple(r) for r in store.query("SELECT * FROM price_moves ORDER BY symbol, date, span")] == first
    assert store.conn.total_changes == before and len(first) == 2


def test_replace_moves_deletes_a_move_no_longer_found(store):
    m = moves.add_causes(_move("2026-10-28", "2026-10-29"), [], [], [], None, None)
    assert store.replace_moves([m], CHECKED) == 1
    assert store.replace_moves([], CHECKED) == 0
    assert store.query("SELECT COUNT(*) FROM price_moves")[0][0] == 0
    assert "price_moves" in store.counts()


def test_engine_step_message_counts_moves(store, cfg):
    _drop(store)
    _weekday_bars(store, "SPY", asset_type="etf")
    store.mark_benchmarks(["SPY"])
    _cover(store)
    rows, msg, status = sync._step_engine(store, cfg, progress=lambda d: None)
    assert (rows, msg, status) == (6, "1 symbols, 4 scores (0 held), 2 moves", "ok")


# ---- the sources test, over a full fixture --------------------------------------------------------------------------

def _full_fixture(store, cfg, tmp_path) -> None:
    """A company (CIK 0000999002) with valuation rows, statements, an earnings event, an 8-K, a headline and the
    insider feed; a fund with snapshots; SPY and XLK; a drop on the newest day."""
    dates = _weekday_bars(store, "AAPL", n=1300, overrides={-1: 94.0})
    store.set_security_cik("AAPL", CIK, "Sample Earnings Co")
    store.set_sector("AAPL", "Technology", "test")
    rows = []
    for d in dates:
        r = dict(BASE, symbol="AAPL", date=d, pe=30.0 if d != END else 28.2)
        rows.append(tuple(r[c] for c in V.COLUMNS))
    store.replace_rows("valuation_daily", V.COLUMNS, rows)
    _quarters(store, {"revenue": 100.0, "ocf": 25.0, "capex": 5.0, "operating_income": 30.0})
    store.replace_earnings("AAPL", [_event("2026-10-28", "after_close", ACCN_ER), _event("2026-07-30", "after_close")])
    store.write_current_reports([_report("2026-10-29", ("5.02", "9.01")), _report("2026-10-28", ("2.02", "9.01"),
                                                                                  ACCN_ER)])
    store.write_headlines([_headline(END, "Sample Earnings Co names a new finance chief",
                                     url="https://example.invalid/news")], FETCHED_S)
    cfg.investing_dir = tmp_path / "investing"
    _insider(cfg.investing_dir / "data" / "insider.json",
             {"AAPL": [{"ticker": "AAPL", "filed": _days_before(END, 10), "accession": "0000999002-26-000050",
                        "direction": "buy", "url": "https://example.invalid/form4"}]})
    fund_dates = _weekday_bars(store, "VFIAX", asset_type="mutual_fund", overrides={-1: 95.0})
    for i, d in enumerate([_days_before(END, k) for k in range(400, -1, -1)]):
        store.write_fund_snapshot(FundSnapshot("VFIAX", d, 20.0 + i / 100, None, 0.015, 0.0004, "yfinance"),
                                  "2026-10-29T22:00:00Z")
    assert fund_dates[-1] == END
    _weekday_bars(store, "SPY", n=1300, asset_type="etf", overrides={-1: 96.0})
    _weekday_bars(store, "XLK", n=1300, asset_type="etf", overrides={-1: 95.5})
    store.mark_benchmarks(["SPY", "XLK"])
    _cover(store)


def test_every_ok_row_has_a_source_and_every_link_is_built(store, cfg, tmp_path):   # the spec's sources test
    _full_fixture(store, cfg, tmp_path)
    rep = engine.run(store, cfg)
    assert rep.skipped is None and rep.moves >= 2
    kinds = {r["symbol"]: [c["kind"] for c in json.loads(r["causes"])]
             for r in store.query("SELECT symbol, causes FROM price_moves WHERE span = '1d'")}
    assert kinds["AAPL"] == ["earnings", "filing", "market", "sector", "news"]
    assert kinds["VFIAX"] == ["market"]
    seen = set()
    for r in store.query("SELECT status, sources FROM valuation_scores_raw UNION ALL SELECT 'ok', sources FROM "
                         "price_moves"):
        srcs = json.loads(r["sources"])
        assert r["status"] != "ok" or srcs
        for s in srcs:
            seen.add(s["kind"])
            assert set(s) <= {"kind", "label", "date", "ref", "url", "period"}
            if s["kind"] == "filing":
                assert s["url"] == sources.filing_url(CIK, s["ref"])
    for r in store.query("SELECT causes FROM price_moves"):
        for c in json.loads(r["causes"]):
            assert c["kind"] == "none" or c["sources"]
            for s in c["sources"]:
                if s["kind"] == "filing":
                    assert s["url"] == sources.filing_url(CIK, s["ref"])
    assert {"price", "filing", "feed", "config", "derived"} <= seen
