"""config.toml (paths, cadences) + .env (secrets). Both gitignored; examples committed."""
from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import dotenv_values

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

[sources.snaptrade]
enabled = true                     # owner-only: investing's SnapTrade export. Ignored if dir is missing.
dir = "../investing/fidelity/dashboard-data"

[sources.investing]
content_dir = "../investing"       # owner-only: theses/ + watchlist/ frontmatter tickers and basket.txt join the universe. Ignored if missing.

[export.cockpit]
enabled = false                    # write prices.json / dividends.json / fundamentals.json for the investing cockpit
dir = "../investing/data"          # must already exist; never created

[classify]                         # manual asset-type overrides: SYMBOL = "stock|etf|mutual_fund|money_market|crypto"
# VXUS = "etf"
"""

ENV_EXAMPLE = """# Copy to .env (gitignored). Values are never logged.
# Required for SEC EDGAR (they block anonymous clients): your name and email.
SEC_USER_AGENT=Your Name you@example.com
# Optional: Tiingo token for prices (free at tiingo.com). Without it, yfinance is used.
TIINGO_API_TOKEN=
"""


class ConfigError(Exception):
    pass


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
    snaptrade_enabled: bool = True
    snaptrade_dir: Path | None = None
    classify: dict[str, str] = field(default_factory=dict)
    tiingo_token: str | None = None
    sec_user_agent: str | None = None
    investing_dir: Path | None = None
    export_cockpit: bool = False
    export_dir: Path | None = None


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
        snaptrade_enabled=bool(snap.get("enabled", True)),
        snaptrade_dir=(root / snap_dir).resolve() if snap_dir else None,
        classify={str(k).upper(): str(v) for k, v in raw.get("classify", {}).items()},
        tiingo_token=_secret("TIINGO_API_TOKEN", dotenv),
        sec_user_agent=_secret("SEC_USER_AGENT", dotenv),
        investing_dir=(root / investing_dir).resolve() if investing_dir else None,
        export_cockpit=bool(export.get("enabled", False)),
        export_dir=(root / export_dir).resolve() if export_dir else None,
    )
