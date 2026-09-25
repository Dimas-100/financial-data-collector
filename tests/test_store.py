import sqlite3
from pathlib import Path

import pytest

from financial_data_collector import migrate
from financial_data_collector.models import (
    AccountRef, CashRow, Fact, LineItem, PositionRow, PriceBar, Snapshot, TransactionRow,
)
from financial_data_collector.store import Store

ACCT = AccountRef("Sample Brokerage", "brokerage", "fidelity", "brokerage")
ROTH = AccountRef("Sample Roth", "roth", "fidelity", "roth_ira")


@pytest.fixture
def store(tmp_path: Path) -> Store:
    s = Store.open(tmp_path / "w.db")
    yield s
    s.close()


def _snap(date: str, qty: float = 10.0, cash: float = 5.0) -> Snapshot:
    return Snapshot(
        as_of_date=date,
        source="fidelity_csv",
        positions=[PositionRow(ACCT, "AAPL", "APPLE INC", qty, 100.0, qty * 100.0, 900.0, 90.0, 100.0)],
        cash=[CashRow(ACCT, cash)],
    )


def test_migrate_creates_schema_and_seeds(store: Store):
    versions = [r[0] for r in store.query("SELECT version FROM schema_version ORDER BY version")]
    assert versions[0] == 1
    tables = {r[0] for r in store.query("SELECT name FROM sqlite_master WHERE type='table'")}
    assert {"accounts", "securities", "position_snapshots", "cash_balances", "transactions",
            "prices", "sec_facts", "concept_map", "financial_line_items", "sync_runs",
            "ingested_files", "schema_version"} <= tables
    assert store.query("SELECT COUNT(*) FROM concept_map")[0][0] >= 60
    cols = {r[1] for r in store.query("PRAGMA table_info(financials_annual)")}
    assert {"cik", "symbol", "company", "fiscal_year", "revenue", "total_assets", "ocf",
            "capex", "fcf", "gross_margin", "net_margin"} <= cols
    qcols = {r[1] for r in store.query("PRAGMA table_info(financials_quarterly)")}
    assert "is_derived_q4" in qcols


def test_migrate_is_idempotent(store: Store):
    assert store.migrate() == []


def test_apply_migrations_backs_up_before_pending(tmp_path: Path):
    db = tmp_path / "w.db"
    s = Store.open(db)
    s.close()
    extra = tmp_path / "mig"
    extra.mkdir()
    (extra / "0001_init.sql").write_text((migrate.MIGRATIONS_DIR / "0001_init.sql").read_text())
    (extra / "0003_more.sql").write_text("CREATE TABLE extra_t (x INTEGER);")
    conn = sqlite3.connect(db)
    backup = tmp_path / "backups" / "w-backup.db"
    applied = migrate.apply_migrations(conn, extra, backup_to=backup)
    assert applied == [3]
    assert backup.exists()
    assert conn.execute("SELECT COUNT(*) FROM extra_t").fetchone()[0] == 0
    conn.close()


def test_upsert_account_returns_same_id_and_keeps_earliest_first_seen(store: Store):
    a = store.upsert_account(ACCT, "2026-02-01")
    b = store.upsert_account(ACCT, "2026-01-01")
    assert a == b
    assert store.query("SELECT first_seen FROM accounts WHERE id=?", (a,))[0][0] == "2026-01-01"


def test_upsert_security_never_downgrades_asset_type(store: Store):
    store.upsert_security("AAPL", "APPLE INC", "stock", "2026-01-01")
    store.upsert_security("AAPL", None, None, "2026-01-02")
    row = store.query("SELECT description, asset_type FROM securities WHERE symbol='AAPL'")[0]
    assert (row[0], row[1]) == ("APPLE INC", "stock")


def test_write_snapshot_upserts(store: Store):
    assert store.write_snapshot(_snap("2026-01-02", 10)) == 2
    assert store.write_snapshot(_snap("2026-01-02", 12, 7)) == 2
    assert store.query("SELECT COUNT(*) FROM position_snapshots")[0][0] == 1
    assert store.query("SELECT quantity FROM position_snapshots")[0][0] == 12
    assert store.query("SELECT amount FROM cash_balances")[0][0] == 7
    assert store.latest_snapshot_date() == "2026-01-02"


def test_write_transactions_dedupes(store: Store):
    rows = [
        TransactionRow(ACCT, "2026-01-02", "buy", "AAPL", 1.0, 100.0, -100.0, 0.0, "YOU BOUGHT", "fidelity_csv"),
        TransactionRow(ROTH, "2026-01-03", "contribution", None, None, None, 500.0, None, "EFT", "fidelity_csv"),
    ]
    assert store.write_transactions(rows) == 2
    assert store.write_transactions(rows) == 0
    assert store.query("SELECT COUNT(*) FROM transactions")[0][0] == 2
    assert store.query("SELECT COUNT(*) FROM accounts")[0][0] == 2


def test_prices_roundtrip(store: Store):
    store.upsert_security("AAPL", first_seen="2026-01-01")
    assert store.last_price_date("AAPL") is None
    bars = [PriceBar("2026-01-02", 100.0, 99.0), PriceBar("2026-01-05", 101.0, 100.0, 0.25, 1.0)]
    assert store.write_prices("AAPL", bars, "tiingo") == 2
    assert store.last_price_date("AAPL") == "2026-01-05"
    assert store.write_prices("AAPL", bars, "tiingo") == 2
    assert store.query("SELECT COUNT(*) FROM prices")[0][0] == 2
    assert store.query("SELECT price_source FROM securities WHERE symbol='AAPL'")[0][0] == "tiingo"


def _fact(concept="Revenues", start="2025-01-01", end="2025-12-31", value=100.0, accn="a1", filed="2026-02-01"):
    return Fact("us-gaap", concept, "USD", start, end, value, 2025, "FY", "10-K", filed, accn)


def test_sec_facts_no_duplicates_on_refetch(store: Store):
    facts = [_fact(), _fact(concept="Assets", start="", value=500.0)]
    assert store.write_sec_facts("0000320193", facts) == 2
    assert store.write_sec_facts("0000320193", facts) == 2
    assert store.query("SELECT COUNT(*) FROM sec_facts")[0][0] == 2
    back = store.facts_for("0000320193")
    assert {f.concept for f in back} == {"Revenues", "Assets"}
    assert [f for f in back if f.concept == "Assets"][0].period_start == ""


def test_replace_line_items(store: Store):
    items = [LineItem("revenue", "annual", "2025-01-01", "2025-12-31", 2025, None, 100.0, "Revenues", "2026-02-01", "a1")]
    assert store.replace_line_items("0000320193", items) == 1
    assert store.replace_line_items("0000320193", items * 1) == 1
    assert store.query("SELECT COUNT(*) FROM financial_line_items")[0][0] == 1


def test_concept_rules_sorted(store: Store):
    rules = store.concept_rules()
    rev = [r for r in rules if r.line_item == "revenue"]
    assert [r.priority for r in rev] == [1, 2, 3, 4]
    assert rev[0].kind == "duration" and rev[0].statement == "income"


def test_files_and_runs(store: Store):
    assert not store.file_seen("abc")
    store.record_file("abc", "inbox/x.csv", "fidelity_positions", 3)
    assert store.file_seen("abc")
    store.log_run("r1", "prices", "ok", 5, "", "2026-01-01T00:00:00Z", "2026-01-01T00:00:05Z")
    assert store.query("SELECT step, rows_written FROM sync_runs")[0][1] == 5
    counts = store.counts()
    assert counts["ingested_files"] == 1 and counts["sync_runs"] == 1


def test_security_sec_fields(store: Store):
    store.upsert_security("AAPL", first_seen="2026-01-01")
    store.set_security_cik("AAPL", "0000320193", "Apple Inc.")
    store.mark_sec_fetch("AAPL", "2026-01-02T00:00:00Z")
    store.set_asset_type("AAPL", "stock")
    row = store.query("SELECT cik, sec_name, last_sec_fetch, asset_type FROM securities WHERE symbol='AAPL'")[0]
    assert tuple(row) == ("0000320193", "Apple Inc.", "2026-01-02T00:00:00Z", "stock")
