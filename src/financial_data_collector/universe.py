"""Which symbols the collectors work on: everything ever held, plus the watchlist."""
from __future__ import annotations

import json
import re
from pathlib import Path

from .store import Store, today
from .symbols import canonical, is_money_market

# A symbol with no CIK, no position and no bars from any provider for this many runs in a row is parked:
# left out of the universe until a position for it shows up.
PARK_AFTER = 3
PARK_REASON = "no price from any provider, no CIK, not held"
_SKIP_TYPES = {"money_market", "crypto"}
_TICKER_LINE = re.compile(r"^ticker:\s*([A-Za-z][A-Za-z0-9./-]*)", re.MULTILINE)
# investing/pipeline/_scan_metrics.AUTO_PROMOTE_COUNT: scan names seen this often graduate to the basket
INVESTING_PROMOTE_COUNT = 2


def _scan_promoted(content_dir: Path, promote_count: int) -> set[str]:
    p = content_dir / "data" / "scan-history.json"
    if not p.is_file():
        return set()
    try:
        history = json.loads(p.read_text(encoding="utf-8")).get("history", {})
    except (OSError, ValueError):
        return set()
    return {canonical(t) for t, v in history.items() if (v or {}).get("count", 0) >= promote_count}


def read_investing_tickers(content_dir: Path | None, promote_count: int = INVESTING_PROMOTE_COUNT) -> list[str]:
    """Tickers the investing project researches: theses/ + watchlist/ frontmatter, basket.txt,
    and scan names promoted in data/scan-history.json (mirrors investing's _basket.basket_tickers)."""
    if not content_dir or not Path(content_dir).is_dir():
        return []
    content_dir = Path(content_dir)
    found: set[str] = _scan_promoted(content_dir, promote_count)
    for sub in ("theses", "watchlist"):
        folder = content_dir / sub
        if not folder.is_dir():
            continue
        for p in sorted(folder.glob("*.md")):
            if p.name.startswith("_") or p.name.upper() == "README.MD":
                continue
            m = _TICKER_LINE.search(p.read_text(encoding="utf-8", errors="replace"))
            if m:
                found.add(canonical(m.group(1)))
    basket = content_dir / "basket.txt"
    if basket.is_file():
        for line in basket.read_text(encoding="utf-8", errors="replace").splitlines():
            s = line.split("#", 1)[0].strip()
            if s:
                found.add(canonical(s.split()[0]))
    return sorted(found)


def read_watchlist(path: Path) -> list[str]:
    if not path.is_file():
        return []
    out: list[str] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        s = line.split("#", 1)[0].strip()
        if s:
            sym = canonical(s)
            if sym not in out:
                out.append(sym)
    return out


def universe_symbols(store: Store, watchlist_path: Path, classify: dict[str, str] | None = None,
                     investing_dir: Path | None = None, *, include_benchmarks: bool = True) -> list[str]:
    """The symbols the collector follows, computed without writing: the securities on record plus the watchlist
    and research names, [classify] overrides applied to the answer, money markets, skipped kinds and parked symbols left out.
    With `include_benchmarks=False` a benchmark is left out unless it is also held or on the watchlist or research lists
    (the export is about the owner's names, not the yardsticks). What the console counts; works on a read-only store."""
    kinds: dict[str, tuple[str, str | None]] = {}
    left_out: set[str] = set()                # parked symbols, and benchmarks the export does not want
    benchmarks: set[str] = set()
    for row in store.securities():
        kinds[row["symbol"]] = (row["asset_type"], row["description"])
        if row["parked_reason"]:
            left_out.add(row["symbol"])
        if row["benchmark"]:
            benchmarks.add(row["symbol"])
    listed = read_watchlist(watchlist_path) + read_investing_tickers(investing_dir)
    for sym in listed:
        kinds.setdefault(sym, ("unknown", None))
    if not include_benchmarks:
        left_out |= benchmarks - store.held_symbols() - set(listed)
    for sym, asset_type in (classify or {}).items():
        sym = canonical(sym)
        if sym in kinds:
            kinds[sym] = (asset_type, kinds[sym][1])
    return sorted(sym for sym, (asset_type, description) in kinds.items()
                  if sym not in left_out and asset_type not in _SKIP_TYPES and not is_money_market(sym, description))


def build_universe(store: Store, watchlist_path: Path, classify: dict[str, str] | None = None,
                   investing_dir: Path | None = None, *, include_benchmarks: bool = True) -> list[str]:
    """universe_symbols, after recording the watchlist and research names as securities and applying the
    [classify] overrides from config.toml to the table. What a sync calls. It never marks benchmarks: the sync's
    steps do that first (sync._universe), and the export leaves them out."""
    for sym in read_watchlist(watchlist_path) + read_investing_tickers(investing_dir):
        store.upsert_security(sym, first_seen=today())
    for sym, asset_type in (classify or {}).items():
        store.set_asset_type(canonical(sym), asset_type)
    return universe_symbols(store, watchlist_path, classify, investing_dir, include_benchmarks=include_benchmarks)
