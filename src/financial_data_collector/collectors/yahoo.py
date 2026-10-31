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


class _Kept(logging.Handler):
    """Keeps the error lines yfinance logs during one ask, instead of printing them."""

    def __init__(self) -> None:
        super().__init__(logging.ERROR)
        self.records: list[logging.LogRecord] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.records.append(record)


@contextmanager
def _quiet() -> Iterator[list[logging.LogRecord]]:
    """yfinance (1.7) hides a failed request: with its default `hide_exceptions` it logs the error on the `yfinance`
    logger and hands back {} or an empty frame, which would read as "Yahoo has nothing". While asking, that logger's
    lines go only to a handler that keeps the errors (nothing reaches the console) and the caller decides what one
    means. The logger's handlers, level and propagation are put back afterwards."""
    logger = logging.getLogger("yfinance")
    handlers, level, propagate = logger.handlers[:], logger.level, logger.propagate
    kept = _Kept()
    logger.handlers = [kept]
    logger.setLevel(logging.ERROR)
    logger.propagate = False
    try:
        yield kept.records
    finally:
        logger.handlers = handlers
        logger.setLevel(level)
        logger.propagate = propagate


def _text(record: logging.LogRecord) -> str:
    try:
        return record.getMessage()
    except Exception:           # a malformed log call must not hide the error it reports
        return str(record.msg)


def _fail_on(errors: list[logging.LogRecord], what: str) -> None:
    """An error yfinance logged during the ask means the answer is not Yahoo's "nothing" but a failed request: raise,
    so the collector does not mark the symbol and asks again. The text is cut (Yahoo may send a whole page) and is
    redacted by the collector."""
    if errors:
        raise RuntimeError(f"yfinance logged an error asking {what}: " + "; ".join(_text(r) for r in errors)[:300])


def _empty(frame: object) -> bool:
    return frame is None or getattr(frame, "empty", True)


class YfinanceClient:
    """The real client. Every ask runs with yfinance's own error logging kept off the console (see `_quiet`); an error
    it logged while asking for a fund's info or a company's estimates raises."""

    def fund_info(self, symbol: str) -> dict | None:
        import yfinance as yf
        with _quiet() as errors:
            info = yf.Ticker(to_yfinance(symbol)).info
        _fail_on(errors, "for the fund's info")
        return info or None

    def earnings_dates(self, symbol: str, limit: int) -> list[YahooEarning]:
        """`get_earnings_dates` has a tz-aware index and the columns `EPS Estimate`, `Reported EPS` and `Surprise(%)`
        (percent); NaN becomes None. A failed ask is logged and comes back as None, which is [] here: the merge then
        keeps what was stored, so nothing is lost."""
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
        with _quiet() as errors:
            frame = yf.Ticker(to_yfinance(symbol)).earnings_estimate
        _fail_on(errors, "for the estimates")
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
