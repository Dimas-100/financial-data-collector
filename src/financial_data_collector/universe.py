"""Which symbols the collectors work on: everything ever held, plus the watchlist."""
from __future__ import annotations

from pathlib import Path

from .store import Store, today
from .symbols import canonical, is_money_market

_SKIP_TYPES = {"money_market", "crypto"}


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


def build_universe(store: Store, watchlist_path: Path, classify: dict[str, str] | None = None) -> list[str]:
    for sym in read_watchlist(watchlist_path):
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
