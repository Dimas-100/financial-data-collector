"""Plain, frozen dataclasses passed between adapters, collectors and the store.

Nothing here knows about SQL. period_start is '' (never None) for instant facts
so it can take part in a primary key.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, time
from zoneinfo import ZoneInfo


@dataclass(frozen=True)
class AccountRef:
    label: str
    slug: str = "other"
    institution: str = "unknown"
    account_type: str = "other"
    external_key: str | None = None  # set for an account from a connection: its identity, never its label
    origin: str = "file"             # file | snaptrade | simplefin
    kind_confirmed: bool = True      # False while account_type is a guess the person hasn't confirmed
    flows: str = "transactions"      # balance: no transactions explain its changes, so every change is money moved


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
    available: float | None = None   # a card's remaining credit, when the service reports it


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


_NEW_YORK = ZoneInfo("America/New_York")
_FINAL_AFTER = time(16, 15)  # the close at 16:00 plus a quarter hour for the day's last prints to settle


def is_final(bar_date: str, fetched_at: datetime) -> bool:
    """A bar is the day's final close when it was fetched on a later New York day, or after 16:15 on its own day."""
    ny = fetched_at.astimezone(_NEW_YORK)
    day = ny.date().isoformat()
    return day > bar_date or (day == bar_date and ny.time() >= _FINAL_AFTER)


@dataclass(frozen=True)
class MacroObservation:
    series_id: str
    obs_date: str
    value: float


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
    filed: str  # latest filing that reported the period (the restatement in force)
    accn: str
    is_derived: bool = False
    first_filed: str = ""  # when the period was first public; point-in-time analysis uses this


@dataclass(frozen=True)
class FundSnapshot:
    """What Yahoo said about a fund on one New York day. Yield and expense ratio are fractions (0.0121 = 1.21%).
    `source` is 'yfinance', or 'proxy: SPY via yfinance' when an ETF tracking the same index supplied the P/E and yield."""
    symbol: str
    date: str
    trailing_pe: float | None
    forward_pe: float | None
    dividend_yield: float | None
    expense_ratio: float | None
    source: str
