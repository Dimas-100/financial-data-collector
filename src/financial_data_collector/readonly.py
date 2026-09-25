"""Read-only SQL for `fdc query` and the MCP server: guard + read-only connection."""
from __future__ import annotations

import re
import sqlite3
from pathlib import Path

_ALLOWED = ("select", "with", "explain")
_FORBIDDEN = re.compile(
    r"\b(attach|detach|pragma|insert|update|delete|drop|alter|create|vacuum|reindex)\b", re.IGNORECASE
)


class UnsafeSql(ValueError):
    pass


def guard_sql(sql: str) -> str:
    s = (sql or "").strip().rstrip(";").strip()
    if not s:
        raise UnsafeSql("empty statement")
    if ";" in s:
        raise UnsafeSql("one statement at a time")
    if not s.lower().startswith(_ALLOWED):
        raise UnsafeSql("statement must start with SELECT, WITH or EXPLAIN; writes are not allowed")
    m = _FORBIDDEN.search(s)
    if m:
        raise UnsafeSql(f"{m.group(1).upper()} is not allowed on the read-only connection")
    return s


def _connect(db_path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(f"file:{Path(db_path).as_posix()}?mode=ro", uri=True, timeout=5.0)
    conn.execute("PRAGMA query_only=1")
    return conn


def run_readonly(db_path: Path, sql: str, limit: int = 500) -> tuple[list[str], list[list], bool]:
    safe = guard_sql(sql)
    conn = _connect(db_path)
    try:
        cur = conn.execute(safe)
        cols = [d[0] for d in cur.description or []]
        rows = cur.fetchmany(limit + 1)
        truncated = len(rows) > limit
        return cols, [list(r) for r in rows[:limit]], truncated
    finally:
        conn.close()
