"""What a check returns: one Finding per thing it would hold back or warn about."""
from __future__ import annotations

import json
from dataclasses import dataclass, field

SEVERITIES = ("hold", "warn", "info")


@dataclass(frozen=True)
class Finding:
    """One problem a check found. `field` is a valuation_daily column name, or "*" for every value column.
    `date_from`..`date_to` is the span of days it covers (`date_to` None = still going). `evidence` is plain
    data (numbers, dates, names) that explains the finding; it is stored as JSON."""
    check_name: str
    symbol: str
    field: str
    date_from: str
    date_to: str | None
    severity: str
    message: str
    evidence: dict = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.severity not in SEVERITIES:
            raise ValueError(f"severity must be one of {SEVERITIES}, not {self.severity!r}")


def still_open(store, check_name: str, symbol: str) -> list[Finding]:
    """This symbol's open hold and warn findings of one check, rebuilt unchanged. A check that could not look
    at a symbol today (its outside source failed) returns these with whatever it did find, so that recording the
    run does not clear what it could not re-examine: not asked is not cleared. Same keys as before, so they stay
    open with their first_seen."""
    out: list[Finding] = []
    for r in store.check_open_findings_of(check_name, symbol):
        try:
            evidence = json.loads(r["evidence"]) if r["evidence"] else {}
        except ValueError:
            evidence = {}
        out.append(Finding(r["check_name"], r["symbol"], r["field"], r["date_from"], r["date_to"], r["severity"],
                           r["message"], evidence))
    return out
