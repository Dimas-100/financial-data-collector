"""Yahoo through yfinance, behind a small seam so the collectors can be tested without the network.

`YahooClient` is what a collector asks; `YfinanceClient` is the real one, and `default_client()` is what a sync uses
when it is not given another (the tests' conftest replaces it). yfinance is imported when first needed, so tests and
offline runs never load it."""
from __future__ import annotations

import logging
from typing import Protocol

from ..symbols import to_yfinance

PAUSE_SECONDS = 0.5                     # between symbols asked


class YahooClient(Protocol):
    def fund_info(self, symbol: str) -> dict | None:
        """Yahoo's `info` for a fund, or None when it has nothing. Raises when the ask itself fails."""
        ...


class YfinanceClient:
    """yfinance logs each failed call as an error of its own; those lines are silenced while asking, since a failure
    comes back to the collector as an exception or None. The logger's level is put back afterwards."""

    def fund_info(self, symbol: str) -> dict | None:
        import yfinance as yf
        logger = logging.getLogger("yfinance")
        level = logger.level
        logger.setLevel(logging.CRITICAL)
        try:
            return yf.Ticker(to_yfinance(symbol)).info or None
        finally:
            logger.setLevel(level)


def default_client() -> YahooClient:
    return YfinanceClient()
