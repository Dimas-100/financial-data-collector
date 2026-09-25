# financial-data-collector Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A local SQLite warehouse (`data/warehouse.db`) filled by one command, `fdc sync`, with daily positions, cash, transactions, prices and full as-reported SEC financial statements for every symbol ever held, plus a read-only MCP server so Claude Desktop can query it.

**Architecture:** Adapters turn local files (Fidelity CSV exports, investing's SnapTrade JSON) into plain dataclasses; collectors fetch prices (Tiingo/yfinance) and SEC company facts through an injected `fetch` function; a single `Store` class owns all SQL and applies numbered migrations; `statements.py` shapes raw XBRL facts into a `financial_line_items` table that thin views pivot into wide annual/quarterly statements. `sync.py` runs the steps independently and logs each to `sync_runs`.

**Tech Stack:** Python 3.11, stdlib `sqlite3` (WAL), `requests`, `python-dotenv`, `yfinance`, `mcp` (FastMCP), `pytest`. No ORM, no pandas inside the package.

**Spec:** `docs/superpowers/specs/2026-09-25-financial-data-collector-design.md`

## Global Constraints

- Python `>=3.11`; dependencies exactly `requests`, `python-dotenv`, `yfinance`, `mcp`; dev `pytest`. Nothing else without a spec change.
- Symbols stored in canonical form: uppercase, share classes with a dot (`BRK.B`).
- Dates ISO `YYYY-MM-DD`; timestamps ISO-8601 UTC; money REAL.
- `sec_facts.period_start` and `financial_line_items.period_start` are `''` for instants, never NULL.
- Account numbers are never stored, logged or printed.
- Fixtures are synthetic: account labels `Sample Brokerage` / `Sample Roth`, symbols only from `AAPL`, `KO`, `VTI`, `SPAXX`, invented round quantities.
- Tests never touch the network: every collector takes `fetch` as a parameter.
- `.ps1` scripts are pure ASCII, no BOM; scheduling uses `Register-ScheduledTask`, never `schtasks`.
- Commit after every task with the `Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>` trailer.
- Deviation from spec §12 recorded here: `migrations/` and `seeds/` live **inside the package** (`src/financial_data_collector/migrations`, `.../seeds`) so an installed package is self-contained. Spec §12 is updated in Task 16.

## Review Focus

1. A Fidelity positions CSV saved with a different name (e.g. `Portfolio_Positions (1).csv`) must still ingest, dated from the `Date downloaded` footer. Test in Task 5.
2. A history CSV whose `Amount ($)` is blank for a stock-split or transfer row must import with `amount NULL`, not crash. Test in Task 6.
3. A SnapTrade snapshot whose `fetchedAt` is midnight UTC dates the row by that UTC date, consistently with every other snapshot; the adapter never converts to local time. Test in Task 7.
4. A ticker with no price history from either provider (delisted, or a new fund) leaves prices untouched and the run status `ok` with a message, never `error`. Test in Task 9.
5. A company that files cash-flow only year-to-date (6- and 9-month durations, the common case) still gets quarterly OCF through YTD differencing, and never gets a derived Q4 when Q3 is missing. Test in Task 11.

## File Structure

```
pyproject.toml                                   package metadata, deps, `fdc` entry point, pytest config
src/financial_data_collector/
  __init__.py                                    __version__
  __main__.py                                    python -m entry
  models.py                                      frozen dataclasses shared by adapters, collectors, store
  symbols.py                                     canonical symbol form, provider forms, money-market list
  config.py                                      Config dataclass; load from config.toml + .env; init_project()
  migrate.py                                     apply numbered SQL migrations, seed concept_map, build wide views
  store.py                                       Store: the only module that writes SQL
  migrations/0001_init.sql                       tables
  migrations/0002_views.sql                      static views
  seeds/concept_map.csv                          line item -> XBRL concept priorities
  adapters/__init__.py
  adapters/base.py                               clean_number, sha256_file, dedupe_key, csv helpers
  adapters/fidelity_positions.py                 Portfolio_Positions CSV -> Snapshot
  adapters/fidelity_history.py                   History CSV -> TransactionRow list
  adapters/snaptrade.py                          investing live/snapshot JSON -> Snapshot / TransactionRow list
  ingest.py                                      inbox routing, idempotency, snaptrade folder walk
  http.py                                        fetch() with retry/backoff; Fetch type alias
  collectors/__init__.py
  collectors/prices.py                           Tiingo / yfinance bars, incremental collection
  collectors/sec_cik.py                          ticker -> CIK map with 7-day cache
  collectors/sec_facts.py                        company-facts parse + collection, triggers statements.rebuild
  statements.py                                  facts + concept rules -> LineItem list (annual, quarter, YTD diff, Q4)
  universe.py                                    symbols to collect
  sync.py                                        run_sync(): steps, sync_runs logging, exit codes
  cli.py                                         argparse: init, sync, import, status, query, mcp
  mcp_server.py                                  FastMCP read-only server + SQL guard + --print-config
tests/
  conftest.py                                    tmp store fixture, fixture paths
  fixtures/…                                     synthetic CSV/JSON files
  test_*.py                                      one per module
scripts/run_sync.ps1, scripts/schedule_sync.ps1
README.md, AGENTS.md, CLAUDE.md, LICENSE, CHANGELOG.md, config.example.toml, .env.example, watchlist.example.txt, docs/QUERIES.md
```

---

### Task 1: Project scaffold and test harness

**Files:**
- Create: `pyproject.toml`, `src/financial_data_collector/__init__.py`, `src/financial_data_collector/__main__.py`, `tests/conftest.py`, `tests/test_package.py`

**Interfaces:**
- Produces: importable package `financial_data_collector` with `__version__ = "0.1.0"`; a `.venv` whose `python` runs pytest.

- [ ] **Step 1: Write pyproject.toml**

```toml
[build-system]
requires = ["setuptools>=68"]
build-backend = "setuptools.build_meta"

[project]
name = "financial-data-collector"
version = "0.1.0"
description = "Free, local SQLite warehouse for your brokerage positions, prices and SEC financial statements"
readme = "README.md"
requires-python = ">=3.11"
license = { text = "MIT" }
dependencies = [
  "requests>=2.31",
  "python-dotenv>=1.0",
  "yfinance>=0.2.40",
  "mcp>=1.2",
]

[project.optional-dependencies]
dev = ["pytest>=8"]

[project.scripts]
fdc = "financial_data_collector.cli:main"

[tool.setuptools.packages.find]
where = ["src"]

[tool.setuptools.package-data]
financial_data_collector = ["migrations/*.sql", "seeds/*.csv"]

[tool.pytest.ini_options]
testpaths = ["tests"]
addopts = "-q"
```

- [ ] **Step 2: Write the package files**

`src/financial_data_collector/__init__.py`:
```python
"""financial-data-collector: a local SQLite warehouse for brokerage positions."""
__version__ = "0.1.0"
```

`src/financial_data_collector/__main__.py`:
```python
from .cli import main

if __name__ == "__main__":
    raise SystemExit(main())
```

`README.md` (placeholder so the build has a readme; Task 16 writes the real one):
```markdown
# financial-data-collector

A free, local SQLite warehouse for your brokerage positions, prices and SEC financial statements. Documentation lands with the first release.
```

- [ ] **Step 3: Write the failing smoke test**

`tests/conftest.py`:
```python
from pathlib import Path

import pytest

FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture
def fixtures() -> Path:
    return FIXTURES
```

`tests/test_package.py`:
```python
def test_version():
    import financial_data_collector

    assert financial_data_collector.__version__ == "0.1.0"
```

- [ ] **Step 4: Create the venv, install, run the test**

Run (Git Bash on Windows):
```bash
py -3.11 -m venv .venv && .venv/Scripts/python -m pip install -q --upgrade pip && .venv/Scripts/python -m pip install -q -e ".[dev]" && .venv/Scripts/python -m pytest
```
Expected: `1 passed`. (Before `__init__.py` exists the import fails; after Step 2 it passes.)

- [ ] **Step 5: Commit**

```bash
git add pyproject.toml README.md src tests
git commit -m "Scaffold package, venv and pytest harness"
```

---

### Task 2: Symbols

**Files:**
- Create: `src/financial_data_collector/symbols.py`, `tests/test_symbols.py`

**Interfaces:**
- Produces: `canonical(raw: str) -> str`, `to_tiingo(sym) -> str`, `to_yfinance(sym) -> str`, `to_sec(sym) -> str`, `is_money_market(symbol: str, description: str | None = None) -> bool`, `MONEY_MARKET: frozenset[str]`.

- [ ] **Step 1: Write the failing tests**

`tests/test_symbols.py`:
```python
import pytest

from financial_data_collector import symbols as S


@pytest.mark.parametrize(
    "raw, want",
    [
        ("brk/b", "BRK.B"),
        ("BRKB", "BRK.B"),
        ("BRK-B", "BRK.B"),
        ("BRK.B", "BRK.B"),
        (" aapl ", "AAPL"),
        ("FXAIX", "FXAIX"),
        ("SPAXX**", "SPAXX"),
    ],
)
def test_canonical(raw, want):
    assert S.canonical(raw) == want


def test_provider_forms():
    assert S.to_tiingo("BRK.B") == "BRK-B"
    assert S.to_yfinance("BRK.B") == "BRK-B"
    assert S.to_sec("BRK.B") == "BRK-B"
    assert S.to_tiingo("AAPL") == "AAPL"


def test_money_market():
    assert S.is_money_market("SPAXX")
    assert S.is_money_market("spaxx**")
    assert S.is_money_market("XYZ", "FIDELITY GOVERNMENT MONEY MARKET")
    assert not S.is_money_market("AAPL", "APPLE INC")
```

- [ ] **Step 2: Run to verify failure**

Run: `.venv/Scripts/python -m pytest tests/test_symbols.py -v`
Expected: FAIL, `ModuleNotFoundError: financial_data_collector.symbols`

- [ ] **Step 3: Implement**

`src/financial_data_collector/symbols.py`:
```python
"""Canonical symbol handling.

Canonical form: uppercase, share classes joined with a dot (BRK.B). Adapters
call canonical() on everything they read; providers get their own spelling via
to_tiingo / to_yfinance / to_sec.
"""
from __future__ import annotations

# Fidelity core / money-market positions. These are cash, not holdings.
MONEY_MARKET: frozenset[str] = frozenset(
    {"SPAXX", "FDRXX", "FZFXX", "FCASH", "FDLXX", "SPRXX", "FZDXX", "FZCXX", "CORE"}
)

# Symbols whose provider spelling has no separator at all (SnapTrade style).
_NO_SEPARATOR_ALIASES: dict[str, str] = {
    "BRKB": "BRK.B",
    "BRKA": "BRK.A",
    "BFB": "BF.B",
    "BFA": "BF.A",
}


def canonical(raw: str) -> str:
    """Normalise any provider spelling to the canonical form."""
    sym = (raw or "").strip().upper().rstrip("*")
    if not sym:
        return sym
    sym = sym.replace("/", ".").replace("-", ".")
    return _NO_SEPARATOR_ALIASES.get(sym, sym)


def _dashed(sym: str) -> str:
    return canonical(sym).replace(".", "-")


def to_tiingo(sym: str) -> str:
    return _dashed(sym)


def to_yfinance(sym: str) -> str:
    return _dashed(sym)


def to_sec(sym: str) -> str:
    return _dashed(sym)


def is_money_market(symbol: str, description: str | None = None) -> bool:
    if canonical(symbol) in MONEY_MARKET:
        return True
    return bool(description) and "MONEY MARKET" in description.upper()
```

- [ ] **Step 4: Run to verify pass**

Run: `.venv/Scripts/python -m pytest tests/test_symbols.py -v`
Expected: 10 passed

- [ ] **Step 5: Commit**

```bash
git add src/financial_data_collector/symbols.py tests/test_symbols.py
git commit -m "Add canonical symbol handling and money-market list"
```

---

### Task 3: Models and adapter helpers

**Files:**
- Create: `src/financial_data_collector/models.py`, `src/financial_data_collector/adapters/__init__.py`, `src/financial_data_collector/adapters/base.py`, `tests/test_adapter_base.py`

**Interfaces:**
- Produces (models): frozen dataclasses `AccountRef(label, slug="other", institution="unknown", account_type="other")`, `PositionRow(account, symbol, description, quantity, price, market_value, cost_basis_total=None, avg_cost=None, unrealized_pnl=None)`, `CashRow(account, amount, currency="USD")`, `Snapshot(as_of_date, source, positions, cash, fetched_at=None, warnings=[])`, `TransactionRow(account, trade_date, type, symbol, units, price, amount, fee, description, source, settlement_date=None)`, `PriceBar(date, close, adj_close, dividend=0.0, split_factor=1.0)`, `Fact(taxonomy, concept, unit, period_start, period_end, value, fy, fp, form, filed, accn, frame=None)`, `ConceptRule(line_item, statement, kind, taxonomy, concept, priority)`, `LineItem(line_item, period_kind, period_start, period_end, fiscal_year, fiscal_quarter, value, concept, filed, accn, is_derived=False)`.
- Produces (base): `clean_number(text) -> float | None`, `sha256_file(path) -> str`, `dedupe_key(account_label, trade_date, type, symbol, units, amount) -> str`, `read_text_lines(path) -> list[str]` (utf-8-sig, CRLF-safe), `infer_account_type(label) -> str`.

- [ ] **Step 1: Write the failing tests**

`tests/test_adapter_base.py`:
```python
from pathlib import Path

from financial_data_collector.adapters import base
from financial_data_collector.models import AccountRef, PositionRow


def test_clean_number():
    assert base.clean_number("$1,234.56") == 1234.56
    assert base.clean_number("+$12.00") == 12.0
    assert base.clean_number("-$0.50") == -0.5
    assert base.clean_number("12.5%") == 12.5
    assert base.clean_number("--") is None
    assert base.clean_number("") is None
    assert base.clean_number(None) is None
    assert base.clean_number("n/a") is None


def test_dedupe_key_stable_and_rounded():
    a = base.dedupe_key("Sample Brokerage", "2026-01-02", "buy", "AAPL", 1.00001, -100.00004)
    b = base.dedupe_key("Sample Brokerage", "2026-01-02", "buy", "AAPL", 1.0, -100.0)
    c = base.dedupe_key("Sample Brokerage", "2026-01-02", "buy", "AAPL", 2.0, -100.0)
    assert a == b
    assert a != c
    assert len(a) == 64


def test_dedupe_key_handles_none():
    k = base.dedupe_key("Sample Roth", "2026-01-02", "contribution", None, None, 500.0)
    assert len(k) == 64


def test_sha256_file(tmp_path: Path):
    p = tmp_path / "x.csv"
    p.write_bytes(b"abc")
    assert base.sha256_file(p) == "ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad"


def test_read_text_lines_strips_bom_and_crlf(tmp_path: Path):
    p = tmp_path / "x.csv"
    p.write_bytes("﻿A,B\r\n1,2\r\n".encode("utf-8"))
    assert base.read_text_lines(p) == ["A,B", "1,2"]


def test_infer_account_type():
    assert base.infer_account_type("ROTH IRA") == "roth_ira"
    assert base.infer_account_type("Traditional IRA") == "traditional_ira"
    assert base.infer_account_type("Individual - TOD") == "brokerage"
    assert base.infer_account_type("Webull Crypto") == "crypto"


def test_models_are_frozen():
    acct = AccountRef(label="Sample Brokerage")
    row = PositionRow(acct, "AAPL", "APPLE INC", 10, 100.0, 1000.0)
    try:
        row.quantity = 5  # type: ignore[misc]
    except Exception as e:  # FrozenInstanceError
        assert "frozen" in type(e).__name__.lower() or "FrozenInstanceError" in repr(e)
    else:
        raise AssertionError("PositionRow should be frozen")
```

- [ ] **Step 2: Run to verify failure**

Run: `.venv/Scripts/python -m pytest tests/test_adapter_base.py -v`
Expected: FAIL, `ModuleNotFoundError`

- [ ] **Step 3: Implement models**

`src/financial_data_collector/models.py`:
```python
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
```

- [ ] **Step 4: Implement adapter helpers**

`src/financial_data_collector/adapters/__init__.py`: empty file.

`src/financial_data_collector/adapters/base.py`:
```python
"""Helpers shared by the file adapters. No SQL here."""
from __future__ import annotations

import hashlib
import re
from pathlib import Path

_NULL_TOKENS = {"", "--", "-", "n/a", "na", "none", "null"}
_NUM_STRIP = re.compile(r"[$,%+\s]")


def clean_number(text: str | None) -> float | None:
    """'$1,234.56' -> 1234.56; '--', blank, None -> None."""
    if text is None:
        return None
    s = str(text).strip()
    if s.lower() in _NULL_TOKENS:
        return None
    s = _NUM_STRIP.sub("", s)
    neg = s.startswith("(") and s.endswith(")")
    s = s.strip("()")
    try:
        v = float(s)
    except ValueError:
        return None
    return -v if neg else v


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def dedupe_key(
    account_label: str,
    trade_date: str,
    type_: str,
    symbol: str | None,
    units: float | None,
    amount: float | None,
) -> str:
    def r(v: float | None) -> str:
        return "" if v is None else f"{round(v, 4):.4f}"

    raw = "|".join([account_label, trade_date, type_, symbol or "", r(units), r(amount)])
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def read_text_lines(path: Path) -> list[str]:
    """Read a CSV export as lines, tolerant of a UTF-8 BOM and CRLF."""
    text = path.read_text(encoding="utf-8-sig", errors="replace")
    return text.replace("\r\n", "\n").replace("\r", "\n").split("\n")


def infer_account_type(label: str) -> str:
    u = (label or "").upper()
    if "CRYPTO" in u:
        return "crypto"
    if "ROTH" in u:
        return "roth_ira"
    if "TRADITIONAL" in u or "ROLLOVER" in u or "SEP" in u:
        return "traditional_ira"
    return "brokerage"
```

- [ ] **Step 5: Run to verify pass**

Run: `.venv/Scripts/python -m pytest tests/test_adapter_base.py -v`
Expected: 7 passed

- [ ] **Step 6: Commit**

```bash
git add src/financial_data_collector/models.py src/financial_data_collector/adapters tests/test_adapter_base.py
git commit -m "Add shared models and adapter helpers"
```

---
### Task 4: Store, migrations and concept-map seed

**Files:**
- Create: `src/financial_data_collector/migrations/0001_init.sql`, `src/financial_data_collector/seeds/concept_map.csv`, `src/financial_data_collector/migrate.py`, `src/financial_data_collector/store.py`, `tests/test_store.py`

**Interfaces:**
- Consumes: models from Task 3, `dedupe_key` from `adapters/base.py`.
- Produces: `Store.open(path, *, migrate=True, backup_dir=None) -> Store`; `store.migrate(backup_dir=None) -> list[int]`; `store.upsert_account(ref, first_seen) -> int`; `store.upsert_security(symbol, description=None, asset_type=None, first_seen=None)`; `store.write_snapshot(snap) -> int`; `store.write_transactions(rows) -> int` (new rows only); `store.last_price_date(symbol) -> str | None`; `store.write_prices(symbol, bars, source) -> int`; `store.write_sec_facts(cik, facts) -> int`; `store.facts_for(cik) -> list[Fact]`; `store.replace_line_items(cik, items) -> int`; `store.concept_rules() -> list[ConceptRule]`; `store.file_seen(sha) -> bool`; `store.record_file(sha, path, kind, rows)`; `store.log_run(run_id, step, status, rows_written, message, started_at, finished_at)`; `store.query(sql, params=()) -> list[sqlite3.Row]`; `store.securities() -> list[sqlite3.Row]`; `store.set_security_cik(symbol, cik, sec_name)`; `store.mark_sec_fetch(symbol, when)`; `store.set_asset_type(symbol, asset_type)`; `store.counts() -> dict[str, int]`; `store.latest_snapshot_date() -> str | None`; `store.close()`.
- Produces (migrate): `apply_migrations(conn, migrations_dir, backup_to=None) -> list[int]`, `seed_concept_map(conn, csv_path) -> int`, `rebuild_wide_views(conn) -> None`, constants `MIGRATIONS_DIR`, `SEEDS_DIR`.

- [ ] **Step 1: Write the schema**

`src/financial_data_collector/migrations/0001_init.sql`:
```sql
-- 0001: core tables. schema_version itself is created by migrate.py.

CREATE TABLE accounts (
  id            INTEGER PRIMARY KEY AUTOINCREMENT,
  label         TEXT NOT NULL UNIQUE,
  slug          TEXT NOT NULL DEFAULT 'other',
  institution   TEXT NOT NULL DEFAULT 'unknown',
  account_type  TEXT NOT NULL DEFAULT 'other',
  first_seen    TEXT NOT NULL
);

CREATE TABLE securities (
  symbol          TEXT PRIMARY KEY,
  description     TEXT,
  asset_type      TEXT NOT NULL DEFAULT 'unknown',
  cik             TEXT,
  sec_name        TEXT,
  first_seen      TEXT NOT NULL,
  last_sec_fetch  TEXT,
  price_source    TEXT
);

CREATE TABLE position_snapshots (
  as_of_date        TEXT NOT NULL,
  account_id        INTEGER NOT NULL REFERENCES accounts(id),
  symbol            TEXT NOT NULL REFERENCES securities(symbol),
  quantity          REAL NOT NULL,
  price             REAL,
  market_value      REAL,
  cost_basis_total  REAL,
  avg_cost          REAL,
  unrealized_pnl    REAL,
  source            TEXT NOT NULL,
  source_fetched_at TEXT,
  PRIMARY KEY (as_of_date, account_id, symbol)
);
CREATE INDEX idx_positions_symbol ON position_snapshots (symbol, as_of_date);

CREATE TABLE cash_balances (
  as_of_date  TEXT NOT NULL,
  account_id  INTEGER NOT NULL REFERENCES accounts(id),
  currency    TEXT NOT NULL DEFAULT 'USD',
  amount      REAL NOT NULL,
  source      TEXT NOT NULL,
  PRIMARY KEY (as_of_date, account_id, currency)
);

CREATE TABLE transactions (
  id              INTEGER PRIMARY KEY AUTOINCREMENT,
  account_id      INTEGER NOT NULL REFERENCES accounts(id),
  trade_date      TEXT NOT NULL,
  settlement_date TEXT,
  type            TEXT NOT NULL,
  symbol          TEXT,
  units           REAL,
  price           REAL,
  amount          REAL,
  fee             REAL,
  description     TEXT NOT NULL DEFAULT '',
  source          TEXT NOT NULL,
  dedupe_key      TEXT NOT NULL UNIQUE
);
CREATE INDEX idx_transactions_symbol ON transactions (symbol, trade_date);
CREATE INDEX idx_transactions_account ON transactions (account_id, trade_date);

CREATE TABLE prices (
  symbol        TEXT NOT NULL,
  date          TEXT NOT NULL,
  close         REAL NOT NULL,
  adj_close     REAL NOT NULL,
  dividend      REAL NOT NULL DEFAULT 0,
  split_factor  REAL NOT NULL DEFAULT 1,
  source        TEXT NOT NULL,
  PRIMARY KEY (symbol, date)
);

CREATE TABLE sec_facts (
  cik           TEXT NOT NULL,
  taxonomy      TEXT NOT NULL,
  concept       TEXT NOT NULL,
  unit          TEXT NOT NULL,
  period_start  TEXT NOT NULL DEFAULT '',
  period_end    TEXT NOT NULL,
  value         REAL NOT NULL,
  fy            INTEGER,
  fp            TEXT,
  form          TEXT NOT NULL,
  filed         TEXT NOT NULL,
  accn          TEXT NOT NULL,
  frame         TEXT,
  PRIMARY KEY (cik, taxonomy, concept, unit, period_start, period_end, accn)
) WITHOUT ROWID;
CREATE INDEX idx_sec_facts_lookup ON sec_facts (cik, concept, period_end);

CREATE TABLE concept_map (
  line_item  TEXT NOT NULL,
  statement  TEXT NOT NULL,
  kind       TEXT NOT NULL,
  taxonomy   TEXT NOT NULL,
  concept    TEXT NOT NULL,
  priority   INTEGER NOT NULL,
  PRIMARY KEY (line_item, taxonomy, concept)
);

CREATE TABLE financial_line_items (
  cik             TEXT NOT NULL,
  line_item       TEXT NOT NULL,
  period_kind     TEXT NOT NULL,
  period_start    TEXT NOT NULL DEFAULT '',
  period_end      TEXT NOT NULL,
  fiscal_year     INTEGER NOT NULL,
  fiscal_quarter  INTEGER,
  value           REAL NOT NULL,
  concept         TEXT NOT NULL,
  filed           TEXT NOT NULL,
  accn            TEXT NOT NULL,
  is_derived      INTEGER NOT NULL DEFAULT 0,
  PRIMARY KEY (cik, line_item, period_kind, period_end)
);

CREATE TABLE sync_runs (
  id            INTEGER PRIMARY KEY AUTOINCREMENT,
  run_id        TEXT NOT NULL,
  step          TEXT NOT NULL,
  started_at    TEXT NOT NULL,
  finished_at   TEXT NOT NULL,
  status        TEXT NOT NULL,
  rows_written  INTEGER NOT NULL DEFAULT 0,
  message       TEXT NOT NULL DEFAULT ''
);
CREATE INDEX idx_sync_runs_step ON sync_runs (step, id);

CREATE TABLE ingested_files (
  sha256       TEXT PRIMARY KEY,
  path         TEXT NOT NULL,
  kind         TEXT NOT NULL,
  ingested_at  TEXT NOT NULL,
  rows         INTEGER NOT NULL DEFAULT 0
);
```

- [ ] **Step 2: Write the concept-map seed**

`src/financial_data_collector/seeds/concept_map.csv` (exactly spec §5.3; `priority` counts from 1):
```csv
line_item,statement,kind,taxonomy,concept,priority
revenue,income,duration,us-gaap,RevenueFromContractWithCustomerExcludingAssessedTax,1
revenue,income,duration,us-gaap,Revenues,2
revenue,income,duration,us-gaap,SalesRevenueNet,3
revenue,income,duration,us-gaap,SalesRevenueGoodsNet,4
cost_of_revenue,income,duration,us-gaap,CostOfRevenue,1
cost_of_revenue,income,duration,us-gaap,CostOfGoodsAndServicesSold,2
cost_of_revenue,income,duration,us-gaap,CostOfGoodsSold,3
gross_profit,income,duration,us-gaap,GrossProfit,1
research_and_development,income,duration,us-gaap,ResearchAndDevelopmentExpense,1
sga,income,duration,us-gaap,SellingGeneralAndAdministrativeExpense,1
operating_expenses,income,duration,us-gaap,OperatingExpenses,1
operating_expenses,income,duration,us-gaap,CostsAndExpenses,2
operating_income,income,duration,us-gaap,OperatingIncomeLoss,1
interest_expense,income,duration,us-gaap,InterestExpense,1
interest_expense,income,duration,us-gaap,InterestExpenseNonoperating,2
pretax_income,income,duration,us-gaap,IncomeLossFromContinuingOperationsBeforeIncomeTaxesExtraordinaryItemsNoncontrollingInterest,1
pretax_income,income,duration,us-gaap,IncomeLossFromContinuingOperationsBeforeIncomeTaxesMinorityInterestAndIncomeLossFromEquityMethodInvestments,2
income_tax,income,duration,us-gaap,IncomeTaxExpenseBenefit,1
net_income,income,duration,us-gaap,NetIncomeLoss,1
net_income,income,duration,us-gaap,ProfitLoss,2
eps_basic,income,per_share,us-gaap,EarningsPerShareBasic,1
eps_diluted,income,per_share,us-gaap,EarningsPerShareDiluted,1
dividends_per_share,income,per_share,us-gaap,CommonStockDividendsPerShareDeclared,1
dividends_per_share,income,per_share,us-gaap,CommonStockDividendsPerShareCashPaid,2
shares_basic,income,shares,us-gaap,WeightedAverageNumberOfSharesOutstandingBasic,1
shares_diluted,income,shares,us-gaap,WeightedAverageNumberOfDilutedSharesOutstanding,1
cash,balance,instant,us-gaap,CashAndCashEquivalentsAtCarryingValue,1
cash,balance,instant,us-gaap,Cash,2
cash,balance,instant,us-gaap,CashCashEquivalentsRestrictedCashAndRestrictedCashEquivalents,3
short_term_investments,balance,instant,us-gaap,ShortTermInvestments,1
short_term_investments,balance,instant,us-gaap,MarketableSecuritiesCurrent,2
short_term_investments,balance,instant,us-gaap,AvailableForSaleSecuritiesDebtSecuritiesCurrent,3
receivables,balance,instant,us-gaap,AccountsReceivableNetCurrent,1
receivables,balance,instant,us-gaap,ReceivablesNetCurrent,2
inventory,balance,instant,us-gaap,InventoryNet,1
current_assets,balance,instant,us-gaap,AssetsCurrent,1
ppe_net,balance,instant,us-gaap,PropertyPlantAndEquipmentNet,1
goodwill,balance,instant,us-gaap,Goodwill,1
intangibles,balance,instant,us-gaap,IntangibleAssetsNetExcludingGoodwill,1
total_assets,balance,instant,us-gaap,Assets,1
accounts_payable,balance,instant,us-gaap,AccountsPayableCurrent,1
current_liabilities,balance,instant,us-gaap,LiabilitiesCurrent,1
long_term_debt,balance,instant,us-gaap,LongTermDebtNoncurrent,1
long_term_debt,balance,instant,us-gaap,LongTermDebt,2
long_term_debt,balance,instant,us-gaap,LongTermDebtAndCapitalLeaseObligations,3
total_liabilities,balance,instant,us-gaap,Liabilities,1
stockholders_equity,balance,instant,us-gaap,StockholdersEquity,1
stockholders_equity,balance,instant,us-gaap,StockholdersEquityIncludingPortionAttributableToNoncontrollingInterest,2
retained_earnings,balance,instant,us-gaap,RetainedEarningsAccumulatedDeficit,1
shares_outstanding,balance,shares,us-gaap,CommonStockSharesOutstanding,1
shares_outstanding,balance,shares,dei,EntityCommonStockSharesOutstanding,2
ocf,cashflow,duration,us-gaap,NetCashProvidedByUsedInOperatingActivities,1
ocf,cashflow,duration,us-gaap,NetCashProvidedByUsedInOperatingActivitiesContinuingOperations,2
capex,cashflow,duration,us-gaap,PaymentsToAcquirePropertyPlantAndEquipment,1
capex,cashflow,duration,us-gaap,PaymentsToAcquireProductiveAssets,2
investing_cash_flow,cashflow,duration,us-gaap,NetCashProvidedByUsedInInvestingActivities,1
financing_cash_flow,cashflow,duration,us-gaap,NetCashProvidedByUsedInFinancingActivities,1
depreciation_amortization,cashflow,duration,us-gaap,DepreciationDepletionAndAmortization,1
depreciation_amortization,cashflow,duration,us-gaap,DepreciationAndAmortization,2
depreciation_amortization,cashflow,duration,us-gaap,DepreciationAmortizationAndAccretionNet,3
stock_based_compensation,cashflow,duration,us-gaap,ShareBasedCompensation,1
stock_based_compensation,cashflow,duration,us-gaap,AllocatedShareBasedCompensationExpense,2
dividends_paid,cashflow,duration,us-gaap,PaymentsOfDividends,1
dividends_paid,cashflow,duration,us-gaap,PaymentsOfDividendsCommonStock,2
buybacks,cashflow,duration,us-gaap,PaymentsForRepurchaseOfCommonStock,1
debt_issued,cashflow,duration,us-gaap,ProceedsFromIssuanceOfLongTermDebt,1
debt_repaid,cashflow,duration,us-gaap,RepaymentsOfLongTermDebt,1
```

- [ ] **Step 3: Write the failing tests**

`tests/test_store.py`:
```python
import sqlite3
from pathlib import Path

import pytest

from financial_data_collector import migrate
from financial_data_collector.models import (
    AccountRef, CashRow, Fact, LineItem, PositionRow, PriceBar, Snapshot, TransactionRow,
)
from financial_data_collector.store import Store

ACCT = AccountRef("Sample Brokerage", "brokerage", "fidelity", "brokerage")
ROTH = AccountRef("Sample Roth", "roth", "fidelity", "roth_ira")


@pytest.fixture
def store(tmp_path: Path) -> Store:
    s = Store.open(tmp_path / "w.db")
    yield s
    s.close()


def _snap(date: str, qty: float = 10.0, cash: float = 5.0) -> Snapshot:
    return Snapshot(
        as_of_date=date,
        source="fidelity_csv",
        positions=[PositionRow(ACCT, "AAPL", "APPLE INC", qty, 100.0, qty * 100.0, 900.0, 90.0, 100.0)],
        cash=[CashRow(ACCT, cash)],
    )


def test_migrate_creates_schema_and_seeds(store: Store):
    versions = [r[0] for r in store.query("SELECT version FROM schema_version ORDER BY version")]
    assert versions[0] == 1
    tables = {r[0] for r in store.query("SELECT name FROM sqlite_master WHERE type='table'")}
    assert {"accounts", "securities", "position_snapshots", "cash_balances", "transactions",
            "prices", "sec_facts", "concept_map", "financial_line_items", "sync_runs",
            "ingested_files", "schema_version"} <= tables
    assert store.query("SELECT COUNT(*) FROM concept_map")[0][0] >= 60
    cols = {r[1] for r in store.query("PRAGMA table_info(financials_annual)")}
    assert {"cik", "symbol", "company", "fiscal_year", "revenue", "total_assets", "ocf",
            "capex", "fcf", "gross_margin", "net_margin"} <= cols
    qcols = {r[1] for r in store.query("PRAGMA table_info(financials_quarterly)")}
    assert "is_derived_q4" in qcols


def test_migrate_is_idempotent(store: Store):
    assert store.migrate() == []


def test_apply_migrations_backs_up_before_pending(tmp_path: Path):
    db = tmp_path / "w.db"
    s = Store.open(db)
    s.close()
    extra = tmp_path / "mig"
    extra.mkdir()
    (extra / "0001_init.sql").write_text((migrate.MIGRATIONS_DIR / "0001_init.sql").read_text())
    (extra / "0002_more.sql").write_text("CREATE TABLE extra_t (x INTEGER);")
    conn = sqlite3.connect(db)
    backup = tmp_path / "backups" / "w-backup.db"
    applied = migrate.apply_migrations(conn, extra, backup_to=backup)
    assert applied == [2]
    assert backup.exists()
    assert conn.execute("SELECT COUNT(*) FROM extra_t").fetchone()[0] == 0
    conn.close()


def test_upsert_account_returns_same_id_and_keeps_earliest_first_seen(store: Store):
    a = store.upsert_account(ACCT, "2026-02-01")
    b = store.upsert_account(ACCT, "2026-01-01")
    assert a == b
    assert store.query("SELECT first_seen FROM accounts WHERE id=?", (a,))[0][0] == "2026-01-01"


def test_upsert_security_never_downgrades_asset_type(store: Store):
    store.upsert_security("AAPL", "APPLE INC", "stock", "2026-01-01")
    store.upsert_security("AAPL", None, None, "2026-01-02")
    row = store.query("SELECT description, asset_type FROM securities WHERE symbol='AAPL'")[0]
    assert (row[0], row[1]) == ("APPLE INC", "stock")


def test_write_snapshot_upserts(store: Store):
    assert store.write_snapshot(_snap("2026-01-02", 10)) == 2
    assert store.write_snapshot(_snap("2026-01-02", 12, 7)) == 2
    assert store.query("SELECT COUNT(*) FROM position_snapshots")[0][0] == 1
    assert store.query("SELECT quantity FROM position_snapshots")[0][0] == 12
    assert store.query("SELECT amount FROM cash_balances")[0][0] == 7
    assert store.latest_snapshot_date() == "2026-01-02"


def test_write_transactions_dedupes(store: Store):
    rows = [
        TransactionRow(ACCT, "2026-01-02", "buy", "AAPL", 1.0, 100.0, -100.0, 0.0, "YOU BOUGHT", "fidelity_csv"),
        TransactionRow(ROTH, "2026-01-03", "contribution", None, None, None, 500.0, None, "EFT", "fidelity_csv"),
    ]
    assert store.write_transactions(rows) == 2
    assert store.write_transactions(rows) == 0
    assert store.query("SELECT COUNT(*) FROM transactions")[0][0] == 2
    assert store.query("SELECT COUNT(*) FROM accounts")[0][0] == 2


def test_prices_roundtrip(store: Store):
    store.upsert_security("AAPL", first_seen="2026-01-01")
    assert store.last_price_date("AAPL") is None
    bars = [PriceBar("2026-01-02", 100.0, 99.0), PriceBar("2026-01-05", 101.0, 100.0, 0.25, 1.0)]
    assert store.write_prices("AAPL", bars, "tiingo") == 2
    assert store.last_price_date("AAPL") == "2026-01-05"
    assert store.write_prices("AAPL", bars, "tiingo") == 2
    assert store.query("SELECT COUNT(*) FROM prices")[0][0] == 2
    assert store.query("SELECT price_source FROM securities WHERE symbol='AAPL'")[0][0] == "tiingo"


def _fact(concept="Revenues", start="2025-01-01", end="2025-12-31", value=100.0, accn="a1", filed="2026-02-01"):
    return Fact("us-gaap", concept, "USD", start, end, value, 2025, "FY", "10-K", filed, accn)


def test_sec_facts_no_duplicates_on_refetch(store: Store):
    facts = [_fact(), _fact(concept="Assets", start="", value=500.0)]
    assert store.write_sec_facts("0000320193", facts) == 2
    assert store.write_sec_facts("0000320193", facts) == 2
    assert store.query("SELECT COUNT(*) FROM sec_facts")[0][0] == 2
    back = store.facts_for("0000320193")
    assert {f.concept for f in back} == {"Revenues", "Assets"}
    assert [f for f in back if f.concept == "Assets"][0].period_start == ""


def test_replace_line_items(store: Store):
    items = [LineItem("revenue", "annual", "2025-01-01", "2025-12-31", 2025, None, 100.0, "Revenues", "2026-02-01", "a1")]
    assert store.replace_line_items("0000320193", items) == 1
    assert store.replace_line_items("0000320193", items * 1) == 1
    assert store.query("SELECT COUNT(*) FROM financial_line_items")[0][0] == 1


def test_concept_rules_sorted(store: Store):
    rules = store.concept_rules()
    rev = [r for r in rules if r.line_item == "revenue"]
    assert [r.priority for r in rev] == [1, 2, 3, 4]
    assert rev[0].kind == "duration" and rev[0].statement == "income"


def test_files_and_runs(store: Store):
    assert not store.file_seen("abc")
    store.record_file("abc", "inbox/x.csv", "fidelity_positions", 3)
    assert store.file_seen("abc")
    store.log_run("r1", "prices", "ok", 5, "", "2026-01-01T00:00:00Z", "2026-01-01T00:00:05Z")
    assert store.query("SELECT step, rows_written FROM sync_runs")[0][1] == 5
    counts = store.counts()
    assert counts["ingested_files"] == 1 and counts["sync_runs"] == 1


def test_security_sec_fields(store: Store):
    store.upsert_security("AAPL", first_seen="2026-01-01")
    store.set_security_cik("AAPL", "0000320193", "Apple Inc.")
    store.mark_sec_fetch("AAPL", "2026-01-02T00:00:00Z")
    store.set_asset_type("AAPL", "stock")
    row = store.query("SELECT cik, sec_name, last_sec_fetch, asset_type FROM securities WHERE symbol='AAPL'")[0]
    assert tuple(row) == ("0000320193", "Apple Inc.", "2026-01-02T00:00:00Z", "stock")
```

- [ ] **Step 4: Run to verify failure**

Run: `.venv/Scripts/python -m pytest tests/test_store.py -v`
Expected: FAIL, `ModuleNotFoundError: financial_data_collector.migrate`

- [ ] **Step 5: Implement migrate.py**

`src/financial_data_collector/migrate.py`:
```python
"""Numbered SQL migrations, the concept-map seed, and the generated wide views."""
from __future__ import annotations

import csv
import re
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

MIGRATIONS_DIR = Path(__file__).parent / "migrations"
SEEDS_DIR = Path(__file__).parent / "seeds"
_MIG_NAME = re.compile(r"^(\d{4})_.+\.sql$")


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _ensure_version_table(conn: sqlite3.Connection) -> None:
    conn.execute(
        "CREATE TABLE IF NOT EXISTS schema_version (version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL)"
    )


def pending_migrations(conn: sqlite3.Connection, migrations_dir: Path) -> list[tuple[int, Path]]:
    _ensure_version_table(conn)
    applied = {r[0] for r in conn.execute("SELECT version FROM schema_version")}
    out: list[tuple[int, Path]] = []
    for p in sorted(migrations_dir.glob("*.sql")):
        m = _MIG_NAME.match(p.name)
        if m and int(m.group(1)) not in applied:
            out.append((int(m.group(1)), p))
    return out


def _has_user_tables(conn: sqlite3.Connection) -> bool:
    n = conn.execute(
        "SELECT COUNT(*) FROM sqlite_master WHERE type='table' AND name NOT IN ('schema_version') "
        "AND name NOT LIKE 'sqlite_%'"
    ).fetchone()[0]
    return n > 0


def apply_migrations(
    conn: sqlite3.Connection, migrations_dir: Path = MIGRATIONS_DIR, backup_to: Path | None = None
) -> list[int]:
    """Apply every unapplied NNNN_*.sql in order. Returns the versions applied.

    When there is something to apply and the database already holds tables, a
    file backup is written to backup_to first (SQLite online backup API).
    """
    pending = pending_migrations(conn, migrations_dir)
    if not pending:
        return []
    if backup_to is not None and _has_user_tables(conn):
        backup_to.parent.mkdir(parents=True, exist_ok=True)
        dest = sqlite3.connect(backup_to)
        try:
            conn.backup(dest)
        finally:
            dest.close()
    applied: list[int] = []
    for version, path in pending:
        sql = path.read_text(encoding="utf-8")
        conn.executescript("BEGIN;\n" + sql + "\nCOMMIT;")
        conn.execute("INSERT INTO schema_version (version, applied_at) VALUES (?, ?)", (version, _now()))
        conn.commit()
        applied.append(version)
    return applied


def seed_concept_map(conn: sqlite3.Connection, csv_path: Path = SEEDS_DIR / "concept_map.csv") -> int:
    with open(csv_path, newline="", encoding="utf-8") as f:
        rows = [
            (r["line_item"], r["statement"], r["kind"], r["taxonomy"], r["concept"], int(r["priority"]))
            for r in csv.DictReader(f)
        ]
    with conn:
        conn.execute("DELETE FROM concept_map")
        conn.executemany(
            "INSERT INTO concept_map (line_item, statement, kind, taxonomy, concept, priority) VALUES (?,?,?,?,?,?)",
            rows,
        )
    return len(rows)


_IDENT = re.compile(r"^[a-z][a-z0-9_]*$")


def rebuild_wide_views(conn: sqlite3.Connection) -> None:
    """(Re)create financials_annual / financials_quarterly from concept_map's line items."""
    items = [r[0] for r in conn.execute("SELECT DISTINCT line_item FROM concept_map ORDER BY line_item")]
    for it in items:
        if not _IDENT.match(it):
            raise ValueError(f"line_item {it!r} is not a safe SQL identifier")
    pivot = ",\n".join(f"    MAX(CASE WHEN line_item = '{it}' THEN value END) AS {it}" for it in items)
    derived = """
    (ocf - capex) AS fcf,
    CASE WHEN revenue > 0 THEN gross_profit * 1.0 / revenue END AS gross_margin,
    CASE WHEN revenue > 0 THEN operating_income * 1.0 / revenue END AS operating_margin,
    CASE WHEN revenue > 0 THEN net_income * 1.0 / revenue END AS net_margin,
    CASE WHEN revenue > 0 THEN (ocf - capex) * 1.0 / revenue END AS fcf_margin"""
    for view, kind in (("financials_annual", "annual"), ("financials_quarterly", "quarter")):
        sql = f"""
CREATE VIEW {view} AS
WITH base AS (
  SELECT cik, period_end,
    MAX(fiscal_year) AS fiscal_year,
    MAX(fiscal_quarter) AS fiscal_quarter,
    MAX(is_derived) AS is_derived_q4,
    MAX(filed) AS last_filed,
{pivot}
  FROM financial_line_items
  WHERE period_kind = '{kind}'
  GROUP BY cik, period_end
)
SELECT
  (SELECT MIN(symbol) FROM securities s WHERE s.cik = base.cik) AS symbol,
  (SELECT MIN(sec_name) FROM securities s WHERE s.cik = base.cik) AS company,
  base.*,{derived}
FROM base
ORDER BY cik, period_end"""
        with conn:
            conn.execute(f"DROP VIEW IF EXISTS {view}")
            conn.execute(sql)
```

- [ ] **Step 6: Implement store.py**

`src/financial_data_collector/store.py`:
```python
"""The only module that writes SQL. Everything else hands it dataclasses."""
from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable

from . import migrate as _migrate
from .adapters.base import dedupe_key
from .models import (
    AccountRef, ConceptRule, Fact, LineItem, PriceBar, Snapshot, TransactionRow,
)

COUNT_TABLES = (
    "accounts", "securities", "position_snapshots", "cash_balances", "transactions",
    "prices", "sec_facts", "financial_line_items", "sync_runs", "ingested_files",
)


def utcnow() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def today() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


class Store:
    def __init__(self, conn: sqlite3.Connection, path: Path):
        self.conn = conn
        self.path = path

    # ---- lifecycle -------------------------------------------------------
    @classmethod
    def open(cls, path: Path | str, *, migrate: bool = True, backup_dir: Path | None = None) -> "Store":
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(path, timeout=5.0)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=5000")
        conn.execute("PRAGMA foreign_keys=ON")
        store = cls(conn, path)
        if migrate:
            store.migrate(backup_dir)
        return store

    def migrate(self, backup_dir: Path | None = None) -> list[int]:
        backup_to = None
        if backup_dir is not None:
            stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
            backup_to = backup_dir / f"warehouse-{stamp}.db"
        applied = _migrate.apply_migrations(self.conn, backup_to=backup_to)
        _migrate.seed_concept_map(self.conn)
        _migrate.rebuild_wide_views(self.conn)
        return applied

    def close(self) -> None:
        self.conn.close()

    def query(self, sql: str, params: Iterable = ()) -> list[sqlite3.Row]:
        return self.conn.execute(sql, tuple(params)).fetchall()

    # ---- accounts / securities ------------------------------------------
    def _account_id(self, ref: AccountRef, first_seen: str) -> int:
        self.conn.execute(
            """INSERT INTO accounts (label, slug, institution, account_type, first_seen)
               VALUES (?, ?, ?, ?, ?)
               ON CONFLICT(label) DO UPDATE SET
                 slug = excluded.slug,
                 institution = CASE WHEN excluded.institution != 'unknown' THEN excluded.institution ELSE accounts.institution END,
                 account_type = CASE WHEN excluded.account_type != 'other' THEN excluded.account_type ELSE accounts.account_type END,
                 first_seen = MIN(accounts.first_seen, excluded.first_seen)""",
            (ref.label, ref.slug, ref.institution, ref.account_type, first_seen),
        )
        return self.conn.execute("SELECT id FROM accounts WHERE label = ?", (ref.label,)).fetchone()[0]

    def upsert_account(self, ref: AccountRef, first_seen: str) -> int:
        with self.conn:
            return self._account_id(ref, first_seen)

    def _ensure_security(
        self, symbol: str, description: str | None, asset_type: str | None, first_seen: str
    ) -> None:
        self.conn.execute(
            """INSERT INTO securities (symbol, description, asset_type, first_seen)
               VALUES (?, ?, ?, ?)
               ON CONFLICT(symbol) DO UPDATE SET
                 description = COALESCE(excluded.description, securities.description),
                 asset_type = CASE WHEN excluded.asset_type != 'unknown' THEN excluded.asset_type ELSE securities.asset_type END,
                 first_seen = MIN(securities.first_seen, excluded.first_seen)""",
            (symbol, description, asset_type or "unknown", first_seen),
        )

    def upsert_security(
        self, symbol: str, description: str | None = None, asset_type: str | None = None,
        first_seen: str | None = None,
    ) -> None:
        with self.conn:
            self._ensure_security(symbol, description, asset_type, first_seen or today())

    def securities(self) -> list[sqlite3.Row]:
        return self.query("SELECT * FROM securities ORDER BY symbol")

    def set_security_cik(self, symbol: str, cik: str | None, sec_name: str | None) -> None:
        with self.conn:
            self.conn.execute("UPDATE securities SET cik = ?, sec_name = ? WHERE symbol = ?", (cik, sec_name, symbol))

    def mark_sec_fetch(self, symbol: str, when: str) -> None:
        with self.conn:
            self.conn.execute("UPDATE securities SET last_sec_fetch = ? WHERE symbol = ?", (when, symbol))

    def set_asset_type(self, symbol: str, asset_type: str) -> None:
        with self.conn:
            self.conn.execute("UPDATE securities SET asset_type = ? WHERE symbol = ?", (asset_type, symbol))

    # ---- positions / cash / transactions --------------------------------
    def write_snapshot(self, snap: Snapshot) -> int:
        n = 0
        with self.conn:
            for p in snap.positions:
                aid = self._account_id(p.account, snap.as_of_date)
                self._ensure_security(p.symbol, p.description, None, snap.as_of_date)
                self.conn.execute(
                    """INSERT INTO position_snapshots
                       (as_of_date, account_id, symbol, quantity, price, market_value, cost_basis_total,
                        avg_cost, unrealized_pnl, source, source_fetched_at)
                       VALUES (?,?,?,?,?,?,?,?,?,?,?)
                       ON CONFLICT(as_of_date, account_id, symbol) DO UPDATE SET
                         quantity = excluded.quantity, price = excluded.price,
                         market_value = excluded.market_value, cost_basis_total = excluded.cost_basis_total,
                         avg_cost = excluded.avg_cost, unrealized_pnl = excluded.unrealized_pnl,
                         source = excluded.source, source_fetched_at = excluded.source_fetched_at""",
                    (snap.as_of_date, aid, p.symbol, p.quantity, p.price, p.market_value, p.cost_basis_total,
                     p.avg_cost, p.unrealized_pnl, snap.source, snap.fetched_at),
                )
                n += 1
            for c in snap.cash:
                aid = self._account_id(c.account, snap.as_of_date)
                self.conn.execute(
                    """INSERT INTO cash_balances (as_of_date, account_id, currency, amount, source)
                       VALUES (?,?,?,?,?)
                       ON CONFLICT(as_of_date, account_id, currency) DO UPDATE SET
                         amount = excluded.amount, source = excluded.source""",
                    (snap.as_of_date, aid, c.currency, c.amount, snap.source),
                )
                n += 1
        return n

    def latest_snapshot_date(self) -> str | None:
        row = self.conn.execute("SELECT MAX(as_of_date) FROM position_snapshots").fetchone()
        return row[0] if row else None

    def write_transactions(self, rows: Iterable[TransactionRow]) -> int:
        n = 0
        with self.conn:
            for r in rows:
                aid = self._account_id(r.account, r.trade_date)
                if r.symbol:
                    self._ensure_security(r.symbol, None, None, r.trade_date)
                key = dedupe_key(r.account.label, r.trade_date, r.type, r.symbol, r.units, r.amount)
                cur = self.conn.execute(
                    """INSERT OR IGNORE INTO transactions
                       (account_id, trade_date, settlement_date, type, symbol, units, price, amount, fee,
                        description, source, dedupe_key)
                       VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (aid, r.trade_date, r.settlement_date, r.type, r.symbol, r.units, r.price, r.amount,
                     r.fee, r.description, r.source, key),
                )
                n += cur.rowcount
        return n

    # ---- prices -----------------------------------------------------------
    def last_price_date(self, symbol: str) -> str | None:
        return self.conn.execute("SELECT MAX(date) FROM prices WHERE symbol = ?", (symbol,)).fetchone()[0]

    def write_prices(self, symbol: str, bars: Iterable[PriceBar], source: str) -> int:
        bars = list(bars)
        with self.conn:
            self.conn.executemany(
                """INSERT INTO prices (symbol, date, close, adj_close, dividend, split_factor, source)
                   VALUES (?,?,?,?,?,?,?)
                   ON CONFLICT(symbol, date) DO UPDATE SET
                     close = excluded.close, adj_close = excluded.adj_close, dividend = excluded.dividend,
                     split_factor = excluded.split_factor, source = excluded.source""",
                [(symbol, b.date, b.close, b.adj_close, b.dividend, b.split_factor, source) for b in bars],
            )
            if bars:
                self.conn.execute("UPDATE securities SET price_source = ? WHERE symbol = ?", (source, symbol))
        return len(bars)

    # ---- SEC --------------------------------------------------------------
    def write_sec_facts(self, cik: str, facts: Iterable[Fact]) -> int:
        facts = list(facts)
        with self.conn:
            self.conn.executemany(
                """INSERT INTO sec_facts
                   (cik, taxonomy, concept, unit, period_start, period_end, value, fy, fp, form, filed, accn, frame)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)
                   ON CONFLICT(cik, taxonomy, concept, unit, period_start, period_end, accn) DO UPDATE SET
                     value = excluded.value, fy = excluded.fy, fp = excluded.fp, form = excluded.form,
                     filed = excluded.filed, frame = excluded.frame""",
                [(cik, f.taxonomy, f.concept, f.unit, f.period_start, f.period_end, f.value, f.fy, f.fp,
                  f.form, f.filed, f.accn, f.frame) for f in facts],
            )
        return len(facts)

    def facts_for(self, cik: str) -> list[Fact]:
        rows = self.query(
            "SELECT taxonomy, concept, unit, period_start, period_end, value, fy, fp, form, filed, accn, frame "
            "FROM sec_facts WHERE cik = ?", (cik,),
        )
        return [Fact(*tuple(r)) for r in rows]

    def replace_line_items(self, cik: str, items: Iterable[LineItem]) -> int:
        items = list(items)
        with self.conn:
            self.conn.execute("DELETE FROM financial_line_items WHERE cik = ?", (cik,))
            self.conn.executemany(
                """INSERT INTO financial_line_items
                   (cik, line_item, period_kind, period_start, period_end, fiscal_year, fiscal_quarter,
                    value, concept, filed, accn, is_derived)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
                [(cik, i.line_item, i.period_kind, i.period_start, i.period_end, i.fiscal_year,
                  i.fiscal_quarter, i.value, i.concept, i.filed, i.accn, int(i.is_derived)) for i in items],
            )
        return len(items)

    def concept_rules(self) -> list[ConceptRule]:
        rows = self.query(
            "SELECT line_item, statement, kind, taxonomy, concept, priority FROM concept_map "
            "ORDER BY line_item, priority"
        )
        return [ConceptRule(*tuple(r)) for r in rows]

    # ---- bookkeeping --------------------------------------------------------
    def file_seen(self, sha256: str) -> bool:
        return self.conn.execute("SELECT 1 FROM ingested_files WHERE sha256 = ?", (sha256,)).fetchone() is not None

    def record_file(self, sha256: str, path: str, kind: str, rows: int) -> None:
        with self.conn:
            self.conn.execute(
                "INSERT OR REPLACE INTO ingested_files (sha256, path, kind, ingested_at, rows) VALUES (?,?,?,?,?)",
                (sha256, path, kind, utcnow(), rows),
            )

    def log_run(
        self, run_id: str, step: str, status: str, rows_written: int, message: str,
        started_at: str, finished_at: str,
    ) -> None:
        with self.conn:
            self.conn.execute(
                "INSERT INTO sync_runs (run_id, step, started_at, finished_at, status, rows_written, message) "
                "VALUES (?,?,?,?,?,?,?)",
                (run_id, step, started_at, finished_at, status, rows_written, message[:4000]),
            )

    def counts(self) -> dict[str, int]:
        return {t: self.conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0] for t in COUNT_TABLES}
```

- [ ] **Step 7: Run to verify pass**

Run: `.venv/Scripts/python -m pytest tests/test_store.py -v`
Expected: 13 passed

- [ ] **Step 8: Commit**

```bash
git add src/financial_data_collector/migrations src/financial_data_collector/seeds src/financial_data_collector/migrate.py src/financial_data_collector/store.py tests/test_store.py
git commit -m "Add SQLite store, migrations and concept-map seed"
```

---
### Task 5: Fidelity positions CSV adapter

**Files:**
- Create: `src/financial_data_collector/adapters/fidelity_positions.py`, `tests/fixtures/Portfolio_Positions_Jan-15-2026.csv`, `tests/test_fidelity_positions.py`

**Interfaces:**
- Consumes: `read_text_lines`, `clean_number`, `infer_account_type` (Task 3); `canonical`, `is_money_market` (Task 2); models.
- Produces: `detect(lines: list[str]) -> bool`, `parse(path: Path) -> Snapshot` (source `"fidelity_csv"`), `snapshot_date(path, lines) -> tuple[str, str | None]` (date, warning), constant `HEADER_PREFIX`.

- [ ] **Step 1: Write the fixture**

`tests/fixtures/Portfolio_Positions_Jan-15-2026.csv` (no BOM; the BOM case is tested from a temp file):
```csv
Account Number,Account Name,Symbol,Description,Quantity,Last Price,Last Price Change,Current Value,Today's Gain/Loss Dollar,Today's Gain/Loss Percent,Total Gain/Loss Dollar,Total Gain/Loss Percent,Percent Of Account,Cost Basis Total,Average Cost Basis,Type
Z12345678,Sample Brokerage,AAPL,APPLE INC,10,$100.00,+$1.00,$1000.00,+$10.00,+1.00%,+$100.00,+11.11%,49.37%,$900.00,$90.00,Cash
Z12345678,Sample Brokerage,KO,COCA COLA CO,20,$50.00,-$0.50,$1000.00,-$10.00,-0.99%,--,--,49.37%,--,--,Cash
Z12345678,Sample Brokerage,SPAXX**,HELD IN MONEY MARKET,25.5,$1.00,$0.00,$25.50,$0.00,0.00%,--,--,1.26%,--,--,Cash
Z12345678,Sample Brokerage,Pending Activity,,,,,$12.00,,,,,,,,
Z87654321,Sample Roth,VTI,VANGUARD TOTAL STOCK MARKET ETF,5,$200.00,+$2.00,$1000.00,+$10.00,+1.00%,+$200.00,+25.00%,100.00%,$800.00,$160.00,Cash

"The data and information in this spreadsheet is provided to you solely for your use and is not for distribution."

"Date downloaded Jan-15-2026 9:00 a.m. ET"
```

- [ ] **Step 2: Write the failing tests**

`tests/test_fidelity_positions.py`:
```python
import shutil
from pathlib import Path

from financial_data_collector.adapters import fidelity_positions as fp
from financial_data_collector.adapters.base import read_text_lines

FIX = "Portfolio_Positions_Jan-15-2026.csv"


def test_detect(fixtures: Path):
    assert fp.detect(read_text_lines(fixtures / FIX))
    assert not fp.detect(["Run Date,Action,Symbol,Description,Type"])
    assert not fp.detect([])


def test_parse_positions_cash_and_dates(fixtures: Path):
    snap = fp.parse(fixtures / FIX)
    assert snap.as_of_date == "2026-01-15"
    assert snap.source == "fidelity_csv"
    assert [p.symbol for p in snap.positions] == ["AAPL", "KO", "VTI"]
    aapl = snap.positions[0]
    assert aapl.account.label == "Sample Brokerage"
    assert aapl.account.institution == "fidelity"
    assert aapl.account.account_type == "brokerage"
    assert (aapl.quantity, aapl.price, aapl.market_value) == (10.0, 100.0, 1000.0)
    assert (aapl.cost_basis_total, aapl.avg_cost, aapl.unrealized_pnl) == (900.0, 90.0, 100.0)
    ko = snap.positions[1]
    assert ko.cost_basis_total is None and ko.unrealized_pnl is None
    vti = snap.positions[2]
    assert vti.account.label == "Sample Roth" and vti.account.account_type == "roth_ira"
    assert vti.account.slug == "roth"
    assert len(snap.cash) == 1
    assert snap.cash[0].account.label == "Sample Brokerage" and snap.cash[0].amount == 25.5
    assert snap.warnings == []


def test_account_numbers_never_leak(fixtures: Path):
    snap = fp.parse(fixtures / FIX)
    assert "Z12345678" not in repr(snap) and "Z87654321" not in repr(snap)


def test_renamed_file_dates_from_footer(fixtures: Path, tmp_path: Path):
    dst = tmp_path / "Portfolio_Positions (1).csv"
    shutil.copy(fixtures / FIX, dst)
    snap = fp.parse(dst)
    assert snap.as_of_date == "2026-01-15"
    assert snap.warnings == []


def test_no_date_anywhere_falls_back_to_mtime_with_warning(fixtures: Path, tmp_path: Path):
    lines = read_text_lines(fixtures / FIX)
    body = [l for l in lines if not l.startswith('"Date downloaded')]
    dst = tmp_path / "positions.csv"
    dst.write_text("\n".join(body), encoding="utf-8")
    snap = fp.parse(dst)
    assert len(snap.as_of_date) == 10
    assert any("mtime" in w for w in snap.warnings)


def test_bom_and_crlf(fixtures: Path, tmp_path: Path):
    raw = (fixtures / FIX).read_text(encoding="utf-8").replace("\n", "\r\n")
    dst = tmp_path / "Portfolio_Positions_Jan-15-2026.csv"
    dst.write_bytes(("﻿" + raw).encode("utf-8"))
    snap = fp.parse(dst)
    assert len(snap.positions) == 3
```

- [ ] **Step 3: Run to verify failure**

Run: `.venv/Scripts/python -m pytest tests/test_fidelity_positions.py -v`
Expected: FAIL, `ImportError: cannot import name 'fidelity_positions'`

- [ ] **Step 4: Implement**

`src/financial_data_collector/adapters/fidelity_positions.py`:
```python
"""Fidelity "Portfolio Positions" CSV export -> Snapshot.

Fidelity.com -> Accounts -> Positions -> Download. The file has a BOM, a header
row, one row per position (plus a "Pending Activity" row per account), then a
blank line and a quoted disclaimer block ending in a "Date downloaded" line.
"""
from __future__ import annotations

import csv
import re
from datetime import datetime, timezone
from pathlib import Path

from ..models import AccountRef, CashRow, PositionRow, Snapshot
from ..symbols import canonical, is_money_market
from .base import clean_number, infer_account_type, read_text_lines

HEADER_PREFIX = "Account Number,Account Name,Symbol,Description,Quantity,Last Price"
SOURCE = "fidelity_csv"

_MONTHS = {m: i for i, m in enumerate(
    ["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"], start=1)}
_FILENAME_DATE = re.compile(r"Portfolio_Positions_([A-Za-z]{3})-(\d{1,2})-(\d{4})")
_FOOTER_DATE = re.compile(r"Date downloaded\s+([A-Za-z]{3})-(\d{1,2})-(\d{4})")


def detect(lines: list[str]) -> bool:
    for line in lines:
        if line.strip():
            return line.lstrip("﻿").startswith(HEADER_PREFIX)
    return False


def _iso(mon: str, day: str, year: str) -> str | None:
    m = _MONTHS.get(mon.lower())
    if not m:
        return None
    return f"{int(year):04d}-{m:02d}-{int(day):02d}"


def snapshot_date(path: Path, lines: list[str]) -> tuple[str, str | None]:
    """Date the snapshot from the filename, else the footer, else file mtime."""
    m = _FILENAME_DATE.search(path.name)
    if m:
        d = _iso(*m.groups())
        if d:
            return d, None
    for line in reversed(lines):
        m = _FOOTER_DATE.search(line)
        if m:
            d = _iso(*m.groups())
            if d:
                return d, None
    mtime = datetime.fromtimestamp(path.stat().st_mtime, timezone.utc).strftime("%Y-%m-%d")
    return mtime, f"{path.name}: no date in filename or footer; used file mtime {mtime}"


def _account(label: str) -> AccountRef:
    kind = infer_account_type(label)
    slug = {"roth_ira": "roth", "brokerage": "brokerage"}.get(kind, "other")
    return AccountRef(label=label, slug=slug, institution="fidelity", account_type=kind)


def parse(path: Path) -> Snapshot:
    lines = read_text_lines(path)
    if not detect(lines):
        raise ValueError(f"{path.name}: not a Fidelity positions export")
    start = next(i for i, l in enumerate(lines) if l.lstrip("﻿").startswith(HEADER_PREFIX))
    body: list[str] = []
    for line in lines[start:]:
        if not line.strip():
            break
        body.append(line)
    as_of, warning = snapshot_date(path, lines)
    warnings = [warning] if warning else []
    positions: list[PositionRow] = []
    cash: dict[tuple[str, str], tuple[AccountRef, float]] = {}
    for row in csv.DictReader(body):
        raw_symbol = (row.get("Symbol") or "").strip()
        if not raw_symbol or raw_symbol.lower() == "pending activity":
            continue
        label = (row.get("Account Name") or "").strip()
        if not label:
            warnings.append(f"row for {raw_symbol} has no account name; skipped")
            continue
        acct = _account(label)
        desc = (row.get("Description") or "").strip() or None
        value = clean_number(row.get("Current Value"))
        if is_money_market(raw_symbol, desc):
            key = (label, "USD")
            prev = cash.get(key, (acct, 0.0))[1]
            cash[key] = (acct, prev + (value or 0.0))
            continue
        qty = clean_number(row.get("Quantity"))
        if qty is None:
            warnings.append(f"{raw_symbol} in {label}: no quantity; skipped")
            continue
        positions.append(PositionRow(
            account=acct,
            symbol=canonical(raw_symbol),
            description=desc,
            quantity=qty,
            price=clean_number(row.get("Last Price")),
            market_value=value,
            cost_basis_total=clean_number(row.get("Cost Basis Total")),
            avg_cost=clean_number(row.get("Average Cost Basis")),
            unrealized_pnl=clean_number(row.get("Total Gain/Loss Dollar")),
        ))
    cash_rows = [CashRow(acct, amount) for (acct, amount) in cash.values()]
    return Snapshot(as_of_date=as_of, source=SOURCE, positions=positions, cash=cash_rows,
                    fetched_at=None, warnings=warnings)
```

- [ ] **Step 5: Run to verify pass**

Run: `.venv/Scripts/python -m pytest tests/test_fidelity_positions.py -v`
Expected: 6 passed

- [ ] **Step 6: Commit**

```bash
git add src/financial_data_collector/adapters/fidelity_positions.py tests/fixtures/Portfolio_Positions_Jan-15-2026.csv tests/test_fidelity_positions.py
git commit -m "Add Fidelity positions CSV adapter"
```

---

### Task 6: Fidelity history CSV adapter

**Files:**
- Create: `src/financial_data_collector/adapters/fidelity_history.py`, `tests/fixtures/History_SampleBrokerage_2026.csv`, `tests/test_fidelity_history.py`

**Interfaces:**
- Consumes: Task 3 helpers, Task 2 `canonical`, models.
- Produces: `detect(lines) -> bool`, `classify_action(action: str) -> str`, `parse(path: Path, account: str | None = None) -> list[TransactionRow]` (source `"fidelity_csv"`), exception `AccountUnknown(ValueError)`, `account_from_filename(path) -> str | None`.

- [ ] **Step 1: Write the fixture**

`tests/fixtures/History_SampleBrokerage_2026.csv` (Fidelity pads fields with a leading space and puts two blank lines before the header; both are reproduced):
```csv


Run Date,Action,Symbol,Description,Type,Price ($),Quantity,Commission ($),Fees ($),Accrued Interest ($),Amount ($),Cash Balance ($),Settlement Date
 01/05/2026, YOU BOUGHT APPLE INC (AAPL) (Cash), AAPL, APPLE INC, Cash, 100.00, 10, 0.00, 0.00, 0.00, -1000.00, 500.00, 01/07/2026
 01/06/2026, DIVIDEND RECEIVED COCA COLA CO (KO) (Cash), KO, COCA COLA CO, Cash, , , , , , 9.80, 509.80, 
 01/06/2026, REINVESTMENT COCA COLA CO (KO) (Cash), KO, COCA COLA CO, Cash, 49.00, 0.2, , , , -9.80, 500.00, 
 01/10/2026, ELECTRONIC FUNDS TRANSFER RECEIVED (Cash), , , Cash, , , , , , 500.00, 1000.00, 
 01/12/2026, YOU SOLD VANGUARD TOTAL STOCK MARKET ETF (VTI) (Cash), VTI, VANGUARD TOTAL STOCK MARKET ETF, Cash, 200.00, -2, 0.00, 0.01, 0.00, 399.99, 1399.99, 01/14/2026
 01/15/2026, DISTRIBUTION (Cash), , , Cash, , , , , , -100.00, 1299.99, 
 01/20/2026, JOURNALED SHARES (Cash), KO, COCA COLA CO, Cash, , 3, , , , , 1299.99, 

"The data and information in this spreadsheet is provided to you solely for your use and is not for distribution."
"Date downloaded 01/25/2026 9:00 pm"
```

- [ ] **Step 2: Write the failing tests**

`tests/test_fidelity_history.py`:
```python
import shutil
from pathlib import Path

import pytest

from financial_data_collector.adapters import fidelity_history as fh
from financial_data_collector.adapters.base import read_text_lines

FIX = "History_SampleBrokerage_2026.csv"


def test_detect(fixtures: Path):
    assert fh.detect(read_text_lines(fixtures / FIX))
    assert not fh.detect(["Account Number,Account Name,Symbol"])


@pytest.mark.parametrize(
    "action, want",
    [
        ("YOU BOUGHT APPLE INC (AAPL) (Cash)", "buy"),
        ("YOU SOLD VTI (Cash)", "sell"),
        ("DIVIDEND RECEIVED KO (Cash)", "dividend"),
        ("REINVESTMENT KO (Cash)", "reinvest"),
        ("ELECTRONIC FUNDS TRANSFER RECEIVED (Cash)", "contribution"),
        ("CONTRIBUTION (Cash)", "contribution"),
        ("DIRECT DEPOSIT PAYROLL (Cash)", "contribution"),
        ("ELECTRONIC FUNDS TRANSFER PAID (Cash)", "withdrawal"),
        ("DISTRIBUTION (Cash)", "withdrawal"),
        ("INTEREST EARNED SPAXX", "interest"),
        ("ADVISOR FEE", "fee"),
        ("FEE CHARGED", "fee"),
        ("TRANSFERRED FROM VS X12", "transfer"),
        ("JOURNALED SHARES (Cash)", "other"),
        ("", "other"),
    ],
)
def test_classify_action(action, want):
    assert fh.classify_action(action) == want


def test_parse_with_explicit_account(fixtures: Path):
    rows = fh.parse(fixtures / FIX, account="Sample Brokerage")
    assert len(rows) == 7
    assert all(r.account.label == "Sample Brokerage" for r in rows)
    assert all(r.source == "fidelity_csv" for r in rows)
    assert [r.type for r in rows] == [
        "buy", "dividend", "reinvest", "contribution", "sell", "withdrawal", "other"]
    buy = rows[0]
    assert (buy.trade_date, buy.settlement_date) == ("2026-01-05", "2026-01-07")
    assert (buy.symbol, buy.units, buy.price, buy.amount, buy.fee) == ("AAPL", 10.0, 100.0, -1000.0, 0.0)
    assert buy.description.startswith("YOU BOUGHT")
    div = rows[1]
    assert div.units is None and div.amount == 9.8 and div.fee is None
    contrib = rows[3]
    assert contrib.symbol is None and contrib.amount == 500.0
    sell = rows[4]
    assert sell.units == -2.0 and sell.fee == 0.01
    journal = rows[6]
    assert journal.amount is None and journal.units == 3.0 and journal.symbol == "KO"


def test_account_from_filename(fixtures: Path):
    assert fh.account_from_filename(fixtures / FIX) == "SampleBrokerage"
    assert fh.account_from_filename(Path("History.csv")) is None
    assert fh.account_from_filename(Path("Accounts_History.csv")) is None


def test_parse_falls_back_to_filename(fixtures: Path):
    rows = fh.parse(fixtures / FIX)
    assert rows[0].account.label == "SampleBrokerage"


def test_parse_without_any_account_raises(fixtures: Path, tmp_path: Path):
    dst = tmp_path / "Accounts_History.csv"
    shutil.copy(fixtures / FIX, dst)
    with pytest.raises(fh.AccountUnknown):
        fh.parse(dst)


def test_account_column_wins(fixtures: Path, tmp_path: Path):
    lines = read_text_lines(fixtures / FIX)
    out = []
    for line in lines:
        if line.startswith("Run Date,"):
            out.append("Account," + line)
        elif line.startswith(" 0"):
            out.append("Sample Roth," + line)
        else:
            out.append(line)
    dst = tmp_path / "Accounts_History.csv"
    dst.write_text("\n".join(out), encoding="utf-8")
    rows = fh.parse(dst)
    assert {r.account.label for r in rows} == {"Sample Roth"}
    assert rows[0].account.account_type == "roth_ira"
```

- [ ] **Step 3: Run to verify failure**

Run: `.venv/Scripts/python -m pytest tests/test_fidelity_history.py -v`
Expected: FAIL, `ImportError`

- [ ] **Step 4: Implement**

`src/financial_data_collector/adapters/fidelity_history.py`:
```python
"""Fidelity "Accounts -> Activity & Orders -> Download" history CSV -> transactions.

Per-account downloads have no Account column, so the label comes from the
--account flag, else from the filename (History_<Label>_<anything>.csv), else
the file is refused with AccountUnknown.
"""
from __future__ import annotations

import csv
import re
from pathlib import Path

from ..models import AccountRef, TransactionRow
from ..symbols import canonical
from .base import clean_number, infer_account_type, read_text_lines

HEADER_PREFIX = "Run Date,Action,Symbol,Description,Type"
SOURCE = "fidelity_csv"

_ACTION_TYPES: tuple[tuple[str, str], ...] = (
    ("YOU BOUGHT", "buy"),
    ("YOU SOLD", "sell"),
    ("DIVIDEND RECEIVED", "dividend"),
    ("REINVESTMENT", "reinvest"),
    ("ELECTRONIC FUNDS TRANSFER RECEIVED", "contribution"),
    ("CONTRIBUTION", "contribution"),
    ("DIRECT DEPOSIT", "contribution"),
    ("ELECTRONIC FUNDS TRANSFER PAID", "withdrawal"),
    ("DISTRIBUTION", "withdrawal"),
    ("INTEREST EARNED", "interest"),
    ("ADVISOR FEE", "fee"),
    ("FEE", "fee"),
    ("TRANSFERRED", "transfer"),
)
_FILENAME_ACCOUNT = re.compile(r"^History_([^_]+)_")
_DATE = re.compile(r"^(\d{1,2})/(\d{1,2})/(\d{4})$")


class AccountUnknown(ValueError):
    """Raised when a history file carries no account label and none was given."""


def detect(lines: list[str]) -> bool:
    return any(_is_header(l) for l in lines[:10])


def _is_header(line: str) -> bool:
    s = line.lstrip("﻿").strip()
    return s.startswith(HEADER_PREFIX) or s.startswith("Account," + HEADER_PREFIX)


def classify_action(action: str) -> str:
    u = (action or "").strip().upper()
    for prefix, kind in _ACTION_TYPES:
        if u.startswith(prefix):
            return kind
    return "other"


def account_from_filename(path: Path) -> str | None:
    m = _FILENAME_ACCOUNT.match(path.name)
    return m.group(1).strip() if m else None


def _iso(text: str | None) -> str | None:
    s = (text or "").strip()
    m = _DATE.match(s)
    if not m:
        return None
    mo, d, y = m.groups()
    return f"{int(y):04d}-{int(mo):02d}-{int(d):02d}"


def _account(label: str) -> AccountRef:
    kind = infer_account_type(label)
    slug = {"roth_ira": "roth", "brokerage": "brokerage"}.get(kind, "other")
    return AccountRef(label=label, slug=slug, institution="fidelity", account_type=kind)


def parse(path: Path, account: str | None = None) -> list[TransactionRow]:
    lines = read_text_lines(path)
    if not detect(lines):
        raise ValueError(f"{path.name}: not a Fidelity history export")
    start = next(i for i, l in enumerate(lines) if _is_header(l))
    body: list[str] = []
    for line in lines[start:]:
        if not line.strip():
            break
        body.append(line.lstrip("﻿"))
    reader = csv.DictReader(body, skipinitialspace=True)
    has_account_col = "Account" in (reader.fieldnames or [])
    fallback = account or account_from_filename(path)
    if not has_account_col and not fallback:
        raise AccountUnknown(
            f"{path.name}: no Account column and no label; re-run: fdc import {path.name} --account \"<label>\""
        )
    rows: list[TransactionRow] = []
    for row in reader:
        date = _iso(row.get("Run Date"))
        if not date:
            continue  # disclaimer / footer lines never reach here, but be safe
        label = (row.get("Account") or "").strip() if has_account_col else ""
        label = label or fallback or ""
        if not label:
            raise AccountUnknown(f"{path.name}: row dated {date} has an empty Account cell")
        action = (row.get("Action") or "").strip()
        symbol_raw = (row.get("Symbol") or "").strip()
        commission = clean_number(row.get("Commission ($)"))
        fees = clean_number(row.get("Fees ($)"))
        fee = None if commission is None and fees is None else (commission or 0.0) + (fees or 0.0)
        rows.append(TransactionRow(
            account=_account(label),
            trade_date=date,
            type=classify_action(action),
            symbol=canonical(symbol_raw) or None,
            units=clean_number(row.get("Quantity")),
            price=clean_number(row.get("Price ($)")),
            amount=clean_number(row.get("Amount ($)")),
            fee=fee,
            description=action,
            source=SOURCE,
            settlement_date=_iso(row.get("Settlement Date")),
        ))
    return rows
```

- [ ] **Step 5: Run to verify pass**

Run: `.venv/Scripts/python -m pytest tests/test_fidelity_history.py -v`
Expected: 21 passed

- [ ] **Step 6: Commit**

```bash
git add src/financial_data_collector/adapters/fidelity_history.py tests/fixtures/History_SampleBrokerage_2026.csv tests/test_fidelity_history.py
git commit -m "Add Fidelity history CSV adapter"
```

---
### Task 7: SnapTrade JSON adapter (owner's investing export)

**Files:**
- Create: `src/financial_data_collector/adapters/snaptrade.py`, `tests/fixtures/snaptrade/live-positions.json`, `tests/fixtures/snaptrade/live-activity.json`, `tests/fixtures/snaptrade/snapshots/live-positions-2026-01-10.json`, `tests/test_snaptrade.py`

**Interfaces:**
- Consumes: models, `canonical`, `is_money_market`, `infer_account_type`.
- Produces: `parse_positions(path: Path) -> Snapshot` (source `"snaptrade"`), `parse_activity(path: Path) -> list[TransactionRow]`, `find_snapshot_files(dir: Path) -> list[Path]` (snapshots oldest→newest, then the live file last).

- [ ] **Step 1: Write the fixtures**

`tests/fixtures/snaptrade/live-positions.json`:
```json
{
  "fetchedAt": "2026-01-16T00:00:00.000Z",
  "accounts": [
    {
      "label": "Sample Brokerage", "slug": "brokerage",
      "holdings": [
        {"symbol": "AAPL", "description": "APPLE INC", "units": 10, "price": 101.0, "marketValue": 1010.0, "averageCost": 90.0, "openPnl": 110.0, "currency": "USD"},
        {"symbol": "SPAXX", "description": "FIDELITY GOVERNMENT MONEY MARKET", "units": 25.5, "price": 1.0, "marketValue": 25.5, "averageCost": 1.0, "openPnl": 0, "currency": "USD"}
      ],
      "cash": [{"currency": "USD", "amount": 25.5}],
      "totalMarketValue": 1035.5
    },
    {
      "label": "Webull Sample Cash", "slug": "brokerage",
      "holdings": [
        {"symbol": "KO", "description": "COCA COLA CO", "units": 3, "price": 50.0, "marketValue": 150.0, "averageCost": 48.0, "openPnl": 6.0, "currency": "USD"}
      ],
      "cash": [{"currency": "USD", "amount": 10.0}],
      "totalMarketValue": 160.0
    },
    {
      "label": "Sample Roth", "slug": "roth",
      "holdings": [
        {"symbol": "VTI", "description": "VANGUARD TOTAL STOCK MARKET ETF", "units": 5, "price": 201.0, "marketValue": 1005.0, "averageCost": 160.0, "openPnl": 205.0, "currency": "USD"}
      ],
      "cash": [{"currency": "USD", "amount": 0.2}],
      "totalMarketValue": 1005.2
    },
    {"label": "Webull Crypto", "slug": "other", "holdings": [], "cash": [], "totalMarketValue": 0}
  ],
  "totalMarketValue": 2200.7
}
```

`tests/fixtures/snaptrade/snapshots/live-positions-2026-01-10.json`:
```json
{
  "fetchedAt": "2026-01-10T13:05:00.000Z",
  "accounts": [
    {
      "label": "Sample Brokerage", "slug": "brokerage",
      "holdings": [
        {"symbol": "AAPL", "description": "APPLE INC", "units": 10, "price": 99.0, "marketValue": 990.0, "averageCost": 90.0, "openPnl": 90.0, "currency": "USD"}
      ],
      "cash": [{"currency": "USD", "amount": 20.0}],
      "totalMarketValue": 1010.0
    }
  ],
  "totalMarketValue": 1010.0
}
```

`tests/fixtures/snaptrade/live-activity.json`:
```json
{
  "fetchedAt": "2026-01-16T00:00:00.000Z",
  "activities": [
    {"tradeDate": "2026-01-05T00:00:00.000Z", "settlementDate": "2026-01-07T00:00:00.000Z", "type": "BUY", "symbol": "AAPL", "units": 10, "price": 100.0, "amount": -1000.0, "fee": 0, "accountLabel": "Sample Brokerage"},
    {"tradeDate": "2026-01-06T00:00:00.000Z", "settlementDate": null, "type": "DIVIDEND", "symbol": "KO", "units": 0, "price": 0, "amount": 9.8, "fee": 0, "accountLabel": "Sample Brokerage"},
    {"tradeDate": "2026-01-06T00:00:00.000Z", "settlementDate": null, "type": "REI", "symbol": "KO", "units": 0.2, "price": 49.0, "amount": -9.8, "fee": 0, "accountLabel": "Sample Brokerage"},
    {"tradeDate": "2026-01-10T00:00:00.000Z", "settlementDate": null, "type": "CONTRIBUTION", "symbol": null, "units": 0, "price": 0, "amount": 500.0, "fee": 0, "accountLabel": "Sample Roth"},
    {"tradeDate": "2026-01-12T00:00:00.000Z", "settlementDate": "2026-01-14T00:00:00.000Z", "type": "SELL", "symbol": "BRKB", "units": -1, "price": 400.0, "amount": 400.0, "fee": 0.01, "accountLabel": "Webull Sample Cash"},
    {"tradeDate": "2026-01-13T00:00:00.000Z", "settlementDate": null, "type": "FEE", "symbol": null, "units": 0, "price": 0, "amount": -1.0, "fee": 0, "accountLabel": "Webull Sample Cash"}
  ]
}
```

- [ ] **Step 2: Write the failing tests**

`tests/test_snaptrade.py`:
```python
import json
from pathlib import Path

from financial_data_collector.adapters import snaptrade as st


def test_parse_positions(fixtures: Path):
    snap = st.parse_positions(fixtures / "snaptrade" / "live-positions.json")
    assert snap.as_of_date == "2026-01-16"           # midnight UTC stays that UTC date
    assert snap.fetched_at == "2026-01-16T00:00:00.000Z"
    assert snap.source == "snaptrade"
    assert [(p.account.label, p.symbol) for p in snap.positions] == [
        ("Sample Brokerage", "AAPL"), ("Webull Sample Cash", "KO"), ("Sample Roth", "VTI")]
    aapl = snap.positions[0]
    assert (aapl.quantity, aapl.price, aapl.market_value) == (10.0, 101.0, 1010.0)
    assert (aapl.avg_cost, aapl.cost_basis_total, aapl.unrealized_pnl) == (90.0, 900.0, 110.0)
    assert aapl.account.institution == "fidelity" and aapl.account.slug == "brokerage"
    ko = snap.positions[1]
    assert ko.account.institution == "webull" and ko.account.account_type == "brokerage"
    assert [(c.account.label, c.amount) for c in snap.cash] == [
        ("Sample Brokerage", 25.5), ("Webull Sample Cash", 10.0), ("Sample Roth", 0.2)]


def test_local_time_never_used(fixtures: Path, tmp_path: Path):
    data = json.loads((fixtures / "snaptrade" / "live-positions.json").read_text())
    data["fetchedAt"] = "2026-03-01T03:30:00.000Z"
    p = tmp_path / "live-positions.json"
    p.write_text(json.dumps(data))
    assert st.parse_positions(p).as_of_date == "2026-03-01"


def test_parse_activity(fixtures: Path):
    rows = st.parse_activity(fixtures / "snaptrade" / "live-activity.json")
    assert [r.type for r in rows] == ["buy", "dividend", "reinvest", "contribution", "sell", "other"]
    buy = rows[0]
    assert (buy.trade_date, buy.settlement_date, buy.symbol) == ("2026-01-05", "2026-01-07", "AAPL")
    assert (buy.units, buy.price, buy.amount, buy.fee) == (10.0, 100.0, -1000.0, 0.0)
    assert buy.source == "snaptrade" and buy.description == "BUY"
    div = rows[1]
    assert div.units is None and div.price is None and div.amount == 9.8
    contrib = rows[3]
    assert contrib.symbol is None and contrib.account.label == "Sample Roth"
    assert contrib.account.account_type == "roth_ira"
    sell = rows[4]
    assert sell.symbol == "BRK.B" and sell.account.institution == "webull"


def test_find_snapshot_files(fixtures: Path):
    files = st.find_snapshot_files(fixtures / "snaptrade")
    assert [f.name for f in files] == ["live-positions-2026-01-10.json", "live-positions.json"]
    assert st.find_snapshot_files(fixtures / "nope") == []
```

- [ ] **Step 3: Run to verify failure**

Run: `.venv/Scripts/python -m pytest tests/test_snaptrade.py -v`
Expected: FAIL, `ImportError`

- [ ] **Step 4: Implement**

`src/financial_data_collector/adapters/snaptrade.py`:
```python
"""investing's SnapTrade export (live-positions.json, snapshots/, live-activity.json).

Owner-only source. Shapes are those written by investing/pipeline/export_positions.mjs:
  live-positions.json  {fetchedAt, accounts[{label, slug, holdings[], cash[], totalMarketValue}]}
  live-activity.json   {fetchedAt, activities[{tradeDate, settlementDate, type, symbol, units,
                        price, amount, fee, accountLabel}]}
Dates are taken from the UTC timestamps as written; never converted to local time.
"""
from __future__ import annotations

import json
from pathlib import Path

from ..models import AccountRef, CashRow, PositionRow, Snapshot, TransactionRow
from ..symbols import canonical, is_money_market
from .base import infer_account_type

SOURCE = "snaptrade"
_ACTIVITY_TYPES = {"BUY": "buy", "SELL": "sell", "DIVIDEND": "dividend", "CONTRIBUTION": "contribution",
                   "REI": "reinvest", "WITHDRAWAL": "withdrawal", "INTEREST": "interest"}


def _account(label: str, slug: str | None = None) -> AccountRef:
    institution = "webull" if "WEBULL" in label.upper() else "fidelity"
    kind = infer_account_type(label)
    return AccountRef(label=label, slug=slug or "other", institution=institution, account_type=kind)


def _num(v) -> float | None:
    if v is None:
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _nonzero(v) -> float | None:
    f = _num(v)
    return None if not f else f


def parse_positions(path: Path) -> Snapshot:
    data = json.loads(path.read_text(encoding="utf-8"))
    fetched = data.get("fetchedAt") or ""
    if len(fetched) < 10:
        raise ValueError(f"{path.name}: missing fetchedAt")
    positions: list[PositionRow] = []
    cash: list[CashRow] = []
    for acct in data.get("accounts", []):
        ref = _account(acct.get("label", ""), acct.get("slug"))
        for h in acct.get("holdings", []) or []:
            raw = h.get("symbol") or ""
            desc = h.get("description")
            if not raw or is_money_market(raw, desc):
                continue
            units = _num(h.get("units")) or 0.0
            avg = _num(h.get("averageCost"))
            positions.append(PositionRow(
                account=ref, symbol=canonical(raw), description=desc, quantity=units,
                price=_num(h.get("price")), market_value=_num(h.get("marketValue")),
                cost_basis_total=(avg * units) if avg is not None else None,
                avg_cost=avg, unrealized_pnl=_num(h.get("openPnl")),
            ))
        for c in acct.get("cash", []) or []:
            amount = _num(c.get("amount"))
            if amount is None:
                continue
            cash.append(CashRow(account=ref, amount=amount, currency=c.get("currency") or "USD"))
    return Snapshot(as_of_date=fetched[:10], source=SOURCE, positions=positions, cash=cash, fetched_at=fetched)


def parse_activity(path: Path) -> list[TransactionRow]:
    data = json.loads(path.read_text(encoding="utf-8"))
    rows: list[TransactionRow] = []
    for a in data.get("activities", []):
        trade = (a.get("tradeDate") or "")[:10]
        label = a.get("accountLabel") or ""
        if len(trade) < 10 or not label:
            continue
        raw_type = (a.get("type") or "").upper()
        settle = (a.get("settlementDate") or "")[:10] or None
        raw_symbol = a.get("symbol") or ""
        rows.append(TransactionRow(
            account=_account(label),
            trade_date=trade,
            type=_ACTIVITY_TYPES.get(raw_type, "other"),
            symbol=canonical(raw_symbol) or None,
            units=_nonzero(a.get("units")),
            price=_nonzero(a.get("price")),
            amount=_num(a.get("amount")),
            fee=_num(a.get("fee")),
            description=raw_type,
            source=SOURCE,
            settlement_date=settle,
        ))
    return rows


def find_snapshot_files(directory: Path) -> list[Path]:
    if not directory.is_dir():
        return []
    files = sorted((directory / "snapshots").glob("live-positions-*.json")) if (directory / "snapshots").is_dir() else []
    live = directory / "live-positions.json"
    if live.is_file():
        files.append(live)
    return files
```

- [ ] **Step 5: Run to verify pass**

Run: `.venv/Scripts/python -m pytest tests/test_snaptrade.py -v`
Expected: 4 passed

- [ ] **Step 6: Commit**

```bash
git add src/financial_data_collector/adapters/snaptrade.py tests/fixtures/snaptrade tests/test_snaptrade.py
git commit -m "Add SnapTrade JSON adapter for the investing export"
```

---

### Task 8: Config and ingest layer

**Files:**
- Create: `src/financial_data_collector/config.py`, `src/financial_data_collector/ingest.py`, `tests/test_config.py`, `tests/test_ingest.py`

**Interfaces:**
- Consumes: `Store` (Task 4), the three adapters, `sha256_file`.
- Produces (config): `Config` dataclass with fields `root, db_path, inbox, processed_dir, watchlist, backup_dir, cache_dir, lookback_years, sec_max_age_hours, snaptrade_enabled, snaptrade_dir, classify, tiingo_token, sec_user_agent`; `load_config(root: Path) -> Config`; `init_project(root: Path) -> list[str]` (paths created); `ConfigError(Exception)`; constants `CONFIG_EXAMPLE: str`, `ENV_EXAMPLE: str`.
- Produces (ingest): `IngestResult(path: str, kind: str | None, rows: int, skipped: str | None, warnings: list[str])`; `IngestSummary(results: list[IngestResult], rows: int, messages: list[str])`; `classify_file(path) -> str | None` (`"fidelity_positions"` / `"fidelity_history"` / `None`); `ingest_file(store, path, *, account=None, move_to=None) -> IngestResult`; `ingest_inbox(store, config) -> IngestSummary`; `ingest_snaptrade(store, config) -> IngestSummary`.

- [ ] **Step 1: Write the failing config tests**

`tests/test_config.py`:
```python
from pathlib import Path

import pytest

from financial_data_collector import config as C


def test_init_project_creates_layout(tmp_path: Path):
    created = C.init_project(tmp_path)
    for rel in ("config.toml", ".env", "data", "data/backups", "data/cache", "inbox", "inbox/processed"):
        assert (tmp_path / rel).exists(), rel
    assert "config.toml" in " ".join(created)
    assert C.init_project(tmp_path) == []          # second call creates nothing


def test_load_config_defaults_and_env(tmp_path: Path):
    C.init_project(tmp_path)
    (tmp_path / ".env").write_text("TIINGO_API_TOKEN=abc\nSEC_USER_AGENT=Sample Person sample@example.com\n")
    cfg = C.load_config(tmp_path)
    assert cfg.db_path == tmp_path / "data" / "warehouse.db"
    assert cfg.inbox == tmp_path / "inbox"
    assert cfg.processed_dir == tmp_path / "inbox" / "processed"
    assert cfg.backup_dir == tmp_path / "data" / "backups"
    assert cfg.cache_dir == tmp_path / "data" / "cache"
    assert cfg.lookback_years == 5 and cfg.sec_max_age_hours == 24
    assert cfg.snaptrade_enabled is True
    assert cfg.snaptrade_dir == (tmp_path / ".." / "investing" / "fidelity" / "dashboard-data").resolve()
    assert cfg.tiingo_token == "abc"
    assert cfg.sec_user_agent == "Sample Person sample@example.com"
    assert cfg.classify == {}


def test_load_config_overrides(tmp_path: Path):
    (tmp_path / "config.toml").write_text(
        '[paths]\ndb = "x/y.db"\n[prices]\nlookback_years = 2\n[sec]\nmax_age_hours = 1\n'
        '[sources.snaptrade]\nenabled = false\n[classify]\nVTI = "etf"\n'
    )
    cfg = C.load_config(tmp_path)
    assert cfg.db_path == tmp_path / "x" / "y.db"
    assert cfg.lookback_years == 2 and cfg.sec_max_age_hours == 1
    assert cfg.snaptrade_enabled is False
    assert cfg.classify == {"VTI": "etf"}
    assert cfg.tiingo_token is None and cfg.sec_user_agent is None


def test_load_config_missing_raises(tmp_path: Path):
    with pytest.raises(C.ConfigError):
        C.load_config(tmp_path)


def test_process_env_overrides_dotenv(tmp_path: Path, monkeypatch):
    C.init_project(tmp_path)
    (tmp_path / ".env").write_text("TIINGO_API_TOKEN=fromfile\n")
    monkeypatch.setenv("TIINGO_API_TOKEN", "fromenv")
    assert C.load_config(tmp_path).tiingo_token == "fromenv"
```

- [ ] **Step 2: Implement config.py**

`src/financial_data_collector/config.py`:
```python
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


def _secret(name: str, dotenv: dict[str, str | None]) -> str | None:
    v = os.environ.get(name) or dotenv.get(name) or ""
    v = v.strip()
    return v or None


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
    snap = raw.get("sources", {}).get("snaptrade", {})
    db_path = (root / paths.get("db", "data/warehouse.db")).resolve()
    inbox = (root / paths.get("inbox", "inbox")).resolve()
    dotenv = dotenv_values(root / ".env") if (root / ".env").is_file() else {}
    snap_dir = snap.get("dir", "../investing/fidelity/dashboard-data")
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
    )
```

- [ ] **Step 3: Run config tests**

Run: `.venv/Scripts/python -m pytest tests/test_config.py -v`
Expected: 5 passed

- [ ] **Step 4: Write the failing ingest tests**

`tests/test_ingest.py`:
```python
import shutil
from pathlib import Path

import pytest

from financial_data_collector import config as C
from financial_data_collector import ingest
from financial_data_collector.store import Store

POS = "Portfolio_Positions_Jan-15-2026.csv"
HIST = "History_SampleBrokerage_2026.csv"


@pytest.fixture
def project(tmp_path: Path, fixtures: Path):
    C.init_project(tmp_path)
    (tmp_path / "config.toml").write_text(
        '[sources.snaptrade]\nenabled = true\ndir = "snap"\n'
    )
    shutil.copytree(fixtures / "snaptrade", tmp_path / "snap")
    cfg = C.load_config(tmp_path)
    store = Store.open(cfg.db_path)
    yield cfg, store
    store.close()


def test_classify_file(fixtures: Path, tmp_path: Path):
    assert ingest.classify_file(fixtures / POS) == "fidelity_positions"
    assert ingest.classify_file(fixtures / HIST) == "fidelity_history"
    other = tmp_path / "other.csv"
    other.write_text("a,b\n1,2\n")
    assert ingest.classify_file(other) is None


def test_ingest_inbox_routes_and_is_idempotent(project, fixtures: Path):
    cfg, store = project
    shutil.copy(fixtures / POS, cfg.inbox / POS)
    shutil.copy(fixtures / HIST, cfg.inbox / HIST)
    (cfg.inbox / "junk.csv").write_text("a,b\n1,2\n")
    s1 = ingest.ingest_inbox(store, cfg)
    kinds = {r.path.split("/")[-1].split("\\")[-1]: (r.kind, r.rows, r.skipped) for r in s1.results}
    assert kinds[POS] == ("fidelity_positions", 4, None)     # 3 positions + 1 cash row
    assert kinds[HIST] == ("fidelity_history", 7, None)
    assert kinds["junk.csv"][0] is None and "unrecognized" in kinds["junk.csv"][2]
    assert s1.rows == 11
    assert (cfg.processed_dir / POS).exists() and (cfg.processed_dir / HIST).exists()
    assert (cfg.inbox / "junk.csv").exists()                   # unrecognized files stay put
    before = store.counts()
    shutil.copy(fixtures / POS, cfg.inbox / POS)               # same bytes again
    s2 = ingest.ingest_inbox(store, cfg)
    assert s2.rows == 0 and "already ingested" in s2.results[0].skipped
    assert store.counts() == before


def test_history_without_account_is_skipped_with_hint(project, fixtures: Path):
    cfg, store = project
    shutil.copy(fixtures / HIST, cfg.inbox / "Accounts_History.csv")
    s = ingest.ingest_inbox(store, cfg)
    assert s.rows == 0
    assert "--account" in s.results[0].skipped
    assert (cfg.inbox / "Accounts_History.csv").exists()


def test_ingest_file_with_account(project, fixtures: Path):
    cfg, store = project
    r = ingest.ingest_file(store, fixtures / HIST, account="Sample Brokerage")
    assert r.rows == 7 and r.skipped is None
    assert store.query("SELECT label FROM accounts")[0][0] == "Sample Brokerage"


def test_ingest_snaptrade(project):
    cfg, store = project
    s = ingest.ingest_snaptrade(store, cfg)
    assert s.rows > 0
    dates = [r[0] for r in store.query("SELECT DISTINCT as_of_date FROM position_snapshots ORDER BY 1")]
    assert dates == ["2026-01-10", "2026-01-16"]
    assert store.query("SELECT COUNT(*) FROM transactions")[0][0] == 6
    inst = {r[0]: r[1] for r in store.query("SELECT label, institution FROM accounts")}
    assert inst["Webull Sample Cash"] == "webull" and inst["Sample Roth"] == "fidelity"
    again = ingest.ingest_snaptrade(store, cfg)
    assert again.rows == 0


def test_ingest_snaptrade_disabled_or_missing(project, tmp_path: Path):
    cfg, store = project
    cfg.snaptrade_enabled = False
    assert ingest.ingest_snaptrade(store, cfg).rows == 0
    cfg.snaptrade_enabled = True
    cfg.snaptrade_dir = tmp_path / "missing"
    s = ingest.ingest_snaptrade(store, cfg)
    assert s.rows == 0 and any("missing" in m for m in s.messages)
```

- [ ] **Step 5: Implement ingest.py**

`src/financial_data_collector/ingest.py`:
```python
"""Route local files into the store: inbox CSVs and the SnapTrade export folder.

Every file is hashed; a hash already in ingested_files is skipped, which is
what makes re-running sync a no-op.
"""
from __future__ import annotations

import shutil
from dataclasses import dataclass, field
from pathlib import Path

from .adapters import fidelity_history, fidelity_positions, snaptrade
from .adapters.base import read_text_lines, sha256_file
from .config import Config
from .store import Store


@dataclass
class IngestResult:
    path: str
    kind: str | None
    rows: int = 0
    skipped: str | None = None
    warnings: list[str] = field(default_factory=list)


@dataclass
class IngestSummary:
    results: list[IngestResult] = field(default_factory=list)
    rows: int = 0
    messages: list[str] = field(default_factory=list)

    def add(self, r: IngestResult) -> None:
        self.results.append(r)
        self.rows += r.rows
        if r.skipped:
            self.messages.append(f"{r.path}: {r.skipped}")
        self.messages.extend(f"{r.path}: {w}" for w in r.warnings)


def classify_file(path: Path) -> str | None:
    if path.suffix.lower() != ".csv":
        return None
    lines = read_text_lines(path)[:12]
    if fidelity_positions.detect(lines):
        return "fidelity_positions"
    if fidelity_history.detect(lines):
        return "fidelity_history"
    return None


def ingest_file(store: Store, path: Path, *, account: str | None = None, move_to: Path | None = None) -> IngestResult:
    sha = sha256_file(path)
    if store.file_seen(sha):
        return IngestResult(str(path), None, skipped="already ingested (same content)")
    kind = classify_file(path)
    if kind is None:
        return IngestResult(str(path), None, skipped="unrecognized file; expected a Fidelity positions or history export")
    warnings: list[str] = []
    if kind == "fidelity_positions":
        snap = fidelity_positions.parse(path)
        rows = store.write_snapshot(snap)
        warnings = list(snap.warnings)
    else:
        try:
            rows = store.write_transactions(fidelity_history.parse(path, account=account))
        except fidelity_history.AccountUnknown as e:
            return IngestResult(str(path), kind, skipped=str(e))
    store.record_file(sha, str(path), kind, rows)
    if move_to is not None:
        move_to.mkdir(parents=True, exist_ok=True)
        shutil.move(str(path), str(move_to / path.name))
    return IngestResult(str(path), kind, rows=rows, warnings=warnings)


def ingest_inbox(store: Store, cfg: Config) -> IngestSummary:
    summary = IngestSummary()
    if not cfg.inbox.is_dir():
        summary.messages.append(f"inbox {cfg.inbox} does not exist")
        return summary
    for path in sorted(p for p in cfg.inbox.iterdir() if p.is_file()):
        summary.add(ingest_file(store, path, move_to=cfg.processed_dir))
    return summary


def ingest_snaptrade(store: Store, cfg: Config) -> IngestSummary:
    summary = IngestSummary()
    if not cfg.snaptrade_enabled:
        summary.messages.append("snaptrade: disabled in config")
        return summary
    if cfg.snaptrade_dir is None or not cfg.snaptrade_dir.is_dir():
        summary.messages.append(f"snaptrade: directory missing ({cfg.snaptrade_dir}); skipped")
        return summary
    for path in snaptrade.find_snapshot_files(cfg.snaptrade_dir):
        sha = sha256_file(path)
        if store.file_seen(sha):
            continue
        snap = snaptrade.parse_positions(path)
        rows = store.write_snapshot(snap)
        store.record_file(sha, str(path), "snaptrade_positions", rows)
        summary.add(IngestResult(str(path), "snaptrade_positions", rows=rows, warnings=list(snap.warnings)))
    activity = cfg.snaptrade_dir / "live-activity.json"
    if activity.is_file():
        sha = sha256_file(activity)
        if not store.file_seen(sha):
            rows = store.write_transactions(snaptrade.parse_activity(activity))
            store.record_file(sha, str(activity), "snaptrade_activity", rows)
            summary.add(IngestResult(str(activity), "snaptrade_activity", rows=rows))
    return summary
```

- [ ] **Step 6: Run to verify pass**

Run: `.venv/Scripts/python -m pytest tests/test_config.py tests/test_ingest.py -v`
Expected: 11 passed

- [ ] **Step 7: Commit**

```bash
git add src/financial_data_collector/config.py src/financial_data_collector/ingest.py tests/test_config.py tests/test_ingest.py
git commit -m "Add config loading and inbox/SnapTrade ingest"
```

---
### Task 9: HTTP helper, universe and price collector

**Files:**
- Create: `src/financial_data_collector/http.py`, `src/financial_data_collector/universe.py`, `src/financial_data_collector/collectors/__init__.py`, `src/financial_data_collector/collectors/prices.py`, `tests/test_http.py`, `tests/test_universe.py`, `tests/test_prices.py`

**Interfaces:**
- Consumes: `Store` (Task 4), `Config` (Task 8), `symbols`, `PriceBar`.
- Produces (http): `Fetch = Callable[[str, dict[str, str] | None], bytes]`; `HttpError(Exception)` with `.status: int`; `fetch(url, headers=None, *, timeout=30, retries=3, sleep=time.sleep) -> bytes`.
- Produces (universe): `read_watchlist(path: Path) -> list[str]`; `build_universe(store, watchlist_path: Path) -> list[str]` (canonical symbols, sorted; money-market and crypto excluded; watchlist symbols upserted into `securities`).
- Produces (prices): `PriceResult(symbol, source, rows, message)`; `next_start(last_date: str | None, today: date, lookback_years: int) -> str`; `tiingo_bars(symbol, start, token, fetch) -> list[PriceBar]`; `yfinance_bars(symbol, start) -> list[PriceBar]`; `collect_prices(store, universe, cfg, *, fetch, today, yf=yfinance_bars, sleep=time.sleep) -> list[PriceResult]`.

- [ ] **Step 1: Write the failing http tests**

`tests/test_http.py`:
```python
import pytest

from financial_data_collector import http


class _Resp:
    def __init__(self, status, content=b"ok"):
        self.status_code = status
        self.content = content


def _get_factory(statuses):
    calls = []

    def fake_get(url, headers=None, timeout=None):
        calls.append((url, headers, timeout))
        return _Resp(statuses.pop(0))

    fake_get.calls = calls
    return fake_get


def test_retries_then_succeeds(monkeypatch):
    fake = _get_factory([429, 503, 200])
    monkeypatch.setattr(http.requests, "get", fake)
    slept = []
    out = http.fetch("https://x/y", {"User-Agent": "t"}, sleep=slept.append)
    assert out == b"ok"
    assert len(fake.calls) == 3 and slept == [1, 2]
    assert fake.calls[0][1] == {"User-Agent": "t"} and fake.calls[0][2] == 30


def test_404_is_final(monkeypatch):
    fake = _get_factory([404, 200])
    monkeypatch.setattr(http.requests, "get", fake)
    with pytest.raises(http.HttpError) as e:
        http.fetch("https://x/y", sleep=lambda s: None)
    assert e.value.status == 404 and len(fake.calls) == 1


def test_gives_up_after_retries(monkeypatch):
    fake = _get_factory([500, 500, 500])
    monkeypatch.setattr(http.requests, "get", fake)
    with pytest.raises(http.HttpError) as e:
        http.fetch("https://x/y", sleep=lambda s: None)
    assert e.value.status == 500 and len(fake.calls) == 3
```

- [ ] **Step 2: Implement http.py**

`src/financial_data_collector/http.py`:
```python
"""One GET with retry/backoff. Collectors take this as an injected `fetch`."""
from __future__ import annotations

import time
from typing import Callable

import requests

Fetch = Callable[[str, dict[str, str] | None], bytes]
_RETRY_STATUSES = {403, 429}


class HttpError(Exception):
    def __init__(self, status: int, url: str):
        super().__init__(f"HTTP {status} for {url}")
        self.status = status
        self.url = url


def fetch(
    url: str,
    headers: dict[str, str] | None = None,
    *,
    timeout: int = 30,
    retries: int = 3,
    sleep: Callable[[float], None] = time.sleep,
) -> bytes:
    """GET url. Retries 403/429/5xx with 1s/2s/4s backoff; 404 and other 4xx are final."""
    last: HttpError | None = None
    for attempt in range(retries):
        resp = requests.get(url, headers=headers, timeout=timeout)
        status = resp.status_code
        if status == 200:
            return resp.content
        err = HttpError(status, url)
        if status in _RETRY_STATUSES or status >= 500:
            last = err
            if attempt < retries - 1:
                sleep(2 ** attempt)
            continue
        raise err
    assert last is not None
    raise last
```

- [ ] **Step 3: Write the failing universe tests**

`tests/test_universe.py`:
```python
from pathlib import Path

from financial_data_collector import universe as U
from financial_data_collector.store import Store


def test_read_watchlist(tmp_path: Path):
    p = tmp_path / "watchlist.txt"
    p.write_text("# comment\n brk/b \n\nko\nKO\n")
    assert U.read_watchlist(p) == ["BRK.B", "KO"]
    assert U.read_watchlist(tmp_path / "missing.txt") == []


def test_build_universe(tmp_path: Path):
    s = Store.open(tmp_path / "w.db")
    s.upsert_security("AAPL", asset_type="stock", first_seen="2026-01-01")
    s.upsert_security("SPAXX", asset_type="money_market", first_seen="2026-01-01")
    s.upsert_security("FDRXX", first_seen="2026-01-01")             # money market by symbol list
    s.upsert_security("BTC", asset_type="crypto", first_seen="2026-01-01")
    wl = tmp_path / "watchlist.txt"
    wl.write_text("VTI\n")
    assert U.build_universe(s, wl) == ["AAPL", "VTI"]
    assert s.query("SELECT COUNT(*) FROM securities WHERE symbol='VTI'")[0][0] == 1
    s.close()
```

- [ ] **Step 4: Implement universe.py**

`src/financial_data_collector/universe.py`:
```python
"""Which symbols the collectors work on: everything ever held, plus the watchlist."""
from __future__ import annotations

from pathlib import Path

from .store import Store, today
from .symbols import canonical, is_money_market

_SKIP_TYPES = {"money_market", "crypto"}


def read_watchlist(path: Path) -> list[str]:
    if not path.is_file():
        return []
    out: list[str] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        s = line.split("#", 1)[0].strip()
        if s:
            sym = canonical(s)
            if sym not in out:
                out.append(sym)
    return out


def build_universe(store: Store, watchlist_path: Path) -> list[str]:
    for sym in read_watchlist(watchlist_path):
        store.upsert_security(sym, first_seen=today())
    out: list[str] = []
    for row in store.securities():
        sym = row["symbol"]
        if row["asset_type"] in _SKIP_TYPES or is_money_market(sym, row["description"]):
            continue
        out.append(sym)
    return sorted(out)
```

- [ ] **Step 5: Write the failing price tests**

`tests/test_prices.py`:
```python
import json
from datetime import date
from pathlib import Path

import pytest

from financial_data_collector import config as C
from financial_data_collector.collectors import prices as P
from financial_data_collector.http import HttpError
from financial_data_collector.models import PriceBar
from financial_data_collector.store import Store

TIINGO_AAPL = [
    {"date": "2026-01-02T00:00:00.000Z", "close": 100.0, "adjClose": 99.5, "divCash": 0.0, "splitFactor": 1.0},
    {"date": "2026-01-05T00:00:00.000Z", "close": 102.0, "adjClose": 101.5, "divCash": 0.25, "splitFactor": 1.0},
    {"date": "2026-01-06T00:00:00.000Z", "close": 51.0, "adjClose": 51.0, "divCash": 0.0, "splitFactor": 2.0},
]


def test_next_start():
    assert P.next_start(None, date(2026, 1, 15), 5) == "2021-01-15"
    assert P.next_start("2026-01-10", date(2026, 1, 15), 5) == "2026-01-11"


def test_tiingo_bars_parses_and_sends_token():
    seen = {}

    def fetch(url, headers):
        seen["url"], seen["headers"] = url, headers
        return json.dumps(TIINGO_AAPL).encode()

    bars = P.tiingo_bars("BRK.B", "2026-01-01", "tok", fetch)
    assert "tiingo/daily/BRK-B/prices" in seen["url"] and "startDate=2026-01-01" in seen["url"]
    assert seen["headers"]["Authorization"] == "Token tok"
    assert bars[0] == PriceBar("2026-01-02", 100.0, 99.5, 0.0, 1.0)
    assert bars[2].split_factor == 2.0 and bars[1].dividend == 0.25


@pytest.fixture
def cfg(tmp_path: Path):
    C.init_project(tmp_path)
    (tmp_path / ".env").write_text("TIINGO_API_TOKEN=tok\n")
    return C.load_config(tmp_path)


def test_collect_prices_fallback_and_no_data(cfg, tmp_path: Path):
    store = Store.open(cfg.db_path)
    for s in ("AAPL", "KO", "VTI"):
        store.upsert_security(s, first_seen="2026-01-01")
    store.write_prices("AAPL", [PriceBar("2026-01-01", 99.0, 99.0)], "tiingo")

    def fetch(url, headers):
        if "/AAPL/" in url:
            assert "startDate=2026-01-02" in url          # incremental from last stored + 1
            return json.dumps(TIINGO_AAPL).encode()
        raise HttpError(404, url)

    def yf(symbol, start):
        return [PriceBar("2026-01-02", 60.0, 60.0)] if symbol == "KO" else []

    slept = []
    results = P.collect_prices(store, ["AAPL", "KO", "VTI"], cfg, fetch=fetch, today=date(2026, 1, 15), yf=yf, sleep=slept.append)
    by = {r.symbol: r for r in results}
    assert (by["AAPL"].source, by["AAPL"].rows) == ("tiingo", 3)
    assert (by["KO"].source, by["KO"].rows) == ("yfinance", 1)
    assert by["VTI"].rows == 0 and by["VTI"].source is None and "no bars" in by["VTI"].message
    assert store.query("SELECT COUNT(*) FROM prices WHERE symbol='AAPL'")[0][0] == 4
    assert store.query("SELECT price_source FROM securities WHERE symbol='KO'")[0][0] == "yfinance"
    assert slept and all(s == 0.5 for s in slept)
    store.close()


def test_collect_prices_skips_up_to_date(cfg):
    store = Store.open(cfg.db_path)
    store.upsert_security("AAPL", first_seen="2026-01-01")
    store.write_prices("AAPL", [PriceBar("2026-01-15", 99.0, 99.0)], "tiingo")
    calls = []
    results = P.collect_prices(store, ["AAPL"], cfg, fetch=lambda u, h: calls.append(u), today=date(2026, 1, 15), yf=lambda s, d: [], sleep=lambda s: None)
    assert calls == [] and results[0].message == "up to date"
    store.close()


def test_collect_prices_without_token_uses_yfinance_only(cfg, tmp_path: Path):
    cfg.tiingo_token = None
    store = Store.open(cfg.db_path)
    store.upsert_security("AAPL", first_seen="2026-01-01")
    calls = []
    results = P.collect_prices(store, ["AAPL"], cfg, fetch=lambda u, h: calls.append(u), today=date(2026, 1, 15), yf=lambda s, d: [PriceBar("2026-01-02", 1.0, 1.0)], sleep=lambda s: None)
    assert calls == [] and results[0].source == "yfinance"
    store.close()
```

- [ ] **Step 6: Implement prices.py**

`src/financial_data_collector/collectors/__init__.py`: empty.

`src/financial_data_collector/collectors/prices.py`:
```python
"""Daily bars: Tiingo when a token is configured, yfinance otherwise (or as fallback)."""
from __future__ import annotations

import json
import math
import time
from dataclasses import dataclass
from datetime import date, timedelta
from typing import Callable

from ..config import Config
from ..http import Fetch, HttpError
from ..models import PriceBar
from ..store import Store
from ..symbols import to_tiingo, to_yfinance

TIINGO_URL = "https://api.tiingo.com/tiingo/daily/{sym}/prices?startDate={start}&columns=date,close,adjClose,divCash,splitFactor"
TIINGO_PAUSE_SECONDS = 0.5


@dataclass
class PriceResult:
    symbol: str
    source: str | None
    rows: int
    message: str = ""


def next_start(last_date: str | None, today: date, lookback_years: int) -> str:
    if last_date:
        return (date.fromisoformat(last_date) + timedelta(days=1)).isoformat()
    try:
        return today.replace(year=today.year - lookback_years).isoformat()
    except ValueError:  # Feb 29
        return (today - timedelta(days=365 * lookback_years)).isoformat()


def tiingo_bars(symbol: str, start: str, token: str, fetch: Fetch) -> list[PriceBar]:
    url = TIINGO_URL.format(sym=to_tiingo(symbol), start=start)
    raw = fetch(url, {"Authorization": f"Token {token}", "Accept": "application/json"})
    data = json.loads(raw or b"[]")
    if not isinstance(data, list):
        return []
    bars: list[PriceBar] = []
    for o in data:
        if o.get("close") is None or o.get("adjClose") is None:
            continue
        bars.append(PriceBar(
            date=str(o["date"])[:10],
            close=float(o["close"]),
            adj_close=float(o["adjClose"]),
            dividend=float(o.get("divCash") or 0.0),
            split_factor=float(o.get("splitFactor") or 1.0),
        ))
    return bars


def yfinance_bars(symbol: str, start: str) -> list[PriceBar]:
    import yfinance as yf  # imported lazily: slow, and tests never need it

    hist = yf.Ticker(to_yfinance(symbol)).history(start=start, auto_adjust=False, actions=True)
    bars: list[PriceBar] = []
    for idx, row in hist.iterrows():
        close, adj = row.get("Close"), row.get("Adj Close", row.get("Close"))
        if close is None or (isinstance(close, float) and math.isnan(close)):
            continue
        div = row.get("Dividends", 0.0) or 0.0
        split = row.get("Stock Splits", 0.0) or 0.0
        bars.append(PriceBar(
            date=idx.strftime("%Y-%m-%d"),
            close=float(close),
            adj_close=float(adj if adj == adj else close),
            dividend=float(div if div == div else 0.0),
            split_factor=float(split) if split and split == split else 1.0,
        ))
    return bars


def collect_prices(
    store: Store,
    universe: list[str],
    cfg: Config,
    *,
    fetch: Fetch,
    today: date,
    yf: Callable[[str, str], list[PriceBar]] = yfinance_bars,
    sleep: Callable[[float], None] = time.sleep,
) -> list[PriceResult]:
    results: list[PriceResult] = []
    for symbol in universe:
        start = next_start(store.last_price_date(symbol), today, cfg.lookback_years)
        if start > today.isoformat():
            results.append(PriceResult(symbol, None, 0, "up to date"))
            continue
        bars: list[PriceBar] = []
        source: str | None = None
        notes: list[str] = []
        if cfg.tiingo_token:
            try:
                bars = tiingo_bars(symbol, start, cfg.tiingo_token, fetch)
                source = "tiingo" if bars else None
            except (HttpError, ValueError, json.JSONDecodeError) as e:
                notes.append(f"tiingo: {e}")
            sleep(TIINGO_PAUSE_SECONDS)
        if not bars:
            try:
                bars = yf(symbol, start)
                source = "yfinance" if bars else None
            except Exception as e:  # yfinance raises many types; a symbol miss must not stop the run
                notes.append(f"yfinance: {type(e).__name__}: {e}")
        if bars and source:
            n = store.write_prices(symbol, bars, source)
            results.append(PriceResult(symbol, source, n, "; ".join(notes)))
        else:
            notes.append("no bars from any provider")
            results.append(PriceResult(symbol, None, 0, "; ".join(notes)))
    return results
```

- [ ] **Step 7: Run to verify pass**

Run: `.venv/Scripts/python -m pytest tests/test_http.py tests/test_universe.py tests/test_prices.py -v`
Expected: 10 passed

- [ ] **Step 8: Commit**

```bash
git add src/financial_data_collector/http.py src/financial_data_collector/universe.py src/financial_data_collector/collectors tests/test_http.py tests/test_universe.py tests/test_prices.py
git commit -m "Add HTTP helper, universe and price collector"
```

---

### Task 10: SEC CIK map and company-facts collector

**Files:**
- Create: `src/financial_data_collector/collectors/sec_cik.py`, `src/financial_data_collector/collectors/sec_facts.py`, `tests/fixtures/sec/company_tickers.json`, `tests/fixtures/sec/companyfacts_SAMPLE.json`, `tests/test_sec.py`

**Interfaces:**
- Consumes: `Store`, `Config`, `ConfigError`, `Fetch`, `HttpError`, `Fact`, `to_sec`; and from Task 11 `statements.rebuild(facts: list[Fact], rules: list[ConceptRule]) -> list[LineItem]` (injected as `rebuild=`; the default imports it lazily, so this task's tests pass a stub).
- Produces (sec_cik): `CIK_URL`; `load_cik_map(cache_path, fetch, user_agent, now: datetime, max_age_days=7) -> dict[str, tuple[str, str]]` keyed by SEC ticker form → `(cik10, name)`; `lookup(cik_map, symbol) -> tuple[str, str] | None`.
- Produces (sec_facts): `FACTS_URL`; `parse_company_facts(data: dict) -> list[Fact]`; `has_operating_facts(facts) -> bool`; `SecResult(symbol, cik, facts, line_items, message)`; `collect_sec(store, cfg, *, fetch, now: datetime, rebuild=None, sleep=time.sleep) -> list[SecResult]`.

- [ ] **Step 1: Write the fixtures**

`tests/fixtures/sec/company_tickers.json`:
```json
{
  "0": {"cik_str": 1, "ticker": "AAPL", "title": "Sample Corp"},
  "1": {"cik_str": 2, "ticker": "KO", "title": "Sample Beverage Co"},
  "2": {"cik_str": 3, "ticker": "BRK-B", "title": "Sample Holding B"}
}
```

`tests/fixtures/sec/companyfacts_SAMPLE.json` — a synthetic company with a calendar fiscal year. Filings: `k24` (FY2024 10-K, filed 2025-02-01), `q125`/`q225`/`q325` (2025 10-Qs), `k25` (FY2025 10-K, filed 2026-02-01, which restates FY2024 revenue and year-end assets). Cash flow is reported year-to-date only, the common case.
```json
{
  "cik": 1,
  "entityName": "Sample Corp",
  "facts": {
    "dei": {
      "EntityCommonStockSharesOutstanding": {"units": {"shares": [
        {"end": "2026-01-20", "val": 1000, "accn": "k25", "fy": 2025, "fp": "FY", "form": "10-K", "filed": "2026-02-01", "frame": "CY2025Q4I"}
      ]}}
    },
    "us-gaap": {
      "Revenues": {"units": {"USD": [
        {"start": "2023-01-01", "end": "2023-12-31", "val": 900, "accn": "k24", "fy": 2024, "fp": "FY", "form": "10-K", "filed": "2025-02-01"},
        {"start": "2024-01-01", "end": "2024-12-31", "val": 1000, "accn": "k24", "fy": 2024, "fp": "FY", "form": "10-K", "filed": "2025-02-01", "frame": "CY2024"},
        {"start": "2024-10-01", "end": "2024-12-31", "val": 280, "accn": "k24", "fy": 2024, "fp": "FY", "form": "10-K", "filed": "2025-02-01", "frame": "CY2024Q4"},
        {"start": "2024-01-01", "end": "2024-03-31", "val": 230, "accn": "q125", "fy": 2025, "fp": "Q1", "form": "10-Q", "filed": "2025-05-01"},
        {"start": "2025-01-01", "end": "2025-03-31", "val": 260, "accn": "q125", "fy": 2025, "fp": "Q1", "form": "10-Q", "filed": "2025-05-01", "frame": "CY2025Q1"},
        {"start": "2024-04-01", "end": "2024-06-30", "val": 240, "accn": "q225", "fy": 2025, "fp": "Q2", "form": "10-Q", "filed": "2025-08-01"},
        {"start": "2024-01-01", "end": "2024-06-30", "val": 470, "accn": "q225", "fy": 2025, "fp": "Q2", "form": "10-Q", "filed": "2025-08-01"},
        {"start": "2025-04-01", "end": "2025-06-30", "val": 270, "accn": "q225", "fy": 2025, "fp": "Q2", "form": "10-Q", "filed": "2025-08-01", "frame": "CY2025Q2"},
        {"start": "2025-01-01", "end": "2025-06-30", "val": 530, "accn": "q225", "fy": 2025, "fp": "Q2", "form": "10-Q", "filed": "2025-08-01"},
        {"start": "2024-07-01", "end": "2024-09-30", "val": 250, "accn": "q325", "fy": 2025, "fp": "Q3", "form": "10-Q", "filed": "2025-11-01"},
        {"start": "2024-01-01", "end": "2024-09-30", "val": 720, "accn": "q325", "fy": 2025, "fp": "Q3", "form": "10-Q", "filed": "2025-11-01"},
        {"start": "2025-07-01", "end": "2025-09-30", "val": 280, "accn": "q325", "fy": 2025, "fp": "Q3", "form": "10-Q", "filed": "2025-11-01", "frame": "CY2025Q3"},
        {"start": "2025-01-01", "end": "2025-09-30", "val": 810, "accn": "q325", "fy": 2025, "fp": "Q3", "form": "10-Q", "filed": "2025-11-01"},
        {"start": "2024-01-01", "end": "2024-12-31", "val": 1010, "accn": "k25", "fy": 2025, "fp": "FY", "form": "10-K", "filed": "2026-02-01"},
        {"start": "2025-01-01", "end": "2025-12-31", "val": 1100, "accn": "k25", "fy": 2025, "fp": "FY", "form": "10-K", "filed": "2026-02-01", "frame": "CY2025"},
        {"start": "2025-10-01", "end": "2025-12-31", "val": 290, "accn": "k25", "fy": 2025, "fp": "FY", "form": "10-K", "filed": "2026-02-01", "frame": "CY2025Q4"}
      ]}},
      "NetCashProvidedByUsedInOperatingActivities": {"units": {"USD": [
        {"start": "2024-01-01", "end": "2024-12-31", "val": 200, "accn": "k24", "fy": 2024, "fp": "FY", "form": "10-K", "filed": "2025-02-01"},
        {"start": "2025-01-01", "end": "2025-03-31", "val": 50, "accn": "q125", "fy": 2025, "fp": "Q1", "form": "10-Q", "filed": "2025-05-01"},
        {"start": "2025-01-01", "end": "2025-06-30", "val": 110, "accn": "q225", "fy": 2025, "fp": "Q2", "form": "10-Q", "filed": "2025-08-01"},
        {"start": "2025-01-01", "end": "2025-09-30", "val": 180, "accn": "q325", "fy": 2025, "fp": "Q3", "form": "10-Q", "filed": "2025-11-01"},
        {"start": "2025-01-01", "end": "2025-12-31", "val": 250, "accn": "k25", "fy": 2025, "fp": "FY", "form": "10-K", "filed": "2026-02-01"}
      ]}},
      "PaymentsToAcquirePropertyPlantAndEquipment": {"units": {"USD": [
        {"start": "2025-01-01", "end": "2025-12-31", "val": 40, "accn": "k25", "fy": 2025, "fp": "FY", "form": "10-K", "filed": "2026-02-01"}
      ]}},
      "Assets": {"units": {"USD": [
        {"end": "2024-12-31", "val": 5000, "accn": "k24", "fy": 2024, "fp": "FY", "form": "10-K", "filed": "2025-02-01"},
        {"end": "2025-03-31", "val": 5100, "accn": "q125", "fy": 2025, "fp": "Q1", "form": "10-Q", "filed": "2025-05-01"},
        {"end": "2025-06-30", "val": 5200, "accn": "q225", "fy": 2025, "fp": "Q2", "form": "10-Q", "filed": "2025-08-01"},
        {"end": "2025-09-30", "val": 5300, "accn": "q325", "fy": 2025, "fp": "Q3", "form": "10-Q", "filed": "2025-11-01"},
        {"end": "2024-12-31", "val": 5050, "accn": "k25", "fy": 2025, "fp": "FY", "form": "10-K", "filed": "2026-02-01"},
        {"end": "2025-12-31", "val": 5500, "accn": "k25", "fy": 2025, "fp": "FY", "form": "10-K", "filed": "2026-02-01"}
      ]}},
      "CommonStockSharesOutstanding": {"units": {"shares": [
        {"end": "2025-12-31", "val": 990, "accn": "k25", "fy": 2025, "fp": "FY", "form": "10-K", "filed": "2026-02-01"}
      ]}},
      "EarningsPerShareDiluted": {"units": {"USD/shares": [
        {"start": "2025-01-01", "end": "2025-03-31", "val": 0.5, "accn": "q125", "fy": 2025, "fp": "Q1", "form": "10-Q", "filed": "2025-05-01"},
        {"start": "2025-04-01", "end": "2025-06-30", "val": 0.5, "accn": "q225", "fy": 2025, "fp": "Q2", "form": "10-Q", "filed": "2025-08-01"},
        {"start": "2025-07-01", "end": "2025-09-30", "val": 0.5, "accn": "q325", "fy": 2025, "fp": "Q3", "form": "10-Q", "filed": "2025-11-01"},
        {"start": "2025-01-01", "end": "2025-12-31", "val": 2.0, "accn": "k25", "fy": 2025, "fp": "FY", "form": "10-K", "filed": "2026-02-01"}
      ]}},
      "OtherAssetsNoncurrent": {"units": {"USD": [
        {"end": "2025-12-31", "val": 7, "accn": "k25", "fy": 2025, "fp": "FY", "form": "10-K", "filed": "2026-02-01"}
      ]}}
    }
  }
}
```

- [ ] **Step 2: Write the failing tests**

`tests/test_sec.py`:
```python
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from financial_data_collector import config as C
from financial_data_collector.collectors import sec_cik, sec_facts
from financial_data_collector.http import HttpError
from financial_data_collector.store import Store

NOW = datetime(2026, 3, 1, tzinfo=timezone.utc)


def _fetcher(fixtures: Path):
    calls = []

    def fetch(url, headers):
        calls.append((url, headers))
        if url == sec_cik.CIK_URL:
            return (fixtures / "sec" / "company_tickers.json").read_bytes()
        if url == sec_facts.FACTS_URL.format(cik="0000000001"):
            return (fixtures / "sec" / "companyfacts_SAMPLE.json").read_bytes()
        raise HttpError(404, url)

    fetch.calls = calls
    return fetch


def test_load_cik_map_caches(fixtures: Path, tmp_path: Path):
    fetch = _fetcher(fixtures)
    cache = tmp_path / "cache" / "company_tickers.json"
    m = sec_cik.load_cik_map(cache, fetch, "Sample Person s@example.com", NOW)
    assert m["AAPL"] == ("0000000001", "Sample Corp") and m["BRK-B"] == ("0000000003", "Sample Holding B")
    assert fetch.calls[0][1]["User-Agent"] == "Sample Person s@example.com"
    assert cache.exists()
    sec_cik.load_cik_map(cache, fetch, "ua", NOW + timedelta(days=6))
    assert len(fetch.calls) == 1                                  # fresh cache, no refetch
    sec_cik.load_cik_map(cache, fetch, "ua", NOW + timedelta(days=8))
    assert len(fetch.calls) == 2                                  # stale cache, refetched
    assert sec_cik.lookup(m, "BRK.B") == ("0000000003", "Sample Holding B")
    assert sec_cik.lookup(m, "VTI") is None


def test_parse_company_facts(fixtures: Path):
    data = json.loads((fixtures / "sec" / "companyfacts_SAMPLE.json").read_text())
    facts = sec_facts.parse_company_facts(data)
    assert len(facts) == 34
    assets = [f for f in facts if f.concept == "Assets"]
    assert all(f.period_start == "" and f.taxonomy == "us-gaap" and f.unit == "USD" for f in assets)
    rev = [f for f in facts if f.concept == "Revenues" and f.frame == "CY2025"][0]
    assert (rev.value, rev.fy, rev.fp, rev.form, rev.filed, rev.accn) == (1100.0, 2025, "FY", "10-K", "2026-02-01", "k25")
    dei = [f for f in facts if f.taxonomy == "dei"]
    assert len(dei) == 1 and dei[0].unit == "shares"
    assert sec_facts.has_operating_facts(facts)
    assert not sec_facts.has_operating_facts(assets)


@pytest.fixture
def project(tmp_path: Path):
    C.init_project(tmp_path)
    (tmp_path / ".env").write_text("SEC_USER_AGENT=Sample Person s@example.com\n")
    cfg = C.load_config(tmp_path)
    store = Store.open(cfg.db_path)
    for sym, kind in (("AAPL", None), ("VTI", "etf"), ("SPAXX", "money_market"), ("ZZZZ", None)):
        store.upsert_security(sym, asset_type=kind, first_seen="2026-01-01")
    yield cfg, store
    store.close()


def test_collect_sec_end_to_end_with_stub_rebuild(project, fixtures: Path):
    cfg, store = project
    fetch = _fetcher(fixtures)
    rebuilt = []

    def rebuild(facts, rules):
        rebuilt.append(len(facts))
        return []

    results = sec_facts.collect_sec(store, cfg, fetch=fetch, now=NOW, rebuild=rebuild, sleep=lambda s: None)
    by = {r.symbol: r for r in results}
    assert by["AAPL"].cik == "0000000001" and by["AAPL"].facts == 34
    assert rebuilt == [34]
    row = store.query("SELECT cik, sec_name, asset_type, last_sec_fetch FROM securities WHERE symbol='AAPL'")[0]
    assert tuple(row)[:3] == ("0000000001", "Sample Corp", "stock") and row[3].startswith("2026-03-01")
    assert store.query("SELECT COUNT(*) FROM sec_facts")[0][0] == 34
    assert store.query("SELECT cik FROM securities WHERE symbol='ZZZZ'")[0][0] is None
    assert "no CIK" in by["ZZZZ"].message
    assert "VTI" not in by and "SPAXX" not in by                  # funds never hit EDGAR
    facts_calls = [u for u, _ in fetch.calls if "companyfacts" in u]
    assert len(facts_calls) == 1
    again = sec_facts.collect_sec(store, cfg, fetch=fetch, now=NOW + timedelta(hours=1), rebuild=rebuild, sleep=lambda s: None)
    assert len([u for u, _ in fetch.calls if "companyfacts" in u]) == 1      # within max_age: no refetch
    assert {r.symbol: r.message for r in again}["AAPL"] == "fresh"


def test_collect_sec_requires_user_agent(project, fixtures: Path):
    cfg, store = project
    cfg.sec_user_agent = None
    with pytest.raises(C.ConfigError):
        sec_facts.collect_sec(store, cfg, fetch=_fetcher(fixtures), now=NOW, rebuild=lambda f, r: [])


def test_collect_sec_flags_fund_with_cik_as_etf(project, fixtures: Path, tmp_path: Path):
    cfg, store = project

    def fetch(url, headers):
        if url == sec_cik.CIK_URL:
            return json.dumps({"0": {"cik_str": 9, "ticker": "AAPL", "title": "Sample Trust"}}).encode()
        return json.dumps({"cik": 9, "facts": {"dei": {"EntityCommonStockSharesOutstanding": {"units": {"shares": [
            {"end": "2025-12-31", "val": 1, "accn": "x", "fy": 2025, "fp": "FY", "form": "N-CSR", "filed": "2026-01-01"}]}}}}}).encode()

    results = sec_facts.collect_sec(store, cfg, fetch=fetch, now=NOW, rebuild=lambda f, r: [], sleep=lambda s: None)
    assert store.query("SELECT asset_type FROM securities WHERE symbol='AAPL'")[0][0] == "etf"
    assert "no operating" in {r.symbol: r.message for r in results}["AAPL"]
```

- [ ] **Step 3: Run to verify failure**

Run: `.venv/Scripts/python -m pytest tests/test_sec.py -v`
Expected: FAIL, `ImportError`

- [ ] **Step 4: Implement sec_cik.py**

`src/financial_data_collector/collectors/sec_cik.py`:
```python
"""Ticker -> CIK map from SEC's company_tickers.json, cached for a week."""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

from ..http import Fetch
from ..symbols import to_sec

CIK_URL = "https://www.sec.gov/files/company_tickers.json"


def _headers(user_agent: str) -> dict[str, str]:
    return {"User-Agent": user_agent, "Accept-Encoding": "gzip, deflate", "Accept": "application/json"}


def load_cik_map(
    cache_path: Path, fetch: Fetch, user_agent: str, now: datetime, max_age_days: int = 7
) -> dict[str, tuple[str, str]]:
    fresh = False
    if cache_path.is_file():
        age = now - datetime.fromtimestamp(cache_path.stat().st_mtime, timezone.utc)
        fresh = age <= timedelta(days=max_age_days)
    if fresh:
        raw = cache_path.read_bytes()
    else:
        raw = fetch(CIK_URL, _headers(user_agent))
        cache_path.parent.mkdir(parents=True, exist_ok=True)
        cache_path.write_bytes(raw)
    data = json.loads(raw)
    rows = data.values() if isinstance(data, dict) else data
    out: dict[str, tuple[str, str]] = {}
    for r in rows:
        ticker = str(r.get("ticker", "")).upper()
        if ticker:
            out[ticker] = (f"{int(r['cik_str']):010d}", str(r.get("title", "")))
    return out


def lookup(cik_map: dict[str, tuple[str, str]], symbol: str) -> tuple[str, str] | None:
    return cik_map.get(to_sec(symbol))
```

- [ ] **Step 5: Implement sec_facts.py**

`src/financial_data_collector/collectors/sec_facts.py`:
```python
"""SEC EDGAR company-facts: every XBRL fact a company ever filed, stored raw."""
from __future__ import annotations

import json
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Callable

from ..config import Config, ConfigError
from ..http import Fetch, HttpError
from ..models import ConceptRule, Fact, LineItem
from ..store import Store
from .sec_cik import load_cik_map, lookup

FACTS_URL = "https://data.sec.gov/api/xbrl/companyfacts/CIK{cik}.json"
PAUSE_SECONDS = 1.0
_FUND_TYPES = {"etf", "mutual_fund", "money_market", "crypto"}
Rebuild = Callable[[list[Fact], list[ConceptRule]], list[LineItem]]


@dataclass
class SecResult:
    symbol: str
    cik: str | None
    facts: int = 0
    line_items: int = 0
    message: str = ""


def _headers(user_agent: str) -> dict[str, str]:
    return {"User-Agent": user_agent, "Accept-Encoding": "gzip, deflate", "Accept": "application/json"}


def parse_company_facts(data: dict) -> list[Fact]:
    out: list[Fact] = []
    for taxonomy, concepts in (data.get("facts") or {}).items():
        for concept, body in concepts.items():
            for unit, observations in (body.get("units") or {}).items():
                for o in observations:
                    val = o.get("val")
                    end = o.get("end")
                    if val is None or not end:
                        continue
                    try:
                        value = float(val)
                    except (TypeError, ValueError):
                        continue
                    out.append(Fact(
                        taxonomy=taxonomy, concept=concept, unit=unit,
                        period_start=o.get("start") or "", period_end=end, value=value,
                        fy=int(o["fy"]) if o.get("fy") is not None else None,
                        fp=o.get("fp"), form=o.get("form") or "", filed=o.get("filed") or "",
                        accn=o.get("accn") or "", frame=o.get("frame"),
                    ))
    return out


def has_operating_facts(facts: list[Fact]) -> bool:
    return any(f.taxonomy == "us-gaap" and f.period_start for f in facts)


def _looks_like_mutual_fund(symbol: str) -> bool:
    return len(symbol) == 5 and symbol.endswith("X") and symbol.isalpha()


def _is_fresh(last: str | None, now: datetime, max_age_hours: int) -> bool:
    if not last:
        return False
    try:
        t = datetime.strptime(last, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    except ValueError:
        return False
    return now - t < timedelta(hours=max_age_hours)


def collect_sec(
    store: Store,
    cfg: Config,
    *,
    fetch: Fetch,
    now: datetime,
    rebuild: Rebuild | None = None,
    sleep: Callable[[float], None] = time.sleep,
) -> list[SecResult]:
    if not cfg.sec_user_agent:
        raise ConfigError("SEC_USER_AGENT is not set (put 'Your Name you@example.com' in .env); SEC blocks anonymous clients")
    if rebuild is None:
        from ..statements import rebuild as _rebuild
        rebuild = _rebuild
    headers = _headers(cfg.sec_user_agent)
    cik_map = load_cik_map(cfg.cache_dir / "company_tickers.json", fetch, cfg.sec_user_agent, now)
    rules = store.concept_rules()
    stamp = now.strftime("%Y-%m-%dT%H:%M:%SZ")
    results: list[SecResult] = []
    for row in store.securities():
        symbol, asset_type, cik = row["symbol"], row["asset_type"], row["cik"]
        if asset_type in _FUND_TYPES:
            continue
        if not cik:
            hit = lookup(cik_map, symbol)
            if hit is None:
                if asset_type == "unknown" and _looks_like_mutual_fund(symbol):
                    store.set_asset_type(symbol, "mutual_fund")
                results.append(SecResult(symbol, None, message="no CIK in SEC ticker map"))
                continue
            cik, name = hit
            store.set_security_cik(symbol, cik, name)
            if asset_type == "unknown":
                store.set_asset_type(symbol, "stock")
        if _is_fresh(row["last_sec_fetch"], now, cfg.sec_max_age_hours):
            results.append(SecResult(symbol, cik, message="fresh"))
            continue
        try:
            data = json.loads(fetch(FACTS_URL.format(cik=cik), headers))
        except (HttpError, ValueError, json.JSONDecodeError) as e:
            store.mark_sec_fetch(symbol, stamp)
            results.append(SecResult(symbol, cik, message=f"fetch failed: {e}"))
            sleep(PAUSE_SECONDS)
            continue
        facts = parse_company_facts(data)
        if not has_operating_facts(facts):
            store.set_asset_type(symbol, "etf")
            store.mark_sec_fetch(symbol, stamp)
            results.append(SecResult(symbol, cik, facts=len(facts), message="no operating facts; classified as etf"))
            sleep(PAUSE_SECONDS)
            continue
        store.write_sec_facts(cik, facts)
        items = rebuild(facts, rules)
        store.replace_line_items(cik, items)
        store.mark_sec_fetch(symbol, stamp)
        results.append(SecResult(symbol, cik, facts=len(facts), line_items=len(items)))
        sleep(PAUSE_SECONDS)
    return results
```

- [ ] **Step 6: Run to verify pass**

Run: `.venv/Scripts/python -m pytest tests/test_sec.py -v`
Expected: 5 passed

- [ ] **Step 7: Commit**

```bash
git add src/financial_data_collector/collectors/sec_cik.py src/financial_data_collector/collectors/sec_facts.py tests/fixtures/sec tests/test_sec.py
git commit -m "Add SEC CIK map and company-facts collector"
```

---
### Task 11: Statement shaping (facts → line items)

**Files:**
- Create: `src/financial_data_collector/statements.py`, `tests/test_statements.py`

**Interfaces:**
- Consumes: `Fact`, `ConceptRule`, `LineItem`; the fixture and `parse_company_facts` from Task 10; `migrate.SEEDS_DIR`.
- Produces: `rebuild(facts: list[Fact], rules: list[ConceptRule]) -> list[LineItem]`; `fiscal_year_of(period_end: str) -> int`; constants `ANNUAL_DAYS = (350, 380)`, `QUARTER_DAYS = (80, 100)`, `HALF_DAYS = (170, 190)`, `NINE_MONTH_DAYS = (260, 280)`.

Rules implemented (spec §5.4 plus year-to-date differencing):
- A fact is an **instant** when `period_start == ''`, else a **duration**. The rule's `kind` only decides the unit filter (`duration`/`instant` → `USD`, `per_share` → `USD/shares`, `shares` → `shares`) and whether derivation is allowed (`duration` only).
- Per line item and period, candidates are filtered by rule priority: the lowest-priority concept that has any fact for that period wins; within it, the latest `(filed, accn)` wins.
- Annual = duration 350–380 days. Quarter = 80–100. Half = 170–190 and nine-month = 260–280 are used only for differencing.
- Fiscal periods are grouped by their **start date**: a start is a fiscal start when an annual, half or nine-month fact starts there, when a quarter tagged `fp = Q1` starts there, or when it is the day after an annual end. Within a fiscal year, a quarter's number is its position: `round(days_since_start / 91.3)` clamped to 1–4. `fiscal_year` is `fiscal_year_of(annual end)`, or of `start + 364 days` when the year has no annual yet. `fiscal_year_of` returns the calendar year of the end date, minus one when the end falls in the first fifteen days of January (52/53-week years ending around New Year).
- Derived quarters (duration kind only, never when a reported quarter already ends on that date): Q2 = H1 − Q1; Q3 = 9M − (Q1 + Q2), else 9M − H1; Q4 = FY − (Q1 + Q2 + Q3), else FY − 9M. Derived rows carry `is_derived = True`, the YTD fact's concept, filing date and accession, and a start of the day after the previous quarter's end.
- Instants: annual rows at every annual end seen anywhere in the company's facts; quarter rows at every quarter, half, nine-month or annual end, numbered from the duration pass (4 at an annual end).
- Output never contains two rows with the same `(line_item, period_kind, period_end)`; on a clash the later-filed row is kept.

- [ ] **Step 1: Write the failing tests**

`tests/test_statements.py`:
```python
import csv
import json
from pathlib import Path

import pytest

from financial_data_collector import migrate, statements as S
from financial_data_collector.collectors.sec_facts import parse_company_facts
from financial_data_collector.models import ConceptRule


def load_rules() -> list[ConceptRule]:
    with open(migrate.SEEDS_DIR / "concept_map.csv", newline="", encoding="utf-8") as f:
        return [ConceptRule(r["line_item"], r["statement"], r["kind"], r["taxonomy"], r["concept"], int(r["priority"]))
                for r in csv.DictReader(f)]


@pytest.fixture(scope="module")
def items(fixtures_module: Path):
    facts = parse_company_facts(json.loads((fixtures_module / "sec" / "companyfacts_SAMPLE.json").read_text()))
    return S.rebuild(facts, load_rules())


@pytest.fixture(scope="module")
def fixtures_module() -> Path:
    return Path(__file__).parent / "fixtures"


def _get(items, line_item, kind, end):
    hits = [i for i in items if i.line_item == line_item and i.period_kind == kind and i.period_end == end]
    assert len(hits) <= 1, hits
    return hits[0] if hits else None


def test_fiscal_year_of():
    assert S.fiscal_year_of("2025-12-31") == 2025
    assert S.fiscal_year_of("2026-01-25") == 2026     # NVDA-style late-January year end
    assert S.fiscal_year_of("2026-01-03") == 2025     # 53-week year spilling into January
    assert S.fiscal_year_of("2025-12-28") == 2025


def test_primary_key_is_unique(items):
    keys = [(i.line_item, i.period_kind, i.period_end) for i in items]
    assert len(keys) == len(set(keys))


def test_annual_revenue_latest_filed_wins(items):
    r23 = _get(items, "revenue", "annual", "2023-12-31")
    r24 = _get(items, "revenue", "annual", "2024-12-31")
    r25 = _get(items, "revenue", "annual", "2025-12-31")
    assert (r23.value, r23.fiscal_year) == (900.0, 2023)
    assert (r24.value, r24.accn, r24.fiscal_year) == (1010.0, "k25", 2024)   # restated by the FY2025 10-K
    assert (r25.value, r25.concept, r25.is_derived) == (1100.0, "Revenues", False)


def test_reported_quarters_including_q4_from_10k(items):
    q = {e: _get(items, "revenue", "quarter", e) for e in ("2025-03-31", "2025-06-30", "2025-09-30", "2025-12-31")}
    assert [q[e].value for e in q] == [260.0, 270.0, 280.0, 290.0]
    assert [q[e].fiscal_quarter for e in q] == [1, 2, 3, 4]
    assert all(i.fiscal_year == 2025 and not i.is_derived for i in q.values())
    assert q["2025-12-31"].period_start == "2025-10-01"
    prior = _get(items, "revenue", "quarter", "2024-06-30")
    assert (prior.value, prior.fiscal_year, prior.fiscal_quarter) == (240.0, 2024, 2)


def test_ytd_differencing_for_cash_flow(items):
    q1 = _get(items, "ocf", "quarter", "2025-03-31")
    q2 = _get(items, "ocf", "quarter", "2025-06-30")
    q3 = _get(items, "ocf", "quarter", "2025-09-30")
    q4 = _get(items, "ocf", "quarter", "2025-12-31")
    assert (q1.value, q1.is_derived) == (50.0, False)
    assert (q2.value, q2.is_derived, q2.accn, q2.period_start) == (60.0, True, "q225", "2025-04-01")
    assert (q3.value, q3.is_derived, q3.accn, q3.period_start) == (70.0, True, "q325", "2025-07-01")
    assert (q4.value, q4.is_derived, q4.accn, q4.period_start, q4.fiscal_quarter) == (70.0, True, "k25", "2025-10-01", 4)
    assert _get(items, "ocf", "annual", "2025-12-31").value == 250.0


def test_no_derived_q4_without_siblings(items):
    assert _get(items, "ocf", "annual", "2024-12-31").value == 200.0
    assert _get(items, "ocf", "quarter", "2024-12-31") is None
    assert [i for i in items if i.line_item == "ocf" and i.fiscal_year == 2024 and i.period_kind == "quarter"] == []


def test_per_share_never_derived(items):
    assert _get(items, "eps_diluted", "annual", "2025-12-31").value == 2.0
    assert [_get(items, "eps_diluted", "quarter", e).value for e in ("2025-03-31", "2025-06-30", "2025-09-30")] == [0.5, 0.5, 0.5]
    assert _get(items, "eps_diluted", "quarter", "2025-12-31") is None


def test_instants_at_period_ends(items):
    assert _get(items, "total_assets", "annual", "2024-12-31").value == 5050.0     # restated
    assert _get(items, "total_assets", "annual", "2025-12-31").value == 5500.0
    q = {e: _get(items, "total_assets", "quarter", e) for e in ("2025-03-31", "2025-06-30", "2025-09-30", "2025-12-31")}
    assert [q[e].value for e in q] == [5100.0, 5200.0, 5300.0, 5500.0]
    assert [q[e].fiscal_quarter for e in q] == [1, 2, 3, 4]
    assert _get(items, "total_assets", "quarter", "2024-12-31").fiscal_quarter == 4
    assert all(i.period_start == "" for i in q.values())


def test_priority_and_unmapped_concepts(items):
    so = _get(items, "shares_outstanding", "annual", "2025-12-31")
    assert (so.value, so.concept) == (990.0, "CommonStockSharesOutstanding")     # us-gaap beats dei
    assert _get(items, "shares_outstanding", "quarter", "2026-01-20") is None       # not a period end
    assert not [i for i in items if "OtherAssets" in i.concept]
    assert _get(items, "capex", "annual", "2025-12-31").value == 40.0
    assert [i for i in items if i.line_item == "capex" and i.period_kind == "quarter"] == []


def test_in_progress_year_without_annual():
    rules = [ConceptRule("revenue", "income", "duration", "us-gaap", "Revenues", 1)]
    from financial_data_collector.models import Fact
    facts = [
        Fact("us-gaap", "Revenues", "USD", "2026-01-26", "2026-04-26", 10.0, 2027, "Q1", "10-Q", "2026-05-20", "a"),
        Fact("us-gaap", "Revenues", "USD", "2026-04-27", "2026-07-26", 11.0, 2027, "Q2", "10-Q", "2026-08-20", "b"),
    ]
    out = S.rebuild(facts, rules)
    assert [(i.fiscal_year, i.fiscal_quarter, i.value) for i in out] == [(2027, 1, 10.0), (2027, 2, 11.0)]
```

- [ ] **Step 2: Run to verify failure**

Run: `.venv/Scripts/python -m pytest tests/test_statements.py -v`
Expected: FAIL, `ImportError`

- [ ] **Step 3: Implement**

`src/financial_data_collector/statements.py`:
```python
"""Shape raw XBRL facts into financial_line_items rows.

See the design spec §5.4. Summary: pick per line item and period the best
concept (priority, then latest filed); classify durations as annual / quarter /
half / nine-month by length; group a fiscal year by its start date; derive
missing quarters from year-to-date facts; place instants at period ends.
"""
from __future__ import annotations

from datetime import date, timedelta

from .models import ConceptRule, Fact, LineItem

ANNUAL_DAYS = (350, 380)
QUARTER_DAYS = (80, 100)
HALF_DAYS = (170, 190)
NINE_MONTH_DAYS = (260, 280)
_UNITS = {"duration": {"USD"}, "instant": {"USD"}, "per_share": {"USD/shares"}, "shares": {"shares"}}
_DERIVABLE = {"duration"}


def fiscal_year_of(period_end: str) -> int:
    d = date.fromisoformat(period_end)
    return d.year - 1 if (d.month == 1 and d.day <= 15) else d.year


def _days(f: Fact) -> int:
    return (date.fromisoformat(f.period_end) - date.fromisoformat(f.period_start)).days


def _within(n: int, rng: tuple[int, int]) -> bool:
    return rng[0] <= n <= rng[1]


def _day_after(iso: str) -> str:
    return (date.fromisoformat(iso) + timedelta(days=1)).isoformat()


def _later(a: Fact, b: Fact) -> Fact:
    return b if (b.filed, b.accn) > (a.filed, a.accn) else a


def _pick(cands: list[tuple[int, Fact]]) -> Fact:
    best = min(p for p, _ in cands)
    facts = [f for p, f in cands if p == best]
    out = facts[0]
    for f in facts[1:]:
        out = _later(out, f)
    return out


def _chosen(by_concept: dict[tuple[str, str], list[Fact]], rules: list[ConceptRule]) -> dict[tuple[str, str], Fact]:
    """(period_start, period_end) -> winning fact for one line item."""
    units = _UNITS[rules[0].kind]
    cands: dict[tuple[str, str], list[tuple[int, Fact]]] = {}
    for r in rules:
        for f in by_concept.get((r.taxonomy, r.concept), []):
            if f.unit in units:
                cands.setdefault((f.period_start, f.period_end), []).append((r.priority, f))
    return {k: _pick(v) for k, v in cands.items()}


def _row(item: str, kind: str, f: Fact, fy: int, fq: int | None, *, value: float | None = None,
         start: str | None = None, derived: bool = False) -> LineItem:
    return LineItem(item, kind, f.period_start if start is None else start, f.period_end, fy, fq,
                    f.value if value is None else value, f.concept, f.filed, f.accn, derived)


def _put(rows: dict[tuple[str, str], LineItem], li: LineItem) -> None:
    key = (li.period_kind, li.period_end)
    old = rows.get(key)
    if old is None or (li.filed, li.accn) > (old.filed, old.accn):
        rows[key] = li


def _qnum(start: str, end: str) -> int:
    n = (date.fromisoformat(end) - date.fromisoformat(start)).days + 1
    return max(1, min(4, round(n / 91.3)))


def _duration_rows(item: str, kind: str, chosen: dict[tuple[str, str], Fact],
                   quarter_number: dict[str, int], quarter_fy: dict[str, int]) -> list[LineItem]:
    annuals, quarters, halves, nines = {}, {}, {}, {}
    for (s, e), f in chosen.items():
        n = _days(f)
        if _within(n, ANNUAL_DAYS):
            annuals[(s, e)] = f
        elif _within(n, QUARTER_DAYS):
            quarters[(s, e)] = f
        elif _within(n, HALF_DAYS):
            halves[(s, e)] = f
        elif _within(n, NINE_MONTH_DAYS):
            nines[(s, e)] = f
    rows: dict[tuple[str, str], LineItem] = {}
    for (s, e), f in annuals.items():
        _put(rows, _row(item, "annual", f, fiscal_year_of(e), None))
    starts = {s for s, _ in list(annuals) + list(halves) + list(nines)}
    starts |= {s for (s, _), f in quarters.items() if f.fp == "Q1"}
    starts |= {_day_after(e) for _, e in annuals}
    for S in sorted(starts):
        fy_fact = next((f for (s, e), f in annuals.items() if s == S), None)
        fy_end = fy_fact.period_end if fy_fact else (date.fromisoformat(S) + timedelta(days=364)).isoformat()
        fiscal_year = fiscal_year_of(fy_end)
        known: dict[int, float] = {}
        ends: dict[int, str] = {}
        in_year = sorted(((k, f) for k, f in quarters.items() if S <= k[0] and k[1] <= fy_end), key=lambda kf: kf[0][1])
        for (s, e), f in in_year:
            n = _qnum(S, e)
            _put(rows, _row(item, "quarter", f, fiscal_year, n))
            known[n], ends[n] = f.value, e
        if kind in _DERIVABLE:
            h1 = next((f for (s, e), f in halves.items() if s == S), None)
            nm = next((f for (s, e), f in nines.items() if s == S), None)
            if h1 and 2 not in known and 1 in known:
                v = h1.value - known[1]
                _put(rows, _row(item, "quarter", h1, fiscal_year, 2, value=v, start=_day_after(ends[1]), derived=True))
                known[2], ends[2] = v, h1.period_end
            if nm and 3 not in known:
                base = known[1] + known[2] if 1 in known and 2 in known else (h1.value if h1 else None)
                if base is not None:
                    prev_end = ends.get(2) or (h1.period_end if h1 else None)
                    if prev_end:
                        v = nm.value - base
                        _put(rows, _row(item, "quarter", nm, fiscal_year, 3, value=v, start=_day_after(prev_end), derived=True))
                        known[3], ends[3] = v, nm.period_end
            if fy_fact and 4 not in known:
                base = known[1] + known[2] + known[3] if {1, 2, 3} <= known.keys() else (nm.value if nm else None)
                if base is not None:
                    prev_end = ends.get(3) or (nm.period_end if nm else None)
                    if prev_end:
                        v = fy_fact.value - base
                        _put(rows, _row(item, "quarter", fy_fact, fiscal_year, 4, value=v, start=_day_after(prev_end), derived=True))
                        known[4], ends[4] = v, fy_fact.period_end
        for n, e in ends.items():
            quarter_number[e], quarter_fy[e] = n, fiscal_year
    return list(rows.values())


def rebuild(facts: list[Fact], rules: list[ConceptRule]) -> list[LineItem]:
    by_concept: dict[tuple[str, str], list[Fact]] = {}
    for f in facts:
        by_concept.setdefault((f.taxonomy, f.concept), []).append(f)
    by_item: dict[str, list[ConceptRule]] = {}
    for r in sorted(rules, key=lambda r: (r.line_item, r.priority)):
        by_item.setdefault(r.line_item, []).append(r)

    annual_ends: set[str] = set()
    quarter_ends: set[str] = set()
    for f in facts:
        if f.period_start:
            n = _days(f)
            if _within(n, ANNUAL_DAYS):
                annual_ends.add(f.period_end)
            elif _within(n, QUARTER_DAYS) or _within(n, HALF_DAYS) or _within(n, NINE_MONTH_DAYS):
                quarter_ends.add(f.period_end)

    quarter_number: dict[str, int] = {}
    quarter_fy: dict[str, int] = {}
    out: list[LineItem] = []
    instant_items: list[tuple[str, dict[str, Fact]]] = []
    for item, item_rules in by_item.items():
        chosen = _chosen(by_concept, item_rules)
        durations = {k: f for k, f in chosen.items() if k[0]}
        instants = {k[1]: f for k, f in chosen.items() if not k[0]}
        if instants:
            instant_items.append((item, instants))
        if durations:
            out.extend(_duration_rows(item, item_rules[0].kind, durations, quarter_number, quarter_fy))
    for item, instants in instant_items:
        for end, f in instants.items():
            if end in annual_ends:
                out.append(_row(item, "annual", f, fiscal_year_of(end), None))
            if end in annual_ends or end in quarter_ends:
                fq = 4 if end in annual_ends else quarter_number.get(end)
                out.append(_row(item, "quarter", f, quarter_fy.get(end, fiscal_year_of(end)), fq))
    return out
```

- [ ] **Step 4: Run to verify pass**

Run: `.venv/Scripts/python -m pytest tests/test_statements.py tests/test_sec.py -v`
Expected: 15 passed (the SEC collector's default `rebuild` now resolves).

- [ ] **Step 5: Commit**

```bash
git add src/financial_data_collector/statements.py tests/test_statements.py
git commit -m "Add statement shaping with YTD differencing and derived Q4"
```

---

### Task 12: Static views

**Files:**
- Create: `src/financial_data_collector/migrations/0002_views.sql`, `tests/test_views.py`

**Interfaces:**
- Produces views `positions_latest`, `holdings_history`, `account_values_daily`, `portfolio_daily`, `sync_status` (columns listed in the SQL below). Task 4's `financials_annual` / `financials_quarterly` are exercised here with real line items.

- [ ] **Step 1: Write the failing tests**

`tests/test_views.py`:
```python
import json
from pathlib import Path

import pytest

from financial_data_collector import statements
from financial_data_collector.collectors.sec_facts import parse_company_facts
from financial_data_collector.models import AccountRef, CashRow, PositionRow, Snapshot
from financial_data_collector.store import Store

FID = AccountRef("Sample Brokerage", "brokerage", "fidelity", "brokerage")
WEB = AccountRef("Webull Sample Cash", "brokerage", "webull", "brokerage")


@pytest.fixture
def store(tmp_path: Path):
    s = Store.open(tmp_path / "w.db")
    s.write_snapshot(Snapshot("2026-01-10", "snaptrade",
        [PositionRow(FID, "AAPL", "APPLE INC", 10, 99.0, 990.0), PositionRow(WEB, "KO", "COCA COLA CO", 2, 50.0, 100.0)],
        [CashRow(FID, 20.0), CashRow(WEB, 5.0)]))
    s.write_snapshot(Snapshot("2026-01-16", "snaptrade",
        [PositionRow(FID, "AAPL", "APPLE INC", 10, 101.0, 1010.0), PositionRow(FID, "KO", "COCA COLA CO", 1, 50.0, 50.0)],
        [CashRow(FID, 25.5)]))
    yield s
    s.close()


def test_positions_latest_per_account(store: Store):
    rows = store.query("SELECT account, symbol, as_of_date FROM positions_latest ORDER BY account, symbol")
    assert [tuple(r) for r in rows] == [
        ("Sample Brokerage", "AAPL", "2026-01-16"), ("Sample Brokerage", "KO", "2026-01-16"),
        ("Webull Sample Cash", "KO", "2026-01-10")]


def test_holdings_history_sums_accounts(store: Store):
    rows = store.query("SELECT as_of_date, symbol, quantity, market_value FROM holdings_history WHERE symbol='KO' ORDER BY 1")
    assert [tuple(r) for r in rows] == [("2026-01-10", "KO", 2.0, 100.0), ("2026-01-16", "KO", 1.0, 50.0)]


def test_account_values_and_portfolio_daily(store: Store):
    rows = store.query("SELECT as_of_date, account, holdings_value, cash, total FROM account_values_daily ORDER BY 1, 2")
    assert [tuple(r) for r in rows] == [
        ("2026-01-10", "Sample Brokerage", 990.0, 20.0, 1010.0),
        ("2026-01-10", "Webull Sample Cash", 100.0, 5.0, 105.0),
        ("2026-01-16", "Sample Brokerage", 1060.0, 25.5, 1085.5)]
    p = store.query("SELECT as_of_date, total, fidelity_total, accounts FROM portfolio_daily ORDER BY 1")
    assert [tuple(r) for r in p] == [("2026-01-10", 1115.0, 1010.0, 2), ("2026-01-16", 1085.5, 1085.5, 1)]


def test_sync_status_latest_per_step(store: Store):
    store.log_run("r1", "prices", "error", 0, "boom", "2026-01-01T00:00:00Z", "2026-01-01T00:00:01Z")
    store.log_run("r2", "prices", "ok", 3, "", "2026-01-02T00:00:00Z", "2026-01-02T00:00:01Z")
    store.log_run("r2", "sec", "ok", 1, "", "2026-01-02T00:00:00Z", "2026-01-02T00:00:02Z")
    rows = {r["step"]: r for r in store.query("SELECT * FROM sync_status")}
    assert rows["prices"]["status"] == "ok" and rows["prices"]["rows_written"] == 3
    assert rows["sec"]["age_hours"] > 0


def test_wide_financial_views(store: Store, fixtures: Path):
    facts = parse_company_facts(json.loads((fixtures / "sec" / "companyfacts_SAMPLE.json").read_text()))
    store.upsert_security("AAPL", "APPLE INC", "stock", "2026-01-01")
    store.set_security_cik("AAPL", "0000000001", "Sample Corp")
    store.write_sec_facts("0000000001", facts)
    store.replace_line_items("0000000001", statements.rebuild(facts, store.concept_rules()))
    a = store.query("SELECT symbol, company, fiscal_year, revenue, ocf, capex, fcf, fcf_margin, total_assets, eps_diluted "
                    "FROM financials_annual WHERE fiscal_year = 2025")[0]
    assert tuple(a)[:7] == ("AAPL", "Sample Corp", 2025, 1100.0, 250.0, 40.0, 210.0)
    assert abs(a["fcf_margin"] - 210 / 1100) < 1e-9 and a["total_assets"] == 5500.0 and a["eps_diluted"] == 2.0
    q = {r["period_end"]: r for r in store.query("SELECT * FROM financials_quarterly WHERE fiscal_year = 2025")}
    assert [q[e]["revenue"] for e in sorted(q)] == [260.0, 270.0, 280.0, 290.0]
    assert q["2025-06-30"]["ocf"] == 60.0 and q["2025-06-30"]["is_derived_q4"] == 1
    assert q["2025-03-31"]["is_derived_q4"] == 0 and q["2025-03-31"]["fiscal_quarter"] == 1
    assert q["2025-12-31"]["total_assets"] == 5500.0 and q["2025-12-31"]["gross_margin"] is None
```

- [ ] **Step 2: Run to verify failure**

Run: `.venv/Scripts/python -m pytest tests/test_views.py -v`
Expected: FAIL, `no such table: positions_latest`

- [ ] **Step 3: Write the views**

`src/financial_data_collector/migrations/0002_views.sql`:
```sql
-- 0002: analysis-friendly views over the raw tables.

CREATE VIEW positions_latest AS
SELECT p.as_of_date, a.label AS account, a.institution, a.account_type,
       p.symbol, s.description, s.asset_type,
       p.quantity, p.price, p.market_value, p.cost_basis_total, p.avg_cost, p.unrealized_pnl, p.source
FROM position_snapshots p
JOIN accounts a ON a.id = p.account_id
JOIN securities s ON s.symbol = p.symbol
WHERE p.as_of_date = (SELECT MAX(p2.as_of_date) FROM position_snapshots p2 WHERE p2.account_id = p.account_id);

CREATE VIEW holdings_history AS
SELECT as_of_date, symbol,
       SUM(quantity) AS quantity,
       SUM(market_value) AS market_value,
       SUM(cost_basis_total) AS cost_basis_total,
       COUNT(*) AS accounts
FROM position_snapshots
GROUP BY as_of_date, symbol;

CREATE VIEW account_values_daily AS
WITH h AS (SELECT as_of_date, account_id, SUM(market_value) AS holdings_value FROM position_snapshots GROUP BY 1, 2),
     c AS (SELECT as_of_date, account_id, SUM(amount) AS cash FROM cash_balances GROUP BY 1, 2),
     d AS (SELECT as_of_date, account_id FROM h UNION SELECT as_of_date, account_id FROM c)
SELECT d.as_of_date, a.label AS account, a.institution, a.account_type,
       COALESCE(h.holdings_value, 0) AS holdings_value,
       COALESCE(c.cash, 0) AS cash,
       COALESCE(h.holdings_value, 0) + COALESCE(c.cash, 0) AS total
FROM d
JOIN accounts a ON a.id = d.account_id
LEFT JOIN h ON h.as_of_date = d.as_of_date AND h.account_id = d.account_id
LEFT JOIN c ON c.as_of_date = d.as_of_date AND c.account_id = d.account_id;

CREATE VIEW portfolio_daily AS
SELECT as_of_date,
       SUM(total) AS total,
       SUM(CASE WHEN institution = 'fidelity' THEN total ELSE 0 END) AS fidelity_total,
       SUM(holdings_value) AS holdings_value,
       SUM(cash) AS cash,
       COUNT(*) AS accounts
FROM account_values_daily
GROUP BY as_of_date;

CREATE VIEW sync_status AS
SELECT r.step, r.status, r.rows_written, r.message, r.started_at, r.finished_at,
       ROUND((julianday('now') - julianday(r.finished_at)) * 24, 1) AS age_hours
FROM sync_runs r
WHERE r.id = (SELECT MAX(r2.id) FROM sync_runs r2 WHERE r2.step = r.step);
```

- [ ] **Step 4: Run to verify pass**

Run: `.venv/Scripts/python -m pytest tests/test_views.py tests/test_store.py -v`
Expected: 18 passed (`test_migrate_creates_schema_and_seeds` now sees version 2 as well).

- [ ] **Step 5: Commit**

```bash
git add src/financial_data_collector/migrations/0002_views.sql tests/test_views.py
git commit -m "Add positions, portfolio and sync views"
```

---
### Task 13: Sync orchestration

**Files:**
- Create: `src/financial_data_collector/sync.py`, `tests/test_sync.py`

**Interfaces:**
- Consumes: `Store`, `Config`, `ConfigError`, `ingest_inbox`, `ingest_snaptrade`, `build_universe`, `collect_prices`, `collect_sec`, `http.fetch`.
- Produces: `STEPS = ("ingest", "prices", "sec")`; `StepResult(step, status, rows, message, started_at, finished_at)`; `SyncReport(run_id, steps, dry_run)` with property `exit_code -> int`; `run_sync(cfg, *, only=None, skip=None, dry_run=False, fetch=http.fetch, now=None, yf=None, sleep=time.sleep) -> SyncReport`.

- [ ] **Step 1: Write the failing tests**

`tests/test_sync.py`:
```python
import json
import shutil
from datetime import datetime, timezone
from pathlib import Path

import pytest

from financial_data_collector import config as C
from financial_data_collector import sync
from financial_data_collector.collectors import sec_cik, sec_facts
from financial_data_collector.http import HttpError
from financial_data_collector.models import PriceBar
from financial_data_collector.store import Store

NOW = datetime(2026, 3, 1, 12, 0, tzinfo=timezone.utc)
TIINGO = [{"date": "2026-01-02T00:00:00.000Z", "close": 1.0, "adjClose": 1.0, "divCash": 0, "splitFactor": 1}]


@pytest.fixture
def project(tmp_path: Path, fixtures: Path):
    C.init_project(tmp_path)
    (tmp_path / "config.toml").write_text('[sources.snaptrade]\nenabled = true\ndir = "snap"\n')
    (tmp_path / ".env").write_text("TIINGO_API_TOKEN=tok\nSEC_USER_AGENT=Sample Person s@example.com\n")
    shutil.copytree(fixtures / "snaptrade", tmp_path / "snap")
    shutil.copy(fixtures / "Portfolio_Positions_Jan-15-2026.csv", tmp_path / "inbox")
    return C.load_config(tmp_path)


def _fetch(fixtures: Path):
    def fetch(url, headers):
        if url == sec_cik.CIK_URL:
            return (fixtures / "sec" / "company_tickers.json").read_bytes()
        if "companyfacts/CIK0000000001" in url:
            return (fixtures / "sec" / "companyfacts_SAMPLE.json").read_bytes()
        if "tiingo" in url:
            return json.dumps(TIINGO).encode()
        raise HttpError(404, url)
    return fetch


def _counts(cfg):
    s = Store.open(cfg.db_path, migrate=False)
    try:
        return s.counts()
    finally:
        s.close()


def test_full_sync_then_noop(project, fixtures: Path):
    rep = sync.run_sync(project, fetch=_fetch(fixtures), now=NOW, yf=lambda s, d: [], sleep=lambda s: None)
    assert [s.step for s in rep.steps] == ["ingest", "prices", "sec"]
    assert all(s.status == "ok" for s in rep.steps), rep.steps
    assert rep.exit_code == 0
    c1 = _counts(project)
    assert c1["position_snapshots"] > 0 and c1["prices"] > 0 and c1["sec_facts"] == 34
    assert c1["financial_line_items"] > 0 and c1["sync_runs"] == 3
    assert "no data" in rep.steps[1].message or "without data" in rep.steps[1].message   # VTI, KO, BRK.B had no bars
    rep2 = sync.run_sync(project, fetch=_fetch(fixtures), now=NOW, yf=lambda s, d: [], sleep=lambda s: None)
    assert rep2.steps[0].rows == 0
    c2 = _counts(project)
    assert {k: v for k, v in c2.items() if k != "sync_runs"} == {k: v for k, v in c1.items() if k != "sync_runs"}
    assert rep2.steps[2].message.startswith("0 fetched")


def test_only_and_skip(project, fixtures: Path):
    rep = sync.run_sync(project, only=["prices"], fetch=_fetch(fixtures), now=NOW, yf=lambda s, d: [], sleep=lambda s: None)
    assert [s.step for s in rep.steps] == ["prices"]
    rep = sync.run_sync(project, skip=["sec"], fetch=_fetch(fixtures), now=NOW, yf=lambda s, d: [], sleep=lambda s: None)
    assert [s.step for s in rep.steps] == ["ingest", "prices"]


def test_dry_run_touches_nothing(project):
    rep = sync.run_sync(project, dry_run=True)
    assert rep.dry_run and [s.step for s in rep.steps] == ["ingest", "prices", "sec"]
    assert not project.db_path.exists()


def test_step_failure_is_isolated(project, fixtures: Path):
    (project.snaptrade_dir / "snapshots" / "live-positions-2026-01-11.json").write_text("{not json")
    rep = sync.run_sync(project, fetch=_fetch(fixtures), now=NOW, yf=lambda s, d: [], sleep=lambda s: None)
    by = {s.step: s for s in rep.steps}
    assert by["ingest"].status == "error" and "JSONDecodeError" in by["ingest"].message
    assert by["prices"].status == "ok" and by["sec"].status == "ok"
    assert rep.exit_code == 0


def test_missing_user_agent_skips_sec(project, fixtures: Path):
    project.sec_user_agent = None
    rep = sync.run_sync(project, fetch=_fetch(fixtures), now=NOW, yf=lambda s, d: [], sleep=lambda s: None)
    by = {s.step: s for s in rep.steps}
    assert by["sec"].status == "skipped" and "SEC_USER_AGENT" in by["sec"].message
    assert rep.exit_code == 0


def test_network_down_exit_code(project):
    def down(url, headers):
        raise HttpError(500, url)
    rep = sync.run_sync(project, fetch=down, now=NOW, yf=lambda s, d: [], sleep=lambda s: None)
    by = {s.step: s for s in rep.steps}
    assert by["ingest"].status == "ok"
    assert by["prices"].status == "error" and by["sec"].status == "error"
    assert rep.exit_code == 1
```

- [ ] **Step 2: Run to verify failure**

Run: `.venv/Scripts/python -m pytest tests/test_sync.py -v`
Expected: FAIL, `ImportError`

- [ ] **Step 3: Implement**

`src/financial_data_collector/sync.py`:
```python
"""fdc sync: run each step independently, log each to sync_runs, never let one failure stop the rest."""
from __future__ import annotations

import time
import traceback
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Callable

from . import http
from .collectors import prices as prices_mod
from .collectors.sec_facts import collect_sec
from .config import Config, ConfigError
from .ingest import ingest_inbox, ingest_snaptrade
from .models import PriceBar
from .store import Store, utcnow
from .universe import build_universe

STEPS = ("ingest", "prices", "sec")
NETWORK_STEPS = {"prices", "sec"}


@dataclass
class StepResult:
    step: str
    status: str  # ok | skipped | error | dry-run
    rows: int = 0
    message: str = ""
    started_at: str = ""
    finished_at: str = ""


@dataclass
class SyncReport:
    run_id: str
    steps: list[StepResult] = field(default_factory=list)
    dry_run: bool = False

    @property
    def exit_code(self) -> int:
        network = [s for s in self.steps if s.step in NETWORK_STEPS]
        if network and all(s.status == "error" for s in network):
            return 1
        return 0


def _select_steps(only, skip) -> list[str]:
    only = set(only or ())
    skip = set(skip or ())
    unknown = (only | skip) - set(STEPS)
    if unknown:
        raise ConfigError(f"unknown step(s): {', '.join(sorted(unknown))}; valid: {', '.join(STEPS)}")
    return [s for s in STEPS if (not only or s in only) and s not in skip]


def _step_ingest(store: Store, cfg: Config, **_) -> tuple[int, str, str]:
    a = ingest_inbox(store, cfg)
    b = ingest_snaptrade(store, cfg)
    msgs = a.messages + b.messages
    return a.rows + b.rows, "; ".join(msgs), "ok"


def _step_prices(store: Store, cfg: Config, *, fetch, now, yf, sleep) -> tuple[int, str, str]:
    universe = build_universe(store, cfg.watchlist)
    kwargs = {"fetch": fetch, "today": now.date(), "sleep": sleep}
    if yf is not None:
        kwargs["yf"] = yf
    results = prices_mod.collect_prices(store, universe, cfg, **kwargs)
    updated = [r for r in results if r.rows > 0]
    nodata = [r for r in results if r.rows == 0 and r.message != "up to date"]
    msg = f"{len(updated)} symbols updated"
    if nodata:
        msg += f", {len(nodata)} without data: " + ", ".join(f"{r.symbol} ({r.message})" for r in nodata)
    status = "error" if results and not updated and nodata else "ok"
    return sum(r.rows for r in results), msg, status


def _step_sec(store: Store, cfg: Config, *, fetch, now, sleep, **_) -> tuple[int, str, str]:
    results = collect_sec(store, cfg, fetch=fetch, now=now, sleep=sleep)
    fetched = [r for r in results if r.facts and r.line_items >= 0 and not r.message]
    failed = [r for r in results if r.message.startswith("fetch failed")]
    fresh = [r for r in results if r.message == "fresh"]
    nocik = [r for r in results if r.cik is None]
    msg = f"{len(fetched)} fetched, {len(fresh)} fresh, {len(nocik)} without CIK"
    if nocik:
        msg += " (" + ", ".join(r.symbol for r in nocik) + ")"
    if failed:
        msg += "; failed: " + ", ".join(f"{r.symbol} ({r.message})" for r in failed)
    status = "error" if results and failed and not fetched and not fresh else "ok"
    return sum(r.facts for r in results), msg, status


_RUNNERS: dict[str, Callable] = {"ingest": _step_ingest, "prices": _step_prices, "sec": _step_sec}


def run_sync(
    cfg: Config,
    *,
    only: list[str] | None = None,
    skip: list[str] | None = None,
    dry_run: bool = False,
    fetch: http.Fetch = http.fetch,
    now: datetime | None = None,
    yf: Callable[[str, str], list[PriceBar]] | None = None,
    sleep: Callable[[float], None] = time.sleep,
) -> SyncReport:
    now = now or datetime.now(timezone.utc)
    run_id = now.strftime("%Y%m%dT%H%M%SZ")
    steps = _select_steps(only, skip)
    report = SyncReport(run_id=run_id, dry_run=dry_run)
    if dry_run:
        report.steps = [StepResult(s, "dry-run") for s in steps]
        return report
    store = Store.open(cfg.db_path, backup_dir=cfg.backup_dir)
    try:
        for step in steps:
            started = utcnow()
            try:
                rows, message, status = _RUNNERS[step](store, cfg, fetch=fetch, now=now, yf=yf, sleep=sleep)
            except ConfigError as e:
                rows, message, status = 0, str(e), "skipped"
            except Exception as e:  # isolate: log and continue with the next step
                tb = traceback.format_exc().strip().splitlines()
                rows, message, status = 0, f"{type(e).__name__}: {e} | " + " | ".join(tb[-3:]), "error"
            finished = utcnow()
            store.log_run(run_id, step, status, rows, message, started, finished)
            report.steps.append(StepResult(step, status, rows, message, started, finished))
    finally:
        store.close()
    return report
```

- [ ] **Step 4: Run to verify pass**

Run: `.venv/Scripts/python -m pytest tests/test_sync.py -v`
Expected: 6 passed

- [ ] **Step 5: Commit**

```bash
git add src/financial_data_collector/sync.py tests/test_sync.py
git commit -m "Add sync orchestration with isolated steps and run log"
```

---

### Task 14: Read-only query path and CLI

**Files:**
- Create: `src/financial_data_collector/readonly.py`, `src/financial_data_collector/cli.py`, `tests/test_readonly.py`, `tests/test_cli.py`

**Interfaces:**
- Produces (readonly): `UnsafeSql(ValueError)`; `guard_sql(sql: str) -> str`; `run_readonly(db_path: Path, sql: str, limit: int = 500) -> tuple[list[str], list[list], bool]` (columns, rows, truncated).
- Produces (cli): `main(argv: list[str] | None = None) -> int`; subcommands `init`, `sync`, `import`, `status`, `query`, `mcp`; global `--root PATH` (default `.`).
- Consumes from Task 15: `mcp_server.serve(db_path, root)` and `mcp_server.print_config(root)` (imported lazily inside the `mcp` command so this task's tests do not need it).

- [ ] **Step 1: Write the failing readonly tests**

`tests/test_readonly.py`:
```python
from pathlib import Path

import pytest

from financial_data_collector import readonly as R
from financial_data_collector.store import Store


@pytest.mark.parametrize("sql", [
    "SELECT 1", "  select * from prices ", "WITH t AS (SELECT 1 x) SELECT * FROM t",
    "EXPLAIN QUERY PLAN SELECT 1", "select 1;", "select replace('a','a','b')",
])
def test_guard_accepts(sql):
    assert R.guard_sql(sql)


@pytest.mark.parametrize("sql", [
    "DELETE FROM prices", "pragma journal_mode=delete", "ATTACH 'x.db' AS y",
    "select 1; drop table prices", "insert into prices values (1)", "update prices set close=0",
    "WITH t AS (SELECT 1) DELETE FROM prices", "", "   ",
])
def test_guard_rejects(sql):
    with pytest.raises(R.UnsafeSql):
        R.guard_sql(sql)


def test_run_readonly_caps_and_truncates(tmp_path: Path):
    s = Store.open(tmp_path / "w.db")
    s.upsert_security("AAPL", first_seen="2026-01-01")
    s.upsert_security("KO", first_seen="2026-01-01")
    s.close()
    cols, rows, truncated = R.run_readonly(tmp_path / "w.db", "select symbol from securities order by 1", limit=1)
    assert cols == ["symbol"] and rows == [["AAPL"]] and truncated is True
    cols, rows, truncated = R.run_readonly(tmp_path / "w.db", "select symbol from securities order by 1", limit=5)
    assert len(rows) == 2 and truncated is False


def test_run_readonly_cannot_write_even_if_guard_bypassed(tmp_path: Path):
    Store.open(tmp_path / "w.db").close()
    import sqlite3
    with pytest.raises(sqlite3.OperationalError):
        R._connect(tmp_path / "w.db").execute("DELETE FROM securities")
```

- [ ] **Step 2: Implement readonly.py**

`src/financial_data_collector/readonly.py`:
```python
"""Read-only SQL for `fdc query` and the MCP server: guard + read-only connection."""
from __future__ import annotations

import re
import sqlite3
from pathlib import Path

_ALLOWED = ("select", "with", "explain")
_FORBIDDEN = re.compile(
    r"\b(attach|detach|pragma|insert|update|delete|drop|alter|create|vacuum|reindex)\b", re.IGNORECASE
)


class UnsafeSql(ValueError):
    pass


def guard_sql(sql: str) -> str:
    s = (sql or "").strip().rstrip(";").strip()
    if not s:
        raise UnsafeSql("empty statement")
    if ";" in s:
        raise UnsafeSql("one statement at a time")
    if not s.lower().startswith(_ALLOWED):
        raise UnsafeSql("only SELECT / WITH / EXPLAIN are allowed")
    m = _FORBIDDEN.search(s)
    if m:
        raise UnsafeSql(f"{m.group(1).upper()} is not allowed on the read-only connection")
    return s


def _connect(db_path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(f"file:{Path(db_path).as_posix()}?mode=ro", uri=True, timeout=5.0)
    conn.execute("PRAGMA query_only=1")
    return conn


def run_readonly(db_path: Path, sql: str, limit: int = 500) -> tuple[list[str], list[list], bool]:
    safe = guard_sql(sql)
    conn = _connect(db_path)
    try:
        cur = conn.execute(safe)
        cols = [d[0] for d in cur.description or []]
        rows = cur.fetchmany(limit + 1)
        truncated = len(rows) > limit
        return cols, [list(r) for r in rows[:limit]], truncated
    finally:
        conn.close()
```

- [ ] **Step 3: Write the failing CLI tests**

`tests/test_cli.py`:
```python
from pathlib import Path

import pytest

from financial_data_collector import cli
from financial_data_collector import sync as sync_mod


def test_init_and_status_fresh(tmp_path: Path, capsys):
    assert cli.main(["--root", str(tmp_path), "init"]) == 0
    out = capsys.readouterr().out
    assert "config.toml" in out and (tmp_path / "data" / "warehouse.db").exists()
    assert cli.main(["--root", str(tmp_path), "status"]) == 0
    out = capsys.readouterr().out
    assert "never" in out and "SEC_USER_AGENT" in out


def test_status_without_init(tmp_path: Path, capsys):
    assert cli.main(["--root", str(tmp_path), "status"]) == 2
    assert "fdc init" in capsys.readouterr().err


def test_import_and_query(tmp_path: Path, fixtures: Path, capsys):
    cli.main(["--root", str(tmp_path), "init"])
    rc = cli.main(["--root", str(tmp_path), "import", str(fixtures / "History_SampleBrokerage_2026.csv"), "--account", "Sample Brokerage"])
    assert rc == 0 and "7 rows" in capsys.readouterr().out
    rc = cli.main(["--root", str(tmp_path), "query", "select type, count(*) n from transactions group by 1 order by 1"])
    out = capsys.readouterr().out
    assert rc == 0 and "buy" in out and "contribution" in out
    rc = cli.main(["--root", str(tmp_path), "query", "--csv", "select count(*) n from transactions"])
    assert rc == 0 and capsys.readouterr().out.strip().splitlines() == ["n", "7"]
    rc = cli.main(["--root", str(tmp_path), "query", "delete from transactions"])
    assert rc == 1 and "not allowed" in capsys.readouterr().err


def test_import_directory_and_unknown(tmp_path: Path, fixtures: Path, capsys):
    cli.main(["--root", str(tmp_path), "init"])
    rc = cli.main(["--root", str(tmp_path), "import", str(fixtures)])
    out = capsys.readouterr().out
    assert rc == 0 and "Portfolio_Positions_Jan-15-2026.csv" in out and "--account" in out


def test_sync_delegates_and_prints(tmp_path: Path, monkeypatch, capsys):
    cli.main(["--root", str(tmp_path), "init"])
    captured = {}

    def fake_run_sync(cfg, **kw):
        captured.update(kw)
        return sync_mod.SyncReport("r", [sync_mod.StepResult("prices", "ok", 3, "3 symbols updated")])

    monkeypatch.setattr(cli, "run_sync", fake_run_sync)
    rc = cli.main(["--root", str(tmp_path), "sync", "--only", "prices,sec", "--skip", "sec"])
    out = capsys.readouterr().out
    assert rc == 0 and "prices" in out and "ok" in out
    assert captured["only"] == ["prices", "sec"] and captured["skip"] == ["sec"] and captured["dry_run"] is False


def test_sync_dry_run(tmp_path: Path, capsys):
    cli.main(["--root", str(tmp_path), "init"])
    rc = cli.main(["--root", str(tmp_path), "sync", "--dry-run"])
    out = capsys.readouterr().out
    assert rc == 0 and "ingest" in out and "prices" in out and "sec" in out
```

- [ ] **Step 4: Implement cli.py**

`src/financial_data_collector/cli.py`:
```python
"""fdc command line."""
from __future__ import annotations

import argparse
import csv
import sys
from pathlib import Path

from . import __version__
from .config import ConfigError, init_project, load_config
from .ingest import ingest_file
from .readonly import UnsafeSql, run_readonly
from .store import Store
from .sync import STEPS, run_sync

STALE_HOURS = {"ingest": 48, "prices": 48, "sec": 336}


def _parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="fdc", description="Local SQLite warehouse for your brokerage positions.")
    p.add_argument("--root", default=".", help="project folder holding config.toml (default: current directory)")
    p.add_argument("--version", action="version", version=f"fdc {__version__}")
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("init", help="create data/, inbox/, config.toml, .env and the database")
    s = sub.add_parser("sync", help="ingest inbox + SnapTrade, then fetch prices and SEC facts")
    s.add_argument("--only", help=f"comma list of steps to run ({', '.join(STEPS)})")
    s.add_argument("--skip", help="comma list of steps to skip")
    s.add_argument("--dry-run", action="store_true")
    i = sub.add_parser("import", help="ingest one export file or a folder of them (files are not moved)")
    i.add_argument("path")
    i.add_argument("--account", help="account label for history files that carry none")
    sub.add_parser("status", help="last run per step, staleness, row counts")
    q = sub.add_parser("query", help="run a read-only SQL statement")
    q.add_argument("sql")
    q.add_argument("--csv", action="store_true")
    q.add_argument("--limit", type=int, default=500)
    m = sub.add_parser("mcp", help="run the read-only MCP server for Claude Desktop (stdio)")
    m.add_argument("--print-config", action="store_true", help="print the claude_desktop_config.json snippet")
    return p


def _csv_list(s: str | None) -> list[str] | None:
    return [x.strip() for x in s.split(",") if x.strip()] if s else None


def _table(cols: list[str], rows: list[list]) -> str:
    cells = [[("" if v is None else str(v)) for v in r] for r in rows]
    widths = [max([len(c)] + [len(r[i]) for r in cells]) for i, c in enumerate(cols)]
    line = "  ".join(c.ljust(widths[i]) for i, c in enumerate(cols))
    out = [line, "  ".join("-" * w for w in widths)]
    out += ["  ".join(r[i].ljust(widths[i]) for i in range(len(cols))) for r in cells]
    return "\n".join(out)


def cmd_init(root: Path) -> int:
    created = init_project(root)
    for c in created:
        print(f"created {c}")
    cfg = load_config(root)
    store = Store.open(cfg.db_path, backup_dir=cfg.backup_dir)
    store.close()
    print(f"database ready: {cfg.db_path}")
    print("next: put your name and email in .env (SEC_USER_AGENT), drop a Fidelity export in inbox/, run: fdc sync")
    return 0


def cmd_sync(root: Path, args) -> int:
    cfg = load_config(root)
    report = run_sync(cfg, only=_csv_list(args.only), skip=_csv_list(args.skip), dry_run=args.dry_run)
    for s in report.steps:
        print(f"{s.step:8} {s.status:8} {s.rows:>7} rows  {s.message}")
    return report.exit_code


def cmd_import(root: Path, args) -> int:
    cfg = load_config(root)
    target = Path(args.path)
    files = sorted(p for p in target.iterdir() if p.is_file()) if target.is_dir() else [target]
    store = Store.open(cfg.db_path, backup_dir=cfg.backup_dir)
    try:
        for f in files:
            r = ingest_file(store, f, account=args.account)
            if r.skipped:
                print(f"{f.name}: skipped - {r.skipped}")
            else:
                print(f"{f.name}: {r.kind}, {r.rows} rows")
            for w in r.warnings:
                print(f"  warning: {w}")
    finally:
        store.close()
    return 0


def cmd_status(root: Path) -> int:
    cfg = load_config(root)
    if not cfg.db_path.exists():
        print("no database yet; run: fdc init", file=sys.stderr)
        return 2
    store = Store.open(cfg.db_path, migrate=False)
    try:
        runs = {r["step"]: r for r in store.query("SELECT * FROM sync_status")}
        print("steps:")
        for step in STEPS:
            r = runs.get(step)
            if r is None:
                print(f"  {step:8} never run")
                continue
            stale = " STALE" if (r["age_hours"] or 0) > STALE_HOURS[step] else ""
            print(f"  {step:8} {r['status']:8} {r['age_hours']:>7} h ago  {r['rows_written']} rows{stale}  {r['message'][:120]}")
        print(f"latest snapshot: {store.latest_snapshot_date() or 'none'}")
        print("rows:", ", ".join(f"{k}={v}" for k, v in store.counts().items()))
    finally:
        store.close()
    print(f"SEC_USER_AGENT: {'set' if cfg.sec_user_agent else 'NOT SET (SEC step will be skipped)'}")
    print(f"TIINGO_API_TOKEN: {'set' if cfg.tiingo_token else 'not set (yfinance will be used)'}")
    return 0


def cmd_query(root: Path, args) -> int:
    cfg = load_config(root)
    try:
        cols, rows, truncated = run_readonly(cfg.db_path, args.sql, limit=args.limit)
    except UnsafeSql as e:
        print(f"refused: {e}", file=sys.stderr)
        return 1
    if args.csv:
        w = csv.writer(sys.stdout, lineterminator="\n")
        w.writerow(cols)
        w.writerows(rows)
    else:
        print(_table(cols, rows))
        if truncated:
            print(f"(truncated at {args.limit} rows; use --limit)")
    return 0


def cmd_mcp(root: Path, args) -> int:
    from . import mcp_server

    cfg = load_config(root)
    if args.print_config:
        print(mcp_server.print_config(root))
        return 0
    mcp_server.serve(cfg.db_path)
    return 0


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    root = Path(args.root).resolve()
    try:
        if args.cmd == "init":
            return cmd_init(root)
        if args.cmd == "sync":
            return cmd_sync(root, args)
        if args.cmd == "import":
            return cmd_import(root, args)
        if args.cmd == "status":
            return cmd_status(root)
        if args.cmd == "query":
            return cmd_query(root, args)
        if args.cmd == "mcp":
            return cmd_mcp(root, args)
    except ConfigError as e:
        print(f"error: {e}", file=sys.stderr)
        return 2
    return 2
```

- [ ] **Step 5: Run to verify pass**

Run: `.venv/Scripts/python -m pytest tests/test_readonly.py tests/test_cli.py -v`
Expected: 21 passed

- [ ] **Step 6: Commit**

```bash
git add src/financial_data_collector/readonly.py src/financial_data_collector/cli.py tests/test_readonly.py tests/test_cli.py
git commit -m "Add fdc CLI and read-only query path"
```

---

### Task 15: MCP server for Claude Desktop

**Files:**
- Create: `src/financial_data_collector/mcp_server.py`, `tests/test_mcp.py`

**Interfaces:**
- Consumes: `run_readonly`, `guard_sql`, `Store`.
- Produces: `tool_schema(db_path) -> dict`, `tool_query(db_path, sql, limit=500) -> dict`, `tool_status(db_path) -> dict`, `build_server(db_path) -> FastMCP`, `serve(db_path) -> None`, `print_config(root: Path) -> str`, `SERVER_NAME = "financial-data-collector"`.

- [ ] **Step 1: Write the failing tests**

`tests/test_mcp.py`:
```python
import asyncio
import json
import sys
from pathlib import Path

import pytest

from financial_data_collector import mcp_server as M
from financial_data_collector.store import Store


@pytest.fixture
def db(tmp_path: Path) -> Path:
    s = Store.open(tmp_path / "w.db")
    s.upsert_security("AAPL", "APPLE INC", "stock", "2026-01-01")
    s.log_run("r", "prices", "ok", 1, "", "2026-01-01T00:00:00Z", "2026-01-01T00:00:01Z")
    s.close()
    return tmp_path / "w.db"


def test_tool_schema(db: Path):
    out = M.tool_schema(db)
    names = {t["name"] for t in out["tables"]}
    assert {"securities", "prices", "sec_facts", "financial_line_items"} <= names
    sec = next(t for t in out["tables"] if t["name"] == "securities")
    assert "symbol" in sec["columns"] and sec["rows"] == 1
    assert {"positions_latest", "portfolio_daily", "financials_annual"} <= {v["name"] for v in out["views"]}


def test_tool_query_and_guard(db: Path):
    out = M.tool_query(db, "select symbol, asset_type from securities")
    assert out == {"columns": ["symbol", "asset_type"], "rows": [["AAPL", "stock"]], "truncated": False}
    bad = M.tool_query(db, "delete from securities")
    assert "error" in bad and "not allowed" in bad["error"]


def test_tool_status(db: Path):
    out = M.tool_status(db)
    assert out["steps"][0]["step"] == "prices" and out["latest_snapshot"] is None


def test_build_server_lists_tools(db: Path):
    server = M.build_server(db)
    tools = asyncio.run(server.list_tools())
    assert {t.name for t in tools} == {"schema", "query", "status"}


def test_print_config(tmp_path: Path):
    text = M.print_config(tmp_path)
    cfg = json.loads(text)
    entry = cfg["mcpServers"][M.SERVER_NAME]
    assert entry["command"] == sys.executable
    assert entry["args"][:3] == ["-m", "financial_data_collector", "--root"]
    assert entry["args"][3] == str(tmp_path) and entry["args"][4] == "mcp"
```

- [ ] **Step 2: Run to verify failure**

Run: `.venv/Scripts/python -m pytest tests/test_mcp.py -v`
Expected: FAIL, `ImportError`

- [ ] **Step 3: Implement**

`src/financial_data_collector/mcp_server.py`:
```python
"""Read-only MCP server (stdio) so Claude Desktop can query the warehouse.

Three tools: schema, query, status. Every query runs through readonly.run_readonly:
a guarded statement on a mode=ro connection with query_only set.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

from .readonly import UnsafeSql, run_readonly
from .store import COUNT_TABLES, Store

SERVER_NAME = "financial-data-collector"


def tool_schema(db_path: Path) -> dict:
    store = Store.open(db_path, migrate=False)
    try:
        counts = store.counts()
        tables, views = [], []
        for kind, name in store.query("SELECT type, name FROM sqlite_master WHERE type IN ('table','view') "
                                      "AND name NOT LIKE 'sqlite_%' ORDER BY type, name"):
            cols = [r[1] for r in store.query(f"PRAGMA table_info({name})")]
            entry = {"name": name, "columns": cols}
            if kind == "table":
                entry["rows"] = counts.get(name, store.query(f"SELECT COUNT(*) FROM {name}")[0][0])
                tables.append(entry)
            else:
                views.append(entry)
        return {"database": str(db_path), "tables": tables, "views": views,
                "hint": "Start from positions_latest, portfolio_daily, holdings_history, prices, "
                        "financials_annual, financials_quarterly. Dates are ISO text."}
    finally:
        store.close()


def tool_query(db_path: Path, sql: str, limit: int = 500) -> dict:
    try:
        cols, rows, truncated = run_readonly(db_path, sql, limit=max(1, min(int(limit), 5000)))
    except UnsafeSql as e:
        return {"error": f"refused: {e}"}
    except Exception as e:  # sqlite errors back to the model, never a crash
        return {"error": f"{type(e).__name__}: {e}"}
    return {"columns": cols, "rows": rows, "truncated": truncated}


def tool_status(db_path: Path) -> dict:
    store = Store.open(db_path, migrate=False)
    try:
        steps = [dict(r) for r in store.query("SELECT * FROM sync_status ORDER BY step")]
        return {"steps": steps, "latest_snapshot": store.latest_snapshot_date(),
                "rows": {t: c for t, c in store.counts().items() if t in COUNT_TABLES}}
    finally:
        store.close()


def build_server(db_path: Path):
    from mcp.server.fastmcp import FastMCP

    mcp = FastMCP(SERVER_NAME)

    @mcp.tool()
    def schema() -> dict:
        """List tables and views with their columns and row counts."""
        return tool_schema(db_path)

    @mcp.tool()
    def query(sql: str, limit: int = 500) -> dict:
        """Run one read-only SELECT (or WITH / EXPLAIN). Returns columns, rows (capped at limit), truncated flag."""
        return tool_query(db_path, sql, limit)

    @mcp.tool()
    def status() -> dict:
        """Last sync run per step, latest snapshot date and row counts."""
        return tool_status(db_path)

    return mcp


def serve(db_path: Path) -> None:
    build_server(db_path).run()


def print_config(root: Path) -> str:
    entry = {"command": sys.executable,
             "args": ["-m", "financial_data_collector", "--root", str(root), "mcp"]}
    return json.dumps({"mcpServers": {SERVER_NAME: entry}}, indent=2)
```

- [ ] **Step 4: Run to verify pass**

Run: `.venv/Scripts/python -m pytest tests/test_mcp.py -v`
Expected: 5 passed

- [ ] **Step 5: Commit**

```bash
git add src/financial_data_collector/mcp_server.py tests/test_mcp.py
git commit -m "Add read-only MCP server for Claude Desktop"
```

---
### Task 16: Examples, scripts, docs and the privacy guard

**Files:**
- Create: `config.example.toml`, `.env.example`, `watchlist.example.txt`, `scripts/run_sync.ps1`, `scripts/schedule_sync.ps1`, `README.md` (replace placeholder), `AGENTS.md`, `CLAUDE.md`, `LICENSE`, `CHANGELOG.md`, `docs/QUERIES.md`, `tests/test_privacy.py`
- Modify: `docs/superpowers/specs/2026-09-25-financial-data-collector-design.md` §12 (layout: migrations and seeds inside the package; add `models.py`, `readonly.py`, `ingest.py`, `sync.py`)

**Interfaces:**
- Produces: nothing importable. The privacy test is the gate before Task 17 pushes anything.

- [ ] **Step 1: Write the failing privacy test**

`tests/test_privacy.py`:
```python
"""Nothing private may be tracked by git. Runs against the real repo."""
import subprocess
from pathlib import Path

from financial_data_collector import config as C

ROOT = Path(__file__).resolve().parents[1]
ALLOWED_DATA_DIRS = ("tests/fixtures/", "src/financial_data_collector/seeds/")


def _tracked() -> list[str]:
    out = subprocess.run(["git", "ls-files"], cwd=ROOT, capture_output=True, text=True, check=True).stdout
    return [l.strip() for l in out.splitlines() if l.strip()]


def test_no_private_files_tracked():
    bad = []
    for f in _tracked():
        low = f.lower()
        if low.endswith((".db", ".db-wal", ".db-shm", ".sqlite", ".sqlite3")):
            bad.append(f)
        if f in (".env", "config.toml", "watchlist.txt"):
            bad.append(f)
        if (f.startswith("data/") or f.startswith("inbox/")) and not f.endswith(".gitkeep"):
            bad.append(f)
        if low.endswith((".csv", ".json")) and not f.startswith(ALLOWED_DATA_DIRS):
            bad.append(f)
    assert bad == [], f"private or data files tracked by git: {bad}"


def test_examples_match_package_constants():
    assert (ROOT / "config.example.toml").read_text(encoding="utf-8") == C.CONFIG_EXAMPLE
    assert (ROOT / ".env.example").read_text(encoding="utf-8") == C.ENV_EXAMPLE


def test_powershell_scripts_are_ascii_without_bom():
    for p in (ROOT / "scripts").glob("*.ps1"):
        raw = p.read_bytes()
        assert not raw.startswith(b"\xef\xbb\xbf"), f"{p.name} has a BOM"
        assert all(b < 128 for b in raw), f"{p.name} is not pure ASCII"
```

- [ ] **Step 2: Write the example files**

`config.example.toml`: exactly the text of `CONFIG_EXAMPLE` in `config.py`. `.env.example`: exactly `ENV_EXAMPLE`. Generate them from the package so they cannot drift:

```bash
.venv/Scripts/python -c "from financial_data_collector import config as C; open('config.example.toml','w',encoding='utf-8',newline='').write(C.CONFIG_EXAMPLE); open('.env.example','w',encoding='utf-8',newline='').write(C.ENV_EXAMPLE)"
```

`watchlist.example.txt`:
```
# Copy to watchlist.txt (gitignored). One symbol per line; '#' starts a comment.
# Symbols here get prices and SEC statements collected even if you never held them.
VTI
KO
```

- [ ] **Step 3: Write the scheduler scripts (pure ASCII, no BOM)**

`scripts/run_sync.ps1`:
```powershell
# Runs one fdc sync from the repo root and appends to the log. Called by the scheduled task.
$ErrorActionPreference = "Continue"
$root = Split-Path -Parent $PSScriptRoot
$logDir = Join-Path $env:LOCALAPPDATA "financial-data-collector"
if (-not (Test-Path $logDir)) { New-Item -ItemType Directory -Path $logDir | Out-Null }
$log = Join-Path $logDir "sync.log"
$python = Join-Path $root ".venv\Scripts\python.exe"
$stamp = (Get-Date).ToString("yyyy-MM-dd HH:mm:ss")
Add-Content -Path $log -Value "[$stamp] sync start"
Set-Location $root
& $python -m financial_data_collector --root $root sync 2>&1 | ForEach-Object { Add-Content -Path $log -Value "  $_" }
$code = $LASTEXITCODE
$stamp = (Get-Date).ToString("yyyy-MM-dd HH:mm:ss")
Add-Content -Path $log -Value "[$stamp] sync end (exit $code)"
exit $code
```

`scripts/schedule_sync.ps1`:
```powershell
# Registers (or removes) the "FinancialDataCollector Sync" scheduled task:
# at logon (delayed 20 min) and then every 12 hours, running scripts\run_sync.ps1.
# Usage:  .\scripts\schedule_sync.ps1        .\scripts\schedule_sync.ps1 -Remove
param([switch]$Remove)
$taskName = "FinancialDataCollector Sync"
if ($Remove) {
  Unregister-ScheduledTask -TaskName $taskName -Confirm:$false -ErrorAction SilentlyContinue
  Write-Host "removed task '$taskName'"
  exit 0
}
$script = Join-Path $PSScriptRoot "run_sync.ps1"
$action = New-ScheduledTaskAction -Execute "powershell.exe" -Argument "-NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File `"$script`""
$logon = New-ScheduledTaskTrigger -AtLogOn -User $env:USERNAME
$logon.Delay = "PT20M"
$logon.Repetition = (New-ScheduledTaskTrigger -Once -At (Get-Date) -RepetitionInterval (New-TimeSpan -Hours 12)).Repetition
$settings = New-ScheduledTaskSettingsSet -StartWhenAvailable -ExecutionTimeLimit (New-TimeSpan -Hours 2) -MultipleInstances IgnoreNew
Register-ScheduledTask -TaskName $taskName -Action $action -Trigger $logon -Settings $settings -Force | Out-Null
Write-Host "registered task '$taskName' (logon +20min, every 12h) -> $script"
```

- [ ] **Step 4: Write LICENSE, CLAUDE.md, CHANGELOG.md**

`LICENSE`: the MIT license text with `Copyright (c) 2026 Dimas Diaz`.

`CLAUDE.md`:
```markdown
@AGENTS.md
```

`CHANGELOG.md`:
```markdown
# Changelog

## 0.1.0 - 2026-09-25

First release: SQLite warehouse, Fidelity positions/history CSV adapters, SnapTrade JSON adapter,
Tiingo/yfinance prices, SEC EDGAR company facts with derived statements, `fdc` CLI, read-only MCP
server for Claude Desktop, Windows scheduler scripts.
```

- [ ] **Step 5: Write AGENTS.md**

`AGENTS.md`:
```markdown
# AGENTS.md — financial-data-collector

Guidance for any coding agent working in this repo. `CLAUDE.md` points here.

## What this is

A local SQLite warehouse (`data/warehouse.db`) for brokerage positions, cash, transactions,
daily prices and full as-reported SEC financial statements, filled by `fdc sync`. Other
projects consume the database; this one owns collection and storage. Design spec:
`docs/superpowers/specs/2026-09-25-financial-data-collector-design.md`. Implementation plan:
`docs/superpowers/plans/2026-09-25-financial-data-collector.md`.

## Rules

- **Privacy.** `data/`, `inbox/`, `config.toml`, `.env`, `watchlist.txt` are gitignored and must stay so.
  Fixtures are synthetic: invented account names, invented quantities, widely held symbols only.
  Never paste a real balance, label or credential into code, tests, docs or commit messages.
  `tests/test_privacy.py` is the gate.
- **Store owns SQL.** Only `store.py` (and `migrate.py`, `readonly.py`) execute SQL. Adapters and
  collectors produce dataclasses from `models.py`.
- **Schema changes are migrations.** Add `migrations/NNNN_name.sql`; never edit an applied file.
  New statement line items are seed edits in `seeds/concept_map.csv`, not code.
- **No network in tests.** Collectors take `fetch` (and `yf`, `sleep`) as parameters.
- **Idempotent steps.** Re-running `fdc sync` with nothing new must not change data rows.
- **New feeds live here**, not in consumer projects.

## Layout

```
src/financial_data_collector/
  cli.py            fdc init | sync | import | status | query | mcp
  sync.py           runs steps ingest -> prices -> sec, logs sync_runs, exit codes
  ingest.py         inbox routing by file signature; SnapTrade folder walk; hash-based idempotency
  adapters/         fidelity_positions.py, fidelity_history.py, snaptrade.py -> models.Snapshot / TransactionRow
  collectors/       prices.py (Tiingo|yfinance), sec_cik.py, sec_facts.py (EDGAR company facts)
  statements.py     facts + concept_map -> financial_line_items (annual, quarter, YTD differencing, derived Q4)
  store.py          Store: every write; migrate.py: migrations, seed, generated wide views
  readonly.py       SQL guard + mode=ro connection; mcp_server.py: schema/query/status tools
  migrations/*.sql  0001 tables, 0002 views;  seeds/concept_map.csv
```

## Tables and views (start here when querying)

- `positions_latest`, `holdings_history`, `account_values_daily`, `portfolio_daily` (household total plus `fidelity_total`)
- `prices` (symbol, date, close, adj_close, dividend, split_factor)
- `transactions` (typed: buy, sell, dividend, reinvest, contribution, withdrawal, interest, fee, transfer, other)
- `financials_annual`, `financials_quarterly` (one wide row per company per period; `is_derived_q4` marks computed quarters)
- `sec_facts` (raw XBRL, every filing's value for every period), `financial_line_items` (shaped), `concept_map`
- `sync_status` / `sync_runs`, `ingested_files`, `securities`, `accounts`

More examples: `docs/QUERIES.md`.

## Commands

```
.venv\Scripts\python -m pytest            # all tests, no network
fdc sync --dry-run                        # list steps
fdc sync --only prices                    # one step
fdc query "select * from sync_status"
```
```

- [ ] **Step 6: Write docs/QUERIES.md**

`docs/QUERIES.md`:
```markdown
# Query cookbook

Run any of these with `fdc query "<sql>"`, in Datasette, in pandas (`pd.read_sql`), or through the
Claude Desktop `query` tool. Dates are ISO text, so string comparison works.

## Portfolio

```sql
-- household value by day (and Fidelity-only)
SELECT as_of_date, total, fidelity_total FROM portfolio_daily ORDER BY as_of_date;

-- what I hold right now, by account
SELECT account, symbol, quantity, market_value, unrealized_pnl FROM positions_latest ORDER BY account, market_value DESC;

-- one symbol's exposure over time, all accounts combined
SELECT as_of_date, quantity, market_value FROM holdings_history WHERE symbol = 'AAPL' ORDER BY as_of_date;

-- month-end exposure per symbol
SELECT substr(as_of_date, 1, 7) AS month, symbol, market_value
FROM holdings_history h
WHERE as_of_date = (SELECT MAX(as_of_date) FROM holdings_history h2 WHERE substr(h2.as_of_date,1,7) = substr(h.as_of_date,1,7))
ORDER BY month, market_value DESC;
```

## Cash flows

```sql
-- contributions per month
SELECT substr(trade_date, 1, 7) AS month, SUM(amount) AS contributed
FROM transactions WHERE type = 'contribution' GROUP BY 1 ORDER BY 1;

-- dividends received per symbol per year
SELECT substr(trade_date, 1, 4) AS year, symbol, SUM(amount) AS dividends
FROM transactions WHERE type = 'dividend' GROUP BY 1, 2 ORDER BY 1, 3 DESC;
```

## Prices

```sql
-- total return over the stored history (adjusted close)
SELECT symbol, MIN(date) AS first, MAX(date) AS last,
       ROUND((MAX(CASE WHEN date = (SELECT MAX(date) FROM prices p2 WHERE p2.symbol = p.symbol) THEN adj_close END)
             / MAX(CASE WHEN date = (SELECT MIN(date) FROM prices p2 WHERE p2.symbol = p.symbol) THEN adj_close END) - 1) * 100, 1) AS pct
FROM prices p GROUP BY symbol ORDER BY pct DESC;
```

## Financial statements

```sql
-- annual income statement summary
SELECT symbol, fiscal_year, revenue, gross_margin, operating_margin, net_income, eps_diluted, fcf
FROM financials_annual WHERE symbol = 'AAPL' ORDER BY fiscal_year;

-- quarterly revenue and cash flow, marking computed fourth quarters
SELECT symbol, period_end, fiscal_quarter, revenue, ocf, is_derived_q4
FROM financials_quarterly WHERE symbol = 'AAPL' ORDER BY period_end;

-- what a company reported for a period at the time, and how it was restated later
SELECT accn, filed, form, value FROM sec_facts
WHERE cik = (SELECT cik FROM securities WHERE symbol = 'AAPL')
  AND concept = 'Revenues' AND period_start = '2024-01-01' AND period_end = '2024-12-31'
ORDER BY filed;

-- my exposure next to the company's quarterly revenue
SELECT q.period_end, q.revenue, h.market_value
FROM financials_quarterly q
JOIN holdings_history h ON h.symbol = q.symbol
 AND h.as_of_date = (SELECT MAX(as_of_date) FROM holdings_history h2 WHERE h2.symbol = q.symbol AND h2.as_of_date <= q.period_end)
WHERE q.symbol = 'AAPL' ORDER BY q.period_end;
```

## Health

```sql
SELECT * FROM sync_status;
SELECT * FROM sync_runs ORDER BY id DESC LIMIT 20;
```
```

- [ ] **Step 7: Write README.md**

`README.md`:
```markdown
# financial-data-collector

A free, local SQLite warehouse for your brokerage positions. One command collects what you hold
each day, what it is worth, every transaction, daily prices, and the full as-reported financial
statements of every company you own, so you can analyze it with SQL, pandas, or Claude.

- **Free.** SQLite (in Python), SEC EDGAR (no key), yfinance (no key) or Tiingo (free key).
- **Local.** One file, `data/warehouse.db`. Nothing leaves your machine.
- **Yours.** Drop a Fidelity "Portfolio Positions" export in `inbox/` and run `fdc sync`.
- **History that accumulates.** Positions are stored per day; SEC facts keep every filing's value, so
  you can see what a company reported at the time and how it was restated.
- **Claude-ready.** A read-only MCP server lets Claude Desktop query the database directly.

## Quick start

```bash
git clone https://github.com/Dimas-100/financial-data-collector.git
cd financial-data-collector
python -m venv .venv
.venv\Scripts\activate            # macOS/Linux: source .venv/bin/activate
pip install -e ".[dev]"
fdc init
```

Then:

1. Open `.env` and set `SEC_USER_AGENT=Your Name you@example.com` (SEC requires it). Optionally
   add a free Tiingo token; without one, prices come from yfinance.
2. On fidelity.com: Accounts, Positions, Download. Save the CSV into `inbox/`.
   Optional: Accounts, Activity and Orders, Download, for transaction history. If that file has no
   `Account` column, import it explicitly: `fdc import path\to\History.csv --account "My Brokerage"`.
3. Run `fdc sync`. The first run pulls five years of prices and every SEC filing for your companies.
   Later runs are incremental. Run it whenever you like, or schedule it.

```bash
fdc status                                            # last run per step, staleness, row counts
fdc query "select * from portfolio_daily order by 1 desc limit 10"
fdc query --csv "select * from financials_annual where symbol='AAPL'" > aapl.csv
```

## What gets stored

| Table / view | What it holds |
|---|---|
| `position_snapshots`, `cash_balances` | one row per day, account and symbol; cash separately |
| `transactions` | buys, sells, dividends, reinvestments, contributions, withdrawals, fees; de-duplicated across re-imports |
| `prices` | daily close, adjusted close, dividends and splits per symbol |
| `sec_facts` | every XBRL fact each held company ever filed (all forms, all periods) |
| `financial_line_items` | those facts shaped into income statement, balance sheet and cash-flow line items, annual and quarterly |
| `financials_annual`, `financials_quarterly` | one wide row per company per period, with margins and free cash flow |
| `positions_latest`, `holdings_history`, `account_values_daily`, `portfolio_daily` | the shapes most questions start from |
| `sync_runs`, `sync_status` | what ran, when, and whether it worked |

Quarterly cash-flow items are computed from year-to-date filings where companies only report
year-to-date; fourth quarters are derived from the full year. Such rows are flagged `is_derived_q4`.
Funds (ETFs, mutual funds) get positions and prices only; they file no statements.

See `docs/QUERIES.md` for a cookbook.

## Claude Desktop

```bash
fdc mcp --print-config
```

Paste the printed block into `claude_desktop_config.json` (Claude Desktop: Settings, Developer,
Edit Config), restart Claude Desktop, and ask it anything about your portfolio. The server is
read-only: it opens the database in read-only mode and refuses anything but SELECT.

Claude Code needs no setup: the database is a file, and `fdc query` works from the terminal.
`AGENTS.md` tells coding agents where everything is.

## Scheduling (Windows)

```powershell
.\scripts\schedule_sync.ps1          # at logon (+20 min) and every 12 hours
.\scripts\schedule_sync.ps1 -Remove
```

Logs go to `%LOCALAPPDATA%\financial-data-collector\sync.log`. On macOS/Linux use cron or launchd
to run `fdc --root /path/to/repo sync`.

## Other brokers and sources

The universal input is Fidelity's positions CSV. A second adapter ingests the JSON produced by a
SnapTrade-based export (see `config.example.toml`, section `sources.snaptrade`); it is skipped when
the folder does not exist. Adding a broker means writing one adapter that returns
`models.Snapshot`; see `src/financial_data_collector/adapters/`.

## Privacy

`data/`, `inbox/`, `config.toml`, `.env` and `watchlist.txt` are gitignored. The test suite fails if
git ever tracks a database, a CSV outside the synthetic fixtures, or an env file. Account numbers in
Fidelity exports are discarded on import and never stored.

## Development

```bash
.venv\Scripts\python -m pytest       # fast, offline
```

MIT licensed. Built for one person's Fidelity book first; issues and adapters for other brokers are welcome.
```

- [ ] **Step 8: Update the spec's layout section**

In `docs/superpowers/specs/2026-09-25-financial-data-collector-design.md` §12, replace the layout block with the real one (migrations/seeds under `src/financial_data_collector/`, plus `models.py`, `ingest.py`, `sync.py`, `readonly.py`), and add one line under §3 decisions: "Migrations and seeds ship inside the package so an installed copy is self-contained (plan deviation, 2026-09-25)."

- [ ] **Step 9: Run the full suite**

Run: `.venv/Scripts/python -m pytest`
Expected: all passed, including `tests/test_privacy.py` (3 tests).

- [ ] **Step 10: Commit**

```bash
git add -A
git commit -m "Add docs, examples, scheduler scripts and privacy guard"
```

---

### Task 17: First real run, scheduling, Claude Desktop, GitHub

**Files:**
- Create (gitignored, owner's machine only): `config.toml`, `.env`
- Modify (outside repo): `%APPDATA%\Claude\claude_desktop_config.json` (backed up first)

**Interfaces:** none; this task verifies spec §13 success criteria 1, 2 and 5 and publishes.

- [ ] **Step 1: Initialise and configure**

```bash
.venv/Scripts/python -m financial_data_collector --root . init
```
Then write `.env` (never printed): `SEC_USER_AGENT` from `git config user.name` + `user.email`; `TIINGO_API_TOKEN` copied from `../webull/.env` (the token investing's pipeline already uses; copying between two gitignored local files). Leave `config.toml` at defaults: the SnapTrade dir `../investing/fidelity/dashboard-data` exists on this machine.

- [ ] **Step 2: First sync and verification**

```bash
.venv/Scripts/python -m financial_data_collector --root . sync
.venv/Scripts/python -m financial_data_collector --root . status
```
Expected: `ingest ok` with the 104 snapshots plus live file and activity, `prices ok`, `sec ok`. Then verify criterion 2 without printing balances: compute the household total from `../investing/fidelity/dashboard-data/live-positions.json` (sum of `accounts[].totalMarketValue`) and compare to `portfolio_daily.total` for that `fetchedAt` date inside a Python one-liner that prints only `MATCH` / `MISMATCH <abs diff>`. Then run `sync` a second time and confirm `store.counts()` for data tables is unchanged (print only the boolean).

Also import the frozen CSV archive: `fdc import ../investing/fidelity/transactions/History_Brokerage_2024.csv --account "Individual - TOD"` (and the other five, Roth files with `--account "ROTH IRA"`), then `fdc import ../investing/fidelity/positions`.

- [ ] **Step 3: Schedule**

```powershell
.\scripts\schedule_sync.ps1
Get-ScheduledTask -TaskName "FinancialDataCollector Sync" | Select State
```

- [ ] **Step 4: Claude Desktop**

```bash
.venv/Scripts/python -m financial_data_collector --root . mcp --print-config
```
Back up `%APPDATA%\Claude\claude_desktop_config.json` to `claude_desktop_config.json.bak-2026-09-25`, merge the `financial-data-collector` entry into its `mcpServers` object with a small Python script (json load, update, dump with indent 2), and print the resulting server names.

- [ ] **Step 5: Publish**

Precondition: `.venv/Scripts/python -m pytest tests/test_privacy.py` passes and `git status` is clean.

```bash
gh repo create Dimas-100/financial-data-collector --public --source . --description "Free, local SQLite warehouse for your brokerage positions, prices and SEC financial statements" --push
```
Expected: repo URL printed; `git remote -v` shows origin.

- [ ] **Step 6: Record**

Append to `CHANGELOG.md` nothing (0.1.0 already covers it). Save a project memory (outside the repo) noting: project role in the warehouse, PC-only decision, scheduled task name, log path, and that finance-tracker retirement is a pending follow-up.

---

## Self-review notes (filled in after writing)

- Spec coverage: §4 steps → Task 13; §5.1 tables → Task 4; §5.2 views → Tasks 4 and 12; §5.3 seed → Task 4; §5.4 rules → Task 11; §6 adapters → Tasks 5, 6, 7 (+ symbols Task 2); §7 collectors → Tasks 9, 10; §8 config/CLI → Tasks 8, 14; §9 Claude access → Tasks 14, 15, 16 (AGENTS.md/QUERIES.md); §10 scheduling/failure → Tasks 13, 16; §11 testing → every task; §12 layout → Task 16 spec update; §13 criteria → Task 17.
- Review Focus items 1–5 map to tests in Tasks 5 (`test_renamed_file_dates_from_footer`), 6 (`journal` row with `amount None`), 7 (`test_local_time_never_used`), 9 (`VTI` no bars → `ok`), 11 (`test_ytd_differencing_for_cash_flow`, `test_no_derived_q4_without_siblings`).
- Type consistency checked: `Store` method names used in Tasks 8–15 match Task 4; `Snapshot`/`TransactionRow` fields match Task 3; `collect_sec(..., rebuild=)` matches Task 10/11; `SyncReport.steps[*].step/status/rows/message` used by Task 14 matches Task 13.

