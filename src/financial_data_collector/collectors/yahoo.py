"""Yahoo through yfinance, behind a small seam so the collectors can be tested without the network.

`YahooClient` is what a collector asks; `YfinanceClient` is the real one, and `default_client()` is what a sync uses
when it is not given another (the tests' conftest replaces it). yfinance is imported when first needed, so tests and
offline runs never load it."""
from __future__ import annotations

import logging
import math
import numbers
from contextlib import contextmanager
from typing import Iterator, Protocol

from ..models import Estimate, YahooEarning
from ..symbols import to_yfinance

PAUSE_SECONDS = 0.5                     # between symbols asked
PERIODS = ("0q", "+1q", "0y", "+1y")    # the estimate periods kept: this and next quarter, this and next year


class YahooClient(Protocol):
    def fund_info(self, symbol: str) -> dict | None:
        """Yahoo's `info` for a fund, or None when it has nothing. Raises when the ask itself fails."""
        ...

    def earnings_dates(self, symbol: str, limit: int) -> list[YahooEarning]:
        """Up to `limit` of the company's earnings dates, past and coming, with EPS and surprise; [] when Yahoo has
        none. Raises when the ask itself fails."""
        ...

    def estimates(self, symbol: str) -> list[Estimate]:
        """Yahoo's EPS estimates for the periods in `PERIODS` it has; [] when none. Raises when the ask fails."""
        ...


def finite(value: object) -> float | None:
    """A finite number as a float, or None: NaN, infinity, text and booleans are not figures. numpy's numbers count
    (pandas hands them over)."""
    if isinstance(value, bool) or not isinstance(value, numbers.Real) or not math.isfinite(value):
        return None
    return float(value)


@contextmanager
def _quiet() -> Iterator[None]:
    """yfinance logs each failed call as an error of its own; those lines are silenced while asking, since a failure
    comes back to the collector as an exception or None. The logger's level is put back afterwards."""
    logger = logging.getLogger("yfinance")
    level = logger.level
    logger.setLevel(logging.CRITICAL)
    try:
        yield
    finally:
        logger.setLevel(level)


def _empty(frame: object) -> bool:
    return frame is None or getattr(frame, "empty", True)


class YfinanceClient:
    """The real client. Every ask runs with yfinance's own error logging silenced (see `_quiet`)."""

    def fund_info(self, symbol: str) -> dict | None:
        import yfinance as yf
        with _quiet():
            return yf.Ticker(to_yfinance(symbol)).info or None

    def earnings_dates(self, symbol: str, limit: int) -> list[YahooEarning]:
        """`get_earnings_dates` has a tz-aware index and the columns `EPS Estimate`, `Reported EPS` and `Surprise(%)`
        (percent); NaN becomes None."""
        import yfinance as yf
        with _quiet():
            frame = yf.Ticker(to_yfinance(symbol)).get_earnings_dates(limit=limit)
        if _empty(frame):
            return []
        return [YahooEarning(when.to_pydatetime(), finite(row.get("EPS Estimate")), finite(row.get("Reported EPS")),
                             finite(row.get("Surprise(%)")))
                for when, row in frame.iterrows()]

    def estimates(self, symbol: str) -> list[Estimate]:
        """`earnings_estimate` has a row per period and the columns `avg`, `low`, `high`, `numberOfAnalysts`."""
        import yfinance as yf
        with _quiet():
            frame = yf.Ticker(to_yfinance(symbol)).earnings_estimate
        if _empty(frame):
            return []
        out = []
        for period, row in frame.iterrows():
            if period not in PERIODS:
                continue
            analysts = finite(row.get("numberOfAnalysts"))
            out.append(Estimate(str(period), finite(row.get("avg")), finite(row.get("low")), finite(row.get("high")),
                                int(analysts) if analysts is not None else None))
        return out


def default_client() -> YahooClient:
    return YfinanceClient()
