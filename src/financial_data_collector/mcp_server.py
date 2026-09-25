"""Read-only MCP server (stdio) so Claude Desktop can query the warehouse.

Three tools: schema, query, status. Every query runs through readonly.run_readonly:
a guarded statement on a mode=ro connection with query_only set.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

from .readonly import UnsafeSql, run_readonly
from .store import COUNT_TABLES, Store

SERVER_NAME = "financial-data-collector"


def tool_schema(db_path: Path) -> dict:
    store = Store.open(db_path, migrate=False)
    try:
        counts = store.counts()
        tables, views = [], []
        for kind, name in store.query("SELECT type, name FROM sqlite_master WHERE type IN ('table','view') "
                                      "AND name NOT LIKE 'sqlite_%' ORDER BY type, name"):
            cols = [r[1] for r in store.query(f"PRAGMA table_info({name})")]
            entry = {"name": name, "columns": cols}
            if kind == "table":
                entry["rows"] = counts.get(name, store.query(f"SELECT COUNT(*) FROM {name}")[0][0])
                tables.append(entry)
            else:
                views.append(entry)
        return {"database": str(db_path), "tables": tables, "views": views,
                "hint": "Start from positions_latest, portfolio_daily, holdings_history, prices, "
                        "financials_annual, financials_quarterly. Dates are ISO text."}
    finally:
        store.close()


def tool_query(db_path: Path, sql: str, limit: int = 500) -> dict:
    try:
        cols, rows, truncated = run_readonly(db_path, sql, limit=max(1, min(int(limit), 5000)))
    except UnsafeSql as e:
        return {"error": f"refused: {e}"}
    except Exception as e:  # sqlite errors back to the model, never a crash
        return {"error": f"{type(e).__name__}: {e}"}
    return {"columns": cols, "rows": rows, "truncated": truncated}


def tool_status(db_path: Path) -> dict:
    store = Store.open(db_path, migrate=False)
    try:
        steps = [dict(r) for r in store.query("SELECT * FROM sync_status ORDER BY step")]
        return {"steps": steps, "latest_snapshot": store.latest_snapshot_date(),
                "rows": {t: c for t, c in store.counts().items() if t in COUNT_TABLES}}
    finally:
        store.close()


def build_server(db_path: Path):
    from mcp.server.mcpserver import MCPServer  # mcp SDK 2.x (FastMCP was renamed to MCPServer)

    mcp = MCPServer(SERVER_NAME)

    @mcp.tool()
    def schema() -> dict:
        """List tables and views with their columns and row counts."""
        return tool_schema(db_path)

    @mcp.tool()
    def query(sql: str, limit: int = 500) -> dict:
        """Run one read-only SELECT (or WITH / EXPLAIN). Returns columns, rows (capped at limit), truncated flag."""
        return tool_query(db_path, sql, limit)

    @mcp.tool()
    def status() -> dict:
        """Last sync run per step, latest snapshot date and row counts."""
        return tool_status(db_path)

    return mcp


def serve(db_path: Path) -> None:
    build_server(db_path).run()


def print_config(root: Path) -> str:
    entry = {"command": sys.executable,
             "args": ["-m", "financial_data_collector", "--root", str(root), "mcp"]}
    return json.dumps({"mcpServers": {SERVER_NAME: entry}}, indent=2)
