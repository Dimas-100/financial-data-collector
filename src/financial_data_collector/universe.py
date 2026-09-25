"""Which symbols the collectors work on: everything ever held, plus the watchlist."""
from __future__ import annotations

import re
from pathlib import Path

from .store import Store, today
from .symbols import canonical, is_money_market

_SKIP_TYPES = {"money_market", "crypto"}
_TICKER_LINE = re.compile(r"^ticker:\s*([A-Za-z][A-Za-z0-9./-]*)", re.MULTILINE)


def read_investing_tickers(content_dir: Path | None) -> list[str]:
    """Tickers the investing project researches: theses/ + watchlist/ frontmatter and basket.txt."""
    if not content_dir or not Path(content_dir).is_dir():
        return []
    content_dir = Path(content_dir)
    found: set[str] = set()
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
