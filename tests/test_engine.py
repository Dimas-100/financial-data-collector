"""The engine step: it runs only once a check run covers the newest derive, reads held values as missing, and writes
one row per symbol and metric at the symbol's newest final close (this file: the frame and the price shape)."""
import json
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pytest

from financial_data_collector import config as C
from financial_data_collector import sync
from financial_data_collector.check.findings import Finding
from financial_data_collector.derive import engine
from financial_data_collector.derive.shape import price_shape
from financial_data_collector.models import AccountRef, PositionRow, PriceBar, Snapshot
from financial_data_collector.store import Store

NOW_S = "2026-10-30T12:00:00Z"
CHECKED = "2026-10-30T12:05:00Z"
FETCHED = datetime(2026, 10, 30, 12, 0, tzinfo=timezone.utc)      # after every bar's close: all final
END = "2026-10-29"
PRICE_METRICS = ("off_high_52w", "vs_200d", "week_vs_market", "week_vs_sector")


@pytest.fixture
def cfg(tmp_path: Path):
    C.init_project(tmp_path)
    return C.load_config(tmp_path)


@pytest.fixture
def store(cfg):
    s = Store.open(cfg.db_path)
    yield s
    s.close()


def _weekdays(end: str, n: int) -> list[str]:
    out, d = [], date.fromisoformat(end)
    while len(out) < n:
        if d.weekday() < 5:
            out.append(d.isoformat())
        d -= timedelta(days=1)
    return out[::-1]


def _weekday_bars(store: Store, symbol: str, end: str = END, n: int = 260, close: float = 100.0, overrides=None,
                  asset_type: str = "stock") -> list[str]:
    """n weekday bars ending `end`, all at `close` except `overrides` {index: close | (close, dividend, split)}
    (Python indexing, so -1 is the newest bar). Returns the dates."""
    dates = _weekdays(end, n)
    vals = {i % n: v for i, v in (overrides or {}).items()}
    bars = []
    for i, d in enumerate(dates):
        v = vals.get(i, close)
        c, div, split = v if isinstance(v, tuple) else (v, 0.0, 1.0)
        bars.append(PriceBar(d, c, c, div, split))
    store.upsert_security(symbol, asset_type=asset_type, first_seen="2025-01-01")
    store.write_prices(symbol, bars, "tiingo", fetched_at=FETCHED)
    return dates


def _cover(store: Store, derive_id: str = "r1", finished: str = CHECKED) -> None:
    store.log_run(derive_id, "derive", "ok", 0, "", NOW_S, NOW_S, "manual")
    store.check_log_run(f"c-{derive_id}", started_at=NOW_S, finished_at=finished, derive_run_id=derive_id,
                        checks_run=8, holds=0, warns=0, error=None)


def _spy(store: Store, fall: float = 0.02) -> None:
    _weekday_bars(store, "SPY", asset_type="etf", overrides={-1: 100.0 * (1 - fall)})
    store.mark_benchmarks(["SPY"])


def _aaa(store: Store) -> list[str]:
    """AAA: 260 weekday bars at 100, a 125 on the 240th bar from the end, the last close 95."""
    return _weekday_bars(store, "AAA", overrides={-240: 125.0, -1: 95.0})


def _hold(store: Store, check: str, symbol: str, field: str, date_from: str, date_to: str | None,
          message: str = "held for a test") -> None:
    store.check_record(check, [Finding(check, symbol, field, date_from, date_to, "hold", message)], NOW_S)


def _scores(store: Store, symbol: str, table: str = "valuation_scores_raw") -> dict:
    return {r["metric"]: dict(r) for r in store.query(f"SELECT * FROM {table} WHERE symbol = ?", (symbol,))}


def _held_position(store: Store, symbol: str) -> None:
    acct = AccountRef(label="Test account", institution="fidelity", account_type="brokerage")
    store.write_snapshot(Snapshot("2026-10-29", "test", [PositionRow(acct, symbol, symbol, 1.0, 10.0, 10.0)], []))


# ---- coverage -------------------------------------------------------------------------------------------------------

def test_engine_waits_for_a_check_run(store, cfg):                       # Review Focus 2
    _weekday_bars(store, "AAA")
    store.log_run("r1", "derive", "ok", 0, "", NOW_S, NOW_S, "manual")
    rep = engine.run(store, cfg)
    assert rep.skipped.startswith("waiting for a check run") and "r1" in rep.skipped
    assert store.query("SELECT COUNT(*) FROM valuation_scores_raw")[0][0] == 0


def test_engine_says_when_no_derive_has_finished(store, cfg):
    _weekday_bars(store, "AAA")
    rep = engine.run(store, cfg)
    assert rep.skipped.endswith("no derive has finished yet") and store.engine_coverage() is None


def test_a_partial_or_failed_check_run_does_not_cover(store, cfg):
    _weekday_bars(store, "AAA")
    store.log_run("r1", "derive", "ok", 0, "", NOW_S, NOW_S, "manual")
    store.check_log_run("c1", started_at=NOW_S, finished_at=CHECKED, derive_run_id=None, checks_run=2, holds=0,
                        warns=0, error=None)
    store.check_log_run("c2", started_at=NOW_S, finished_at=CHECKED, derive_run_id="r1", checks_run=8, holds=0,
                        warns=0, error="the frame broke")
    assert store.engine_coverage() is None
    rep = engine.run(store, cfg)
    assert rep.skipped.startswith("waiting for a check run")
    assert store.query("SELECT COUNT(*) FROM valuation_scores_raw")[0][0] == 0


def test_a_check_run_of_an_older_derive_does_not_cover(store, cfg):
    _cover(store, "r1")
    store.log_run("r2", "derive", "ok", 0, "", NOW_S, NOW_S, "manual")
    assert store.engine_coverage() is None
    _cover(store, "r2", finished="2026-10-30T13:00:00Z")
    assert store.engine_coverage() == {"derive_run_id": "r2", "check_run_id": "c-r2",
                                       "checked_at": "2026-10-30T13:00:00Z"}


# ---- price shape ----------------------------------------------------------------------------------------------------

def test_price_shape(store, cfg):
    _aaa(store)
    _spy(store)
    _cover(store)
    rep = engine.run(store, cfg)
    assert rep.skipped is None and rep.symbols == 1 and rep.scores == 4 and rep.held == 0
    assert rep.moves == 3              # 125 -> 100 the day after the spike (1d and 5d), 100 -> 95 on the last day (1d)
    rows = _scores(store, "AAA")
    assert set(rows) == set(PRICE_METRICS) and {r["date"] for r in rows.values()} == {END}
    assert rows["off_high_52w"]["value"] == pytest.approx(95 / 125 - 1)
    assert rows["vs_200d"]["value"] == pytest.approx(95 / ((199 * 100 + 95) / 200) - 1)
    assert rows["week_vs_market"]["value"] == pytest.approx(-0.05 - (-0.02))
    assert rows["week_vs_sector"]["status"] == "unsupported" and rows["week_vs_sector"]["status_reason"] == "no sector"
    assert json.loads(rows["vs_200d"]["sources"])[0]["kind"] == "price"
    for m in ("off_high_52w", "vs_200d", "week_vs_market"):
        assert rows[m]["status"] == "ok" and rows[m]["inputs"] == ",close," and rows[m]["band"] is None
    src = json.loads(rows["off_high_52w"]["sources"])
    assert src[0] == {"kind": "price", "label": "tiingo close", "date": END, "ref": "2026-10-30T12:00:00Z"}
    assert src[-1]["kind"] == "derived" and src[-1]["label"].startswith("adjusted closes 2025-10-29..2026-10-29 (")
    week = json.loads(rows["week_vs_market"]["sources"])
    assert [s["kind"] for s in week] == ["price", "price", "derived"]


def test_week_vs_sector_against_the_configured_fund(store, cfg):
    _aaa(store)
    _spy(store)
    _weekday_bars(store, "XLK", asset_type="etf", overrides={-1: 97.0})
    store.set_sector("AAA", "Technology", "test")
    _cover(store)
    engine.run(store, cfg)
    rows = _scores(store, "AAA")
    assert cfg.benchmarks.sectors["Technology"] == "XLK"
    assert rows["week_vs_sector"]["value"] == pytest.approx(-0.05 - (-0.03))
    assert json.loads(rows["week_vs_sector"]["sources"])[1]["date"] == END


def test_a_benchmark_missing_a_date_is_unsupported(store, cfg):
    _aaa(store)
    _weekday_bars(store, "SPY", asset_type="etf", end="2026-10-28", n=260)
    store.mark_benchmarks(["SPY"])
    _cover(store)
    engine.run(store, cfg)
    r = _scores(store, "AAA")["week_vs_market"]
    assert (r["status"], r["status_reason"], r["value"]) == ("unsupported", "no SPY close on 2026-10-29", None)


def test_short_history_is_not_enough(store, cfg):
    _weekday_bars(store, "AAA", n=150)
    _spy(store)
    _cover(store)
    engine.run(store, cfg)
    rows = _scores(store, "AAA")
    assert rows["off_high_52w"]["status"] == "not_enough_history" and rows["off_high_52w"]["value"] is None
    assert rows["vs_200d"]["status"] == "not_enough_history"
    assert rows["week_vs_market"]["status"] == "ok"


def test_price_shape_is_pure():
    bars = [(d, 100.0, 0.0, 1.0, "tiingo", None) for d in _weekdays(END, 3)]
    rows = price_shape("AAA", END, bars, None, None)
    assert [r.metric for r in rows] == list(PRICE_METRICS)
    assert len(rows) == 4 and all(r.status == "not_enough_history" for r in rows)


def test_a_split_does_not_bend_the_high_or_the_average(store, cfg):     # Review Focus 1
    over = {i: 100.0 for i in range(-99, 0)}
    over[-100] = (100.0, 0.0, 4.0)                                         # 4-for-1, ex-date 100 bars ago
    _weekday_bars(store, "AAA", close=400.0, overrides=over)
    _spy(store)
    _cover(store)
    engine.run(store, cfg)
    rows = _scores(store, "AAA")
    assert rows["off_high_52w"]["value"] == pytest.approx(0.0, abs=1e-9)
    assert rows["vs_200d"]["value"] == pytest.approx(0.0, abs=1e-9)


def test_a_held_split_day_still_adjusts_the_closes_before_it(store, cfg):   # R5
    over = {i: 100.0 for i in range(-99, 0)}
    over[-100] = (100.0, 0.0, 4.0)
    dates = _weekday_bars(store, "AAA", close=400.0, overrides=over)
    _spy(store)
    _hold(store, "impossible", "AAA", "*", dates[-100], dates[-100])        # the ex-date's close is held
    _cover(store)
    engine.run(store, cfg)
    rows = _scores(store, "AAA")
    assert rows["off_high_52w"]["status"] == "ok"
    assert rows["off_high_52w"]["value"] == pytest.approx(0.0, abs=1e-9)
    assert rows["vs_200d"]["value"] == pytest.approx(0.0, abs=1e-9)


def test_a_benchmarks_held_split_day_still_adjusts_its_closes(store, cfg):
    _aaa(store)
    over = {i: 98.0 if i == -1 else 100.0 for i in range(-3, 0)}
    over[-3] = (100.0, 0.0, 2.0)                                          # SPY 2-for-1 inside the week, ex-date held
    dates = _weekday_bars(store, "SPY", close=200.0, overrides=over, asset_type="etf")
    store.mark_benchmarks(["SPY"])
    _hold(store, "impossible", "SPY", "*", dates[-3], dates[-3])
    _cover(store)
    engine.run(store, cfg)
    assert _scores(store, "AAA")["week_vs_market"]["value"] == pytest.approx(-0.05 - (-0.02))


def test_held_adjusted_bars_adjusts_over_every_bar_then_drops_held_dates():
    bars = [("2026-01-02", 400.0, 0.0, 1.0, "tiingo", None), ("2026-01-05", 100.0, 0.0, 4.0, "tiingo", None),
            ("2026-01-06", 101.0, 0.0, 1.0, "tiingo", "t")]
    holds = [{"id": 1, "check_name": "impossible", "field": "*", "date_from": "2026-01-05", "date_to": "2026-01-05",
              "message": "x"},
             {"id": 2, "check_name": "fresh", "field": "*", "date_from": "2026-01-01", "date_to": None, "message": "y"}]
    assert engine.held_adjusted_bars(bars, holds) == [("2026-01-02", 100.0, 0.0, 1.0, "tiingo", None),
                                                      ("2026-01-06", 101.0, 0.0, 1.0, "tiingo", "t")]
    assert engine.held_adjusted_bars(bars, []) == [("2026-01-02", 100.0, 0.0, 1.0, "tiingo", None),
                                                   ("2026-01-05", 100.0, 0.0, 1.0, "tiingo", None),
                                                   ("2026-01-06", 101.0, 0.0, 1.0, "tiingo", "t")]


# ---- holds ----------------------------------------------------------------------------------------------------------

def test_held_close_days_are_left_out(store, cfg):
    dates = _aaa(store)
    _spy(store)
    _hold(store, "impossible", "AAA", "*", dates[-240], dates[-240])
    _cover(store)
    engine.run(store, cfg)
    r = _scores(store, "AAA")["off_high_52w"]
    assert r["status"] == "ok" and r["value"] == pytest.approx(95 / 100 - 1)


def test_a_stale_statements_hold_leaves_price_shape_alone(store, cfg):
    _aaa(store)
    _spy(store)
    _hold(store, "fresh", "AAA", "*", "2025-10-29", None, "statements are stale")
    _cover(store)
    engine.run(store, cfg)
    r = _scores(store, "AAA")["off_high_52w"]
    assert r["status"] == "ok" and r["value"] == pytest.approx(95 / 125 - 1)
    # ... and the view does not blank a price row for a statements hold
    v = _scores(store, "AAA", "valuation_scores")["off_high_52w"]
    assert v["status"] == "ok" and v["hold_ids"] is None and v["value"] == pytest.approx(95 / 125 - 1)


def test_a_price_hold_on_the_newest_day_holds_the_shape(store, cfg):
    _aaa(store)
    _spy(store)
    _hold(store, "prices2", "AAA", "*", END, END, "a second source disagrees")
    _cover(store)
    rep = engine.run(store, cfg)
    rows = _scores(store, "AAA")
    assert rep.held == 4
    for m in PRICE_METRICS:
        assert (rows[m]["status"], rows[m]["value"], rows[m]["date"]) == ("held", None, END)
        assert rows[m]["status_reason"] == "a second source disagrees" and rows[m]["inputs"] == ",close,"


def test_a_benchmarks_held_close_is_left_out(store, cfg):
    _aaa(store)
    _spy(store)
    _hold(store, "closes", "SPY", "close", END, END)
    _cover(store)
    engine.run(store, cfg)
    r = _scores(store, "AAA")["week_vs_market"]
    assert (r["status"], r["status_reason"]) == ("unsupported", "no SPY close on 2026-10-29")


def test_apply_holds_blanks_covered_fields():
    rows = [{"date": "2026-01-02", "pe": 10.0, "ps": 2.0}, {"date": "2026-01-05", "pe": 11.0, "ps": 2.1}]
    holds = [{"id": 7, "check_name": "impossible", "field": "pe", "date_from": "2026-01-05", "date_to": None,
              "message": "x"},
             {"id": 8, "check_name": "scores", "field": "score:pe", "date_from": "2026-01-01", "date_to": None,
              "message": "y"}]
    out = engine.apply_holds(rows, holds, ("pe", "ps"))
    assert out[0] == {"date": "2026-01-02", "pe": 10.0, "ps": 2.0, "_held": []}
    assert out[1] == {"date": "2026-01-05", "pe": None, "ps": 2.1, "_held": [7]}
    assert rows[1]["pe"] == 11.0                                            # copies: the input is not changed
    star = [{"id": 9, "check_name": "fresh", "field": "*", "date_from": "2026-01-01", "date_to": "2026-01-02",
             "message": "z"}]
    out = engine.apply_holds(rows, star, ("pe", "ps"))
    assert out[0]["pe"] is None and out[0]["ps"] is None and out[0]["_held"] == [9] and out[1]["_held"] == []


def test_price_holds_are_close_or_a_price_checks_star():
    hs = [{"id": 1, "check_name": "impossible", "field": "*"}, {"id": 2, "check_name": "fresh", "field": "*"},
          {"id": 3, "check_name": "recompute", "field": "close"}, {"id": 4, "check_name": "figures", "field": "pe"},
          {"id": 5, "check_name": "filings", "field": "*"}, {"id": 6, "check_name": "prices2", "field": "*"},
          {"id": 7, "check_name": "closes", "field": "*"}]
    assert [h["id"] for h in engine.price_holds(hs)] == [1, 3, 6, 7]
    assert engine.PRICE_CHECKS == ("prices2", "impossible", "closes")


# ---- the view -------------------------------------------------------------------------------------------------------

def _built(store, cfg):
    _aaa(store)
    _spy(store)
    _cover(store)
    engine.run(store, cfg)


def test_view_blanks_a_score_held_after_the_build(store, cfg):          # Review Focus 3 (view part)
    _built(store, cfg)
    _hold(store, "closes", "AAA", "close", END, END, "close looks wrong")
    r = store.query("SELECT value, band, status, status_reason, hold_ids FROM valuation_scores "
                    "WHERE symbol='AAA' AND metric='vs_200d'")[0]
    assert (r["value"], r["status"]) == (None, "held") and r["hold_ids"]
    assert r["status_reason"] == "close looks wrong"
    raw = store.query("SELECT value, status FROM valuation_scores_raw WHERE symbol='AAA' AND metric='vs_200d'")[0]
    assert raw["status"] == "ok" and raw["value"] is not None              # the raw row is never changed


def test_view_a_score_field_hold_blanks_only_that_metric(store, cfg):
    _built(store, cfg)
    _hold(store, "scores", "AAA", "score:vs_200d", END, None, "score looks wrong")
    rows = _scores(store, "AAA", "valuation_scores")
    assert (rows["vs_200d"]["value"], rows["vs_200d"]["status"]) == (None, "held") and rows["vs_200d"]["hold_ids"]
    assert rows["off_high_52w"]["status"] == "ok" and rows["off_high_52w"]["hold_ids"] is None
    assert rows["off_high_52w"]["value"] == pytest.approx(95 / 125 - 1)


def test_view_a_bare_metric_field_does_not_blank_a_score(store, cfg):
    _built(store, cfg)
    _hold(store, "scores", "AAA", "vs_200d", END, None)                     # R1: only 'score:<metric>' names a score
    assert _scores(store, "AAA", "valuation_scores")["vs_200d"]["status"] == "ok"


def test_view_star_holds_on_price_rows(store, cfg):
    _built(store, cfg)
    _hold(store, "figures", "AAA", "*", END, None)                         # a statements hold: price rows stay
    assert _scores(store, "AAA", "valuation_scores")["vs_200d"]["status"] == "ok"
    _hold(store, "impossible", "AAA", "*", END, END)                       # a price hold: price rows held
    rows = _scores(store, "AAA", "valuation_scores")
    assert all(rows[m]["status"] == "held" and rows[m]["value"] is None for m in ("off_high_52w", "vs_200d"))
    assert rows["week_vs_sector"]["status"] == "unsupported"               # not ok: keeps its own status and reason
    assert rows["week_vs_sector"]["status_reason"] == "no sector" and rows["week_vs_sector"]["hold_ids"]


def test_view_ignores_cleared_warn_and_out_of_range_holds(store, cfg):
    _built(store, cfg)
    _hold(store, "closes", "AAA", "close", "2026-10-01", "2026-10-28")    # ended before the row's date
    store.check_record("prices2", [Finding("prices2", "AAA", "close", END, END, "warn", "only a warning")], NOW_S)
    _hold(store, "impossible", "AAA", "close", END, END)
    store.check_record("impossible", [], "2026-10-30T13:00:00Z")            # cleared
    assert _scores(store, "AAA", "valuation_scores")["vs_200d"]["status"] == "ok"


# ---- what gets scored, and when -------------------------------------------------------------------------------------

def test_benchmark_only_symbols_get_no_scores_but_a_held_benchmark_does(store, cfg):
    _aaa(store)
    _spy(store)
    _weekday_bars(store, "XLK", asset_type="etf")
    store.mark_benchmarks(["SPY", "XLK"])
    _held_position(store, "XLK")
    _cover(store)
    rep = engine.run(store, cfg)
    syms = {r[0] for r in store.query("SELECT DISTINCT symbol FROM valuation_scores_raw")}
    assert syms == {"AAA", "XLK"} and rep.symbols == 2
    assert {r["symbol"]: r["held"] for r in store.engine_symbols()} == {"AAA": 0, "XLK": 1}


def test_parked_cash_crypto_and_unpriced_symbols_are_left_out(store, cfg):
    _aaa(store)
    _weekday_bars(store, "BBB")
    store.park("BBB", "no bars from any provider")
    _weekday_bars(store, "CORE", asset_type="money_market")
    _weekday_bars(store, "COIN1", asset_type="crypto")
    store.upsert_security("CCC", asset_type="stock")                       # no bars at all
    store.upsert_security("DDD", asset_type="stock")
    store.write_prices("DDD", [PriceBar(END, 1.0, 1.0)], "tiingo",
                       fetched_at=datetime(2026, 10, 29, 15, 0, tzinfo=timezone.utc))   # not final yet
    assert [r["symbol"] for r in store.engine_symbols()] == ["AAA"]


def test_day_is_the_newest_final_bar(store, cfg):
    _aaa(store)
    store.write_prices("AAA", [PriceBar("2026-10-30", 90.0, 90.0)], "tiingo",
                       fetched_at=datetime(2026, 10, 30, 15, 0, tzinfo=timezone.utc))   # intraday, not final
    _spy(store)
    _cover(store)
    engine.run(store, cfg)
    assert {r["date"] for r in _scores(store, "AAA").values()} == {END}
    assert [b[0] for b in store.final_bars("AAA")][-1] == END


def test_rows_carry_the_covering_check_runs_finish_time(store, cfg):
    _aaa(store)
    _spy(store)
    _cover(store, finished="2026-10-30T12:34:56Z")
    store.check_log_run("c-later", started_at=NOW_S, finished_at="2026-10-30T14:00:00Z", derive_run_id=None,
                        checks_run=1, holds=0, warns=0, error=None)            # a partial run later: not the cover
    engine.run(store, cfg)
    assert {r[0] for r in store.query("SELECT checked_at FROM valuation_scores_raw")} == {"2026-10-30T12:34:56Z"}


def test_second_run_changes_no_row(store, cfg):                         # same rows, same checked_at
    _aaa(store)
    _spy(store)
    _cover(store)
    engine.run(store, cfg)
    first = [tuple(r) for r in store.query("SELECT * FROM valuation_scores_raw ORDER BY symbol, metric")]
    before = store.conn.total_changes
    engine.run(store, cfg)
    second = [tuple(r) for r in store.query("SELECT * FROM valuation_scores_raw ORDER BY symbol, metric")]
    assert first == second and len(first) == 4
    assert store.conn.total_changes == before                                 # nothing rewritten


def test_a_skipped_run_keeps_the_rows_it_had(store, cfg):
    _built(store, cfg)
    store.log_run("r2", "derive", "ok", 0, "", NOW_S, NOW_S, "manual")
    rep = engine.run(store, cfg)
    assert rep.skipped == "waiting for a check run to cover derive r2"
    assert store.query("SELECT COUNT(*) FROM valuation_scores_raw")[0][0] == 4


# ---- the sync step --------------------------------------------------------------------------------------------------

def test_engine_step_message(store, cfg):
    _aaa(store)
    _spy(store)
    _cover(store)
    rows, msg, status = sync._step_engine(store, cfg, progress=lambda d: None)
    assert (rows, msg, status) == (7, "1 symbols, 4 scores (0 held), 3 moves; split: no targets file; checks: 0 holds, 0 warnings, 0 errors", "ok")


def test_engine_step_skips_without_coverage(store, cfg):
    rows, msg, status = sync._step_engine(store, cfg, progress=lambda d: None)
    assert (rows, status) == (0, "skipped") and msg.endswith("no derive has finished yet")
