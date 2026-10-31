"""The checker: independent checks of the numbers `derive` wrote.

A check reads the warehouse through `Store.check_*` methods and, where it needs an outside opinion, through
`ctx.outside`. It never imports the code it checks (`derive/`, `statements.py`, the collectors): a checker that
shares code with what it checks shares its bugs. `run_check` runs the registered checks, records what they
found, and clears a check's earlier findings that it no longer finds.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Callable

from .. import http
from ..config import Config, ConfigError
from ..store import Store
from .findings import Finding

__all__ = ["CHECKS", "Check", "CheckContext", "CheckReport", "Finding", "run_check"]


@dataclass
class CheckContext:
    """What a check is given. `outside` is an object with `quote(symbol) -> Quote | None` and
    `figures(symbol) -> Figures | None`; it is built by `check.outside.default_source()`, which a later task
    writes. It is None until then."""
    store: Store
    cfg: Config
    fetch: http.Fetch
    outside: Any
    now: datetime


Check = Callable[[CheckContext], list[Finding]]

CHECKS: dict[str, Check] = {}      # name -> check; the checks register themselves here


@dataclass
class CheckReport:
    run_id: str
    holds: int
    warns: int
    errors: list[str]              # one "name: ExceptionType: message" per check that raised
    checks_run: int = 0


def run_check(cfg: Config, *, fetch: http.Fetch = http.fetch, outside: Any = None, now: datetime | None = None,
              only: list[str] | None = None, store: Store | None = None) -> CheckReport:
    """Run the registered checks (or just `only`), record the run and its findings. A check that raises is
    reported in `errors` and leaves its own open findings as they were; the others still run. Pass `store` to
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
    own = store is None
    st = store or Store.open(cfg.db_path, backup_dir=cfg.backup_dir)
    try:
        ctx = CheckContext(store=st, cfg=cfg, fetch=fetch, outside=outside, now=now)
        holds = warns = 0
        errors: list[str] = []
        for name in names:
            try:
                found = CHECKS[name](ctx)
            except Exception as e:      # one broken check must not hide the others; its findings stay as they were
                errors.append(f"{name}: {type(e).__name__}: {e}")
                continue
            st.check_record(run_id, name, found, stamp)
            holds += sum(1 for f in found if f.severity == "hold")
            warns += sum(1 for f in found if f.severity == "warn")
        st.check_log_run(run_id, started_at=stamp, finished_at=datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
                         derive_run_id=st.check_derive_run_id(), checks_run=len(names) - len(errors),
                         holds=holds, warns=warns, error="; ".join(errors) or None)
        return CheckReport(run_id=run_id, holds=holds, warns=warns, errors=errors, checks_run=len(names) - len(errors))
    finally:
        if own:
            st.close()
