import json
from pathlib import Path

import pytest

from financial_data_collector import statements
from financial_data_collector.collectors.sec_facts import parse_company_facts
from financial_data_collector.models import AccountRef, CashRow, PositionRow, Snapshot
from financial_data_collector.store import Store

FID = AccountRef("Sample Brokerage", "brokerage", "fidelity", "brokerage")
WEB = AccountRef("Webull Sample Cash", "brokerage", "webull", "brokerage")


@pytest.fixture
def store(tmp_path: Path):
    s = Store.open(tmp_path / "w.db")
    s.write_snapshot(Snapshot("2026-01-10", "snaptrade",
        [PositionRow(FID, "AAPL", "APPLE INC", 10, 99.0, 990.0), PositionRow(WEB, "KO", "COCA COLA CO", 2, 50.0, 100.0)],
        [CashRow(FID, 20.0), CashRow(WEB, 5.0)]))
    s.write_snapshot(Snapshot("2026-01-16", "snaptrade",
        [PositionRow(FID, "AAPL", "APPLE INC", 10, 101.0, 1010.0), PositionRow(FID, "KO", "COCA COLA CO", 1, 50.0, 50.0)],
        [CashRow(FID, 25.5)]))
    yield s
    s.close()


def test_positions_latest_per_account(store: Store):
    rows = store.query("SELECT account, symbol, as_of_date FROM positions_latest ORDER BY account, symbol")
    assert [tuple(r) for r in rows] == [
        ("Sample Brokerage", "AAPL", "2026-01-16"), ("Sample Brokerage", "KO", "2026-01-16"),
        ("Webull Sample Cash", "KO", "2026-01-10")]


def test_holdings_history_sums_accounts(store: Store):
    rows = store.query("SELECT as_of_date, symbol, quantity, market_value FROM holdings_history WHERE symbol='KO' ORDER BY 1")
    assert [tuple(r) for r in rows] == [("2026-01-10", "KO", 2.0, 100.0), ("2026-01-16", "KO", 1.0, 50.0)]


def test_account_values_and_portfolio_daily(store: Store):
    rows = store.query("SELECT as_of_date, account, holdings_value, cash, total FROM account_values_daily ORDER BY 1, 2")
    assert [tuple(r) for r in rows] == [
        ("2026-01-10", "Sample Brokerage", 990.0, 20.0, 1010.0),
        ("2026-01-10", "Webull Sample Cash", 100.0, 5.0, 105.0),
        ("2026-01-16", "Sample Brokerage", 1060.0, 25.5, 1085.5)]
    p = store.query("SELECT as_of_date, total, fidelity_total, accounts FROM portfolio_daily ORDER BY 1")
    assert [tuple(r) for r in p] == [("2026-01-10", 1115.0, 1010.0, 2), ("2026-01-16", 1085.5, 1085.5, 1)]


def test_sync_status_latest_per_step(store: Store):
    store.log_run("r1", "prices", "error", 0, "boom", "2026-01-01T00:00:00Z", "2026-01-01T00:00:01Z")
    store.log_run("r2", "prices", "ok", 3, "", "2026-01-02T00:00:00Z", "2026-01-02T00:00:01Z")
    store.log_run("r2", "sec", "ok", 1, "", "2026-01-02T00:00:00Z", "2026-01-02T00:00:02Z")
    rows = {r["step"]: r for r in store.query("SELECT * FROM sync_status")}
    assert rows["prices"]["status"] == "ok" and rows["prices"]["rows_written"] == 3
    assert rows["sec"]["age_hours"] > 0


def test_wide_financial_views(store: Store, fixtures: Path):
    facts = parse_company_facts(json.loads((fixtures / "sec" / "companyfacts_SAMPLE.json").read_text()))
    store.upsert_security("AAPL", "APPLE INC", "stock", "2026-01-01")
    store.set_security_cik("AAPL", "0000000001", "Sample Corp")
    store.write_sec_facts("0000000001", facts)
    store.replace_line_items("0000000001", statements.rebuild(facts, store.concept_rules()))
    a = store.query("SELECT symbol, company, fiscal_year, revenue, ocf, capex, fcf, fcf_margin, total_assets, eps_diluted "
                    "FROM financials_annual WHERE fiscal_year = 2025")[0]
    assert tuple(a)[:7] == ("AAPL", "Sample Corp", 2025, 1100.0, 250.0, 40.0, 210.0)
    assert abs(a["fcf_margin"] - 210 / 1100) < 1e-9 and a["total_assets"] == 5500.0 and a["eps_diluted"] == 2.0
    q = {r["period_end"]: r for r in store.query("SELECT * FROM financials_quarterly WHERE fiscal_year = 2025")}
    assert [q[e]["revenue"] for e in sorted(q)] == [260.0, 270.0, 280.0, 290.0]
    assert q["2025-06-30"]["ocf"] == 60.0 and q["2025-06-30"]["has_derived_items"] == 1
    assert q["2025-03-31"]["has_derived_items"] == 0 and q["2025-03-31"]["fiscal_quarter"] == 1
    assert q["2025-12-31"]["total_assets"] == 5500.0 and q["2025-12-31"]["gross_margin"] is None


# ---- valuation_latest applies the checker's open holds ----------------------------------------------

OLD_VALUATION_COLUMNS = ["symbol", "date", "close", "shares", "market_cap", "revenue_ttm", "net_income_ttm", "ocf_ttm",
                         "fcf_ttm", "eps_ttm", "pe", "ps", "p_fcf", "dividends_12m", "dividend_yield",
                         "ttm_period_end", "available_from"]


def _val_row(store: Store, symbol: str = "KO", date: str = "2026-10-29", status: str = "ok",
             reason: str | None = None) -> None:
    store.conn.execute(
        "INSERT INTO valuation_daily (symbol, date, close, shares, market_cap, revenue_ttm, net_income_ttm, ocf_ttm, "
        "fcf_ttm, eps_ttm, pe, ps, p_fcf, dividends_12m, dividend_yield, ttm_period_end, available_from, status, "
        "status_reason) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (symbol, date, 60.0, 4000.0, 240000.0, 46000.0, 12000.0, 13000.0, 11000.0, 3.0, 20.0, 5.2, 21.8, 1.9, 0.03,
         "2026-06-30", "2026-08-01", status, reason))
    store.conn.commit()


def _hold(store: Store, field: str = "pe", date_from: str = "2026-10-01", date_to: str | None = None,
          severity: str = "hold", symbol: str = "KO", check: str = "c1", message: str = "pe looks wrong",
          cleared_at: str | None = None) -> int:
    cur = store.conn.execute(
        "INSERT INTO check_findings (check_name, symbol, field, date_from, date_to, severity, message, evidence, "
        "first_seen, last_seen, cleared_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        (check, symbol, field, date_from, date_to, severity, message, "{}", "2026-10-29T10:00:00Z",
         "2026-10-29T10:00:00Z", cleared_at))
    store.conn.commit()
    return cur.lastrowid


def test_open_hold_nulls_its_field_and_says_held(store: Store):
    _val_row(store)
    fid = _hold(store)
    r = store.query("SELECT pe, ps, status, status_reason, hold_ids FROM valuation_latest WHERE symbol='KO'")[0]
    assert r["pe"] is None and r["ps"] == 5.2 and r["status"] == "held" and r["hold_ids"] == str(fid)
    assert r["status_reason"] == "pe looks wrong"
    assert store.query("SELECT pe FROM valuation_daily WHERE symbol='KO'")[0][0] == 20.0   # the raw table is untouched


def test_star_hold_nulls_every_value_but_close(store: Store):
    _val_row(store)
    _hold(store, field="*")
    r = store.query("SELECT * FROM valuation_latest WHERE symbol='KO'")[0]
    nulled = ["shares", "market_cap", "revenue_ttm", "net_income_ttm", "ocf_ttm", "fcf_ttm", "eps_ttm", "pe", "ps",
              "p_fcf", "dividends_12m", "dividend_yield"]
    assert all(r[c] is None for c in nulled)
    assert r["symbol"] == "KO" and r["date"] == "2026-10-29" and r["close"] == 60.0
    assert r["ttm_period_end"] == "2026-06-30" and r["available_from"] == "2026-08-01" and r["status"] == "held"


def test_cleared_hold_restores_the_value(store: Store):
    _val_row(store)
    _hold(store, cleared_at="2026-10-29T12:00:00Z")
    r = store.query("SELECT pe, status, hold_ids FROM valuation_latest WHERE symbol='KO'")[0]
    assert r["pe"] == 20.0 and r["status"] == "ok" and r["hold_ids"] is None


def test_hold_outside_its_dates_does_not_apply(store: Store):
    _val_row(store)
    _hold(store, date_from="2026-09-01", date_to="2026-10-28")
    _hold(store, field="ps", date_from="2026-10-30")
    r = store.query("SELECT pe, ps, status, hold_ids FROM valuation_latest WHERE symbol='KO'")[0]
    assert r["pe"] == 20.0 and r["ps"] == 5.2 and r["status"] == "ok" and r["hold_ids"] is None


def test_warn_and_info_never_null_anything(store: Store):
    _val_row(store)
    _hold(store, severity="warn")
    _hold(store, severity="info", field="*", check="c2")
    r = store.query("SELECT pe, status, hold_ids FROM valuation_latest WHERE symbol='KO'")[0]
    assert r["pe"] == 20.0 and r["status"] == "ok" and r["hold_ids"] is None


def test_other_symbols_holds_do_not_apply(store: Store):
    _val_row(store)
    _hold(store, symbol="PEP")
    assert store.query("SELECT pe FROM valuation_latest WHERE symbol='KO'")[0][0] == 20.0


def test_stale_row_keeps_its_status_but_still_lists_the_hold(store: Store):
    _val_row(store, status="stale", reason="price is old")
    fid = _hold(store)
    r = store.query("SELECT pe, status, status_reason, hold_ids FROM valuation_latest WHERE symbol='KO'")[0]
    assert r["pe"] is None and r["status"] == "stale" and r["status_reason"] == "price is old"
    assert r["hold_ids"] == str(fid)


def test_two_holds_join_messages_and_ids_in_id_order(store: Store):
    _val_row(store)
    a = _hold(store, field="pe", message="first", check="a")
    b = _hold(store, field="ps", message="second", check="b")
    r = store.query("SELECT pe, ps, status_reason, hold_ids FROM valuation_latest WHERE symbol='KO'")[0]
    assert r["pe"] is None and r["ps"] is None
    assert r["status_reason"] == "first; second" and r["hold_ids"] == f"{a},{b}"


def test_only_the_newest_row_per_symbol_shows(store: Store):
    _val_row(store, date="2026-10-28")
    _val_row(store, date="2026-10-29")
    _hold(store, date_from="2026-10-01", date_to="2026-10-28")
    rows = store.query("SELECT date, pe, status FROM valuation_latest WHERE symbol='KO'")
    assert [tuple(r) for r in rows] == [("2026-10-29", 20.0, "ok")]


def test_checked_at_is_null_until_a_check_covers_the_newest_derive(store: Store):
    _val_row(store)
    assert store.query("SELECT checked_at FROM valuation_latest")[0][0] is None            # no derive, no check
    store.log_run("r1", "derive", "ok", 1, "", "2026-10-29T09:00:00Z", "2026-10-29T09:01:00Z")
    assert store.query("SELECT checked_at FROM valuation_latest")[0][0] is None            # derived, never checked
    store.check_log_run("c1", started_at="2026-10-29T09:02:00Z", finished_at="2026-10-29T09:03:00Z",
                        derive_run_id="r1", checks_run=1, holds=0, warns=0, error=None)
    assert store.query("SELECT checked_at FROM valuation_latest")[0][0] == "2026-10-29T09:03:00Z"
    store.log_run("r2", "derive", "ok", 1, "", "2026-10-29T10:00:00Z", "2026-10-29T10:01:00Z")
    assert store.query("SELECT checked_at FROM valuation_latest")[0][0] is None            # a newer derive is unchecked
    store.log_run("r3", "derive", "error", 0, "boom", "2026-10-29T11:00:00Z", "2026-10-29T11:01:00Z")
    assert store.query("SELECT checked_at FROM valuation_latest")[0][0] is None            # r2 is still the newest ok
    store.check_log_run("c2", started_at="2026-10-29T10:02:00Z", finished_at="2026-10-29T10:03:00Z",
                        derive_run_id="r2", checks_run=1, holds=0, warns=0, error=None)
    assert store.query("SELECT checked_at FROM valuation_latest")[0][0] == "2026-10-29T10:03:00Z"


def test_valuation_latest_keeps_every_old_column_name(store: Store):
    cols = [r[1] for r in store.query("PRAGMA table_info(valuation_latest)")]
    assert cols[:len(OLD_VALUATION_COLUMNS)] == OLD_VALUATION_COLUMNS
    assert cols[len(OLD_VALUATION_COLUMNS):] == ["status", "status_reason", "hold_ids", "checked_at"]
