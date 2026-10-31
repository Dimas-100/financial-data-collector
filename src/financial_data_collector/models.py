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


@dataclass(frozen=True)
class YahooEarning:
    """One row of Yahoo's earnings dates: when (tz-aware; exactly midnight New York when Yahoo knows only the day),
    EPS expected and reported, and the surprise in percent (6.74 = +6.74%)."""
    when: datetime
    eps_expected: float | None
    eps_reported: float | None
    surprise_pct: float | None


@dataclass(frozen=True)
class Estimate:
    """Yahoo's EPS estimate for one period: `0q` this quarter, `+1q` next quarter, `0y` this year, `+1y` next year."""
    period: str
    eps_avg: float | None
    eps_low: float | None
    eps_high: float | None
    analysts: int | None


@dataclass(frozen=True)
class EarningsEvent:
    """One earnings report of a company. `date_source` is 'sec' when an 8-K with item 2.02 dates it (its accession
    kept), else 'yahoo'; `time_source` and `eps_source` say who gave the time of day and the EPS figures."""
    symbol: str
    report_date: str
    time_of_day: str | None             # before_open | during | after_close (New York), None when only the day is known
    accession: str | None
    eps_expected: float | None
    eps_reported: float | None
    surprise_pct: float | None          # percent, as Yahoo gives it
    date_source: str
    time_source: str | None
    eps_source: str | None


# The metrics a valuation score is kept for. The checker keeps its own copy of the band rule, not of these names.
SCORE_METRICS: tuple[str, ...] = (
    "pe", "p_fcf", "ps", "ev_ebit", "dividend_yield", "implied_growth", "fcf_growth_5y", "revenue_growth_5y",
    "fcf_margin_change", "op_margin_change", "net_debt_ebitda", "buyback_yield", "last_surprise",
    "estimate_change_30d", "insider_net_90d", "expense_ratio", "off_high_52w", "vs_200d", "week_vs_market",
    "week_vs_sector",
)
# The checks the sync runs again right after the engine writes (check.engine.ENGINE_CHECKS is this). A run of no more
# checks than these is a partial run: the store's `check_last_run` skips it for the last full one.
ENGINE_CHECK_NAMES: tuple[str, ...] = ("scores", "split")
# The asset types of a fund (ETF, mutual fund): the funds step snapshots them and the engine scores them from the snapshots.
FUND_TYPES: tuple[str, ...] = ("etf", "mutual_fund", "fund")
SCORE_STATUSES: tuple[str, ...] = ("ok", "held", "unsupported", "not_enough_history")
SCORE_BANDS: tuple[str | None, ...] = (None, "cheap", "usual", "premium")


@dataclass(frozen=True)
class ScoreRow:
    """One metric of one symbol on one day: the value, where it sits in the symbol's own history (percentile and
    band), why it has none (status, status_reason), and what it came from (inputs, sources)."""
    symbol: str
    date: str
    metric: str
    value: float | None
    percentile_5y: float | None
    band: str | None
    status: str
    status_reason: str | None
    inputs: tuple[str, ...]
    sources: tuple[dict, ...]

    def __post_init__(self) -> None:
        if self.metric not in SCORE_METRICS:
            raise ValueError(f"unknown score metric {self.metric!r}")
        if self.status not in SCORE_STATUSES:
            raise ValueError(f"unknown score status {self.status!r}")
        if self.band not in SCORE_BANDS:
            raise ValueError(f"unknown score band {self.band!r}")


@dataclass(frozen=True)
class PriceMove:
    """A drop worth explaining: `span` is '1d' or '5d' (from `start_date` to `date`), `move` a fraction (-0.05),
    the market's and the sector fund's move over the same span, how many of its own standard deviations a day's
    excess over the market was, which triggers fired, and the causes found (each with its sources)."""
    symbol: str
    date: str
    span: str
    start_date: str
    move: float
    market_symbol: str | None
    market_move: float | None
    sector_symbol: str | None
    sector_move: float | None
    excess_sd: float | None
    triggers: tuple[str, ...]
    causes: tuple[dict, ...]
    sources: tuple[dict, ...]


@dataclass(frozen=True)
class CurrentReport:
    """An 8-K (or similar) SEC lists for a company: its accession, form, filing date and item numbers."""
    cik: str
    accession: str
    form: str
    filed: str
    items: tuple[str, ...]


@dataclass(frozen=True)
class Headline:
    """One headline from the sibling news feed; `feed_as_of` is the feed's own date."""
    symbol: str
    date: str
    title: str
    outlet: str | None
    url: str | None
    published: str | None
    feed_as_of: str | None


@dataclass(frozen=True)
class TargetEntry:
    """One line of the owner's targets file: a holding's target share in an account, with its band.
    `target`, `low` and `high` are percent (15.0 = 15%), except for `direct_passive_ratio`, where they are a plain ratio;
    `kind` says what the share is of; `rule` is how the file lets it be rebalanced (`unknown` keeps the spelling
    it was given in `raw_rule`)."""
    account: str
    symbol: str
    kind: str          # pct_of_account | pct_of_brokerage | direct_passive_ratio
    target: float
    low: float
    high: float
    rule: str          # trades | contributions | hold | unknown
    raw_rule: str | None


@dataclass(frozen=True)
class Sleeve:
    """A group of holdings the targets file limits together; `low` / `high` are percent, None when it gives none."""
    account: str
    label: str
    members: tuple[str, ...]
    low: float | None
    high: float | None
    rule: str


@dataclass(frozen=True)
class Cap:
    """A household-wide ceiling on one holding, in percent of the household (12.0 = 12%)."""
    symbol: str
    high: float


@dataclass(frozen=True)
class Targets:
    """The targets file as read: its entries, sleeves and caps, when it was last revised, its file name (never its
    path) and a line for everything in it that was skipped or not understood."""
    entries: tuple[TargetEntry, ...]
    sleeves: tuple[Sleeve, ...]
    caps: tuple[Cap, ...]
    last_revised: str | None
    file_name: str
    problems: tuple[str, ...]


@dataclass(frozen=True)
class SplitLine:
    """One line of a weekly split: whole dollars of an account's waiting cash toward one holding. `target_gap` is the
    dollars between the holding and its target before the band's tilt; `tilt` the factor its band gave; `reasons`
    the band's reason codes, then the split's own (`trades_as_contributions`, `no_scores`, `look_through_assumed`,
    `sleeve_cut:<label>`, `cap_cut:<symbol>`, `band_top`, `folded`)."""
    symbol: str
    dollars: int
    target_gap: float
    band: str
    tilt: float
    reasons: tuple[str, ...]


PROPOSAL_STATUSES = ("open", "replaced", "spent")


@dataclass(frozen=True)
class AllocationProposal:
    """One account's weekly split as it is stored: `account_key` is the targets file's name for the account (never
    its label), `cash_date` / `cash` the waiting cash it was made from, `placed` the number of lines, `waits` the
    dollars left for next week, `notes` the plan's notes, `sources` the proposal's own sources (the cash row).
    `line_sources[i]` belongs to `lines[i]`."""
    account_key: str
    cash_date: str
    cash: float
    placed: int
    waits: float
    lines: tuple[SplitLine, ...]
    line_sources: tuple[tuple[dict, ...], ...]
    notes: tuple[dict, ...]
    sources: tuple[dict, ...]
