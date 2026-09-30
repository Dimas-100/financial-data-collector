"""A version-5 warehouse becomes version 6 with its rows and views intact."""
import shutil
import sqlite3
from pathlib import Path

from financial_data_collector import migrate
from financial_data_collector.store import Store


def _v5(tmp_path: Path) -> Path:
    older = tmp_path / "migrations"
    older.mkdir()
    for p in migrate.MIGRATIONS_DIR.glob("*.sql"):
        if not p.name.startswith("0006"):
            shutil.copy(p, older)
    db = tmp_path / "w.db"
    conn = sqlite3.connect(db)
    migrate.apply_migrations(conn, migrations_dir=older)
    conn.execute("INSERT INTO accounts (label, slug, institution, account_type, first_seen) "
                 "VALUES ('Sample Brokerage', 'brokerage', 'fidelity', 'brokerage', '2026-01-02')")
    conn.execute("INSERT INTO cash_balances (as_of_date, account_id, currency, amount, source) "
                 "VALUES ('2026-01-02', 1, 'USD', 12.5, 'fidelity_csv')")
    conn.commit()
    assert conn.execute("SELECT MAX(version) FROM schema_version").fetchone()[0] == 5
    conn.close()
    return db


def test_v5_becomes_v6_with_rows_intact(tmp_path: Path):
    db = _v5(tmp_path)
    s = Store.open(db)
    try:
        assert s.query("SELECT MAX(version) FROM schema_version")[0][0] == 6
        acct = s.query("SELECT * FROM accounts")[0]
        assert acct["label"] == "Sample Brokerage" and acct["external_key"] is None
        assert acct["origin"] == "file" and acct["kind_confirmed"] == 1 and acct["flows"] == "transactions"
        assert acct["credit_limit"] is None and acct["rate_pct"] is None
        cash = s.query("SELECT * FROM cash_balances")[0]
        assert cash["amount"] == 12.5 and cash["available"] is None
        assert s.query("SELECT total FROM account_values_daily")[0][0] == 12.5
        assert s.query("SELECT COUNT(*) FROM connections")[0][0] == 0
        assert "connections" in s.counts()
    finally:
        s.close()


def test_external_key_is_unique_only_where_set(tmp_path: Path):
    s = Store.open(tmp_path / "w.db")
    try:
        for label in ("A", "B"):
            s.conn.execute("INSERT INTO accounts (label, first_seen) VALUES (?, '2026-01-02')", (label,))
        s.conn.execute("INSERT INTO accounts (label, first_seen, external_key) VALUES ('C', '2026-01-02', 'k1')")
        s.conn.commit()
        try:
            s.conn.execute("INSERT INTO accounts (label, first_seen, external_key) VALUES ('D', '2026-01-02', 'k1')")
            assert False, "a second account with the same external_key was accepted"
        except sqlite3.IntegrityError:
            pass
    finally:
        s.close()
