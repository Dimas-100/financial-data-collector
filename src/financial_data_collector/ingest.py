"""Route local files into the store: inbox CSVs and the SnapTrade export folder.

Every file is hashed; a hash already in ingested_files is skipped, which is
what makes re-running sync a no-op.
"""
from __future__ import annotations

import shutil
from dataclasses import dataclass, field
from pathlib import Path

from .adapters import fidelity_history, fidelity_positions, snaptrade
from .adapters.base import read_text_lines, sha256_file
from .config import Config
from .store import Store


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
    if kind == "fidelity_positions":
        snap = fidelity_positions.parse(path)
        rows = store.write_snapshot(snap)
        warnings = list(snap.warnings)
    else:
        try:
            rows = store.write_transactions(fidelity_history.parse(path, account=account))
        except fidelity_history.AccountUnknown as e:
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
