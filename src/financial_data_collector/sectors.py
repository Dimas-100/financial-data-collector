"""The eleven sectors the valuation foundation groups companies by, and the fund that stands for each as a yardstick."""
from __future__ import annotations

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
