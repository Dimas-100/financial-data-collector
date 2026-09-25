"""fdc command line."""
from __future__ import annotations

import argparse
import csv
import sys
from pathlib import Path

from . import __version__
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
    s = sub.add_parser("sync", help="ingest, fetch prices and SEC facts, derive analytics, export cockpit files")
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


def _table(cols: list[str], rows: list[list]) -> str:
    cells = [[("" if v is None else str(v)) for v in r] for r in rows]
    widths = [max([len(c)] + [len(r[i]) for r in cells]) for i, c in enumerate(cols)]
    line = "  ".join(c.ljust(widths[i]) for i, c in enumerate(cols))
    out = [line, "  ".join("-" * w for w in widths)]
    out += ["  ".join(r[i].ljust(widths[i]) for i in range(len(cols))) for r in cells]
    return "\n".join(out)


def cmd_init(root: Path) -> int:
    created = init_project(root)
    for c in created:
        print(f"created {c}")
    cfg = load_config(root)
    store = Store.open(cfg.db_path, backup_dir=cfg.backup_dir)
    store.close()
    print(f"database ready: {cfg.db_path}")
    print("next: put your name and email in .env (SEC_USER_AGENT), drop a Fidelity export in inbox/, run: fdc sync")
    return 0


def cmd_sync(root: Path, args) -> int:
    cfg = load_config(root)
    report = run_sync(cfg, only=_csv_list(args.only), skip=_csv_list(args.skip), dry_run=args.dry_run)
    for s in report.steps:
        print(f"{s.step:8} {s.status:8} {s.rows:>7} rows  {s.message}")
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
                print(f"{f.name}: skipped - {r.skipped}")
            else:
                print(f"{f.name}: {r.kind}, {r.rows} rows")
            for w in r.warnings:
                print(f"  warning: {w}")
    finally:
        store.close()
    return 0


def cmd_status(root: Path) -> int:
    cfg = load_config(root)
    if not cfg.db_path.exists():
        print("no database yet; run: fdc init", file=sys.stderr)
        return 2
    store = Store.open(cfg.db_path, migrate=False)
    try:
        runs = {r["step"]: r for r in store.query("SELECT * FROM sync_status")}
        print("steps:")
        for step in STEPS:
            r = runs.get(step)
            if r is None:
                print(f"  {step:8} never run")
                continue
            stale = " STALE" if (r["age_hours"] or 0) > STALE_HOURS.get(step, 48) else ""
            print(f"  {step:8} {r['status']:8} {r['age_hours']:>7} h ago  {r['rows_written']} rows{stale}  {r['message'][:120]}")
        print(f"latest snapshot: {store.latest_snapshot_date() or 'none'}")
        print("rows:", ", ".join(f"{k}={v}" for k, v in store.counts().items()))
    finally:
        store.close()
    print(f"SEC_USER_AGENT: {'set' if cfg.sec_user_agent else 'NOT SET (SEC step will be skipped)'}")
    print(f"TIINGO_API_TOKEN: {'set' if cfg.tiingo_token else 'not set (yfinance will be used)'}")
    return 0


def cmd_query(root: Path, args) -> int:
    cfg = load_config(root)
    try:
        cols, rows, truncated = run_readonly(cfg.db_path, args.sql, limit=args.limit)
    except UnsafeSql as e:
        print(f"refused: {e}", file=sys.stderr)
        return 1
    if args.csv:
        w = csv.writer(sys.stdout, lineterminator="\n")
        w.writerow(cols)
        w.writerows(rows)
    else:
        print(_table(cols, rows))
        if truncated:
            print(f"(truncated at {args.limit} rows; use --limit)")
    return 0


def cmd_export(root: Path, args) -> int:
    from datetime import datetime, timezone

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
    for name, status in results:
        print(f"{name}: {status}")
    if all(s == "skipped" for _, s in results):
        print("target folder missing; pass --dir or set [export.cockpit].dir", file=sys.stderr)
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
        print(f"error: {e}", file=sys.stderr)
        return 2
    return 2
