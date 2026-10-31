"""Route local files into the store: inbox CSVs and the SnapTrade export folder.

Every file is hashed; a hash already in ingested_files is skipped, which is
what makes re-running sync a no-op.
"""
from __future__ import annotations

import json
import shutil
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path

from .adapters import fidelity_history, fidelity_positions, snaptrade
from .adapters.base import read_text_lines, sha256_file
from .config import Config
from .derive.sources import safe_link
from .models import Headline
from .store import RouteConflict, Store, utcnow
from .symbols import canonical

NEWS_FILE = "news.json"


@dataclass
class IngestResult:
    path: str
    kind: str | None
    rows: int = 0
    skipped: str | None = None
    warnings: list[str] = field(default_factory=list)


@dataclass
class IngestSummary:
    results: list[IngestResult] = field(default_factory=list)
    rows: int = 0
    messages: list[str] = field(default_factory=list)

    def add(self, r: IngestResult) -> None:
        self.results.append(r)
        self.rows += r.rows
        if r.skipped:
            self.messages.append(f"{r.path}: {r.skipped}")
        self.messages.extend(f"{r.path}: {w}" for w in r.warnings)


def classify_file(path: Path) -> str | None:
    if path.suffix.lower() != ".csv":
        return None
    lines = read_text_lines(path)[:12]
    if fidelity_positions.detect(lines):
        return "fidelity_positions"
    if fidelity_history.detect(lines):
        return "fidelity_history"
    return None


def ingest_file(store: Store, path: Path, *, account: str | None = None, move_to: Path | None = None) -> IngestResult:
    sha = sha256_file(path)
    if store.file_seen(sha):
        return IngestResult(str(path), None, skipped="already ingested (same content)")
    kind = classify_file(path)
    if kind is None:
        return IngestResult(str(path), None, skipped="unrecognized file; expected a Fidelity positions or history export")
    warnings: list[str] = []
    try:
        if kind == "fidelity_positions":
            snap = fidelity_positions.parse(path)
            rows = store.write_snapshot(snap)
            warnings = list(snap.warnings)
        else:
            rows = store.write_transactions(fidelity_history.parse(path, account=account))
    except (fidelity_history.AccountUnknown, RouteConflict) as e:
        return IngestResult(str(path), kind, skipped=str(e))
    store.record_file(sha, str(path), kind, rows)
    if move_to is not None:
        move_to.mkdir(parents=True, exist_ok=True)
        shutil.move(str(path), str(move_to / path.name))
    return IngestResult(str(path), kind, rows=rows, warnings=warnings)


def ingest_inbox(store: Store, cfg: Config) -> IngestSummary:
    summary = IngestSummary()
    if not cfg.inbox.is_dir():
        summary.messages.append(f"inbox {cfg.inbox} does not exist")
        return summary
    for path in sorted(p for p in cfg.inbox.iterdir() if p.is_file() and not p.name.startswith(".")):
        summary.add(ingest_file(store, path, move_to=cfg.processed_dir))
    return summary


def ingest_snaptrade(store: Store, cfg: Config) -> IngestSummary:
    summary = IngestSummary()
    if not cfg.snaptrade_enabled:
        summary.messages.append("snaptrade: disabled in config")
        return summary
    if cfg.snaptrade_dir is None or not cfg.snaptrade_dir.is_dir():
        summary.messages.append(f"snaptrade: directory missing ({cfg.snaptrade_dir}); skipped")
        return summary
    for path in snaptrade.find_snapshot_files(cfg.snaptrade_dir):
        sha = sha256_file(path)
        if store.file_seen(sha):
            continue
        snap = snaptrade.parse_positions(path)
        rows = store.write_snapshot(snap)
        store.record_file(sha, str(path), "snaptrade_positions", rows)
        summary.add(IngestResult(str(path), "snaptrade_positions", rows=rows, warnings=list(snap.warnings)))
    activity = cfg.snaptrade_dir / "live-activity.json"
    if activity.is_file():
        sha = sha256_file(activity)
        if not store.file_seen(sha):
            rows = store.write_transactions(snaptrade.parse_activity(activity))
            store.record_file(sha, str(activity), "snaptrade_activity", rows)
            summary.add(IngestResult(str(activity), "snaptrade_activity", rows=rows))
    return summary


def news_feed(cfg: Config) -> Path | None:
    """The sibling news feed (`<investing content_dir>/data/news.json`) when there is one."""
    if cfg.investing_dir is None:
        return None
    path = cfg.investing_dir / "data" / NEWS_FILE
    return path if path.is_file() else None


def _text(value: object) -> str | None:
    return value.strip() or None if isinstance(value, str) else None


def read_news(path: Path) -> tuple[list[Headline], str | None]:
    """The feed's headlines (`by_ticker[*]`) with the symbol canonical, `outlet` the item's `source` and `url` only when
    it is a safe https link; and an error note, or None. Nothing but date, published, title, source and url is read from
    an item (no article text, no tags); an item without a date or title, or a ticker whose items are no list, is
    skipped alone. A missing file is `([], None)`; one that can't be read as a feed is `([], note)`."""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        tickers = list(data["by_ticker"].items())
        as_of = _text(data.get("as_of"))
    except FileNotFoundError:
        return [], None
    except (OSError, ValueError, KeyError, TypeError, AttributeError) as e:
        return [], f"{NEWS_FILE} unreadable ({type(e).__name__}); headlines left as they were"
    rows: list[Headline] = []
    for ticker, items in tickers:
        symbol = canonical(ticker) if isinstance(ticker, str) else ""
        if not symbol or not isinstance(items, list):
            continue
        for item in items:
            if not isinstance(item, dict):
                continue
            title, day = _text(item.get("title")), _text(item.get("date"))
            try:
                day = date.fromisoformat(day[:10]).isoformat() if day else None
            except ValueError:
                day = None
            if not title or not day:
                continue
            rows.append(Headline(symbol, day, title, _text(item.get("source")), safe_link(item.get("url")),
                                 _text(item.get("published")), as_of))
    return rows, None


def ingest_news(store: Store, cfg: Config) -> tuple[int, str | None]:
    """Keep the news feed's headlines: (rows new, a note when the feed can't be read). No investing folder or no
    `data/news.json` is nothing at all."""
    path = news_feed(cfg)
    if path is None:
        return 0, None
    rows, note = read_news(path)
    return (store.write_headlines(rows, utcnow()) if rows else 0), note
