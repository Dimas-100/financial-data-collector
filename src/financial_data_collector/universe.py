"""Which symbols the collectors work on: everything ever held, plus the watchlist."""
from __future__ import annotations

import json
import re
from pathlib import Path

from .store import Store, today
from .symbols import canonical, is_money_market

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


def build_universe(store: Store, watchlist_path: Path, classify: dict[str, str] | None = None,
                   investing_dir: Path | None = None) -> list[str]:
    for sym in read_watchlist(watchlist_path) + read_investing_tickers(investing_dir):
        store.upsert_security(sym, first_seen=today())
    for sym, asset_type in (classify or {}).items():  # manual [classify] overrides from config.toml
        store.set_asset_type(canonical(sym), asset_type)
    out: list[str] = []
    for row in store.securities():
        sym = row["symbol"]
        if row["asset_type"] in _SKIP_TYPES or is_money_market(sym, row["description"]):
            continue
        out.append(sym)
    return sorted(out)
