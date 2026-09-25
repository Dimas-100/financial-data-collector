import json
from pathlib import Path

import pytest

from financial_data_collector import statements
from financial_data_collector.collectors.sec_facts import parse_company_facts
from financial_data_collector.models import LineItem
from financial_data_collector.store import Store

CIK = "0000000001"


@pytest.fixture
def store(tmp_path: Path, fixtures: Path):
    s = Store.open(tmp_path / "w.db")
    facts = parse_company_facts(json.loads((fixtures / "sec" / "companyfacts_SAMPLE.json").read_text()))
    s.upsert_security("AAPL", "APPLE INC", "stock", "2026-01-01")
    s.set_security_cik("AAPL", CIK, "Sample Corp")
    s.write_sec_facts(CIK, facts)
    s.replace_line_items(CIK, statements.rebuild(facts, s.concept_rules()))
    yield s
    s.close()


def _ttm(store, item, end):
    rows = store.query("SELECT value, n_quarters, available_from FROM financial_line_items_ttm "
                       "WHERE cik=? AND line_item=? AND period_end=?", (CIK, item, end))
    return tuple(rows[0]) if rows else None


def test_ttm_sums_four_consecutive_quarters_and_passes_instants(store: Store):
    assert _ttm(store, "revenue", "2025-12-31") == (1100.0, 4, "2026-02-01")
    assert _ttm(store, "revenue", "2025-09-30") == (280.0 + 260 + 270 + 280, 4, "2025-11-01")   # uses the 2024 Q4 from the 10-K
    assert _ttm(store, "revenue", "2024-12-31") == (230.0 + 240 + 250 + 280, 4, "2025-11-01")   # newest filing in the window is the Q3-2025 10-Q
    assert _ttm(store, "revenue", "2024-09-30") is None                                          # only three quarters exist before it
    assert _ttm(store, "ocf", "2025-12-31") == (250.0, 4, "2026-02-01")                           # derived Q2-Q4 count
    assert _ttm(store, "ocf", "2025-09-30") is None                                               # 2024 has no quarterly OCF
    assert _ttm(store, "eps_diluted", "2025-12-31") is None                                       # Q4 EPS never reported
    assert _ttm(store, "total_assets", "2025-06-30") == (5200.0, 1, "2025-08-01")                 # instant passes through
    assert _ttm(store, "shares_outstanding", "2025-12-31") == (990.0, 1, "2026-02-01")


def test_ttm_rejects_gapped_window(store: Store):
    items = [LineItem("revenue", "quarter", "", e, 2025, q, 100.0, "Revenues", "2026-01-01", "x")
             for e, q in (("2025-03-31", 1), ("2025-06-30", 2), ("2025-12-31", 4), ("2026-03-31", 1))]
    store.replace_line_items("0000000009", items)
    assert store.query("SELECT COUNT(*) FROM financial_line_items_ttm WHERE cik='0000000009'")[0][0] == 0


def test_financials_ttm_wide_has_available_from(store: Store):
    row = store.query("SELECT symbol, revenue, ocf, capex, fcf, total_assets, available_from FROM financials_ttm "
                      "WHERE cik=? AND period_end='2025-12-31'", (CIK,))[0]
    assert tuple(row)[:3] == ("AAPL", 1100.0, 250.0)
    assert row["total_assets"] == 5500.0 and row["available_from"] == "2026-02-01"
    assert row["capex"] is None and row["fcf"] is None                     # capex has no quarterly rows in the fixture


def test_new_tables_and_views_exist(store: Store):
    names = {r[0] for r in store.query("SELECT name FROM sqlite_master WHERE type IN ('table','view')")}
    assert {"holdings_daily", "cash_daily", "lots", "realized_gains", "reconciliation", "valuation_daily",
            "financial_line_items_ttm", "financials_ttm", "valuation_latest", "portfolio_daily_full"} <= names
    assert [r[0] for r in store.query("SELECT version FROM schema_version ORDER BY 1")] == [1, 2, 3]
