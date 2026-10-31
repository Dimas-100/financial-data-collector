import sqlite3
from pathlib import Path

from financial_data_collector import migrate as M


def _conn(path: Path) -> sqlite3.Connection:
    c = sqlite3.connect(path, timeout=5.0)
    c.execute("PRAGMA journal_mode=WAL")
    return c


def _migrations(tmp_path: Path) -> Path:
    d = tmp_path / "migrations"
    d.mkdir()
    (d / "0001_one.sql").write_text("CREATE TABLE t (a INTEGER);\n", encoding="utf-8")
    (d / "0002_two.sql").write_text("ALTER TABLE t ADD COLUMN b TEXT;\nINSERT INTO t (a, b) VALUES (1, 'x');\n",
                                    encoding="utf-8")
    return d


def test_a_migration_another_opener_applied_meanwhile_is_skipped(tmp_path: Path, monkeypatch):
    """Two processes open the database at once (a sync at logon and the 07:00 check it missed): both see the same
    pending list, the first applies it, and the second must skip what is now applied instead of re-running it
    ("duplicate column name")."""
    migrations = _migrations(tmp_path)
    db = tmp_path / "w.db"
    a, b = _conn(db), _conn(db)
    stale = M.pending_migrations(b, migrations)          # what b saw before a got the lock
    assert [v for v, _ in stale] == [1, 2]
    assert M.apply_migrations(a, migrations) == [1, 2]
    monkeypatch.setattr(M, "pending_migrations", lambda conn, d: stale)
    assert M.apply_migrations(b, migrations) == []       # nothing re-run, nothing raised
    assert b.execute("SELECT a, b FROM t").fetchall() == [(1, "x")]
    assert [r[0] for r in b.execute("SELECT version FROM schema_version ORDER BY version")] == [1, 2]


def test_a_failing_migration_leaves_nothing_half_applied(tmp_path: Path):
    migrations = _migrations(tmp_path)
    (migrations / "0003_bad.sql").write_text("ALTER TABLE t ADD COLUMN c TEXT;\nSELECT * FROM no_such_table;\n",
                                             encoding="utf-8")
    c = _conn(tmp_path / "w.db")
    try:
        M.apply_migrations(c, migrations)
    except sqlite3.OperationalError:
        pass
    else:
        raise AssertionError("the bad migration should raise")
    cols = [r[1] for r in c.execute("PRAGMA table_info(t)")]
    assert cols == ["a", "b"]                              # 0003's column rolled back with it
    assert [r[0] for r in c.execute("SELECT version FROM schema_version ORDER BY version")] == [1, 2]


def test_a_statement_without_its_semicolon_is_refused_not_dropped():
    try:
        M._statements("CREATE TABLE x (a TEXT);\nINSERT INTO x VALUES ('y')\n-- trailing note\n")
    except ValueError as e:
        assert "would never run" in str(e)
    else:
        raise AssertionError("a statement with no ';' must not be dropped silently")


def test_every_shipped_migration_splits_into_whole_statements():
    for path in sorted(M.MIGRATIONS_DIR.glob("*.sql")):
        sql = path.read_text(encoding="utf-8")
        parts = M._statements(sql)
        assert parts, path.name
        assert all(sqlite3.complete_statement(p) for p in parts), path.name


def test_statements_split_on_semicolons_outside_strings():
    sql = "CREATE TABLE x (a TEXT DEFAULT 'a;b');\n-- note; here\nINSERT INTO x VALUES ('c;d');\n\n"
    assert M._statements(sql) == ["CREATE TABLE x (a TEXT DEFAULT 'a;b');",
                                  "-- note; here\nINSERT INTO x VALUES ('c;d');"]
