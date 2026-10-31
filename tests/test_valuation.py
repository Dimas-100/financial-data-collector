import json
from datetime import datetime, timezone
from pathlib import Path

from financial_data_collector import statements
from financial_data_collector.collectors.sec_facts import parse_company_facts
from financial_data_collector.derive import valuation as V
from financial_data_collector.models import PriceBar
from financial_data_collector.store import Store

TTM1 = dict(period_end="2025-03-31", available_from="2025-05-01", revenue=1000.0, net_income=100.0, ocf=150.0,
            fcf=120.0, eps_diluted=2.0, shares_outstanding=50.0, shares_diluted=52.0)
TTM2 = dict(period_end="2025-06-30", available_from="2025-08-01", revenue=1200.0, net_income=120.0, ocf=160.0,
            fcf=-10.0, eps_diluted=None, shares_outstanding=None, shares_diluted=48.0)


def _rows(rows):
    return [dict(zip(V.COLUMNS, r)) for r in rows]


def test_point_in_time_selection():
    prices = [("2025-04-30", 10.0, 0.0, 1.0), ("2025-05-01", 20.0, 0.0, 1.0), ("2025-08-01", 24.0, 0.0, 1.0)]
    rows = _rows(V.build("AAPL", prices, [TTM1, TTM2]))
    assert [r["date"] for r in rows] == ["2025-05-01", "2025-08-01"]       # nothing before the first filing
    r1, r2 = rows
    assert r1["symbol"] == "AAPL" and r1["ttm_period_end"] == "2025-03-31" and r1["available_from"] == "2025-05-01"
    assert (r1["shares"], r1["market_cap"], r1["revenue_ttm"], r1["fcf_ttm"]) == (50.0, 1000.0, 1000.0, 120.0)
    assert (r1["eps_ttm"], r1["pe"], r1["ps"]) == (2.0, 10.0, 1.0) and abs(r1["p_fcf"] - 1000 / 120) < 1e-9
    assert r2["ttm_period_end"] == "2025-06-30" and r2["shares"] == 48.0     # falls back to diluted shares
    assert abs(r2["eps_ttm"] - 120 / 48) < 1e-9 and abs(r2["pe"] - 24 / (120 / 48)) < 1e-9
    assert r2["p_fcf"] is None                                               # negative free cash flow


def test_null_rules():
    ttm = dict(TTM1, revenue=0.0, eps_diluted=-1.0, fcf=0.0)
    (r,) = _rows(V.build("X", [("2025-05-01", 10.0, 0.0, 1.0)], [ttm]))
    assert r["pe"] is None and r["ps"] is None and r["p_fcf"] is None and r["market_cap"] == 500.0


def test_dividends_trailing_window():
    prices = [("2025-05-01", 10.0, 1.0, 1.0), ("2025-11-01", 10.0, 0.5, 1.0), ("2026-04-30", 10.0, 0.0, 1.0), ("2026-05-02", 10.0, 0.0, 1.0)]
    by = {r["date"]: r for r in _rows(V.build("X", prices, [TTM1]))}
    assert by["2025-11-01"]["dividends_12m"] == 1.5 and abs(by["2025-11-01"]["dividend_yield"] - 0.15) < 1e-9
    assert by["2026-04-30"]["dividends_12m"] == 1.5      # 364 days back is still inside the window
    assert by["2026-05-02"]["dividends_12m"] == 0.5      # 366 days back has dropped out


def test_no_ttm_no_rows():
    assert V.build("X", [("2025-05-01", 10.0, 0.0, 1.0)], []) == []


def test_rebuild_writes_only_symbols_with_statements(tmp_path: Path, fixtures: Path):
    s = Store.open(tmp_path / "w.db")
    facts = parse_company_facts(json.loads((fixtures / "sec" / "companyfacts_SAMPLE.json").read_text()))
    s.upsert_security("AAPL", "APPLE INC", "stock", "2026-01-01")
    s.set_security_cik("AAPL", "0000000001", "Sample Corp")
    s.write_sec_facts("0000000001", facts)
    s.replace_line_items("0000000001", statements.rebuild(facts, s.concept_rules()))
    s.upsert_security("KO", "COCA COLA CO", "stock", "2026-01-01")
    for sym in ("AAPL", "KO"):
        s.write_prices(sym, [PriceBar("2026-01-15", 100.0, 100.0), PriceBar("2026-02-02", 110.0, 110.0, 1.0)], "tiingo")
    n = V.rebuild(s)
    rows = s.query("SELECT symbol, date, revenue_ttm, shares, pe FROM valuation_daily ORDER BY symbol, date")
    # 2026-01-15 already has the Q3-2025 10-Q window in force (1090); the 10-K window (1100) starts 2026-02-01
    assert n == 1 and [tuple(r)[:3] for r in rows] == [("AAPL", "2026-01-15", 1090.0), ("AAPL", "2026-02-02", 1100.0)]
    assert rows[0]["shares"] is None                                                     # no share count until the 10-K
    assert rows[1]["shares"] == 990.0 and rows[1]["pe"] is None                          # no four-quarter EPS in the fixture
    assert s.query("SELECT COUNT(*) FROM valuation_latest")[0][0] == 1
    s.close()


def test_split_after_period_end_scales_shares_eps_and_dividends():
    prices = [("2025-05-01", 100.0, 1.0, 1.0), ("2025-06-02", 50.0, 0.0, 2.0)]      # 2:1 split on 06-02
    ttm = dict(TTM1, shares_outstanding=50.0, eps_diluted=2.0)
    by = {r["date"]: r for r in _rows(V.build("X", prices, [ttm]))}
    assert (by["2025-05-01"]["shares"], by["2025-05-01"]["market_cap"], by["2025-05-01"]["pe"]) == (50.0, 5000.0, 50.0)
    assert (by["2025-06-02"]["shares"], by["2025-06-02"]["market_cap"], by["2025-06-02"]["eps_ttm"], by["2025-06-02"]["pe"]) == (100.0, 5000.0, 1.0, 50.0)
    assert by["2025-06-02"]["dividends_12m"] == 0.5                                  # the pre-split dividend in post-split share terms


def test_rebuild_covers_every_share_class_of_a_cik(tmp_path: Path, fixtures: Path):
    s = Store.open(tmp_path / "w.db")
    facts = parse_company_facts(json.loads((fixtures / "sec" / "companyfacts_SAMPLE.json").read_text()))
    for sym in ("BRK.A", "BRK.B"):
        s.upsert_security(sym, "SAMPLE HOLDING", "stock", "2026-01-01")
        s.set_security_cik(sym, "0000000001", "Sample Corp")
        s.write_prices(sym, [PriceBar("2026-02-02", 10.0 if sym == "BRK.B" else 15000.0, 10.0)], "tiingo")
    s.write_sec_facts("0000000001", facts)
    s.replace_line_items("0000000001", statements.rebuild(facts, s.concept_rules()))
    assert V.rebuild(s) == 2
    assert [r[0] for r in s.query("SELECT symbol FROM valuation_latest ORDER BY 1")] == ["BRK.A", "BRK.B"]
    s.close()


def test_valuation_ignores_non_final_bars(tmp_path: Path, fixtures: Path):
    s = Store.open(tmp_path / "w.db")
    facts = parse_company_facts(json.loads((fixtures / "sec" / "companyfacts_SAMPLE.json").read_text()))
    s.upsert_security("AAPL", "APPLE INC", "stock", "2026-01-01")
    s.set_security_cik("AAPL", "0000000001", "Sample Corp")
    s.write_sec_facts("0000000001", facts)
    s.replace_line_items("0000000001", statements.rebuild(facts, s.concept_rules()))
    s.write_prices("AAPL", [PriceBar("2026-01-15", 100.0, 100.0)], "tiingo")
    s.write_prices("AAPL", [PriceBar("2026-02-02", 110.0, 110.0)], "yfinance",
                   fetched_at=datetime(2026, 2, 2, 19, 37, tzinfo=timezone.utc))        # 14:37 in New York: still intraday
    V.rebuild(s)
    rows = s.query("SELECT date, status, status_reason FROM valuation_daily")
    assert [tuple(r) for r in rows] == [("2026-01-15", "ok", None)]
    s.close()


def test_ttm_older_than_15_months_is_stale_not_a_ratio():
    ttm = dict(TTM1, period_end="2012-12-31", available_from="2013-04-02")
    (r,) = _rows(V.build("X", [("2026-10-29", 11.35, 0.0, 1.0)], [ttm]))
    assert r["status"] == "stale" and r["pe"] is None and r["ps"] is None and r["p_fcf"] is None
    assert r["status_reason"] == "newest statements end 2012-12-31"
    assert r["close"] == 11.35 and r["ttm_period_end"] == "2012-12-31" and r["available_from"] == "2013-04-02"
    for col in ("shares", "market_cap", "revenue_ttm", "net_income_ttm", "ocf_ttm", "fcf_ttm", "eps_ttm"):
        assert r[col] is None, col


def test_stale_row_keeps_the_price_side_dividends():
    ttm = dict(TTM1, period_end="2012-12-31", available_from="2013-04-02")
    prices = [("2026-09-01", 10.0, 0.5, 1.0), ("2026-10-29", 10.0, 0.0, 1.0)]
    r = _rows(V.build("X", prices, [ttm]))[-1]
    assert r["status"] == "stale" and r["dividends_12m"] == 0.5 and abs(r["dividend_yield"] - 0.05) < 1e-9


def test_stale_boundary_is_15_calendar_months_with_the_day_clamped():
    ttm = dict(TTM1, period_end="2025-11-30", available_from="2026-01-15")
    prices = [("2027-02-28", 10.0, 0.0, 1.0), ("2027-03-01", 10.0, 0.0, 1.0)]
    by = {r["date"]: r for r in _rows(V.build("X", prices, [ttm]))}
    assert by["2027-02-28"]["status"] == "ok" and by["2027-02-28"]["status_reason"] is None   # 11-30 + 15 months = 02-28
    assert by["2027-03-01"]["status"] == "stale"


def test_stale_recovers_when_a_new_filing_lands():
    old = dict(TTM1, period_end="2024-03-31", available_from="2024-05-01")
    new = dict(TTM1, period_end="2025-06-30", available_from="2025-08-01")
    prices = [("2025-07-15", 10.0, 0.0, 1.0), ("2025-08-01", 10.0, 0.0, 1.0)]
    by = {r["date"]: r for r in _rows(V.build("X", prices, [old, new]))}
    assert by["2025-07-15"]["status"] == "stale" and by["2025-08-01"]["status"] == "ok"


def test_foreign_filer_and_fund_get_unsupported_rows(tmp_path: Path, fixtures: Path):
    s = Store.open(tmp_path / "w.db")
    for sym, kind, cik in (("SAMPLEF", "foreign_filer", "0000000002"), ("SAMPLEX", "fund", "0000000003")):
        s.upsert_security(sym, "SAMPLE", kind, "2026-01-01")
        s.set_security_cik(sym, cik, "Sample")
        s.write_prices(sym, [PriceBar("2026-01-15", 10.0, 10.0), PriceBar("2026-02-02", 11.35, 11.35, 0.4)], "tiingo")
    assert V.rebuild(s) == 2
    rows = s.query("SELECT * FROM valuation_latest ORDER BY symbol")
    reasons = {r["symbol"]: r["status_reason"] for r in rows}
    assert reasons == {"SAMPLEF": "foreign filer: IFRS statements not read yet", "SAMPLEX": "fund: no company statements"}
    for r in rows:
        assert r["status"] == "unsupported" and r["close"] == 11.35 and r["date"] == "2026-02-02"
        assert all(r[c] is None for c in V.COLUMNS if c not in ("symbol", "date", "close", "status", "status_reason"))
    assert s.query("SELECT COUNT(*) FROM valuation_daily")[0][0] == 4
    s.close()
