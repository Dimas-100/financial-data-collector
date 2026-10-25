"""Economic numbers from FRED (St. Louis Fed): inflation, the Fed's range, the 10-year yield, unemployment."""
from __future__ import annotations

from datetime import date, timedelta
from typing import Callable

from ..connections.redact import redact
from ..models import MacroObservation

FRED_URL = ("https://api.stlouisfed.org/fred/series/observations?series_id={id}&api_key={key}"
            "&file_type=json&observation_start={start}")
# Claude OS reads these ids; do not rename or add without changing it. FRED's own units, stored as given.
SERIES = ("CPIAUCSL", "CPILFESL", "DFEDTARL", "DFEDTARU", "DGS10", "UNRATE")
FIRST_YEARS = 3          # depth of the first fetch
REFRESH_DAYS = 400       # later runs re-read this far back, which picks up FRED's revisions

FredFetch = Callable[[str], dict]


class MacroError(Exception):
    """One or more series failed. The message is safe to show (no key); `observations` holds what did arrive."""

    def __init__(self, message: str, observations: list[MacroObservation]):
        super().__init__(message)
        self.observations = observations


def start_date(today: date, have_rows: bool) -> str:
    if have_rows:
        return (today - timedelta(days=REFRESH_DAYS)).isoformat()
    try:
        return today.replace(year=today.year - FIRST_YEARS).isoformat()
    except ValueError:  # Feb 29
        return (today - timedelta(days=365 * FIRST_YEARS)).isoformat()


def _parse(series_id: str, data: object) -> list[MacroObservation]:
    obs = data.get("observations") if isinstance(data, dict) else None
    if not isinstance(obs, list):
        raise ValueError("unexpected answer (no observations)")
    out: list[MacroObservation] = []
    for o in obs:
        try:
            value = float(o["value"])      # FRED writes "." for a day with no number
            out.append(MacroObservation(series_id, str(o["date"])[:10], value))
        except (KeyError, TypeError, ValueError):
            continue
    return out


def collect_macro(api_key: str, fetch: FredFetch, start: str) -> list[MacroObservation]:
    """Every series from `start`. `fetch(url) -> dict` is the parsed JSON. A series that fails does not stop the
    others: after all are tried, any failure raises MacroError (key scrubbed) carrying what did arrive."""
    got: list[MacroObservation] = []
    failed: list[str] = []
    for series_id in SERIES:
        try:
            got.extend(_parse(series_id, fetch(FRED_URL.format(id=series_id, key=api_key, start=start))))
        except Exception as e:
            failed.append(f"{series_id} ({redact(f'{type(e).__name__}: {e}', [api_key])})")
    if failed:
        raise MacroError("failed: " + "; ".join(failed), got) from None
    return got
