"""The checker: independent checks of the numbers `derive` wrote.

A check reads the warehouse through `Store.check_*` methods and, where it needs an outside opinion, through
`ctx.outside`. It never imports the code it checks (`derive/`, `statements.py`, the collectors): a checker that
shares code with what it checks shares its bugs. `run_check` runs the registered checks, records what they
found, and clears a check's earlier findings that it no longer finds.
"""
from __future__ import annotations

from dataclasses import dataclass
import time
from datetime import datetime, timedelta, timezone
from typing import Any, Callable

from .. import http
from ..config import Config, ConfigError
from ..store import Store
from .findings import Finding

__all__ = ["CHECKS", "Check", "CheckContext", "CheckReport", "Finding", "run_check"]


@dataclass
class CheckContext:
    """What a check is given. `outside` is an `outside.OutsideSource` (`quote(symbol, on)` and `figures(symbol)`);
    `run_check` builds the real one only when a check first uses it."""
    store: Store
    cfg: Config
    fetch: http.Fetch
    outside: Any
    now: datetime
    sleep: Callable[[float], None] = time.sleep     # the pause between companies that a check asks an outside service about


Check = Callable[[CheckContext], list[Finding]]


class _LazyOutside:
    """The default outside source, built on first use: a run of offline checks never builds it (nor imports yfinance)."""

    def __init__(self) -> None:
        self._source: Any = None

    def _get(self) -> Any:
        if self._source is None:
            self._source = outside_mod.default_source()
        return self._source

    def quote(self, symbol: str, on: str) -> Any:
        return self._get().quote(symbol, on)

    def figures(self, symbol: str) -> Any:
        return self._get().figures(symbol)

CHECKS: dict[str, Check] = {}      # name -> check; the checks register themselves here


@dataclass
class CheckReport:
    run_id: str
    holds: int
    warns: int
    errors: list[str]              # one "name: ExceptionType: message" per check that failed
    checks_run: int = 0
    open_holds: int = 0            # holds open after this run (any check's), not just those found by it


def run_check(cfg: Config, *, fetch: http.Fetch = http.fetch, outside: Any = None, now: datetime | None = None,
              only: list[str] | None = None, store: Store | None = None,
              sleep: Callable[[float], None] = time.sleep) -> CheckReport:
    """Run the registered checks (or just `only`), record the run and its findings. A check that raises is
    reported in `errors` and leaves its own open findings as they were; the others still run. That covers what a
    check returns too: anything but a list of Findings under its own name, each naming a valuation_daily column
    (or "*"), is that check's error. Pass `store` to
    use a connection that is already open (the sync does); otherwise one is opened and closed here."""
    now = now or datetime.now(timezone.utc)
    names = list(CHECKS)
    if only:
        unknown = [n for n in only if n not in CHECKS]
        if unknown:
            raise ConfigError(f"unknown check(s): {', '.join(unknown)}; valid: {', '.join(CHECKS) or 'none'}")
        names = [n for n in names if n in only]
    run_id = now.strftime("%Y%m%dT%H%M%SZ")
    stamp = now.strftime("%Y-%m-%dT%H:%M:%SZ")
    t0 = time.monotonic()
    own = store is None
    st = store or Store.open(cfg.db_path, backup_dir=cfg.backup_dir)
    try:
        ctx = CheckContext(store=st, cfg=cfg, fetch=fetch, outside=outside if outside is not None else _LazyOutside(),
                           now=now, sleep=sleep)
        holds = warns = 0
        errors: list[str] = []
        fields = set(st.check_valuation_columns()) | {"*"}
        for name in names:
            try:
                found = CHECKS[name](ctx)
                if found is None or isinstance(found, (str, bytes)):
                    raise TypeError(f"returned {type(found).__name__}, not a list of Finding")
                found = list(found)
                for f in found:
                    if not isinstance(f, Finding):
                        raise TypeError(f"returned a {type(f).__name__}, not a Finding")
                    if f.check_name != name:
                        raise ValueError(f"returned a finding for check {f.check_name!r}")
                    if f.field not in fields:       # a hold names a column the view can blank; anything else blanks nothing
                        raise ValueError(f"returned a finding for {f.field!r}, which is not a valuation_daily column or '*'")
                st.check_record(name, found, stamp)     # one transaction: an error leaves its findings as they were
            except Exception as e:      # one broken check must not hide the others; its findings stay as they were
                errors.append(f"{name}: {type(e).__name__}: {e}")
                continue
            holds += sum(1 for f in found if f.severity == "hold")
            warns += sum(1 for f in found if f.severity == "warn")
        finished = (now + timedelta(seconds=time.monotonic() - t0)).strftime("%Y-%m-%dT%H:%M:%SZ")
        ran = len(names) - len(errors)
        st.check_log_run(run_id, started_at=stamp, finished_at=finished, derive_run_id=st.check_derive_run_id(),
                         checks_run=ran, holds=holds, warns=warns, error="; ".join(errors) or None)
        return CheckReport(run_id=run_id, holds=holds, warns=warns, errors=errors, checks_run=ran,
                           open_holds=st.check_open_count("hold"))
    finally:
        if own:
            st.close()


from . import offline, filings, outside as outside_mod  # noqa: E402,F401  (register the checks in CHECKS; last, because they import from here)
