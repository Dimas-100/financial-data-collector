"""Plain, frozen dataclasses passed between adapters, collectors and the store.

Nothing here knows about SQL. period_start is '' (never None) for instant facts
so it can take part in a primary key.
"""
from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class AccountRef:
    label: str
    slug: str = "other"
    institution: str = "unknown"
    account_type: str = "other"


@dataclass(frozen=True)
class PositionRow:
    account: AccountRef
    symbol: str
    description: str | None
    quantity: float
    price: float | None
    market_value: float | None
    cost_basis_total: float | None = None
    avg_cost: float | None = None
    unrealized_pnl: float | None = None


@dataclass(frozen=True)
class CashRow:
    account: AccountRef
    amount: float
    currency: str = "USD"


@dataclass(frozen=True)
class Snapshot:
    as_of_date: str
    source: str
    positions: list[PositionRow]
    cash: list[CashRow]
    fetched_at: str | None = None
    warnings: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class TransactionRow:
    account: AccountRef
    trade_date: str
    type: str
    symbol: str | None
    units: float | None
    price: float | None
    amount: float | None
    fee: float | None
    description: str
    source: str
    settlement_date: str | None = None


@dataclass(frozen=True)
class PriceBar:
    date: str
    close: float
    adj_close: float
    dividend: float = 0.0
    split_factor: float = 1.0


@dataclass(frozen=True)
class Fact:
    taxonomy: str
    concept: str
    unit: str
    period_start: str  # '' for instants
    period_end: str
    value: float
    fy: int | None
    fp: str | None
    form: str
    filed: str
    accn: str
    frame: str | None = None


@dataclass(frozen=True)
class ConceptRule:
    line_item: str
    statement: str  # income | balance | cashflow
    kind: str  # duration | instant | per_share | shares
    taxonomy: str
    concept: str
    priority: int


@dataclass(frozen=True)
class LineItem:
    line_item: str
    period_kind: str  # annual | quarter
    period_start: str  # '' for instants
    period_end: str
    fiscal_year: int
    fiscal_quarter: int | None
    value: float
    concept: str
    filed: str
    accn: str
    is_derived: bool = False
