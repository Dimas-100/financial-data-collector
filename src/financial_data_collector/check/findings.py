"""What a check returns: one Finding per thing it would hold back or warn about."""
from __future__ import annotations

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
