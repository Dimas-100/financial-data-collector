"""A version-7 warehouse becomes version 8: runs logged before it read origin 'unknown'."""
import shutil
import sqlite3
from pathlib import Path

from financial_data_collector import migrate
from financial_data_collector.store import Store


def _v7(tmp_path: Path) -> Path:
    older = tmp_path / "migrations"
    older.mkdir()
    for p in migrate.MIGRATIONS_DIR.glob("*.sql"):
        if int(p.name[:4]) < 8:
            shutil.copy(p, older)
    db = tmp_path / "w.db"
    conn = sqlite3.connect(db)
    migrate.apply_migrations(conn, migrations_dir=older)
    conn.execute("INSERT INTO sync_runs (run_id, step, started_at, finished_at, status, rows_written, message) "
                 "VALUES ('r1', 'prices', '2026-01-01T00:00:00Z', '2026-01-01T00:00:05Z', 'ok', 5, '')")
    conn.commit()
    assert conn.execute("SELECT MAX(version) FROM schema_version").fetchone()[0] == 7
    conn.close()
    return db


def test_v7_becomes_v8_and_old_runs_read_unknown(tmp_path: Path):
    s = Store.open(_v7(tmp_path))
    try:
        assert 8 in {r[0] for r in s.query("SELECT version FROM schema_version")}
        old = s.query("SELECT * FROM sync_runs")[0]
        assert old["origin"] == "unknown" and old["rows_written"] == 5
        s.log_run("r2", "prices", "ok", 1, "", "2026-01-02T00:00:00Z", "2026-01-02T00:00:01Z", origin="scheduled")
        s.log_run("r3", "prices", "ok", 1, "", "2026-01-03T00:00:00Z", "2026-01-03T00:00:01Z")
        origins = [r[0] for r in s.query("SELECT origin FROM sync_runs ORDER BY id")]
        assert origins == ["unknown", "scheduled", "manual"]
        assert s.query("SELECT status FROM sync_status WHERE step = 'prices'")[0][0] == "ok"
    finally:
        s.close()
