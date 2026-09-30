"""fdc command line."""
from __future__ import annotations

import argparse
import csv
import getpass
import sys
import webbrowser
from datetime import datetime, timezone
from pathlib import Path

from rich.text import Text

from . import __version__, http, ui
from .config import ConfigError, init_project, load_config
from .connections import service, simplefin
from .connections.base import ConnectionFailed
from .connections.keys import ENV_REF, KeyHome, SnapTradeKeys
from .connections.names import KINDS
from .ingest import ingest_file
from .readonly import UnsafeSql, run_readonly
from .store import Store
from .sync import STEPS, run_sync

STALE_HOURS = {"ingest": 48, "prices": 48, "sec": 336, "derive": 48, "export": 48}
SNAPTRADE_SITE = "https://dashboard.snaptrade.com/"
SNAPTRADE_STEPS = (
    "1. Sign in at SnapTrade (free for personal use) and connect each brokerage there.\n"
    "2. Create a personal API key: a client id and a consumer key.\n"
    "3. Paste them below. What you type stays hidden and is checked with SnapTrade right away."
)
SIMPLEFIN_STEPS = (
    "1. Sign in at SimpleFIN Bridge ($15 a year) and connect each bank and card there.\n"
    "2. Choose New app and copy the setup token it shows. It works once.\n"
    "3. Paste it below. What you type stays hidden."
)
_ask = getpass.getpass      # hidden input; tests replace it
_open = webbrowser.open     # tests replace it


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
    c = sub.add_parser("connect", help="connect a brokerage (snaptrade) or a bank (simplefin) with your own key")
    c.add_argument("service", choices=service.NAMES)
    c.add_argument("--with-user", action="store_true", help="a SnapTrade key that has a registered user (four values)")
    c.add_argument("--no-browser", action="store_true", help="don't open the service's site")
    sub.add_parser("connections", help="every connection: when it last worked, its accounts, its last error")
    d = sub.add_parser("disconnect", help="forget a connection's key; stored history stays")
    d.add_argument("name", choices=service.NAMES)
    a = sub.add_parser("accounts", help="every account, its kind, limit and rate; 'accounts set' changes them")
    asub = a.add_subparsers(dest="action")
    s2 = asub.add_parser("set", help="confirm or correct an account's kind, set a card's limit or a yearly rate")
    s2.add_argument("label")
    s2.add_argument("--kind", choices=KINDS)
    s2.add_argument("--limit", type=float, help="a card's or line's credit limit")
    s2.add_argument("--rate", type=float, help="yearly rate in percent: what cash earns or debt costs")
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
        "2. Connect a brokerage: fdc connect snaptrade   (or a bank: fdc connect simplefin)",
        "   Or download a Fidelity \"Portfolio Positions\" export and drop it in inbox/.",
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


def _accounts_table(rows) -> object:
    body = [[r.label, r.institution, r.account_type + ("" if r.kind_confirmed else " (guessed)")] for r in rows]
    return ui.table(["account", "institution", "kind"], body, title="accounts found")


def cmd_connect(root: Path, args) -> int:
    cfg = load_config(root)
    store = Store.open(cfg.db_path, backup_dir=cfg.backup_dir)
    home = KeyHome.for_root(cfg.root)
    now = datetime.now(timezone.utc)
    try:
        if args.service == "snaptrade":
            ui.console.print(ui.panel(SNAPTRADE_STEPS, "Connect your brokerages"))
            if not args.no_browser:
                _open(SNAPTRADE_SITE)
            client_id = _ask("Client id: ").strip()
            consumer_key = _ask("Consumer key: ").strip()
            user_id = user_secret = None
            if args.with_user:
                user_id = _ask("User id: ").strip()
                user_secret = _ask("User secret: ").strip()
            if not client_id or not consumer_key or (args.with_user and not (user_id and user_secret)):
                ui.error("every value is needed")
                return 2
            found, notes = service.connect_snaptrade(
                store, home, SnapTradeKeys(client_id, consumer_key, user_id or None, user_secret or None),
                fetch=http.fetch, now=now)
        else:
            ui.console.print(ui.panel(SIMPLEFIN_STEPS, "Connect your banks and cards"))
            if not args.no_browser:
                _open(simplefin.SITE)
            token = _ask("Setup token: ").strip()
            if not token:
                ui.error("a setup token is needed")
                return 2
            found, notes = service.connect_simplefin(store, home, token, post=http.post, fetch=http.fetch, now=now)
        if store.connection(args.service)["key_ref"] == ENV_REF:
            ui.hint("no key store on this computer: the key is in .env instead")
    except ConnectionFailed as e:
        ui.error(str(e))
        return 1
    finally:
        store.close()
    if found:
        ui.console.print(_accounts_table(found))
    else:
        ui.console.print(Text("connected, but no accounts yet: add them on the service's site", style="yellow"))
    for note in notes:
        ui.hint(note)
    ui.hint("run fdc sync to fetch balances, holdings and activity")
    return 0


def cmd_connections(root: Path) -> int:
    cfg = load_config(root)
    store = Store.open(cfg.db_path, backup_dir=cfg.backup_dir)
    try:
        rows = service.overview(store)
    finally:
        store.close()
    if not rows:
        ui.console.print("no connections")
        ui.hint("fdc connect snaptrade   or   fdc connect simplefin")
        return 0
    body = [[r.name, r.created_at[:10], r.last_ok_at or "never", r.accounts, r.last_error] for r in rows]
    ui.console.print(ui.table(["connection", "since", "last worked", "accounts", "last error"], body))
    return 0


def cmd_disconnect(root: Path, args) -> int:
    cfg = load_config(root)
    store = Store.open(cfg.db_path, backup_dir=cfg.backup_dir)
    try:
        gone = service.disconnect(store, KeyHome.for_root(cfg.root), args.name, now=datetime.now(timezone.utc))
    finally:
        store.close()
    if not gone:
        ui.error(f"no {args.name} connection")
        return 1
    ui.console.print(Text(f"{args.name} disconnected; its accounts and history stay", style="green"))
    return 0


def cmd_accounts(root: Path, args) -> int:
    cfg = load_config(root)
    store = Store.open(cfg.db_path, backup_dir=cfg.backup_dir)
    try:
        if args.action == "set":
            if args.limit is not None and args.limit < 0:
                ui.error("--limit is an amount, 0 or more")
                return 2
            if args.rate is not None and not 0 <= args.rate <= 100:
                ui.error("--rate is a yearly percentage between 0 and 100")
                return 2
            if args.kind is None and args.limit is None and args.rate is None:
                ui.error("nothing to set: give --kind, --limit or --rate")
                return 2
            if not store.set_account(args.label, kind=args.kind, credit_limit=args.limit, rate_pct=args.rate):
                ui.error(f"no account called {args.label!r}")
                ui.hint("fdc accounts lists them")
                return 1
            ui.console.print(Text(f"{args.label}: saved", style="green"))
            return 0
        rows = store.accounts_overview()
    finally:
        store.close()
    body = [[r["label"], r["institution"], r["account_type"] + ("" if r["kind_confirmed"] else " (guessed)"),
             r["credit_limit"], r["rate_pct"], r["origin"]] for r in rows]
    ui.console.print(ui.table(["account", "institution", "kind", "limit", "rate %", "from"], body))
    if any(not r["kind_confirmed"] for r in rows):
        ui.hint('confirm a guessed kind: fdc accounts set "<account>" --kind checking')
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
        if args.cmd == "connect":
            return cmd_connect(root, args)
        if args.cmd == "connections":
            return cmd_connections(root)
        if args.cmd == "disconnect":
            return cmd_disconnect(root, args)
        if args.cmd == "accounts":
            return cmd_accounts(root, args)
    except ConfigError as e:
        ui.error(str(e))
        ui.hint("run: fdc init" if "not found" in str(e) else "check config.toml for the line reported above")
        return 2
    return 2
