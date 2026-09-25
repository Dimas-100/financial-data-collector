"""Numbered SQL migrations, the concept-map seed, and the generated wide views."""
from __future__ import annotations

import csv
import re
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

MIGRATIONS_DIR = Path(__file__).parent / "migrations"
SEEDS_DIR = Path(__file__).parent / "seeds"
_MIG_NAME = re.compile(r"^(\d{4})_.+\.sql$")


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _ensure_version_table(conn: sqlite3.Connection) -> None:
    conn.execute(
        "CREATE TABLE IF NOT EXISTS schema_version (version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL)"
    )


def pending_migrations(conn: sqlite3.Connection, migrations_dir: Path) -> list[tuple[int, Path]]:
    _ensure_version_table(conn)
    applied = {r[0] for r in conn.execute("SELECT version FROM schema_version")}
    out: list[tuple[int, Path]] = []
    for p in sorted(migrations_dir.glob("*.sql")):
        m = _MIG_NAME.match(p.name)
        if m and int(m.group(1)) not in applied:
            out.append((int(m.group(1)), p))
    return out


def _has_user_tables(conn: sqlite3.Connection) -> bool:
    n = conn.execute(
        "SELECT COUNT(*) FROM sqlite_master WHERE type='table' AND name NOT IN ('schema_version') "
        "AND name NOT LIKE 'sqlite_%'"
    ).fetchone()[0]
    return n > 0


def apply_migrations(
    conn: sqlite3.Connection, migrations_dir: Path = MIGRATIONS_DIR, backup_to: Path | None = None
) -> list[int]:
    """Apply every unapplied NNNN_*.sql in order. Returns the versions applied.

    When there is something to apply and the database already holds tables, a
    file backup is written to backup_to first (SQLite online backup API).
    """
    pending = pending_migrations(conn, migrations_dir)
    if not pending:
        return []
    if backup_to is not None and _has_user_tables(conn):
        backup_to.parent.mkdir(parents=True, exist_ok=True)
        dest = sqlite3.connect(backup_to)
        try:
            conn.backup(dest)
        finally:
            dest.close()
    applied: list[int] = []
    for version, path in pending:
        sql = path.read_text(encoding="utf-8")
        conn.executescript("BEGIN;\n" + sql + "\nCOMMIT;")
        conn.execute("INSERT INTO schema_version (version, applied_at) VALUES (?, ?)", (version, _now()))
        conn.commit()
        applied.append(version)
    return applied


def seed_concept_map(conn: sqlite3.Connection, csv_path: Path = SEEDS_DIR / "concept_map.csv") -> int:
    with open(csv_path, newline="", encoding="utf-8") as f:
        rows = [
            (r["line_item"], r["statement"], r["kind"], r["taxonomy"], r["concept"], int(r["priority"]))
            for r in csv.DictReader(f)
        ]
    with conn:
        conn.execute("DELETE FROM concept_map")
        conn.executemany(
            "INSERT INTO concept_map (line_item, statement, kind, taxonomy, concept, priority) VALUES (?,?,?,?,?,?)",
            rows,
        )
    return len(rows)


_IDENT = re.compile(r"^[a-z][a-z0-9_]*$")


def rebuild_wide_views(conn: sqlite3.Connection) -> None:
    """(Re)create financials_annual / financials_quarterly from concept_map's line items."""
    items = [r[0] for r in conn.execute("SELECT DISTINCT line_item FROM concept_map ORDER BY line_item")]
    for it in items:
        if not _IDENT.match(it):
            raise ValueError(f"line_item {it!r} is not a safe SQL identifier")
    pivot = ",\n".join(f"    MAX(CASE WHEN line_item = '{it}' THEN value END) AS {it}" for it in items)
    derived = """
    (ocf - capex) AS fcf,
    CASE WHEN revenue > 0 THEN gross_profit * 1.0 / revenue END AS gross_margin,
    CASE WHEN revenue > 0 THEN operating_income * 1.0 / revenue END AS operating_margin,
    CASE WHEN revenue > 0 THEN net_income * 1.0 / revenue END AS net_margin,
    CASE WHEN revenue > 0 THEN (ocf - capex) * 1.0 / revenue END AS fcf_margin"""
    head = """SELECT
  (SELECT MIN(symbol) FROM securities s WHERE s.cik = base.cik) AS symbol,
  (SELECT MIN(sec_name) FROM securities s WHERE s.cik = base.cik) AS company,
  base.*,"""
    for view, kind in (("financials_annual", "annual"), ("financials_quarterly", "quarter")):
        sql = f"""
CREATE VIEW {view} AS
WITH base AS (
  SELECT cik, period_end,
    MAX(fiscal_year) AS fiscal_year,
    MAX(fiscal_quarter) AS fiscal_quarter,
    MAX(is_derived) AS has_derived_items,
    MAX(filed) AS last_filed,
{pivot}
  FROM financial_line_items
  WHERE period_kind = '{kind}'
  GROUP BY cik, period_end
)
{head}{derived}
FROM base
ORDER BY cik, period_end"""
        with conn:
            conn.execute(f"DROP VIEW IF EXISTS {view}")
            conn.execute(sql)
    # Trailing twelve months, only once the TTM long view exists (migration 0003).
    has_ttm = conn.execute("SELECT 1 FROM sqlite_master WHERE name = 'financial_line_items_ttm'").fetchone()
    if has_ttm:
        sql = f"""
CREATE VIEW financials_ttm AS
WITH base AS (
  SELECT cik, period_end,
    MAX(available_from) AS available_from,
{pivot}
  FROM financial_line_items_ttm
  GROUP BY cik, period_end
)
{head}{derived}
FROM base
ORDER BY cik, period_end"""
        with conn:
            conn.execute("DROP VIEW IF EXISTS financials_ttm")
            conn.execute(sql)
