import sqlite3
from pathlib import Path

import pytest

from financial_data_collector import readonly as R
from financial_data_collector.store import Store


@pytest.mark.parametrize("sql", [
    "SELECT 1", "  select * from prices ", "WITH t AS (SELECT 1 x) SELECT * FROM t",
    "EXPLAIN QUERY PLAN SELECT 1", "select 1;", "select replace('a','a','b')",
])
def test_guard_accepts(sql):
    assert R.guard_sql(sql)


@pytest.mark.parametrize("sql", [
    "DELETE FROM prices", "pragma journal_mode=delete", "ATTACH 'x.db' AS y",
    "select 1; drop table prices", "insert into prices values (1)", "update prices set close=0",
    "WITH t AS (SELECT 1) DELETE FROM prices", "", "   ",
])
def test_guard_rejects(sql):
    with pytest.raises(R.UnsafeSql):
        R.guard_sql(sql)


def test_run_readonly_caps_and_truncates(tmp_path: Path):
    s = Store.open(tmp_path / "w.db")
    s.upsert_security("AAPL", first_seen="2026-01-01")
    s.upsert_security("KO", first_seen="2026-01-01")
    s.close()
    cols, rows, truncated = R.run_readonly(tmp_path / "w.db", "select symbol from securities order by 1", limit=1)
    assert cols == ["symbol"] and rows == [["AAPL"]] and truncated is True
    cols, rows, truncated = R.run_readonly(tmp_path / "w.db", "select symbol from securities order by 1", limit=5)
    assert len(rows) == 2 and truncated is False


def test_run_readonly_cannot_write_even_if_guard_bypassed(tmp_path: Path):
    Store.open(tmp_path / "w.db").close()
    with pytest.raises(sqlite3.OperationalError):
        R._connect(tmp_path / "w.db").execute("DELETE FROM securities")


def test_open_store_readonly(tmp_path):
    import sqlite3
    from financial_data_collector import readonly as RO
    from financial_data_collector.store import Store

    Store.open(tmp_path / "w.db").close()
    s = RO.open_store(tmp_path / "w.db")
    try:
        assert s.query("SELECT COUNT(*) FROM securities")[0][0] == 0
        assert s.counts()["accounts"] == 0
        try:
            s.upsert_security("AAPL", first_seen="2026-01-01")
        except sqlite3.OperationalError as e:
            assert "readonly" in str(e) or "read-only" in str(e)
        else:
            raise AssertionError("a read-only store accepted a write")
    finally:
        s.close()
