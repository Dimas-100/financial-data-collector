"""The eleven sectors the valuation foundation groups companies by, the fund that stands for each as a yardstick, and
where each company's own sector comes from: the sibling classifications feed, else its SEC industry (SIC) code."""
from __future__ import annotations

import csv
import json
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import TYPE_CHECKING

from . import migrate as _migrate
from .symbols import canonical

if TYPE_CHECKING:
    from .config import Config
    from .store import Store

# The eleven sectors, named as fdc stores them.
SECTORS: tuple[str, ...] = (
    "Communication Services", "Consumer Discretionary", "Consumer Staples", "Energy", "Financials", "Health Care",
    "Industrials", "Materials", "Real Estate", "Technology", "Utilities",
)

# The Select Sector SPDR fund for each sector: what [benchmarks] uses unless config.toml replaces it.
DEFAULT_SECTOR_ETFS: dict[str, str] = {
    "Communication Services": "XLC", "Consumer Discretionary": "XLY", "Consumer Staples": "XLP", "Energy": "XLE",
    "Financials": "XLF", "Health Care": "XLV", "Industrials": "XLI", "Materials": "XLB", "Real Estate": "XLRE",
    "Technology": "XLK", "Utilities": "XLU",
}

# Yahoo's sector names, as the classifications feed carries them, mapped to the eleven above.
YAHOO_SECTORS: dict[str, str] = {
    "Communication Services": "Communication Services", "Consumer Cyclical": "Consumer Discretionary",
    "Consumer Defensive": "Consumer Staples", "Energy": "Energy", "Financial Services": "Financials",
    "Healthcare": "Health Care", "Industrials": "Industrials", "Basic Materials": "Materials",
    "Real Estate": "Real Estate", "Technology": "Technology", "Utilities": "Utilities",
}

# Only companies have a sector; a fund's is NULL.
COMPANY_TYPES = ("stock", "foreign_filer")
FEED_FILE = "classifications.json"
FEED_UNREADABLE = "classifications feed unreadable; feed sectors kept"


def load_sic_ranges() -> list[tuple[int, int, str]]:
    """The package's SIC-range table as (from, to, sector). A sector that is not one of the eleven is a typo in the
    seed, so it stops here rather than reaching a company."""
    ranges = []
    with open(_migrate.SEEDS_DIR / "sic_sector.csv", newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            if row["sector"] not in SECTORS:
                raise ValueError(f"sic_sector.csv names an unknown sector: {row['sector']}")
            ranges.append((int(row["sic_from"]), int(row["sic_to"]), row["sector"]))
    return ranges


def sector_for_sic(sic: int | None, ranges: list[tuple[int, int, str]]) -> str | None:
    """The sector of the narrowest range holding the code (a wide band with narrower exceptions inside it), else None."""
    if sic is None:
        return None
    held = [(hi - lo, sector) for lo, hi, sector in ranges if lo <= sic <= hi]
    return min(held)[1] if held else None


def read_feed(path: Path) -> tuple[dict[str, str] | None, str | None]:
    """The feed's sector for each ticker, in our eleven names and canonical symbols, and the file's `as_of`. A name
    Yahoo uses that we do not know, or an entry that cannot be read, is skipped (only that entry); a missing or
    unreadable file is None, not an error, so the caller can tell "the feed says nothing of it" from "no feed"."""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        entries = list(data["by_ticker"].items())
        as_of = data.get("as_of")
    except (OSError, ValueError, KeyError, TypeError, AttributeError):
        return None, None
    found = {}
    for ticker, entry in entries:
        try:
            sector = YAHOO_SECTORS.get(entry.get("sector")) if isinstance(entry, dict) else None
            if sector:
                found[canonical(ticker)] = sector
        except (TypeError, AttributeError):         # a sector that is a list or a table, a ticker that is not text
            continue
    return found, as_of if isinstance(as_of, str) else None


def _feed_source(as_of: str | None) -> str:
    try:
        return f"feed: {FEED_FILE} as of {date.fromisoformat((as_of or '')[:10]).isoformat()}"
    except ValueError:
        return f"feed: {FEED_FILE}"


@dataclass
class SectorPass:
    changed: int                        # rows whose sector changed
    feed_kept: int = 0                  # companies whose feed sector was kept because the feed could not be read


def assign_sectors(store: Store, cfg: Config) -> SectorPass:
    """Give every company its sector: the feed's when it names the company, else the SIC table's, else none; a
    security that is not a company has none. A feed that cannot be read at all (missing, not JSON, no `by_ticker`)
    changes no company whose sector came from it: a hiccup in the sibling file must not swap sectors; a feed that reads
    fine but no longer names a company lets it fall back to SIC. No investing folder configured means no feed. Reads
    only stored data and the sibling file, so it runs on every sync."""
    if cfg.investing_dir:
        feed, as_of = read_feed(cfg.investing_dir / "data" / FEED_FILE)
    else:
        feed, as_of = {}, None
    ranges = load_sic_ranges()
    result = SectorPass(0)
    for row in store.securities():
        sector, source = None, None
        if row["asset_type"] in COMPANY_TYPES:
            if feed is None and (row["sector_source"] or "").startswith("feed:"):
                result.feed_kept += 1
                continue
            if feed and row["symbol"] in feed:
                sector, source = feed[row["symbol"]], _feed_source(as_of)
            else:
                sector = sector_for_sic(row["sic"], ranges)
                source = f"sec: SIC {row['sic']}" if sector else None
        result.changed += store.set_sector(row["symbol"], sector, source)
    return result
