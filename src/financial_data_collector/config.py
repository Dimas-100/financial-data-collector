"""config.toml (paths, cadences) + .env (secrets). Both gitignored; examples committed."""
from __future__ import annotations

import math
import os
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import dotenv_values

from .sectors import DEFAULT_SECTOR_ETFS, SECTORS
from .symbols import canonical

CONFIG_EXAMPLE = """# financial-data-collector configuration. Copy to config.toml (gitignored).
# Relative paths resolve against this file's folder.

[paths]
db = "data/warehouse.db"
inbox = "inbox"                    # drop Fidelity CSV exports here; they move to inbox/processed
watchlist = "watchlist.txt"        # optional: one extra symbol per line to collect prices/SEC for

[prices]
lookback_years = 5                 # first fetch depth per symbol

[sec]
max_age_hours = 24                 # refetch a company's facts at most this often

[connections]
min_hours = 6                      # ask a connected service (fdc connect) at most this often

[benchmarks]                       # what a move is measured against; their prices are collected like any symbol, never counted as holdings
market = "SPY"
# sectors = { "Technology" = "XLK", "Health Care" = "XLV" }   # replaces the default: the 11 Select Sector SPDRs

[fund_proxies]                     # a fund Yahoo has no P/E or yield for: an ETF tracking the same index stands in; fund_snapshots.source says so
# "XYZFX" = "SPY"

[sources.snaptrade]
enabled = true                     # owner-only: investing's SnapTrade export. Ignored if dir is missing.
dir = "../investing/fidelity/dashboard-data"

[sources.investing]
content_dir = "../investing"       # owner-only: theses/ + watchlist/ frontmatter tickers and basket.txt join the universe. Ignored if missing.

[export.cockpit]
enabled = false                    # write prices.json / dividends.json / fundamentals.json for the investing cockpit
dir = "../investing/data"          # must already exist; never created

[check]                            # thresholds of the independent checks (fdc check); these are the defaults
fresh_warn_months = 5              # warn when the newest statements end more than this many months before the price
fresh_hold_months = 15             # hold the valuation beyond this many
recompute_tolerance = 0.01         # a stored ratio may differ from its recomputed value by this fraction
max_daily_move = 0.40              # a one-day close move beyond this fraction (no split that day) is impossible
price_warn = 0.005                 # the newest close differs from a second provider's by more than this fraction: warn
price_hold = 0.03                  # ... by more than this: hold the symbol's values
figure_warn = 0.15                 # a figure (EPS, market cap, revenue, free cash flow) differs from Yahoo's by more than this: warn
figure_hold = 0.40                 # ... by more than this: hold that figure
close_warn_days = 2                # the newest final close is this many trading days old: warn
close_hold_days = 5                # ... this many: hold the symbol's values

[engine]                           # the valuation engine (fdc sync's engine step: no network, no AI); these are the defaults
history_years = 5                  # a score's percentile is against this many years of the symbol's own daily history
min_history_days = 365             # ... and needs at least this many days of it (else "not_enough_history", no band)
cheap_percentile = 25              # under this percentile of its own history: cheap (a yield: over 100 minus this)
premium_percentile = 75            # over this: premium (a yield: under 100 minus this)
discount_rate = 0.09               # implied growth: the yearly return the market cap is discounted at
terminal_growth = 0.025            # ... and the yearly growth after its 10 years
drop_day = 0.04                    # a drop: a final close down this fraction in a day
drop_week = 0.08                   # ... or this much over 5 trading days
excess_sd = 2.5                    # ... or a day's move minus SPY's beyond this many of its own 1-year standard deviations
moves_days = 365                   # price_moves covers this many calendar days back

[classify]                         # manual asset-type overrides: SYMBOL = "stock|etf|mutual_fund|money_market|crypto|fund|foreign_filer"
# VXUS = "etf"

[share_classes]                    # per-share figures a company states for one class only (SEC facts carry no class split)
# "XYZ.B" = { per = "XYZ.A", ratio = 1500 }   # ratio = how many XYZ.B make one XYZ.A; EPS is divided, shares multiplied
"""

ENV_EXAMPLE = """# Copy to .env (gitignored). Values are never logged.
# Required for SEC EDGAR (they block anonymous clients): your name and email.
SEC_USER_AGENT=Your Name you@example.com
# Optional: Tiingo token for prices (free at tiingo.com). Without it, yfinance is used.
TIINGO_API_TOKEN=
# Optional: FRED key for economic numbers (inflation, the Fed's rate, the 10-year, unemployment). Free at
# fred.stlouisfed.org (My Account, API Keys). Without it the macro step is skipped.
FRED_API_KEY=
# A connection (fdc connect) keeps its key in your computer's key store. Only on a computer without one
# does fdc connect write it here instead (never paste a key here yourself unless there is no key store):
# FDC_SNAPTRADE_CLIENT_ID=
# FDC_SNAPTRADE_CONSUMER_KEY=
# FDC_SIMPLEFIN_ACCESS_URL=
"""


class ConfigError(Exception):
    pass


@dataclass(frozen=True)
class ShareClass:
    """A symbol whose company states per-share figures in another class's terms.

    ratio = how many shares of this symbol make one share of `per`: EPS is divided by it, shares multiplied.
    """
    per: str
    ratio: float


@dataclass(frozen=True)
class CheckSettings:
    """Thresholds of the offline checks (config [check]); the defaults are the spec's."""
    fresh_warn_months: int = 5
    fresh_hold_months: int = 15
    recompute_tolerance: float = 0.01
    max_daily_move: float = 0.40
    price_warn: float = 0.005
    price_hold: float = 0.03
    figure_warn: float = 0.15
    figure_hold: float = 0.40
    close_warn_days: int = 2
    close_hold_days: int = 5


@dataclass(frozen=True)
class EngineSettings:
    """Settings of the valuation engine (config [engine]); the defaults are the spec's."""
    history_years: int = 5
    min_history_days: int = 365
    cheap_percentile: float = 25.0
    premium_percentile: float = 75.0
    discount_rate: float = 0.09
    terminal_growth: float = 0.025
    drop_day: float = 0.04
    drop_week: float = 0.08
    excess_sd: float = 2.5
    moves_days: int = 365


@dataclass(frozen=True)
class Benchmarks:
    """The yardsticks a move is measured against: one fund for the whole market and one per sector."""
    market: str = "SPY"
    sectors: dict[str, str] = field(default_factory=lambda: dict(DEFAULT_SECTOR_ETFS))

    @property
    def symbols(self) -> tuple[str, ...]:
        """The market fund first, then the sector funds in SECTORS order, each once."""
        out: list[str] = [self.market]
        for name in SECTORS:
            if name in self.sectors and self.sectors[name] not in out:
                out.append(self.sectors[name])
        return tuple(out)


@dataclass
class Config:
    root: Path
    db_path: Path
    inbox: Path
    processed_dir: Path
    watchlist: Path
    backup_dir: Path
    cache_dir: Path
    lookback_years: int = 5
    sec_max_age_hours: int = 24
    connections_min_hours: float = 6.0
    snaptrade_enabled: bool = True
    snaptrade_dir: Path | None = None
    classify: dict[str, str] = field(default_factory=dict)
    share_classes: dict[str, ShareClass] = field(default_factory=dict)
    check: CheckSettings = field(default_factory=CheckSettings)
    engine: EngineSettings = field(default_factory=EngineSettings)
    benchmarks: Benchmarks = field(default_factory=Benchmarks)
    fund_proxies: dict[str, str] = field(default_factory=dict)
    tiingo_token: str | None = None
    fred_api_key: str | None = None
    sec_user_agent: str | None = None
    investing_dir: Path | None = None
    export_cockpit: bool = False
    export_dir: Path | None = None

    @property
    def heartbeat_path(self) -> Path:
        """Where a running sync keeps its heartbeat (heartbeat.py): beside the database."""
        return self.db_path.parent / "sync.heartbeat.json"


def init_project(root: Path) -> list[str]:
    """Create the gitignored working layout. Never overwrites. Returns what it created."""
    created: list[str] = []
    for rel in ("data", "data/backups", "data/cache", "inbox", "inbox/processed"):
        p = root / rel
        if not p.exists():
            p.mkdir(parents=True)
            created.append(rel + "/")
    for name, text in (("config.toml", CONFIG_EXAMPLE), (".env", ENV_EXAMPLE)):
        p = root / name
        if not p.exists():
            p.write_text(text, encoding="utf-8")
            created.append(name)
    return created


_PLACEHOLDERS = {"Your Name you@example.com"}  # the untouched .env example counts as unset


def _secret(name: str, dotenv: dict[str, str | None]) -> str | None:
    v = os.environ.get(name) or dotenv.get(name) or ""
    v = v.strip()
    return None if (not v or v in _PLACEHOLDERS) else v


def _share_classes(raw: object, cfg_path: Path) -> dict[str, ShareClass]:
    if not isinstance(raw, dict):
        raise ConfigError(f"{cfg_path}: [share_classes] must be a table")
    out: dict[str, ShareClass] = {}
    for symbol, entry in raw.items():
        per = entry.get("per") if isinstance(entry, dict) else None
        ratio = entry.get("ratio") if isinstance(entry, dict) else None
        if not isinstance(per, str) or not per.strip():
            raise ConfigError(f'{cfg_path}: [share_classes] "{symbol}" needs per = "SYMBOL" (the class its figures are stated for)')
        if not isinstance(ratio, (int, float)) or isinstance(ratio, bool) or not ratio > 0:
            raise ConfigError(f'{cfg_path}: [share_classes] "{symbol}" needs ratio = a number above 0')
        out[str(symbol).upper()] = ShareClass(per.strip().upper(), float(ratio))
    return out


def _check_settings(raw: object, cfg_path: Path) -> CheckSettings:
    if not isinstance(raw, dict):
        raise ConfigError(f"{cfg_path}: [check] must be a table")
    defaults = CheckSettings()
    unknown = sorted(set(raw) - set(defaults.__dataclass_fields__))
    if unknown:
        raise ConfigError(f"{cfg_path}: [check] has no setting {', '.join(unknown)}")
    values: dict[str, object] = {}
    for name in ("fresh_warn_months", "fresh_hold_months"):
        v = raw.get(name, getattr(defaults, name))
        if not isinstance(v, int) or isinstance(v, bool) or v < 1:
            raise ConfigError(f"{cfg_path}: [check] {name} must be a whole number of months, 1 or more")
        values[name] = v
    if values["fresh_warn_months"] > values["fresh_hold_months"]:
        raise ConfigError(f"{cfg_path}: [check] fresh_warn_months must not be more than fresh_hold_months")
    for name in ("close_warn_days", "close_hold_days"):
        v = raw.get(name, getattr(defaults, name))
        if not isinstance(v, int) or isinstance(v, bool) or v < 1:
            raise ConfigError(f"{cfg_path}: [check] {name} must be a whole number of trading days, 1 or more")
        values[name] = v
    if values["close_warn_days"] > values["close_hold_days"]:
        raise ConfigError(f"{cfg_path}: [check] close_warn_days must not be more than close_hold_days")
    for name in ("recompute_tolerance", "max_daily_move", "price_warn", "price_hold", "figure_warn", "figure_hold"):
        v = raw.get(name, getattr(defaults, name))
        if not isinstance(v, (int, float)) or isinstance(v, bool) or not v > 0:
            raise ConfigError(f"{cfg_path}: [check] {name} must be a number above 0")
        values[name] = float(v)
    for warn, hold in (("price_warn", "price_hold"), ("figure_warn", "figure_hold")):
        if values[warn] > values[hold]:
            raise ConfigError(f"{cfg_path}: [check] {warn} must not be more than {hold}")
    return CheckSettings(**values)


ENGINE_PERCENTILES = ("cheap_percentile", "premium_percentile")      # percent, 25 = the 25th percentile
ENGINE_FRACTIONS = ("drop_day", "drop_week", "discount_rate", "terminal_growth")   # fractions, 0.04 = 4%


def _engine_settings(raw: object, cfg_path: Path) -> EngineSettings:
    if not isinstance(raw, dict):
        raise ConfigError(f"{cfg_path}: [engine] must be a table")
    defaults = EngineSettings()
    unknown = sorted(set(raw) - set(defaults.__dataclass_fields__))
    if unknown:
        raise ConfigError(f"{cfg_path}: [engine] has no setting {', '.join(unknown)}")
    values: dict[str, object] = {}
    for name in ("history_years", "min_history_days", "moves_days"):
        v = raw.get(name, getattr(defaults, name))
        if not isinstance(v, int) or isinstance(v, bool) or v < 1:
            raise ConfigError(f"{cfg_path}: [engine] {name} must be a whole number, 1 or more")
        values[name] = v
    for name in ("cheap_percentile", "premium_percentile", "discount_rate", "terminal_growth",
                 "drop_day", "drop_week", "excess_sd"):
        v = raw.get(name, getattr(defaults, name))
        if not isinstance(v, (int, float)) or isinstance(v, bool) or not math.isfinite(v) or not v > 0:
            raise ConfigError(f"{cfg_path}: [engine] {name} must be a finite number above 0")
        if name in ENGINE_PERCENTILES and v < 1:
            raise ConfigError(f"{cfg_path}: [engine] {name} must be a percentile such as 25, not 0.25")
        if name in ENGINE_FRACTIONS and not v < 1:
            raise ConfigError(f"{cfg_path}: [engine] {name} must be below 1: a fraction, 0.04 = 4%")
        values[name] = float(v)
    if not values["cheap_percentile"] < values["premium_percentile"]:
        raise ConfigError(f"{cfg_path}: [engine] cheap_percentile must be less than premium_percentile")
    if not values["premium_percentile"] < 100:
        raise ConfigError(f"{cfg_path}: [engine] premium_percentile must be less than 100")
    if not values["terminal_growth"] < values["discount_rate"]:
        raise ConfigError(f"{cfg_path}: [engine] terminal_growth must be less than discount_rate")
    return EngineSettings(**values)


def _benchmarks(raw: object, cfg_path: Path) -> Benchmarks:
    if not isinstance(raw, dict):
        raise ConfigError(f"{cfg_path}: [benchmarks] must be a table")
    unknown = sorted(set(raw) - {"market", "sectors"})
    if unknown:
        raise ConfigError(f"{cfg_path}: [benchmarks] has no setting {', '.join(unknown)}")
    market = raw.get("market", Benchmarks().market)
    if not isinstance(market, str) or not market.strip():
        raise ConfigError(f'{cfg_path}: [benchmarks] market must be a symbol, such as "SPY"')
    sector_funds = raw.get("sectors")
    if sector_funds is None:
        return Benchmarks(market=canonical(market))
    if not isinstance(sector_funds, dict):
        raise ConfigError(f'{cfg_path}: [benchmarks] sectors must be a table, such as {{ "Technology" = "XLK" }}')
    bad = [name for name in sector_funds if name not in SECTORS]
    if bad:
        raise ConfigError(f'{cfg_path}: [benchmarks] sectors has no sector "{bad[0]}"; the sectors are: {", ".join(SECTORS)}')
    for name, symbol in sector_funds.items():
        if not isinstance(symbol, str) or not symbol.strip():
            raise ConfigError(f'{cfg_path}: [benchmarks] sectors "{name}" needs a symbol, such as "XLK"')
    return Benchmarks(market=canonical(market), sectors={name: canonical(symbol) for name, symbol in sector_funds.items()})


def _fund_proxies(raw: object, cfg_path: Path) -> dict[str, str]:
    if not isinstance(raw, dict):
        raise ConfigError(f"{cfg_path}: [fund_proxies] must be a table")
    out: dict[str, str] = {}
    for fund, proxy in raw.items():
        if not isinstance(proxy, str) or not proxy.strip():
            raise ConfigError(f'{cfg_path}: [fund_proxies] "{fund}" needs a symbol, such as "SPY"')
        out[canonical(str(fund))] = canonical(proxy)
    return out


def load_config(root: Path) -> Config:
    root = Path(root)
    cfg_path = root / "config.toml"
    if not cfg_path.is_file():
        raise ConfigError(f"{cfg_path} not found. Run: fdc init")
    try:
        raw = tomllib.loads(cfg_path.read_text(encoding="utf-8"))
    except tomllib.TOMLDecodeError as e:
        raise ConfigError(f"{cfg_path}: {e}") from e
    paths = raw.get("paths", {})
    prices = raw.get("prices", {})
    sec = raw.get("sec", {})
    connections = raw.get("connections", {})
    sources = raw.get("sources", {})
    snap = sources.get("snaptrade", {})
    investing = sources.get("investing", {})
    export = raw.get("export", {}).get("cockpit", {})
    db_path = (root / paths.get("db", "data/warehouse.db")).resolve()
    inbox = (root / paths.get("inbox", "inbox")).resolve()
    dotenv = dotenv_values(root / ".env") if (root / ".env").is_file() else {}
    snap_dir = snap.get("dir", "../investing/fidelity/dashboard-data")
    investing_dir = investing.get("content_dir", "../investing")
    export_dir = export.get("dir", "../investing/data")
    min_hours = connections.get("min_hours", 6)
    if not isinstance(min_hours, (int, float)) or isinstance(min_hours, bool) or min_hours < 0:
        raise ConfigError(f"{cfg_path}: [connections] min_hours must be a number of hours, 0 or more")
    return Config(
        root=root,
        db_path=db_path,
        inbox=inbox,
        processed_dir=inbox / "processed",
        watchlist=(root / paths.get("watchlist", "watchlist.txt")).resolve(),
        backup_dir=db_path.parent / "backups",
        cache_dir=db_path.parent / "cache",
        lookback_years=int(prices.get("lookback_years", 5)),
        sec_max_age_hours=int(sec.get("max_age_hours", 24)),
        connections_min_hours=float(min_hours),
        snaptrade_enabled=bool(snap.get("enabled", True)),
        snaptrade_dir=(root / snap_dir).resolve() if snap_dir else None,
        classify={str(k).upper(): str(v) for k, v in raw.get("classify", {}).items()},
        share_classes=_share_classes(raw.get("share_classes", {}), cfg_path),
        check=_check_settings(raw.get("check", {}), cfg_path),
        engine=_engine_settings(raw.get("engine", {}), cfg_path),
        benchmarks=_benchmarks(raw.get("benchmarks", {}), cfg_path),
        fund_proxies=_fund_proxies(raw.get("fund_proxies", {}), cfg_path),
        tiingo_token=_secret("TIINGO_API_TOKEN", dotenv),
        fred_api_key=_secret("FRED_API_KEY", dotenv),
        sec_user_agent=_secret("SEC_USER_AGENT", dotenv),
        investing_dir=(root / investing_dir).resolve() if investing_dir else None,
        export_cockpit=bool(export.get("enabled", False)),
        export_dir=(root / export_dir).resolve() if export_dir else None,
    )
