"""fdc command line."""
from __future__ import annotations

import argparse
import csv
import sys
from datetime import datetime, timezone
from pathlib import Path

from rich.text import Text

from . import __version__, ui
from .config import ConfigError, init_project, load_config
from .ingest import ingest_file
from .readonly import UnsafeSql, run_readonly
from .store import Store
from .sync import STEPS, run_sync

STALE_HOURS = {"ingest": 48, "prices": 48, "sec": 336, "derive": 48, "export": 48}


def _parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="fdc", description="Local SQLite warehouse for your brokerage positions.")
    p.add_argument("--root", default=".", help="project folder holding config.toml (default: current directory)")
    p.add_argument("--version", action="version", version=f"fdc {__version__}")
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("init", help="create data/, inbox/, config.toml, .env and the database")
    s = sub.add_parser("sync", help="ingest, fetch prices and SEC facts, derive analytics, export feed files")
    s.add_argument("--only", help=f"comma list of steps to run ({', '.join(STEPS)})")
    s.add_argument("--skip", help="comma list of steps to skip")
    s.add_argument("--dry-run", action="store_true")
    i = sub.add_parser("import", help="ingest one export file or a folder of them (files are not moved)")
    i.add_argument("path")
    i.add_argument("--account", help="account label for history files that carry none")
    sub.add_parser("status", help="last run per step, staleness, row counts")
    q = sub.add_parser("query", help="run a read-only SQL statement")
    q.add_argument("sql")
    q.add_argument("--csv", action="store_true")
    q.add_argument("--limit", type=int, default=500)
    m = sub.add_parser("mcp", help="run the read-only MCP server for Claude Desktop (stdio)")
    m.add_argument("--print-config", action="store_true", help="print the claude_desktop_config.json snippet")
    e = sub.add_parser("export", help="write the cockpit feed files (prices, dividends, fundamentals) now")
    e.add_argument("--dir", help="target folder (default: [export.cockpit].dir from config.toml)")
    return p


def _csv_list(s: str | None) -> list[str] | None:
    return [x.strip() for x in s.split(",") if x.strip()] if s else None


def cmd_init(root: Path) -> int:
    created = init_project(root)
    for c in created:
        ui.console.print(Text("created ", style="green") + Text(c))
    cfg = load_config(root)
    store = Store.open(cfg.db_path, backup_dir=cfg.backup_dir)
    store.close()
    ui.console.print(Text("database ready: ") + Text(str(cfg.db_path), style="bold"))
    steps = [
        "1. Open .env and set SEC_USER_AGENT to your name and email (SEC requires a contact line).",
        "2. Download a Fidelity \"Portfolio Positions\" export and drop it in inbox/.",
        "3. Run: fdc sync   (the first run fetches five years of prices and every SEC filing)",
    ]
    if cfg.snaptrade_dir and cfg.snaptrade_dir.is_dir():
        steps.append(f"   A SnapTrade export folder was found at {cfg.snaptrade_dir}; it will be ingested too.")
    if cfg.investing_dir and cfg.investing_dir.is_dir():
        steps.append(f"   Research tickers found at {cfg.investing_dir} join the universe.")
    ui.console.print(ui.panel("\n".join(steps), "Next steps"))
    return 0


def _steps_table(report) -> object:
    rows = [[s.step, ui.badge(s.status), s.rows, s.message] for s in report.steps]
    title = "sync (dry run)" if report.dry_run else f"sync {report.run_id}"
    return ui.table(["step", "status", "rows", "message"], rows, title=title)


def cmd_sync(root: Path, args) -> int:
    cfg = load_config(root)
    only, skip = _csv_list(args.only), _csv_list(args.skip)
    if ui.console.is_terminal and not args.dry_run:
        with ui.console.status("sync starting") as live:
            report = run_sync(cfg, only=only, skip=skip, dry_run=False,
                              progress=lambda step, detail: live.update(Text(f"{step} ", style="bold") + Text(detail)))
    else:
        report = run_sync(cfg, only=only, skip=skip, dry_run=args.dry_run)
    ui.console.print(_steps_table(report))
    return report.exit_code


def cmd_import(root: Path, args) -> int:
    cfg = load_config(root)
    target = Path(args.path)
    files = sorted(p for p in target.iterdir() if p.is_file()) if target.is_dir() else [target]
    store = Store.open(cfg.db_path, backup_dir=cfg.backup_dir)
    try:
        for f in files:
            r = ingest_file(store, f, account=args.account)
            if r.skipped:
                ui.console.print(Text(f"{f.name}: skipped - {r.skipped}", style="yellow"))
            else:
                ui.console.print(Text(f"{f.name}: {r.kind}, {r.rows} rows", style="green"))
            for w in r.warnings:
                ui.console.print(Text(f"  warning: {w}", style="dark_orange"))
    finally:
        store.close()
    return 0


def cmd_status(root: Path) -> int:
    cfg = load_config(root)
    if not cfg.db_path.exists():
        ui.error("no database yet")
        ui.hint("run: fdc init")
        return 2
    store = Store.open(cfg.db_path, migrate=False)
    try:
        runs = {r["step"]: r for r in store.query("SELECT * FROM sync_status")}
        rows = []
        for step in STEPS:
            r = runs.get(step)
            if r is None:
                rows.append([step, ui.badge("never"), "", None, ""])
                continue
            age = r["age_hours"] or 0
            status = ui.badge(r["status"])
            if age > STALE_HOURS.get(step, 48):
                status = Text(f"{r['status']} STALE", style="dark_orange")
            rows.append([step, status, f"{age} h ago", r["rows_written"], (r["message"] or "")[:110]])
        ui.console.print(ui.table(["step", "status", "age", "rows", "message"], rows, title="last sync"))
        ui.console.print(Text("latest snapshot: ") + Text(store.latest_snapshot_date() or "none", style="bold"))
        counts = store.counts()
        ui.console.print(ui.table(["table", "rows"], [[k, v] for k, v in counts.items()], title="rows"))
    finally:
        store.close()
    if not cfg.sec_user_agent:
        ui.hint("SEC_USER_AGENT is not set in .env; the sec step will be skipped until it is")
    if not cfg.tiingo_token:
        ui.hint("TIINGO_API_TOKEN is not set; prices come from yfinance (add a free Tiingo token for a faster, cleaner feed)")
    return 0


def cmd_query(root: Path, args) -> int:
    cfg = load_config(root)
    try:
        cols, rows, truncated = run_readonly(cfg.db_path, args.sql, limit=args.limit)
    except UnsafeSql as e:
        ui.error(f"refused: {e}")
        return 1
    if args.csv:
        w = csv.writer(sys.stdout, lineterminator="\n")
        w.writerow(cols)
        w.writerows(rows)
        return 0
    footer = f"truncated at {args.limit} rows; use --limit" if truncated else f"{len(rows)} row{'s' if len(rows) != 1 else ''}"
    ui.console.print(ui.table(cols, rows, footer=footer))
    return 0


def cmd_export(root: Path, args) -> int:
    from .export.cockpit import export_cockpit
    from .universe import build_universe

    cfg = load_config(root)
    store = Store.open(cfg.db_path, backup_dir=cfg.backup_dir)
    try:
        universe = build_universe(store, cfg.watchlist, classify=cfg.classify, investing_dir=cfg.investing_dir)
        results = export_cockpit(store, cfg, universe, datetime.now(timezone.utc),
                                 out_dir=Path(args.dir) if args.dir else None)
    finally:
        store.close()
    styles = {"written": "green", "unchanged": "dim", "skipped": "yellow"}
    for name, status in results:
        ui.console.print(Text(f"{name}: {status}", style=styles.get(status, "")))
    if all(s == "skipped" for _, s in results):
        ui.error("target folder missing")
        ui.hint("pass --dir or set [export.cockpit].dir in config.toml")
        return 2
    return 0


def cmd_mcp(root: Path, args) -> int:
    from . import mcp_server

    cfg = load_config(root)
    if args.print_config:
        print(mcp_server.print_config(root))
        return 0
    mcp_server.serve(cfg.db_path)
    return 0


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    root = Path(args.root).resolve()
    try:
        if args.cmd == "init":
            return cmd_init(root)
        if args.cmd == "sync":
            return cmd_sync(root, args)
        if args.cmd == "import":
            return cmd_import(root, args)
        if args.cmd == "status":
            return cmd_status(root)
        if args.cmd == "query":
            return cmd_query(root, args)
        if args.cmd == "mcp":
            return cmd_mcp(root, args)
        if args.cmd == "export":
            return cmd_export(root, args)
    except ConfigError as e:
        ui.error(str(e))
        ui.hint("run: fdc init" if "not found" in str(e) else "check config.toml for the line reported above")
        return 2
    return 2
