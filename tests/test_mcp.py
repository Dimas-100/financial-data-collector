import asyncio
import json
import sys
from pathlib import Path

import pytest

from financial_data_collector import mcp_server as M
from financial_data_collector.store import Store


@pytest.fixture
def db(tmp_path: Path) -> Path:
    s = Store.open(tmp_path / "w.db")
    s.upsert_security("AAPL", "APPLE INC", "stock", "2026-01-01")
    s.log_run("r", "prices", "ok", 1, "", "2026-01-01T00:00:00Z", "2026-01-01T00:00:01Z")
    s.close()
    return tmp_path / "w.db"


def test_tool_schema(db: Path):
    out = M.tool_schema(db)
    names = {t["name"] for t in out["tables"]}
    assert {"securities", "prices", "sec_facts", "financial_line_items"} <= names
    sec = next(t for t in out["tables"] if t["name"] == "securities")
    assert "symbol" in sec["columns"] and sec["rows"] == 1
    assert {"positions_latest", "portfolio_daily", "financials_annual"} <= {v["name"] for v in out["views"]}


def test_tool_query_and_guard(db: Path):
    out = M.tool_query(db, "select symbol, asset_type from securities")
    assert out == {"columns": ["symbol", "asset_type"], "rows": [["AAPL", "stock"]], "truncated": False}
    bad = M.tool_query(db, "delete from securities")
    assert "error" in bad and "not allowed" in bad["error"]


def test_tool_status(db: Path):
    out = M.tool_status(db)
    assert out["steps"][0]["step"] == "prices" and out["latest_snapshot"] is None


def test_build_server_lists_tools(db: Path):
    server = M.build_server(db)
    tools = asyncio.run(server.list_tools())
    assert {t.name for t in tools} == {"schema", "query", "status"}


def test_print_config(tmp_path: Path):
    text = M.print_config(tmp_path)
    cfg = json.loads(text)
    entry = cfg["mcpServers"][M.SERVER_NAME]
    assert entry["command"] == sys.executable
    assert entry["args"][:3] == ["-m", "financial_data_collector", "--root"]
    assert entry["args"][3] == str(tmp_path) and entry["args"][4] == "mcp"
