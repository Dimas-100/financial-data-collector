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
