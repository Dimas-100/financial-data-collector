# Connections Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A person connects their brokerages (SnapTrade) and banks (SimpleFIN) with their own keys, runs `fdc sync`, and has their accounts, holdings, cash and debts in the warehouse.

**Architecture:** A new `connections/` package holds two read-only fetchers that return the same dataclasses the CSV adapters do, a key home that keeps keys in the operating system's key store (or `.env` when there is none), and a service layer the commands and the sync step call. The store learns to know an account by a hashed external key instead of its label. Nothing here places an order, and no key ever reaches the database, a log or a message.

**Tech Stack:** Python 3.11, sqlite3, requests, python-dotenv, rich, pytest; new dependency `keyring`.

**Spec:** `docs/superpowers/specs/2026-09-29-connections-design.md`

## Global Constraints

- Python `>=3.11`; dependencies stay `requests`, `python-dotenv`, `yfinance`, `mcp`, `rich`, plus `keyring>=25`.
- Only `store.py`, `migrate.py` and `readonly.py` execute SQL. Adapters, connections and collectors produce `models.py` dataclasses.
- Schema changes are migrations: `migrations/0006_connections.sql`; never edit an applied file.
- No network in tests: every fetcher takes `fetch` (and `post`, `now`) as parameters. Fixtures are synthetic: invented institutions, names and amounts; never a real balance, label or key.
- Re-running `fdc sync` with nothing new must not change data rows.
- One connection of each kind: `snaptrade`, `simplefin`.
- A key exists only in the key store (or `.env`) and in memory while a request is made. Never in `config.toml`, the database, `sync_runs`, a log or a message.
- No account number and no service id is stored; names are stripped of digits first.
- The collector gains no code that can place, change or cancel an order; every SnapTrade request is a signed GET to one of the read paths.
- Bank transactions are never requested (`balances-only=1`).
- A confirmed kind, a credit limit and a rate are what the person set: a sync never changes them.

## Review Focus

1. **The same real account from a CSV and from a connection** is two accounts and is counted twice. Expected: `fdc connect` says so when file accounts already exist (Task 9 tests it) and `docs/connections.md` says to use one or the other.
2. **An account that vanishes from the service** (closed, or its login expired) must not crash the sync or delete rows; its last balance stays. Task 9 tests a second run with one account missing.
3. **A balance the service can't give** (`null`, `""`, `"n/a"`) skips that account with a note and the others still update. Task 8 tests it.
4. **Two accounts with the same name at one institution** get labels `X` and `X 2` that stay attached to the same accounts on every sync, whatever order the service lists them in. Task 6 tests it.
5. **A key pasted with spaces or a line break around it** works: every typed value is stripped. Task 4 tests `parse`, Task 11 tests the prompt.

---

## File structure

| Path | Responsibility |
|---|---|
| `src/financial_data_collector/migrations/0006_connections.sql` | the `connections` table, the new account columns, `cash_balances.available` |
| `src/financial_data_collector/models.py` | `AccountRef` gains `external_key`, `origin`, `kind_confirmed`, `flows`; `CashRow` gains `available` |
| `src/financial_data_collector/connections/__init__.py` | package docstring only (no imports: `store.py` imports `names`) |
| `src/financial_data_collector/connections/base.py` | `ConnectionFailed`, `Fetched`, `Fetch`/`Post` types, `num`, `nonzero`, `unreachable` |
| `src/financial_data_collector/connections/names.py` | `clean_name`, `build_label`, `institution_code`, `match_kind`, `slug_for`, `external_key`, `KINDS` |
| `src/financial_data_collector/connections/redact.py` | `redact(text, secrets)` |
| `src/financial_data_collector/connections/keys.py` | `SnapTradeKeys`, `SimpleFinKey`, `parse`, `OsKeyStore`, `MemoryKeyStore`, `EnvFile`, `KeyHome` |
| `src/financial_data_collector/connections/snaptrade.py` | signing, the five read calls, mapping to `Fetched` |
| `src/financial_data_collector/connections/simplefin.py` | claim, one balances-only fetch, mapping to `Fetched` |
| `src/financial_data_collector/connections/service.py` | `connect_snaptrade`, `connect_simplefin`, `disconnect`, `overview`, `run` |
| `src/financial_data_collector/http.py` | gains `post` |
| `src/financial_data_collector/store.py` | accounts by `external_key`, `available`, connections rows, account settings |
| `src/financial_data_collector/config.py` | `[connections] min_hours`; example texts |
| `src/financial_data_collector/sync.py` | the `connections` step |
| `src/financial_data_collector/cli.py` | `connect`, `connections`, `disconnect`, `accounts`, `accounts set` |
| `tests/conftest.py` | keeps every test away from the real key store and the real environment |
| `tests/test_names.py`, `test_redact.py`, `test_keys.py`, `test_migration_0006.py`, `test_connections_store.py`, `test_snaptrade_connection.py`, `test_simplefin.py`, `test_connections_service.py` | new tests |
| `tests/test_http.py`, `test_sync.py`, `test_cli.py`, `test_privacy.py` | extended |
| `docs/connections.md`, `README.md`, `AGENTS.md`, `CHANGELOG.md`, `config.example.toml`, `.env.example` | docs |

Every command below runs from the worktree root with its own environment: `.venv/Scripts/python.exe -m pytest ...` (the tests) and `.venv/Scripts/fdc.exe` (the command). Commit messages follow the repo's style: a sentence, no prefix.

---

### Task 1: Amend the spec, add the dependency, extend the models, write the migration

**Files:**
- Modify: `docs/superpowers/specs/2026-09-29-connections-design.md`
- Modify: `pyproject.toml`
- Modify: `src/financial_data_collector/models.py`
- Create: `src/financial_data_collector/migrations/0006_connections.sql`
- Modify: `src/financial_data_collector/store.py` (only `COUNT_TABLES`)
- Test: `tests/test_migration_0006.py`

**Interfaces:**
- Produces: `AccountRef(label, slug, institution, account_type, external_key=None, origin="file", kind_confirmed=True, flows="transactions")`, `CashRow(account, amount, currency="USD", available=None)`; tables and columns of §7 of the spec.

- [ ] **Step 1: Amend the spec** (the build proceeded before the trial; these are the changes it needs). Apply these edits to `docs/superpowers/specs/2026-09-29-connections-design.md`:

  1. In §3, replace the first paragraph ("Nothing is built ... and confirms:") with:
     > The build went ahead on the shapes SnapTrade documents and on the shapes the owner's own exporters have read daily for months; what remains unproven is only the personal key's authentication. So the trial runs after the build, as its acceptance: by hand, in a separate root (`fdc --root <a test folder>`), with a real personal SnapTrade key and a real SimpleFIN token. `docs/connections.md` holds the checklist. It confirms:
     and replace the last paragraph with:
     > Findings go in Appendix A as facts about shapes and behaviour, never a value, a label or an id. If (1) fails, the SnapTrade fetcher also takes a key that has a registered user (`fdc connect snaptrade --with-user`, four values), which the owner's own key is; that hedge is built, and the personal-key promise is then revisited.
  2. In §5, replace the "Without a key store" bullet with:
     > **Without a key store** (a server with no desktop session), `fdc connect` writes the key to `.env` beside `config.toml` instead, says so, and the connection's `key_ref` is `env`. The variables are `FDC_SNAPTRADE_CLIENT_ID`, `FDC_SNAPTRADE_CONSUMER_KEY`, `FDC_SNAPTRADE_USER_ID`, `FDC_SNAPTRADE_USER_SECRET` and `FDC_SIMPLEFIN_ACCESS_URL`; the environment wins over the file. A key put there by hand is a connection too: the sync step records it.
  3. In §6.1, change "Calls, and only these: list accounts; per account its positions, its balances and its activity." to "**Calls, and only these:** list accounts; list brokerage connections (to name a login that needs repair); per account its positions, its balances and its activity." and add the bullet:
     > **An account with no balance row** gets one of 0, so the day is still a full statement of it and a sold-out account shows as empty rather than keeping yesterday's holdings.
  4. In §8's kind table, add the row `| crypto | crypto |` after the card row, and replace the last row with:
     > | nothing above | a SnapTrade account is `brokerage`; a SimpleFIN account is `credit_card` when its balance is below zero, else `other` |
  5. In §9, change the first bullet to: "`STEPS = ("connections", "ingest", "prices", "sec", "derive", "export")`; `connections` joins `NETWORK_STEPS`, but a `connections` step that was skipped for want of connections does not count toward the all-network-steps-failed exit status."
  6. In §10, replace the last row with `| No key store on this computer | the key is written to \`.env\` and \`fdc connect\` says \`no key store on this computer: the key is in .env instead\` |`.

- [ ] **Step 2: Add the dependency.** In `pyproject.toml`, in `dependencies`, add `"keyring>=25",` after `"rich>=13",`. Then run `.venv/Scripts/python.exe -m pip install -q -e ".[dev]"` and confirm `.venv/Scripts/python.exe -c "import keyring; print(keyring.__version__)"` prints a version.

- [ ] **Step 3: Write the failing migration test** — `tests/test_migration_0006.py`:

```python
"""A version-5 warehouse becomes version 6 with its rows and views intact."""
import shutil
import sqlite3
from pathlib import Path

from financial_data_collector import migrate
from financial_data_collector.store import Store


def _v5(tmp_path: Path) -> Path:
    older = tmp_path / "migrations"
    older.mkdir()
    for p in migrate.MIGRATIONS_DIR.glob("*.sql"):
        if not p.name.startswith("0006"):
            shutil.copy(p, older)
    db = tmp_path / "w.db"
    conn = sqlite3.connect(db)
    migrate.apply_migrations(conn, migrations_dir=older)
    conn.execute("INSERT INTO accounts (label, slug, institution, account_type, first_seen) "
                 "VALUES ('Sample Brokerage', 'brokerage', 'fidelity', 'brokerage', '2026-01-02')")
    conn.execute("INSERT INTO cash_balances (as_of_date, account_id, currency, amount, source) "
                 "VALUES ('2026-01-02', 1, 'USD', 12.5, 'fidelity_csv')")
    conn.commit()
    assert conn.execute("SELECT MAX(version) FROM schema_version").fetchone()[0] == 5
    conn.close()
    return db


def test_v5_becomes_v6_with_rows_intact(tmp_path: Path):
    db = _v5(tmp_path)
    s = Store.open(db)
    try:
        assert s.query("SELECT MAX(version) FROM schema_version")[0][0] == 6
        acct = s.query("SELECT * FROM accounts")[0]
        assert acct["label"] == "Sample Brokerage" and acct["external_key"] is None
        assert acct["origin"] == "file" and acct["kind_confirmed"] == 1 and acct["flows"] == "transactions"
        assert acct["credit_limit"] is None and acct["rate_pct"] is None
        cash = s.query("SELECT * FROM cash_balances")[0]
        assert cash["amount"] == 12.5 and cash["available"] is None
        assert s.query("SELECT total FROM account_values_daily")[0][0] == 12.5
        assert s.query("SELECT COUNT(*) FROM connections")[0][0] == 0
        assert "connections" in s.counts()
    finally:
        s.close()


def test_external_key_is_unique_only_where_set(tmp_path: Path):
    s = Store.open(tmp_path / "w.db")
    try:
        for label in ("A", "B"):
            s.conn.execute("INSERT INTO accounts (label, first_seen) VALUES (?, '2026-01-02')", (label,))
        s.conn.execute("INSERT INTO accounts (label, first_seen, external_key) VALUES ('C', '2026-01-02', 'k1')")
        s.conn.commit()
        try:
            s.conn.execute("INSERT INTO accounts (label, first_seen, external_key) VALUES ('D', '2026-01-02', 'k1')")
            assert False, "a second account with the same external_key was accepted"
        except sqlite3.IntegrityError:
            pass
    finally:
        s.close()
```

- [ ] **Step 4: Run it to see it fail.** `.venv/Scripts/python.exe -m pytest tests/test_migration_0006.py -q` — expected: FAIL (`MAX(version)` is 5; `no such column: external_key`).

- [ ] **Step 5: Write the migration** — `src/financial_data_collector/migrations/0006_connections.sql`:

```sql
-- 0006: connections to SnapTrade and SimpleFIN, and what an account needs to have come through one.

CREATE TABLE connections (
  name           TEXT PRIMARY KEY,               -- snaptrade | simplefin
  key_ref        TEXT NOT NULL,                  -- the key store entry's name, or 'env' when the key is in .env
  created_at     TEXT NOT NULL,
  removed_at     TEXT,
  last_fetch_at  TEXT,
  last_ok_at     TEXT,
  last_error     TEXT NOT NULL DEFAULT ''        -- redacted: never an address with a password, never a key
);

ALTER TABLE accounts ADD COLUMN external_key TEXT;                                 -- a hash; the identity of a connected account
ALTER TABLE accounts ADD COLUMN origin TEXT NOT NULL DEFAULT 'file';               -- file | snaptrade | simplefin
ALTER TABLE accounts ADD COLUMN kind_confirmed INTEGER NOT NULL DEFAULT 1;         -- 0 while account_type is a guess
ALTER TABLE accounts ADD COLUMN credit_limit REAL;                                 -- what the person entered
ALTER TABLE accounts ADD COLUMN rate_pct REAL;                                     -- yearly, in percent
ALTER TABLE accounts ADD COLUMN flows TEXT NOT NULL DEFAULT 'transactions';        -- balance: no transactions explain its changes
CREATE UNIQUE INDEX idx_accounts_external_key ON accounts (external_key) WHERE external_key IS NOT NULL;

ALTER TABLE cash_balances ADD COLUMN available REAL;                               -- only when the service reports one
```

- [ ] **Step 6: Extend the models.** In `src/financial_data_collector/models.py` replace the `AccountRef` and `CashRow` classes with:

```python
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
```

```python
@dataclass(frozen=True)
class CashRow:
    account: AccountRef
    amount: float
    currency: str = "USD"
    available: float | None = None   # a card's remaining credit, when the service reports it
```

- [ ] **Step 7: Count the new table.** In `src/financial_data_collector/store.py` change `COUNT_TABLES` to end with `..."reconciliation", "valuation_daily", "connections",`.

- [ ] **Step 8: Run the tests.** `.venv/Scripts/python.exe -m pytest -q` — expected: all pass (the migration test and the existing suite).

- [ ] **Step 9: Commit.**

```bash
git add docs/superpowers/specs/2026-09-29-connections-design.md pyproject.toml src/financial_data_collector/models.py src/financial_data_collector/migrations/0006_connections.sql src/financial_data_collector/store.py tests/test_migration_0006.py
git commit -m "Migration 0006: the connections table and what an account needs to have come through one"
```

---

### Task 2: Names, kinds and identity

**Files:**
- Create: `src/financial_data_collector/connections/__init__.py`
- Create: `src/financial_data_collector/connections/names.py`
- Test: `tests/test_names.py`

**Interfaces:**
- Produces: `clean_name(name) -> str`, `build_label(institution, name) -> str`, `institution_code(institution) -> str`, `match_kind(text) -> str | None`, `slug_for(kind) -> str`, `external_key(origin, service_id) -> str`, `KINDS: tuple[str, ...]`, `DEBT_KINDS: frozenset[str]`.

- [ ] **Step 1: Write the failing tests** — `tests/test_names.py`:

```python
import pytest

from financial_data_collector.connections import names


@pytest.mark.parametrize("raw, clean", [
    ("Checking ...1234", "Checking"), ("Visa x1234", "Visa"), ("Savings (1234)", "Savings"),
    ("Savings (...1234)", "Savings"), ("TOTAL CHECKING -1234", "TOTAL CHECKING"), ("Checking-1234", "Checking"),
    ("Card #1234", "Card"), ("Rewards ending in 1234", "Rewards"), ("Premier XXXX1234", "Premier"),
    ("Joint 000123456789", "Joint"), ("Sample Roth IRA (4321)", "Sample Roth IRA"), ("Everyday ...9876", "Everyday"),
    ("•••• 1234", "Account"), ("****1234", "Account"), ("x1234", "Account"), ("", "Account"), (None, "Account"),
    ("401k", "401k"), ("401(k) Plan", "401(k) Plan"), ("403(b)", "403(b)"), ("529 Plan", "529 Plan"),
    ("Flex1234", "Flex1234"), ("Account 12", "Account 12"), ("My 401 plan 4015", "My 401 plan"),
])
def test_digits_that_look_like_an_account_number_go(raw, clean):
    assert names.clean_name(raw) == clean


def test_label_is_institution_then_name_unless_the_name_already_starts_with_it():
    assert names.build_label("Example Bank", "Checking ...1234") == "Example Bank Checking"
    assert names.build_label("Example Bank", "Example Bank Savings") == "Example Bank Savings"
    assert names.build_label("", "Roth IRA") == "Roth IRA"
    assert names.build_label(None, "") == "Account"
    assert names.build_label("Example Bank", "x1234") == "Example Bank Account"
    assert len(names.build_label("A" * 60, "B" * 60)) <= names.MAX_LABEL


def test_institution_code_is_the_form_the_accounts_table_uses():
    assert names.institution_code("Fidelity") == "fidelity"
    assert names.institution_code("Example Bank & Trust") == "example_bank_trust"
    assert names.institution_code("") == "unknown"


@pytest.mark.parametrize("text, kind", [
    ("Home Mortgage", "mortgage"), ("HELOC", "line_of_credit"), ("Auto Loan", "loan"), ("Debit Card Checking", "checking"),
    ("Credit Union Savings", "savings"), ("Money Market Plus", "money_market"), ("Cash Rewards Visa", "credit_card"),
    ("Platinum Card", "credit_card"), ("Discover Savings", "savings"), ("Crypto", "crypto"), ("Roth IRA", "roth_ira"),
    ("Rollover IRA", "traditional_ira"), ("Traditional IRA", "traditional_ira"), ("My 401(k)", "401k"),
    ("Individual", "brokerage"), ("Investment Account", "brokerage"), ("Joint TOD", "brokerage"),
    ("Everyday", None), ("Miranda", None), ("", None), (None, None),
])
def test_kind_rules_top_to_bottom(text, kind):
    assert names.match_kind(text) == kind


def test_slug_and_key():
    assert names.slug_for("roth_ira") == "roth" and names.slug_for("brokerage") == "brokerage"
    assert names.slug_for("checking") == "other"
    k = names.external_key("simplefin", "ACT-1")
    assert len(k) == 64 and k == names.external_key("simplefin", "ACT-1") != names.external_key("snaptrade", "ACT-1")
    assert "ACT-1" not in k
    assert "credit_card" in names.KINDS and "other" in names.KINDS and names.DEBT_KINDS <= set(names.KINDS)
```

- [ ] **Step 2: Run them to see them fail.** `.venv/Scripts/python.exe -m pytest tests/test_names.py -q` — expected: FAIL with `ModuleNotFoundError`.

- [ ] **Step 3: Write the package and the module.** `src/financial_data_collector/connections/__init__.py`:

```python
"""Connections to the services a person links with their own key: SnapTrade (brokerages) and SimpleFIN (banks).

Read only. The fetchers return models.py dataclasses; the service layer writes them through the store. Nothing
in this package is imported by this __init__, because store.py imports names.py.
"""
```

`src/financial_data_collector/connections/names.py`:

```python
"""What an account that arrives through a connection is called, what kind it is, and how it is recognised again.

No SQL and no network. Every rule here is the one in the connections spec, section 8.
"""
from __future__ import annotations

import hashlib
import re

KINDS = ("brokerage", "roth_ira", "traditional_ira", "ira", "rollover_ira", "sep_ira", "simple_ira", "401k", "403b",
         "457b", "hsa", "pension", "crypto", "checking", "savings", "cash", "money_market", "credit_card", "loan",
         "mortgage", "line_of_credit", "other")
DEBT_KINDS = frozenset({"credit_card", "loan", "mortgage", "line_of_credit"})
MAX_LABEL = 80

# A run of three or more digits is taken for part of an account number, with the mask in front of it or the
# brackets around it. A run that is part of a word (401k) or names a plan (401(k), 403(b), 457(b), 529) stays.
_KEEP = r"(?:401|403|457|529)"
_MASK = r"(?:[.*#•…\-]+|x+)"
_ENDING = re.compile(rf"(?<![a-z0-9])ending(?:\s+in)?\s*{_MASK}?\s*\d{{3,}}(?![a-z0-9])", re.I)
_BRACKETED = re.compile(rf"[\(\[]\s*{_MASK}?\s*\d{{3,}}\s*[\)\]]", re.I)
_MASKED = re.compile(rf"(?<![a-z0-9]){_MASK}\s*\d{{3,}}(?![a-z0-9])", re.I)
_BARE = re.compile(rf"(?<![a-z0-9])(?!{_KEEP}(?!\d))\d{{3,}}(?![a-z0-9])", re.I)
_EDGES = " \t-–—.,:;#*•…/|("

# Top to bottom, first match wins; a word matches where a word starts, in any case. Checking and savings come
# before the card words, so "Debit Card Checking" is a checking account.
_RULES: tuple[tuple[tuple[str, ...], str], ...] = (
    (("mortgage",), "mortgage"),
    (("line of credit", "heloc"), "line_of_credit"),
    (("loan",), "loan"),
    (("checking",), "checking"),
    (("saving",), "savings"),
    (("money market",), "money_market"),
    (("credit", "card", "visa", "mastercard", "amex", "discover"), "credit_card"),
    (("crypto",), "crypto"),
    (("roth",), "roth_ira"),
    (("ira", "rollover"), "traditional_ira"),
    (("401",), "401k"),
    (("brokerage", "invest", "individual", "joint"), "brokerage"),
)


def clean_name(name: str | None) -> str:
    """The account's name without anything that looks like part of its number; "Account" when nothing is left."""
    text = str(name or "")
    for pattern in (_ENDING, _BRACKETED, _MASKED, _BARE):
        text = pattern.sub(" ", text)
    text = " ".join(text.split()).strip(_EDGES)
    return text or "Account"


def build_label(institution: str | None, name: str | None) -> str:
    """'Example Bank Checking': the institution, then the cleaned name, unless the name already starts with it."""
    inst = " ".join(str(institution or "").split())
    clean = clean_name(name)
    label = clean if not inst or clean.lower().startswith(inst.lower()) else f"{inst} {clean}"
    return label[:MAX_LABEL].rstrip(_EDGES) or "Account"


def institution_code(institution: str | None) -> str:
    """'Example Bank' -> 'example_bank': the form the accounts table already uses ('fidelity')."""
    code = re.sub(r"[^a-z0-9]+", "_", str(institution or "").lower()).strip("_")
    return code or "unknown"


def match_kind(text: str | None) -> str | None:
    """The kind the rules give for a name or a service's own kind text; None when no rule matches."""
    low = " ".join(str(text or "").lower().split())
    for words, kind in _RULES:
        for word in words:
            if re.search(rf"(?<![a-z0-9]){re.escape(word)}", low):
                return kind
    return None


def slug_for(kind: str) -> str:
    """The owner's cockpit groups: roth, brokerage, other."""
    return "roth" if kind == "roth_ira" else "brokerage" if kind == "brokerage" else "other"


def external_key(origin: str, service_id: str) -> str:
    """How the same account is recognised on the next sync. The service's own id is hashed, never stored."""
    return hashlib.sha256(f"{origin}:{service_id}".encode("utf-8")).hexdigest()
```

- [ ] **Step 4: Run the tests.** `.venv/Scripts/python.exe -m pytest tests/test_names.py -q` — expected: PASS.

- [ ] **Step 5: Commit.**

```bash
git add src/financial_data_collector/connections/__init__.py src/financial_data_collector/connections/names.py tests/test_names.py
git commit -m "Connections: what a connected account is called, what kind it is, and how it is recognised again"
```

---

### Task 3: Redaction and the shared base

**Files:**
- Create: `src/financial_data_collector/connections/redact.py`
- Create: `src/financial_data_collector/connections/base.py`
- Test: `tests/test_redact.py`

**Interfaces:**
- Produces: `redact(text, secrets=()) -> str`; `ConnectionFailed(Exception)`; `Fetched(accounts, snapshots, transactions, notes)`; `Fetch = Callable[[str, dict | None], bytes]`; `Post = Callable[[str, dict | None], tuple[int, bytes]]`; `num(value) -> float | None`; `nonzero(value) -> float | None`; `unreachable(host, HttpError) -> ConnectionFailed`.

- [ ] **Step 1: Write the failing tests** — `tests/test_redact.py`:

```python
from financial_data_collector.connections import base
from financial_data_collector.connections.redact import redact
from financial_data_collector.http import HttpError


def test_an_address_loses_its_user_name_and_password():
    text = "HTTP 403 for https://user:p%40ss@bridge.example.org/simplefin/accounts?x=1"
    assert redact(text) == "HTTP 403 for https://bridge.example.org/simplefin/accounts?x=1"


def test_every_form_of_a_secret_goes():
    out = redact("key abc+def/ghi= sent as clientId=abc%2Bdef%2Fghi%3D", ["abc+def/ghi="])
    assert out == "key *** sent as clientId=***"


def test_short_secrets_and_blanks_are_ignored_so_ordinary_words_survive():
    assert redact("the key is ab", ["ab", "", None]) == "the key is ab"


def test_longest_secret_first():
    assert redact("secret-longer secret", ["secret", "secret-longer"]) == "*** ***"


def test_num_and_nonzero():
    assert base.num("12.5") == 12.5 and base.num(None) is None and base.num("n/a") is None
    assert base.num(True) is None and base.num(float("nan")) is None
    assert base.nonzero(0) is None and base.nonzero("0.0") is None and base.nonzero("3") == 3.0


def test_unreachable_names_the_host_and_the_failure_type_only():
    e = HttpError(0, "https://x:secret@api.example.org/api/v1/accounts?clientId=k", "ConnectionError: boom for x")
    msg = str(base.unreachable("api.example.org", e))
    assert msg == "api.example.org couldn't be reached (ConnectionError)"
    assert "secret" not in msg and "clientId" not in msg
```

- [ ] **Step 2: Run them to see them fail.** `.venv/Scripts/python.exe -m pytest tests/test_redact.py -q` — expected: FAIL with `ModuleNotFoundError`.

- [ ] **Step 3: Write the modules.** `src/financial_data_collector/connections/redact.py`:

```python
"""The one place that makes a message safe to show: no address keeps its user name and password, and no secret's
text, in any of the forms it travels in, survives."""
from __future__ import annotations

import re
from typing import Iterable
from urllib.parse import quote, quote_plus

_USERINFO = re.compile(r"(?i)\b([a-z][a-z0-9+.\-]*://)[^/\s@]+@")
MIN_LENGTH = 4  # a shorter "secret" would blank ordinary words


def redact(text: object, secrets: Iterable[str | None] = ()) -> str:
    out = _USERINFO.sub(r"\1", str(text))
    kept = {s for s in secrets if s and len(s) >= MIN_LENGTH}
    for secret in sorted(kept, key=len, reverse=True):
        for form in {secret, quote(secret, safe=""), quote_plus(secret)}:
            out = out.replace(form, "***")
    return out
```

`src/financial_data_collector/connections/base.py`:

```python
"""What every connection shares: the failure type, the fetch result and small helpers. No SQL, no network."""
from __future__ import annotations

import math
import re
from dataclasses import dataclass, field
from typing import Callable

from ..http import HttpError
from ..models import AccountRef, Snapshot, TransactionRow

Fetch = Callable[[str, dict[str, str] | None], bytes]
Post = Callable[[str, dict[str, str] | None], tuple[int, bytes]]
_TYPE_NAME = re.compile(r"[A-Za-z]+")


class ConnectionFailed(Exception):
    """A connection couldn't be made or used. The message is safe to show as it is: it names a host or a service,
    never an address with a password in it and never a key."""


@dataclass
class Fetched:
    """What one fetch from a service produced, ready for the store."""
    accounts: list[AccountRef] = field(default_factory=list)
    snapshots: list[Snapshot] = field(default_factory=list)
    transactions: list[TransactionRow] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)


def num(value: object) -> float | None:
    """A number from what a service sent (a number, a numeric string); None for anything else, NaN included."""
    if value is None or isinstance(value, bool):
        return None
    try:
        f = float(value)
    except (TypeError, ValueError):
        return None
    return None if math.isnan(f) or math.isinf(f) else f


def nonzero(value: object) -> float | None:
    f = num(value)
    return None if not f else f


def unreachable(host: str, error: HttpError) -> ConnectionFailed:
    """A transport failure, worded without the address: http's message starts with the exception's type name."""
    cause = _TYPE_NAME.match(str(error))
    return ConnectionFailed(f"{host} couldn't be reached ({cause.group(0) if cause else 'no answer'})")
```

- [ ] **Step 4: Run the tests.** `.venv/Scripts/python.exe -m pytest tests/test_redact.py -q` — expected: PASS.

- [ ] **Step 5: Commit.**

```bash
git add src/financial_data_collector/connections/redact.py src/financial_data_collector/connections/base.py tests/test_redact.py
git commit -m "Connections: redaction, the failure type and the fetch result every connection shares"
```

---

### Task 4: The key home

**Files:**
- Create: `src/financial_data_collector/connections/keys.py`
- Modify: `tests/conftest.py`
- Test: `tests/test_keys.py`

**Interfaces:**
- Produces: `SnapTradeKeys(client_id, consumer_key, user_id=None, user_secret=None)`, `SimpleFinKey(access_url)`, both with `.values() -> dict[str, str]` and `.secrets() -> list[str]`; `parse(connection, values) -> Keys | None`; `KeyHome(store, env)` with `.save(connection, keys) -> key_ref`, `.load(connection, key_ref) -> Keys | None`, `.forget(connection, key_ref)`, `.in_env(connection) -> Keys | None`, `KeyHome.for_root(root)`; `MemoryKeyStore(broken=False)`; `EnvFile(path, environ=None)`; `ENV_REF = "env"`; `env_name(connection, field)`.

- [ ] **Step 1: Keep every test away from the real key store and environment.** Replace `tests/conftest.py` with:

```python
import os
from pathlib import Path

import pytest

FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture
def fixtures() -> Path:
    return FIXTURES


@pytest.fixture(autouse=True)
def no_real_key_store(monkeypatch):
    """No test ever touches this computer's key store or reads a real FDC_* variable: OsKeyStore becomes an
    in-memory one, and the connection variables are cleared."""
    from financial_data_collector.connections import keys as K

    fake = K.MemoryKeyStore()
    monkeypatch.setattr(K, "OsKeyStore", lambda backend=None: fake)
    for name in list(os.environ):
        if name.startswith("FDC_"):
            monkeypatch.delenv(name, raising=False)
    return fake
```

- [ ] **Step 2: Write the failing tests** — `tests/test_keys.py`:

```python
from pathlib import Path

from financial_data_collector.connections import keys as K


def test_parse_strips_and_requires():
    st = K.parse("snaptrade", {"client_id": " id \n", "consumer_key": "key ", "user_id": "", "user_secret": None})
    assert st == K.SnapTradeKeys("id", "key")
    assert st.values() == {"client_id": "id", "consumer_key": "key"} and st.secrets() == ["id", "key"]
    assert K.parse("snaptrade", {"client_id": "id"}) is None
    full = K.parse("snaptrade", {"client_id": "id", "consumer_key": "key", "user_id": "u", "user_secret": "s"})
    assert full.user_id == "u" and full.secrets() == ["id", "key", "u", "s"]
    sf = K.parse("simplefin", {"access_url": " https://u%40x:p%40ss@bridge.example.org/simplefin "})
    assert sf == K.SimpleFinKey("https://u%40x:p%40ss@bridge.example.org/simplefin")
    assert set(sf.secrets()) >= {sf.access_url, "u%40x", "p%40ss", "u@x", "p@ss"}
    assert K.parse("simplefin", {}) is None and K.parse("other", {"x": "y"}) is None


def test_env_file_reads_environment_over_file_and_writes_only_its_own_lines(tmp_path: Path):
    env = tmp_path / ".env"
    env.write_text("SEC_USER_AGENT=Sample Person s@example.com\nFDC_SIMPLEFIN_ACCESS_URL=https://a:b@h/x\n")
    f = K.EnvFile(env, environ={"FDC_SNAPTRADE_CLIENT_ID": "from-env"})
    assert f.read("simplefin") == {"access_url": "https://a:b@h/x"}
    assert f.read("snaptrade") == {"client_id": "from-env"}
    f.write("snaptrade", {"client_id": "id", "consumer_key": "key"})
    text = env.read_text()
    assert "SEC_USER_AGENT=Sample Person s@example.com" in text
    assert "FDC_SNAPTRADE_CLIENT_ID=id" in text and "FDC_SNAPTRADE_CONSUMER_KEY=key" in text
    f.write("snaptrade", {"client_id": "id2", "consumer_key": "key2"})
    assert env.read_text().count("FDC_SNAPTRADE_CLIENT_ID=") == 1 and "id2" in env.read_text()
    f.remove("snaptrade")
    assert "FDC_SNAPTRADE" not in env.read_text() and "SEC_USER_AGENT" in env.read_text()
    assert "FDC_SIMPLEFIN_ACCESS_URL" in env.read_text()


def test_key_home_saves_in_the_store_and_loads_back(tmp_path: Path):
    store = K.MemoryKeyStore()
    home = K.KeyHome(store, K.EnvFile(tmp_path / ".env", environ={}))
    ref = home.save("snaptrade", K.SnapTradeKeys("id", "key"))
    assert ref != K.ENV_REF and len(ref) == 32
    assert home.load("snaptrade", ref) == K.SnapTradeKeys("id", "key")
    assert not (tmp_path / ".env").exists()
    home.forget("snaptrade", ref)
    assert home.load("snaptrade", ref) is None and store.entries == {}


def test_two_warehouses_never_share_an_entry(tmp_path: Path):
    store = K.MemoryKeyStore()
    a = K.KeyHome(store, K.EnvFile(tmp_path / "a.env", environ={}))
    b = K.KeyHome(store, K.EnvFile(tmp_path / "b.env", environ={}))
    ra = a.save("simplefin", K.SimpleFinKey("https://a:a@h/a"))
    rb = b.save("simplefin", K.SimpleFinKey("https://b:b@h/b"))
    assert ra != rb and len(store.entries) == 2
    assert a.load("simplefin", ra).access_url.endswith("/a") and b.load("simplefin", rb).access_url.endswith("/b")


def test_without_a_key_store_the_key_goes_to_env(tmp_path: Path):
    env = K.EnvFile(tmp_path / ".env", environ={})
    home = K.KeyHome(K.MemoryKeyStore(broken=True), env)
    ref = home.save("simplefin", K.SimpleFinKey("https://u:p@h/x"))
    assert ref == K.ENV_REF
    assert "FDC_SIMPLEFIN_ACCESS_URL=https://u:p@h/x" in (tmp_path / ".env").read_text()
    assert home.load("simplefin", ref) == K.SimpleFinKey("https://u:p@h/x")
    home.forget("simplefin", ref)
    assert home.load("simplefin", ref) is None


def test_a_key_put_in_env_by_hand_is_found(tmp_path: Path):
    home = K.KeyHome(K.MemoryKeyStore(), K.EnvFile(tmp_path / ".env", environ={"FDC_SIMPLEFIN_ACCESS_URL": "https://u:p@h/x"}))
    assert home.in_env("simplefin") == K.SimpleFinKey("https://u:p@h/x")
    assert home.in_env("snaptrade") is None
    assert home.load("simplefin", "missing-ref") == K.SimpleFinKey("https://u:p@h/x")


def test_the_os_key_store_turns_every_failure_into_no_key_store():
    class Backend:
        def get_password(self, service, ref):
            raise RuntimeError("the secret is s3cr3t")

        def set_password(self, service, ref, secret):
            raise RuntimeError("nope")

        def delete_password(self, service, ref):
            raise RuntimeError("nope")

    store = K.OsKeyStore(Backend())
    for call in (lambda: store.get("r"), lambda: store.set("r", "v")):
        try:
            call()
            assert False
        except K.NoKeyStore as e:
            assert "s3cr3t" not in str(e)
    store.delete("r")  # never raises


def test_for_root_uses_the_patched_store_in_tests(tmp_path: Path, no_real_key_store):
    home = K.KeyHome.for_root(tmp_path)
    ref = home.save("snaptrade", K.SnapTradeKeys("id", "key"))
    assert ref in no_real_key_store.entries
```

- [ ] **Step 3: Run them to see them fail.** `.venv/Scripts/python.exe -m pytest tests/test_keys.py -q` — expected: FAIL with `ModuleNotFoundError` (the conftest import fails first: that is the same failure).

- [ ] **Step 4: Write the module** — `src/financial_data_collector/connections/keys.py`:

```python
"""Where a connection's key is kept.

In the operating system's key store (Windows Credential Manager, the macOS Keychain, the Linux Secret Service)
through keyring: one entry per connection per warehouse, named by a random reference that the connections table
holds. On a computer with no key store, in .env beside config.toml, written by fdc connect. Never anywhere else.
"""
from __future__ import annotations

import json
import os
import secrets
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping, Protocol
from urllib.parse import unquote, urlsplit

from dotenv import dotenv_values

SERVICE = "financial-data-collector"
ENV_REF = "env"  # the key_ref of a connection whose key lives in .env
FIELDS = {"snaptrade": ("client_id", "consumer_key", "user_id", "user_secret"), "simplefin": ("access_url",)}
REQUIRED = {"snaptrade": ("client_id", "consumer_key"), "simplefin": ("access_url",)}


def env_name(connection: str, field: str) -> str:
    return f"FDC_{connection}_{field}".upper()


@dataclass(frozen=True)
class SnapTradeKeys:
    client_id: str
    consumer_key: str
    user_id: str | None = None      # only for a key that has a registered user; a personal key has none
    user_secret: str | None = None

    def values(self) -> dict[str, str]:
        pairs = (("client_id", self.client_id), ("consumer_key", self.consumer_key),
                 ("user_id", self.user_id), ("user_secret", self.user_secret))
        return {k: v for k, v in pairs if v}

    def secrets(self) -> list[str]:
        return list(self.values().values())


@dataclass(frozen=True)
class SimpleFinKey:
    access_url: str  # https://user:password@host/path — the whole address is the secret

    def values(self) -> dict[str, str]:
        return {"access_url": self.access_url}

    def secrets(self) -> list[str]:
        parts = urlsplit(self.access_url)
        raw = [parts.username or "", parts.password or ""]
        return [s for s in [self.access_url, *raw, *(unquote(x) for x in raw)] if s]


Keys = SnapTradeKeys | SimpleFinKey


def parse(connection: str, values: Mapping[str, str | None]) -> Keys | None:
    """The keys in a saved or environment mapping, stripped; None when a required value is missing or blank."""
    clean = {k: str(values.get(k) or "").strip() for k in FIELDS.get(connection, ())}
    if connection not in REQUIRED or any(not clean[k] for k in REQUIRED[connection]):
        return None
    if connection == "snaptrade":
        return SnapTradeKeys(clean["client_id"], clean["consumer_key"], clean["user_id"] or None,
                             clean["user_secret"] or None)
    return SimpleFinKey(clean["access_url"])


class NoKeyStore(Exception):
    """This computer has no key store a program can use."""


class KeyStore(Protocol):
    def get(self, ref: str) -> str | None: ...
    def set(self, ref: str, secret: str) -> None: ...
    def delete(self, ref: str) -> None: ...


class OsKeyStore:
    """The operating system's key store, through keyring. Every failure becomes NoKeyStore, named by its type
    only: keyring's own messages say nothing a person can act on, and none of them belongs in a log."""

    def __init__(self, backend=None):
        self._backend = backend

    def _keyring(self):
        if self._backend is None:
            try:
                import keyring
            except ImportError:
                raise NoKeyStore("keyring is not installed") from None
            self._backend = keyring
        return self._backend

    def get(self, ref: str) -> str | None:
        try:
            return self._keyring().get_password(SERVICE, ref)
        except NoKeyStore:
            raise
        except Exception as e:
            raise NoKeyStore(type(e).__name__) from None

    def set(self, ref: str, secret: str) -> None:
        try:
            self._keyring().set_password(SERVICE, ref, secret)
        except NoKeyStore:
            raise
        except Exception as e:
            raise NoKeyStore(type(e).__name__) from None

    def delete(self, ref: str) -> None:
        try:
            self._keyring().delete_password(SERVICE, ref)
        except Exception:
            pass  # gone already, or no key store: either way nothing is left to delete


class MemoryKeyStore:
    """For tests, and for the fixture that keeps every test away from the real key store."""

    def __init__(self, broken: bool = False):
        self.entries: dict[str, str] = {}
        self.broken = broken

    def get(self, ref: str) -> str | None:
        if self.broken:
            raise NoKeyStore("broken")
        return self.entries.get(ref)

    def set(self, ref: str, secret: str) -> None:
        if self.broken:
            raise NoKeyStore("broken")
        self.entries[ref] = secret

    def delete(self, ref: str) -> None:
        self.entries.pop(ref, None)


class EnvFile:
    """The FDC_* variables: the environment first, then .env. Written only when there is no key store."""

    def __init__(self, path: Path, environ: Mapping[str, str] | None = None):
        self.path = Path(path)
        self.environ: Mapping[str, str] = os.environ if environ is None else environ

    def _marker(self, connection: str) -> str:
        return f"# {connection} connection, written by fdc connect (no key store on this computer)"

    def _file_values(self) -> dict[str, str]:
        if not self.path.is_file():
            return {}
        return {k: v for k, v in dotenv_values(self.path).items() if v}

    def read(self, connection: str) -> dict[str, str]:
        file_values = self._file_values()
        out: dict[str, str] = {}
        for field in FIELDS.get(connection, ()):
            name = env_name(connection, field)
            value = str(self.environ.get(name) or file_values.get(name) or "").strip()
            if value:
                out[field] = value
        return out

    def write(self, connection: str, values: Mapping[str, str]) -> None:
        names = {env_name(connection, f): values.get(f, "") for f in FIELDS.get(connection, ())}
        lines = self.path.read_text(encoding="utf-8").splitlines() if self.path.is_file() else []
        kept = [ln for ln in lines if ln.split("=", 1)[0].strip() not in names and ln != self._marker(connection)]
        kept.append(self._marker(connection))
        kept += [f"{name}={value}" for name, value in names.items() if value]
        self.path.write_text("\n".join(kept) + "\n", encoding="utf-8")

    def remove(self, connection: str) -> None:
        if not self.path.is_file():
            return
        names = {env_name(connection, f) for f in FIELDS.get(connection, ())}
        lines = self.path.read_text(encoding="utf-8").splitlines()
        kept = [ln for ln in lines if ln.split("=", 1)[0].strip() not in names and ln != self._marker(connection)]
        self.path.write_text("\n".join(kept) + ("\n" if kept else ""), encoding="utf-8")


class KeyHome:
    """Saves, loads and forgets a connection's key: the key store when it works, .env when it doesn't."""

    def __init__(self, store: KeyStore | None, env: EnvFile):
        self.store = store
        self.env = env

    @classmethod
    def for_root(cls, root: Path) -> "KeyHome":
        return cls(OsKeyStore(), EnvFile(Path(root) / ".env"))

    def save(self, connection: str, keys: Keys) -> str:
        """Returns the key_ref for the connections table: a random reference, or ENV_REF when .env had to be used."""
        if self.store is not None:
            ref = secrets.token_hex(16)
            try:
                self.store.set(ref, json.dumps(keys.values()))
                return ref
            except NoKeyStore:
                pass
        self.env.write(connection, keys.values())
        return ENV_REF

    def load(self, connection: str, key_ref: str) -> Keys | None:
        if key_ref != ENV_REF and self.store is not None:
            try:
                raw = self.store.get(key_ref)
            except NoKeyStore:
                raw = None
            if raw:
                try:
                    return parse(connection, json.loads(raw))
                except ValueError:
                    return None
        return self.in_env(connection)

    def forget(self, connection: str, key_ref: str) -> None:
        if key_ref == ENV_REF:
            self.env.remove(connection)
        elif self.store is not None:
            self.store.delete(key_ref)

    def in_env(self, connection: str) -> Keys | None:
        return parse(connection, self.env.read(connection))
```

- [ ] **Step 5: Run the tests.** `.venv/Scripts/python.exe -m pytest tests/test_keys.py -q` — expected: PASS. Then the whole suite: `.venv/Scripts/python.exe -m pytest -q` — expected: PASS (the new conftest fixture must not break anything).

- [ ] **Step 6: Commit.**

```bash
git add src/financial_data_collector/connections/keys.py tests/conftest.py tests/test_keys.py
git commit -m "Connections: keys live in the key store, or in .env on a computer without one"
```

---

### Task 5: One POST, never retried

**Files:**
- Modify: `src/financial_data_collector/http.py`
- Test: `tests/test_http.py`

**Interfaces:**
- Produces: `post(url, headers=None, *, timeout=30) -> tuple[int, bytes]`.

- [ ] **Step 1: Write the failing tests.** Append to `tests/test_http.py`:

```python
def test_post_returns_status_and_body_without_retrying(monkeypatch):
    calls = []

    def fake_post(url, headers=None, data=None, timeout=None, allow_redirects=None):
        calls.append((url, headers, data, timeout, allow_redirects))
        return _Resp(403, b"used")

    monkeypatch.setattr(http.requests, "post", fake_post)
    assert http.post("https://x/claim/t", {"Content-Length": "0"}) == (403, b"used")
    assert len(calls) == 1 and calls[0][2] == b"" and calls[0][3] == 30 and calls[0][4] is False


def test_post_transport_failure_names_only_the_type(monkeypatch):
    import requests as rq

    def boom(url, headers=None, data=None, timeout=None, allow_redirects=None):
        raise rq.ConnectionError("secret-in-url https://t0k3n@x")

    monkeypatch.setattr(http.requests, "post", boom)
    with pytest.raises(http.HttpError) as e:
        http.post("https://x/claim/t")
    assert e.value.status == 0 and str(e.value).startswith("ConnectionError for ") and "t0k3n" not in str(e.value).split(" for ")[0]
```

- [ ] **Step 2: Run them to see them fail.** `.venv/Scripts/python.exe -m pytest tests/test_http.py -q` — expected: FAIL with `AttributeError: module ... has no attribute 'post'`.

- [ ] **Step 3: Add `post`.** Append to `src/financial_data_collector/http.py`:

```python
def post(url: str, headers: dict[str, str] | None = None, *, timeout: int = 30) -> tuple[int, bytes]:
    """One POST with an empty body and no retry: the status and the body, whatever the status. A SimpleFIN setup
    token is spent by the first attempt, so a second one would only report it used. The detail carries the
    failure's type only, never its text, which can repeat the address."""
    try:
        resp = requests.post(url, headers=headers, data=b"", timeout=timeout, allow_redirects=False)
    except requests.RequestException as e:
        raise HttpError(0, url, type(e).__name__) from None
    return resp.status_code, resp.content
```

- [ ] **Step 4: Run the tests.** `.venv/Scripts/python.exe -m pytest tests/test_http.py -q` — expected: PASS.

- [ ] **Step 5: Commit.**

```bash
git add src/financial_data_collector/http.py tests/test_http.py
git commit -m "http.post: one POST that is never retried, for a setup token that works once"
```

---

### Task 6: The store knows a connected account by its key

**Files:**
- Modify: `src/financial_data_collector/store.py`
- Test: `tests/test_connections_store.py`

**Interfaces:**
- Consumes: `AccountRef` fields from Task 1, `names.slug_for` from Task 2.
- Produces on `Store`: `connection(name) -> Row | None`, `connections(active_only=False) -> list[Row]`, `save_connection(name, key_ref, when)`, `mark_connection(name, *, fetched_at, ok, error="")`, `remove_connection(name, when) -> bool`, `accounts_of(origin) -> int`, `newest_activity_dates(origin) -> dict[str, str]`, `accounts_overview() -> list[Row]`, `set_account(label, *, kind=None, credit_limit=None, rate_pct=None) -> bool`. Rows are `sqlite3.Row`.

- [ ] **Step 1: Write the failing tests** — `tests/test_connections_store.py`:

```python
from pathlib import Path

import pytest

from financial_data_collector.models import AccountRef, CashRow, PositionRow, Snapshot, TransactionRow
from financial_data_collector.store import Store


@pytest.fixture
def store(tmp_path: Path) -> Store:
    s = Store.open(tmp_path / "w.db")
    yield s
    s.close()


def _bank(label: str, key: str, kind: str = "checking", confirmed: bool = False) -> AccountRef:
    return AccountRef(label, "other", "example_bank", kind, external_key=key, origin="simplefin",
                      kind_confirmed=confirmed, flows="balance")


def test_a_connected_account_is_known_by_its_key_and_its_label_is_fixed(store: Store):
    a = store.upsert_account(_bank("Example Bank Checking", "k1"), "2026-01-02")
    b = store.upsert_account(_bank("Example Bank Checking", "k2"), "2026-01-02")   # a second account, same name
    assert a != b
    labels = {r["external_key"]: r["label"] for r in store.query("SELECT external_key, label FROM accounts")}
    assert labels == {"k1": "Example Bank Checking", "k2": "Example Bank Checking 2"}
    # listed in the other order, with a renamed account: same rows, same labels
    assert store.upsert_account(_bank("Example Bank Everyday Checking", "k2"), "2026-01-03") == b
    assert store.upsert_account(_bank("Example Bank Checking", "k1"), "2026-01-03") == a
    assert {r["external_key"]: r["label"] for r in store.query("SELECT external_key, label FROM accounts")} == labels
    row = store.query("SELECT origin, flows, kind_confirmed, first_seen FROM accounts WHERE external_key = 'k1'")[0]
    assert (row["origin"], row["flows"], row["kind_confirmed"], row["first_seen"]) == ("simplefin", "balance", 0, "2026-01-02")


def test_a_file_account_with_the_same_label_keeps_it(store: Store):
    store.upsert_account(AccountRef("Sample Brokerage", "brokerage", "fidelity", "brokerage"), "2026-01-02")
    store.upsert_account(AccountRef("Sample Brokerage", external_key="k9", origin="snaptrade"), "2026-01-02")
    assert [r[0] for r in store.query("SELECT label FROM accounts ORDER BY id")] == ["Sample Brokerage", "Sample Brokerage 2"]


def test_a_guess_may_change_until_the_person_confirms(store: Store):
    store.upsert_account(_bank("Example Bank Everyday", "k1", "other"), "2026-01-02")
    store.upsert_account(_bank("Example Bank Everyday", "k1", "credit_card"), "2026-01-03")   # the balance went negative
    assert store.query("SELECT account_type FROM accounts")[0][0] == "credit_card"
    assert store.set_account("Example Bank Everyday", kind="checking")
    store.upsert_account(_bank("Example Bank Everyday", "k1", "credit_card"), "2026-01-04")
    row = store.query("SELECT account_type, kind_confirmed, slug FROM accounts")[0]
    assert (row[0], row[1], row[2]) == ("checking", 1, "other")
    # a kind the service itself supplied counts as confirmed too
    store.upsert_account(_bank("Example Broker Roth", "k2", "roth_ira", confirmed=True), "2026-01-02")
    store.upsert_account(_bank("Example Broker Roth", "k2", "other"), "2026-01-03")
    assert store.query("SELECT account_type, slug FROM accounts WHERE external_key = 'k2'")[0][0] == "roth_ira"


def test_limit_and_rate_survive_a_sync_and_bad_labels_are_reported(store: Store):
    store.upsert_account(_bank("Example Bank Visa", "k1", "credit_card"), "2026-01-02")
    assert store.set_account("Example Bank Visa", credit_limit=5000, rate_pct=24.9)
    store.upsert_account(_bank("Example Bank Visa", "k1", "credit_card"), "2026-01-03")
    row = store.query("SELECT credit_limit, rate_pct FROM accounts")[0]
    assert (row[0], row[1]) == (5000, 24.9)
    assert not store.set_account("Nobody", kind="checking")
    over = [dict(r) for r in store.accounts_overview()]
    assert over[0]["label"] == "Example Bank Visa" and over[0]["credit_limit"] == 5000 and over[0]["origin"] == "simplefin"


def test_cash_available_is_stored_and_a_debt_is_below_zero(store: Store):
    card = _bank("Example Bank Visa", "k1", "credit_card")
    store.write_snapshot(Snapshot("2026-01-02", "simplefin", [], [CashRow(card, -640.0, "USD", 4360.0)]))
    row = store.query("SELECT amount, available FROM cash_balances")[0]
    assert (row[0], row[1]) == (-640.0, 4360.0)
    assert store.query("SELECT total FROM account_values_daily")[0][0] == -640.0
    store.write_snapshot(Snapshot("2026-01-02", "simplefin", [], [CashRow(card, -600.0)]))
    assert [tuple(r) for r in store.query("SELECT amount, available FROM cash_balances")] == [(-600.0, None)]


def test_transactions_of_a_connected_account_are_keyed_by_its_stored_label(store: Store):
    ref = AccountRef("Example Broker Individual", "brokerage", "example_broker", "brokerage",
                     external_key="k1", origin="snaptrade")
    tx = TransactionRow(ref, "2026-01-05", "buy", "AAPL", 2.0, 100.0, -200.0, None, "BUY", "snaptrade")
    assert store.write_transactions([tx]) == 1
    renamed = AccountRef("Example Broker Renamed", "brokerage", "example_broker", "brokerage",
                         external_key="k1", origin="snaptrade")
    assert store.write_transactions([TransactionRow(renamed, "2026-01-05", "buy", "AAPL", 2.0, 100.0, -200.0, None,
                                                    "BUY", "snaptrade")]) == 0
    assert store.newest_activity_dates("snaptrade") == {"k1": "2026-01-05"}
    assert store.newest_activity_dates("simplefin") == {}


def test_connection_rows(store: Store):
    assert store.connection("snaptrade") is None and store.connections() == []
    store.save_connection("snaptrade", "ref1", "2026-01-02T10:00:00Z")
    store.mark_connection("snaptrade", fetched_at="2026-01-02T11:00:00Z", ok=False, error="boom")
    row = store.connection("snaptrade")
    assert row["key_ref"] == "ref1" and row["last_error"] == "boom" and row["last_ok_at"] is None
    store.mark_connection("snaptrade", fetched_at="2026-01-02T12:00:00Z", ok=True)
    row = store.connection("snaptrade")
    assert row["last_ok_at"] == "2026-01-02T12:00:00Z" and row["last_error"] == ""
    assert store.remove_connection("snaptrade", "2026-01-03T00:00:00Z") and not store.remove_connection("snaptrade", "x")
    assert store.connections(active_only=True) == [] and len(store.connections()) == 1
    store.save_connection("snaptrade", "ref2", "2026-01-04T00:00:00Z")   # connected again: active, clean
    row = store.connection("snaptrade")
    assert row["removed_at"] is None and row["key_ref"] == "ref2" and row["last_error"] == ""
    store.upsert_account(_bank("Example Bank Checking", "k1"), "2026-01-02")
    assert store.accounts_of("simplefin") == 1 and store.accounts_of("file") == 0


def test_positions_of_a_connected_account(store: Store):
    ref = AccountRef("Example Broker Individual", "brokerage", "example_broker", "brokerage",
                     external_key="k1", origin="snaptrade")
    snap = Snapshot("2026-01-02", "snaptrade", [PositionRow(ref, "AAPL", "APPLE", 3.0, 10.0, 30.0)], [CashRow(ref, 5.0)],
                    fetched_at="2026-01-02T05:00:00Z")
    assert store.write_snapshot(snap) == 2
    assert store.query("SELECT account, quantity FROM positions_latest")[0][1] == 3.0
```

- [ ] **Step 2: Run them to see them fail.** `.venv/Scripts/python.exe -m pytest tests/test_connections_store.py -q` — expected: FAIL (`external_key` ignored: two accounts with one label collide; `set_account` missing).

- [ ] **Step 3: Change the store.** In `src/financial_data_collector/store.py`:

  1. Add the import `from .connections.names import slug_for` after `from .adapters.base import dedupe_key`.
  2. Replace `_account_id` with:

```python
    def _account_id(self, ref: AccountRef, first_seen: str) -> int:
        if ref.external_key:
            return self._connected_account_id(ref, first_seen)
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

    def _connected_account_id(self, ref: AccountRef, first_seen: str) -> int:
        """An account from a connection is known by its external_key, never by its label. The label is fixed when
        the account is first seen; the kind changes only while it is still a guess; a confirmed kind, a limit and
        a rate are what the person set and are never touched."""
        row = self.conn.execute("SELECT id, kind_confirmed FROM accounts WHERE external_key = ?",
                                (ref.external_key,)).fetchone()
        if row is None:
            cur = self.conn.execute(
                """INSERT INTO accounts (label, slug, institution, account_type, first_seen, external_key, origin,
                                         kind_confirmed, flows)
                   VALUES (?,?,?,?,?,?,?,?,?)""",
                (self._free_label(ref.label), ref.slug, ref.institution, ref.account_type, first_seen,
                 ref.external_key, ref.origin, int(ref.kind_confirmed), ref.flows),
            )
            return cur.lastrowid
        aid = row[0]
        if not row[1]:
            self.conn.execute("UPDATE accounts SET account_type = ?, slug = ?, kind_confirmed = ? WHERE id = ?",
                              (ref.account_type, ref.slug, int(ref.kind_confirmed), aid))
        self.conn.execute(
            "UPDATE accounts SET first_seen = MIN(first_seen, ?), "
            "institution = CASE WHEN ? != 'unknown' THEN ? ELSE institution END WHERE id = ?",
            (first_seen, ref.institution, ref.institution, aid),
        )
        return aid

    def _free_label(self, label: str) -> str:
        taken = {r[0] for r in self.conn.execute("SELECT label FROM accounts WHERE label = ? OR label LIKE ?",
                                                 (label, label + " %"))}
        if label not in taken:
            return label
        n = 2
        while f"{label} {n}" in taken:
            n += 1
        return f"{label} {n}"

    def _label_of(self, aid: int) -> str:
        return self.conn.execute("SELECT label FROM accounts WHERE id = ?", (aid,)).fetchone()[0]
```

  3. In `write_snapshot`, replace the cash `INSERT` with:

```python
                self.conn.execute(
                    """INSERT INTO cash_balances (as_of_date, account_id, currency, amount, available, source)
                       VALUES (?,?,?,?,?,?)
                       ON CONFLICT(as_of_date, account_id, currency) DO UPDATE SET
                         amount = excluded.amount, available = excluded.available, source = excluded.source""",
                    (snap.as_of_date, aid, c.currency, c.amount, c.available, snap.source),
                )
```

  4. In `write_transactions`, replace `key = dedupe_key(r.account.label, ...)` with:

```python
                label = self._label_of(aid) if r.account.external_key else r.account.label
                key = dedupe_key(label, r.trade_date, r.type, r.symbol, r.units, r.amount)
```

  5. Before `# ---- prices ---` add:

```python
    # ---- connections --------------------------------------------------------
    def connection(self, name: str) -> sqlite3.Row | None:
        return self.conn.execute("SELECT * FROM connections WHERE name = ?", (name,)).fetchone()

    def connections(self, active_only: bool = False) -> list[sqlite3.Row]:
        where = " WHERE removed_at IS NULL" if active_only else ""
        return self.query(f"SELECT * FROM connections{where} ORDER BY name")

    def save_connection(self, name: str, key_ref: str, when: str) -> None:
        with self.conn:
            self.conn.execute(
                """INSERT INTO connections (name, key_ref, created_at) VALUES (?,?,?)
                   ON CONFLICT(name) DO UPDATE SET key_ref = excluded.key_ref, created_at = excluded.created_at,
                     removed_at = NULL, last_error = ''""",
                (name, key_ref, when),
            )

    def mark_connection(self, name: str, *, fetched_at: str, ok: bool, error: str = "") -> None:
        with self.conn:
            if ok:
                self.conn.execute("UPDATE connections SET last_fetch_at = ?, last_ok_at = ?, last_error = '' "
                                  "WHERE name = ?", (fetched_at, fetched_at, name))
            else:
                self.conn.execute("UPDATE connections SET last_fetch_at = ?, last_error = ? WHERE name = ?",
                                  (fetched_at, error[:1000], name))

    def remove_connection(self, name: str, when: str) -> bool:
        with self.conn:
            cur = self.conn.execute("UPDATE connections SET removed_at = ? WHERE name = ? AND removed_at IS NULL",
                                    (when, name))
            return cur.rowcount > 0

    def accounts_of(self, origin: str) -> int:
        return self.conn.execute("SELECT COUNT(*) FROM accounts WHERE origin = ?", (origin,)).fetchone()[0]

    def newest_activity_dates(self, origin: str) -> dict[str, str]:
        """The newest stored activity per connected account of an origin, by external_key."""
        return {r[0]: r[1] for r in self.query(
            "SELECT a.external_key, MAX(t.trade_date) FROM transactions t JOIN accounts a ON a.id = t.account_id "
            "WHERE a.origin = ? AND a.external_key IS NOT NULL GROUP BY a.external_key", (origin,))}

    # ---- what the person sets about an account ---------------------------------
    def accounts_overview(self) -> list[sqlite3.Row]:
        return self.query("SELECT id, label, institution, account_type, kind_confirmed, credit_limit, rate_pct, "
                          "origin, flows FROM accounts ORDER BY label")

    def set_account(self, label: str, *, kind: str | None = None, credit_limit: float | None = None,
                    rate_pct: float | None = None) -> bool:
        """What the person says about an account; a sync never changes it afterwards. False when no account has
        that label."""
        sets: list[str] = []
        params: list[object] = []
        if kind is not None:
            sets += ["account_type = ?", "slug = ?", "kind_confirmed = 1"]
            params += [kind, slug_for(kind)]
        if credit_limit is not None:
            sets.append("credit_limit = ?")
            params.append(float(credit_limit))
        if rate_pct is not None:
            sets.append("rate_pct = ?")
            params.append(float(rate_pct))
        if not sets:
            return self.conn.execute("SELECT 1 FROM accounts WHERE label = ?", (label,)).fetchone() is not None
        with self.conn:
            cur = self.conn.execute(f"UPDATE accounts SET {', '.join(sets)} WHERE label = ?", (*params, label))
            return cur.rowcount > 0
```

- [ ] **Step 4: Run the tests.** `.venv/Scripts/python.exe -m pytest tests/test_connections_store.py tests/test_store.py tests/test_ingest.py tests/test_history.py -q` — expected: PASS.

- [ ] **Step 5: Commit.**

```bash
git add src/financial_data_collector/store.py tests/test_connections_store.py
git commit -m "The store knows a connected account by its key, and keeps what the person set about it"
```

---

### Task 7: The SnapTrade fetcher

**Files:**
- Modify: `src/financial_data_collector/adapters/snaptrade.py` (rename the map)
- Create: `src/financial_data_collector/connections/snaptrade.py`
- Test: `tests/test_snaptrade_connection.py`

**Interfaces:**
- Consumes: `SnapTradeKeys`, `Fetched`, `ConnectionFailed`, `unreachable`, `num`, `nonzero`, `names.*`, `ACTIVITY_TYPES` (renamed from `_ACTIVITY_TYPES`).
- Produces: `sign(path, query, consumer_key) -> str`, `fetch_accounts(keys, *, fetch, now) -> list[AccountRef]`, `fetch_all(keys, *, fetch, now, since=None) -> Fetched`, `HOST`, `READ_PATHS`, `ORIGIN`, `REFUSED`.

- [ ] **Step 1: Rename the activity map.** In `src/financial_data_collector/adapters/snaptrade.py` rename `_ACTIVITY_TYPES` to `ACTIVITY_TYPES` (both the definition and its use in `parse_activity`) and add `_ACTIVITY_TYPES = ACTIVITY_TYPES` under it. Run `.venv/Scripts/python.exe -m pytest tests/test_snaptrade.py -q` — expected: PASS.

- [ ] **Step 2: Write the failing tests** — `tests/test_snaptrade_connection.py`:

```python
"""The SnapTrade fetcher, against synthetic answers. No network."""
import json
import re
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import pytest

from financial_data_collector.connections import snaptrade as st
from financial_data_collector.connections.base import ConnectionFailed
from financial_data_collector.connections.keys import SnapTradeKeys
from financial_data_collector.http import HttpError

NOW = datetime(2026, 1, 1, 0, 0, tzinfo=timezone.utc)   # 1767225600
KEYS = SnapTradeKeys("synthetic-client", "synthetic-consumer-key")
AUTH = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
ROTH = "11111111-1111-4111-8111-111111111111"
EVERY = "22222222-2222-4222-8222-222222222222"
ACCOUNTS = [
    {"id": ROTH, "brokerage_authorization": AUTH, "name": "Sample Roth IRA (4321)", "number": "SYNTHETIC-NUMBER-0001",
     "institution_name": "Example Brokerage", "raw_type": "Roth IRA", "meta": {"type": "Roth IRA"}},
    {"id": EVERY, "brokerage_authorization": AUTH, "name": "Everyday ...9876", "number": "SYNTHETIC-NUMBER-0002",
     "institution_name": "Example Brokerage", "raw_type": None, "meta": {}},
]
AUTHS = [{"id": AUTH, "disabled": False, "brokerage": {"name": "Example Brokerage"}}]
POSITIONS = {
    ROTH: [
        {"symbol": {"symbol": {"symbol": "AAPL", "raw_symbol": "AAPL", "description": "APPLE INC",
                               "currency": {"code": "USD"}}}, "units": 3, "price": 100.0,
         "average_purchase_price": 90.0, "open_pnl": 30.0},
        {"symbol": {"symbol": {"symbol": "SPAXX", "raw_symbol": "SPAXX", "description": "FIDELITY GOVERNMENT MONEY MARKET"}},
         "units": 12, "price": 1.0},
        {"symbol": {"symbol": {"symbol": "BRKB", "raw_symbol": "BRK.B", "description": "BERKSHIRE"}}, "units": 1, "price": 400},
    ],
    EVERY: [],
}
BALANCES = {ROTH: [{"currency": {"code": "USD"}, "cash": 12.5}], EVERY: []}
ACTIVITY = {
    ROTH: [{"trade_date": "2025-12-30T00:00:00Z", "settlement_date": "2026-01-02", "type": "BUY",
            "symbol": {"symbol": "AAPL", "raw_symbol": "AAPL"}, "units": 1, "price": 99.0, "amount": -99.0, "fee": 0},
           {"trade_date": "2025-12-15T00:00:00Z", "type": "CONTRIBUTION", "amount": 500.0},
           {"trade_date": None, "type": "BUY"}],
    EVERY: [],
}


def _fetch(calls, *, fail=None, pages=None):
    def fetch(url, headers):
        parts = urlsplit(url)
        calls.append((parts.path, parse_qs(parts.query), headers))
        assert parts.scheme == "https" and parts.netloc == st.HOST
        if fail and fail(parts.path):
            raise fail(parts.path)
        path = parts.path.removeprefix(st.API)
        if path == "/accounts":
            return json.dumps(ACCOUNTS).encode()
        if path == "/authorizations":
            return json.dumps(AUTHS).encode()
        m = re.fullmatch(r"/accounts/([^/]+)/(positions|balances|activities)", path)
        assert m, path
        acct, what = m.group(1), m.group(2)
        if what == "positions":
            return json.dumps(POSITIONS[acct]).encode()
        if what == "balances":
            return json.dumps(BALANCES[acct]).encode()
        offset = int(parse_qs(parts.query).get("offset", ["0"])[0])
        rows = (pages or {}).get(acct, ACTIVITY[acct])
        return json.dumps({"data": rows[offset:offset + st.PAGE], "pagination": {"offset": offset}}).encode()
    return fetch


def test_signature_is_the_documented_one():
    query = "clientId=synthetic-client&timestamp=1767225600"
    assert st.sign("/api/v1/accounts", query, "synthetic-consumer-key") == "lB+X4H9882KWtumdGiKt8MjTCIW5fW1u1PfSdxfzNlk="


def test_every_request_is_a_signed_get_to_a_read_path_with_no_user_for_a_personal_key():
    calls = []
    st.fetch_all(KEYS, fetch=_fetch(calls), now=NOW)
    assert calls, "no requests were made"
    for path, query, headers in calls:
        assert path.startswith(st.API)
        assert query["clientId"] == ["synthetic-client"] and query["timestamp"] == ["1767225600"]
        assert "userId" not in query and "userSecret" not in query
        assert headers["Signature"] and headers["Accept"] == "application/json"
    paths = {re.sub(r"/accounts/[^/]+/", "/accounts/{id}/", p.removeprefix(st.API)) for p, _, _ in calls}
    assert paths <= set(st.READ_PATHS)


def test_a_key_with_a_registered_user_sends_it():
    calls = []
    st.fetch_accounts(SnapTradeKeys("c", "k", "user-1", "user-secret"), fetch=_fetch(calls), now=NOW)
    assert calls[0][1]["userId"] == ["user-1"] and calls[0][1]["userSecret"] == ["user-secret"]


def test_the_module_only_names_read_paths():
    src = Path(st.__file__).read_text(encoding="utf-8")
    literal = {re.sub(r"\{[a-z_]+\}", "{id}", m) for m in re.findall(r'"(/(?:accounts|authorizations)[^"]*)"', src)}
    assert literal == set(st.READ_PATHS)
    assert "post" not in dir(st) and "orders" not in src and "trade/" not in src


def test_accounts_positions_cash_and_activity_are_mapped():
    out = st.fetch_all(KEYS, fetch=_fetch([]), now=NOW)
    assert [a.label for a in out.accounts] == ["Example Brokerage Sample Roth IRA", "Example Brokerage Everyday"]
    roth, every = out.accounts
    assert roth.account_type == "roth_ira" and roth.kind_confirmed and roth.slug == "roth" and roth.origin == "snaptrade"
    assert every.account_type == "brokerage" and not every.kind_confirmed and every.flows == "transactions"
    assert roth.institution == "example_brokerage" and roth.external_key != every.external_key
    assert "SYNTHETIC-NUMBER" not in repr(out) and ROTH not in repr(out)
    snap = out.snapshots[0]
    assert snap.source == "snaptrade" and snap.as_of_date == "2026-01-01" and snap.fetched_at == "2026-01-01T00:00:00Z"
    by_symbol = {p.symbol: p for p in snap.positions}
    assert set(by_symbol) == {"AAPL", "BRK.B"}                       # the money market fund is cash, not a holding
    assert by_symbol["AAPL"].market_value == 300.0 and by_symbol["AAPL"].cost_basis_total == 270.0
    assert by_symbol["AAPL"].description == "APPLE INC" and by_symbol["AAPL"].unrealized_pnl == 30.0
    cash = {(c.account.label, c.amount) for c in snap.cash}
    assert cash == {("Example Brokerage Sample Roth IRA", 12.5), ("Example Brokerage Everyday", 0.0)}  # no balance: a 0 row
    assert [(t.trade_date, t.type, t.symbol, t.amount) for t in out.transactions] == [
        ("2025-12-30", "buy", "AAPL", -99.0), ("2025-12-15", "contribution", None, 500.0)]
    assert out.transactions[0].settlement_date == "2026-01-02" and out.transactions[0].source == "snaptrade"
    assert out.notes == []


def test_activity_starts_seven_days_before_the_newest_stored_row_and_pages():
    calls = []
    roth_key = st.fetch_accounts(KEYS, fetch=_fetch([]), now=NOW)[0].external_key
    many = [{"trade_date": "2025-12-01T00:00:00Z", "type": "DIVIDEND", "amount": 1.0}] * (st.PAGE + 5)
    out = st.fetch_all(KEYS, fetch=_fetch(calls, pages={ROTH: many}), now=NOW, since={roth_key: "2025-12-20"})
    activity_calls = [(p, q) for p, q, _ in calls if p.endswith("/activities") and ROTH in p]
    assert [q.get("startDate") for _, q in activity_calls] == [["2025-12-13"], ["2025-12-13"]]
    assert [q["offset"] for _, q in activity_calls] == [["0"], [str(st.PAGE)]]
    assert len([t for t in out.transactions if t.type == "dividend"]) == st.PAGE + 5
    first_calls = [(p, q) for p, q, _ in calls if p.endswith("/activities") and EVERY in p]
    assert "startDate" not in first_calls[0][1]


def test_a_refused_key_and_an_unreachable_host_are_worded_without_the_key():
    def refused(path):
        return HttpError(401, "https://x?clientId=synthetic-client&userSecret=s3cret")
    with pytest.raises(ConnectionFailed) as e:
        st.fetch_accounts(KEYS, fetch=_fetch([], fail=refused), now=NOW)
    assert str(e.value) == st.REFUSED and "s3cret" not in str(e.value)

    def down(path):
        return HttpError(0, "https://x?clientId=synthetic-client", "ConnectionError: boom")
    with pytest.raises(ConnectionFailed) as e:
        st.fetch_accounts(KEYS, fetch=_fetch([], fail=down), now=NOW)
    assert str(e.value) == "api.snaptrade.com couldn't be reached (ConnectionError)"

    def other(path):
        return HttpError(500, "https://x")
    with pytest.raises(ConnectionFailed) as e:
        st.fetch_accounts(KEYS, fetch=_fetch([], fail=other), now=NOW)
    assert str(e.value) == "SnapTrade answered 500"


def test_not_json_is_reported():
    with pytest.raises(ConnectionFailed) as e:
        st.fetch_accounts(KEYS, fetch=lambda url, headers: b"<html>sign in</html>", now=NOW)
    assert str(e.value) == "SnapTrade sent something that isn't JSON"


def test_one_account_failing_skips_only_it():
    def flaky(path):
        return HttpError(500, "https://x") if path.endswith(f"/accounts/{ROTH}/positions") else None
    out = st.fetch_all(KEYS, fetch=_fetch([], fail=flaky), now=NOW)
    assert [a.label for a in out.accounts] == ["Example Brokerage Sample Roth IRA", "Example Brokerage Everyday"]
    assert {c.account.label for c in out.snapshots[0].cash} == {"Example Brokerage Everyday"}
    assert out.notes == ["Example Brokerage Sample Roth IRA: SnapTrade answered 500; skipped"]


def test_a_disabled_login_is_named():
    def fetch(url, headers):
        if url.endswith("/authorizations") or "/authorizations?" in url:
            return json.dumps([{"id": AUTH, "disabled": True, "brokerage": {"name": "Example Brokerage"}}]).encode()
        return _fetch([])(url, headers)
    out = st.fetch_all(KEYS, fetch=fetch, now=NOW)
    assert "Example Brokerage needs reconnecting on SnapTrade's site" in out.notes
```

- [ ] **Step 3: Run them to see them fail.** `.venv/Scripts/python.exe -m pytest tests/test_snaptrade_connection.py -q` — expected: FAIL with `ModuleNotFoundError`.

- [ ] **Step 4: Write the fetcher** — `src/financial_data_collector/connections/snaptrade.py`:

```python
"""SnapTrade, read only: the accounts, what they hold, their cash, their activity, and the state of each brokerage
login. Every request is a signed GET to one of READ_PATHS; nothing here can place, change or cancel an order.

The account object's `number` is never read. The service's ids are used in request paths and hashed into
external keys; they are never stored.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
from datetime import datetime, timedelta, timezone
from typing import Any, Mapping
from urllib.parse import quote, urlencode

from ..adapters.snaptrade import ACTIVITY_TYPES
from ..http import HttpError
from ..models import AccountRef, CashRow, PositionRow, Snapshot, TransactionRow
from ..symbols import canonical, is_money_market
from . import names
from .base import ConnectionFailed, Fetch, Fetched, nonzero, num, unreachable
from .keys import SnapTradeKeys

HOST = "api.snaptrade.com"
API = "/api/v1"
READ_PATHS = ("/accounts", "/authorizations", "/accounts/{id}/positions", "/accounts/{id}/balances",
              "/accounts/{id}/activities")
ORIGIN = "snaptrade"
PAGE = 1000
MAX_PAGES = 50
OVERLAP_DAYS = 7
REFUSED = "SnapTrade refused the key: make a new one on SnapTrade's site, then run fdc connect snaptrade"


def sign(path: str, query: str, consumer_key: str) -> str:
    """SnapTrade's request signature: the canonical JSON of {content, path, query}, HMAC-SHA256 with the consumer
    key, base64. A GET has no content."""
    payload = json.dumps({"content": None, "path": path, "query": query}, separators=(",", ":"), sort_keys=True)
    digest = hmac.new(consumer_key.encode("utf-8"), payload.encode("utf-8"), hashlib.sha256).digest()
    return base64.b64encode(digest).decode("ascii")


def _failure(e: HttpError) -> ConnectionFailed:
    if e.status in (401, 403):
        return ConnectionFailed(REFUSED)
    if e.status == 0:
        return unreachable(HOST, e)
    return ConnectionFailed(f"SnapTrade answered {e.status}")


def _get(keys: SnapTradeKeys, path: str, params: Mapping[str, str], *, fetch: Fetch, now: datetime) -> Any:
    query = dict(params)
    query["clientId"] = keys.client_id
    query["timestamp"] = str(int(now.timestamp()))
    if keys.user_id and keys.user_secret:
        query["userId"] = keys.user_id
        query["userSecret"] = keys.user_secret
    encoded = urlencode(query)
    full = API + path
    headers = {"Signature": sign(full, encoded, keys.consumer_key), "Accept": "application/json"}
    try:
        body = fetch(f"https://{HOST}{full}?{encoded}", headers)
    except HttpError as e:
        raise _failure(e) from None   # the HttpError's text carries the address, and with it the key
    try:
        return json.loads(body)
    except ValueError:
        raise ConnectionFailed("SnapTrade sent something that isn't JSON") from None


def _list(payload: Any) -> list[dict]:
    if isinstance(payload, dict) and isinstance(payload.get("data"), list):
        payload = payload["data"]
    return [x for x in payload if isinstance(x, dict)] if isinstance(payload, list) else []


def _kind_text(raw: dict) -> str:
    meta = raw.get("meta") if isinstance(raw.get("meta"), dict) else {}
    return str(raw.get("raw_type") or meta.get("type") or "")


def account_ref(raw: dict) -> AccountRef | None:
    """The account, named and classified. None when the answer has no id for it."""
    service_id = str(raw.get("id") or "")
    if not service_id:
        return None
    institution = str(raw.get("institution_name") or "")
    name = str(raw.get("name") or "")
    kind = names.match_kind(_kind_text(raw))
    confirmed = kind is not None
    if kind is None:
        kind = names.match_kind(name) or "brokerage"
    return AccountRef(label=names.build_label(institution, name), slug=names.slug_for(kind),
                      institution=names.institution_code(institution), account_type=kind,
                      external_key=names.external_key(ORIGIN, service_id), origin=ORIGIN,
                      kind_confirmed=confirmed, flows="transactions")


def _unwrap(node: Any) -> tuple[str, str | None]:
    """(ticker, description) from a position's or an activity's symbol, which may nest a second symbol object."""
    if not isinstance(node, dict):
        return "", None
    inner = node.get("symbol")
    if isinstance(inner, dict):
        ticker = inner.get("symbol") or inner.get("raw_symbol") or ""
        return str(ticker), inner.get("description") or node.get("description")
    ticker = inner if isinstance(inner, str) else node.get("raw_symbol") or ""
    return str(ticker), node.get("description")


def _position(ref: AccountRef, p: dict) -> PositionRow | None:
    ticker, desc = _unwrap(p.get("symbol"))
    if not ticker or is_money_market(ticker, desc):
        return None
    units = num(p.get("units")) or 0.0
    price = num(p.get("price"))
    avg = num(p.get("average_purchase_price"))
    return PositionRow(account=ref, symbol=canonical(ticker), description=desc, quantity=units, price=price,
                       market_value=(units * price) if price is not None else None,
                       cost_basis_total=(avg * units) if avg is not None else None, avg_cost=avg,
                       unrealized_pnl=num(p.get("open_pnl")))


def _cash(ref: AccountRef, balances: list[dict]) -> list[CashRow]:
    rows = []
    for b in balances:
        amount = num(b.get("cash"))
        if amount is None:
            continue
        currency = b.get("currency")
        code = currency.get("code") if isinstance(currency, dict) else currency
        rows.append(CashRow(ref, amount, str(code or "USD")))
    return rows or [CashRow(ref, 0.0)]   # no balance row: still a full statement of the account


def _activity(ref: AccountRef, a: dict) -> TransactionRow | None:
    trade = str(a.get("trade_date") or "")[:10]
    if len(trade) < 10:
        return None
    raw_type = str(a.get("type") or "").upper()
    ticker, _ = _unwrap(a.get("symbol"))
    settle = str(a.get("settlement_date") or "")[:10] or None
    return TransactionRow(account=ref, trade_date=trade, type=ACTIVITY_TYPES.get(raw_type, "other"),
                          symbol=canonical(ticker) or None, units=nonzero(a.get("units")),
                          price=nonzero(a.get("price")), amount=num(a.get("amount")), fee=num(a.get("fee")),
                          description=raw_type, source=ORIGIN, settlement_date=settle)


def _activities(keys: SnapTradeKeys, ref: AccountRef, service_id: str, start: str | None, *, fetch: Fetch,
                now: datetime) -> list[TransactionRow]:
    """`service_id` is already quoted for a path."""
    rows: list[TransactionRow] = []
    params = {"limit": str(PAGE)}
    if start:
        params["startDate"] = start
    for page in range(MAX_PAGES):
        payload = _get(keys, f"/accounts/{service_id}/activities", {**params, "offset": str(page * PAGE)},
                       fetch=fetch, now=now)
        items = _list(payload)
        rows += [r for r in (_activity(ref, a) for a in items) if r]
        if len(items) < PAGE:
            break
    return rows


def _broken_logins(keys: SnapTradeKeys, *, fetch: Fetch, now: datetime) -> list[str]:
    try:
        auths = _list(_get(keys, "/authorizations", {}, fetch=fetch, now=now))
    except ConnectionFailed:
        return []   # the accounts still tell the story; this call only names a login to repair
    notes = []
    for a in auths:
        if a.get("disabled"):
            brokerage = a.get("brokerage") if isinstance(a.get("brokerage"), dict) else {}
            name = brokerage.get("display_name") or brokerage.get("name") or a.get("name") or "A brokerage"
            notes.append(f"{name} needs reconnecting on SnapTrade's site")
    return notes


def fetch_accounts(keys: SnapTradeKeys, *, fetch: Fetch, now: datetime) -> list[AccountRef]:
    """The accounts the key can see. This is how a key is checked."""
    return [ref for ref in (account_ref(raw) for raw in _list(_get(keys, "/accounts", {}, fetch=fetch, now=now)))
            if ref is not None]


def fetch_all(keys: SnapTradeKeys, *, fetch: Fetch, now: datetime,
              since: Mapping[str, str] | None = None) -> Fetched:
    """Everything for one sync. `since` is the newest stored activity date per external key; activity is fetched
    from OVERLAP_DAYS before it, and from the beginning for an account with none."""
    out = Fetched()
    raw_accounts = _list(_get(keys, "/accounts", {}, fetch=fetch, now=now))
    out.notes += _broken_logins(keys, fetch=fetch, now=now)
    positions: list[PositionRow] = []
    cash: list[CashRow] = []
    for raw in raw_accounts:
        ref = account_ref(raw)
        if ref is None:
            continue
        out.accounts.append(ref)
        service_id = quote(str(raw["id"]), safe="")
        try:
            held = _list(_get(keys, f"/accounts/{service_id}/positions", {}, fetch=fetch, now=now))
            balances = _list(_get(keys, f"/accounts/{service_id}/balances", {}, fetch=fetch, now=now))
        except ConnectionFailed as e:
            out.notes.append(f"{ref.label}: {e}; skipped")
            continue
        positions += [row for row in (_position(ref, p) for p in held) if row]
        cash += _cash(ref, balances)
        newest = (since or {}).get(ref.external_key)
        start = (datetime.fromisoformat(newest) - timedelta(days=OVERLAP_DAYS)).strftime("%Y-%m-%d") if newest else None
        try:
            out.transactions += _activities(keys, ref, service_id, start, fetch=fetch, now=now)
        except ConnectionFailed as e:
            out.notes.append(f"{ref.label}: activity: {e}")
    stamp = now.astimezone(timezone.utc)
    if positions or cash:
        out.snapshots.append(Snapshot(as_of_date=stamp.strftime("%Y-%m-%d"), source=ORIGIN, positions=positions,
                                      cash=cash, fetched_at=stamp.strftime("%Y-%m-%dT%H:%M:%SZ")))
    return out
```

- [ ] **Step 5: Run the tests.** `.venv/Scripts/python.exe -m pytest tests/test_snaptrade_connection.py tests/test_snaptrade.py -q` — expected: PASS.

- [ ] **Step 6: Commit.**

```bash
git add src/financial_data_collector/adapters/snaptrade.py src/financial_data_collector/connections/snaptrade.py tests/test_snaptrade_connection.py
git commit -m "Connections: SnapTrade, read only, with the person's own key"
```

---

### Task 8: The SimpleFIN fetcher

**Files:**
- Create: `src/financial_data_collector/connections/simplefin.py`
- Test: `tests/test_simplefin.py`

**Interfaces:**
- Consumes: `SimpleFinKey`, `Fetched`, `ConnectionFailed`, `unreachable`, `num`, `names.*`, `Post`.
- Produces: `claim(setup_token, *, post) -> str`, `fetch_all(key, *, fetch, now) -> Fetched`, `ORIGIN`, `REFUSED`, `USED`, `LAPSED`, `SITE`.

- [ ] **Step 1: Write the failing tests** — `tests/test_simplefin.py`:

```python
"""The SimpleFIN fetcher, against synthetic answers. No network."""
import base64
import json
from datetime import datetime, timezone
from urllib.parse import urlsplit

import pytest

from financial_data_collector.connections import simplefin as sf
from financial_data_collector.connections.base import ConnectionFailed
from financial_data_collector.connections.keys import SimpleFinKey
from financial_data_collector.http import HttpError

NOW = datetime(2026, 1, 3, 12, 0, tzinfo=timezone.utc)
ACCESS = "https://user%40x:p%40ss@bridge.example.org/simplefin"
KEY = SimpleFinKey(ACCESS)
ANSWER = {
    "errlist": [{"code": "con.auth", "msg": "Example Credit Union needs your attention", "conn_id": "C2"}],
    "connections": [{"conn_id": "C1", "name": "Example Bank", "org_id": "o1", "sfin_url": "https://x"},
                    {"conn_id": "C2", "name": "Example Credit Union", "org_id": "o2", "sfin_url": "https://y"}],
    "accounts": [
        {"id": "ACT-1", "conn_id": "C1", "name": "Everyday Checking ...1234", "currency": "USD", "balance": "1500.25",
         "available-balance": "1450.00", "balance-date": 1767355200},       # 2026-01-02 12:00Z
        {"id": "ACT-2", "conn_id": "C1", "name": "Cash Rewards Visa ...5678", "currency": "USD", "balance": "-640.00",
         "available-balance": "4360.00", "balance-date": 1767441600},       # 2026-01-03 12:00Z
        {"id": "ACT-3", "conn_id": "C2", "name": "Share Savings", "currency": "USD", "balance": None, "balance-date": 1767441600},
        {"id": "ACT-4", "conn_id": "C1", "name": "Everyday Checking ...9999", "currency": "usd", "balance": "10", "balance-date": 0},
    ],
}


def _fetch(calls, answer=ANSWER, *, fail=None):
    def fetch(url, headers):
        calls.append((url, headers))
        if fail:
            raise fail
        return json.dumps(answer).encode()
    return fetch


def test_claim_exchanges_a_token_once():
    posts = []

    def post(url, headers):
        posts.append((url, headers))
        return 200, b"https://u:p@bridge.example.org/simplefin\n"

    token = base64.b64encode(b"https://bridge.example.org/simplefin/claim/abc").decode()
    assert sf.claim(f"  {token}\n", post=post) == "https://u:p@bridge.example.org/simplefin"
    assert posts == [("https://bridge.example.org/simplefin/claim/abc", {"Content-Length": "0"})]


@pytest.mark.parametrize("token, message", [
    ("not base64!!", "this doesn't look like a SimpleFIN setup token: copy it again from SimpleFIN's site"),
    (base64.b64encode(b"http://bridge.example.org/claim/abc").decode(), "this doesn't look like a SimpleFIN setup token: copy it again from SimpleFIN's site"),
])
def test_claim_refuses_a_bad_token_before_spending_anything(token, message):
    with pytest.raises(ConnectionFailed) as e:
        sf.claim(token, post=lambda url, headers: (200, b"https://u:p@h/x"))
    assert str(e.value) == message


def test_claim_reports_a_used_token_and_a_bad_answer_without_retrying():
    token = base64.b64encode(b"https://bridge.example.org/simplefin/claim/abc").decode()
    calls = []

    def used(url, headers):
        calls.append(url)
        return 403, b"used"
    with pytest.raises(ConnectionFailed) as e:
        sf.claim(token, post=used)
    assert str(e.value) == sf.USED and len(calls) == 1
    with pytest.raises(ConnectionFailed) as e:
        sf.claim(token, post=lambda url, headers: (200, b"https://bridge.example.org/no-credentials"))
    assert str(e.value) == "SimpleFIN's answer wasn't an access address"
    with pytest.raises(ConnectionFailed) as e:
        sf.claim(token, post=lambda url, headers: (500, b""))
    assert str(e.value) == "SimpleFIN answered 500 to the setup token"

    def down(url, headers):
        raise HttpError(0, url, "ConnectionError")
    with pytest.raises(ConnectionFailed) as e:
        sf.claim(token, post=down)
    assert str(e.value) == "bridge.example.org couldn't be reached (ConnectionError)"


def test_one_balances_only_request_with_basic_auth_and_no_credentials_in_the_address():
    calls = []
    sf.fetch_all(KEY, fetch=_fetch(calls), now=NOW)
    assert len(calls) == 1
    url, headers = calls[0]
    parts = urlsplit(url)
    assert parts.username is None and parts.password is None
    assert url == "https://bridge.example.org/simplefin/accounts?balances-only=1&version=2"
    assert headers["Authorization"] == "Basic " + base64.b64encode(b"user@x:p@ss").decode()


def test_accounts_become_balances_by_day_with_kinds_guessed():
    out = sf.fetch_all(KEY, fetch=_fetch([]), now=NOW)
    by_label = {a.label: a for a in out.accounts}
    assert set(by_label) == {"Example Bank Everyday Checking", "Example Bank Cash Rewards Visa"}
    checking, visa = by_label["Example Bank Everyday Checking"], by_label["Example Bank Cash Rewards Visa"]
    assert checking.account_type == "checking" and visa.account_type == "credit_card"
    assert not checking.kind_confirmed and checking.flows == "balance" and checking.origin == "simplefin"
    assert checking.institution == "example_bank" and checking.external_key != visa.external_key
    assert "ACT-1" not in repr(out) and "1234" not in repr(out)
    days = {s.as_of_date: s for s in out.snapshots}
    assert set(days) == {"2026-01-02", "2026-01-03"}
    assert days["2026-01-02"].source == "simplefin" and days["2026-01-02"].positions == []
    c = days["2026-01-02"].cash[0]
    assert (c.account.label, c.amount, c.available, c.currency) == ("Example Bank Everyday Checking", 1500.25, 1450.0, "USD")
    v = days["2026-01-03"].cash
    assert [(x.account.label, x.amount, x.available) for x in v] == [("Example Bank Cash Rewards Visa", -640.0, 4360.0),
                                                                     ("Example Bank Everyday Checking", 10.0, None)]
    assert out.notes == ["Example Credit Union Share Savings: no balance in SimpleFIN's answer; skipped",
                         "Example Credit Union needs attention on SimpleFIN's site: Example Credit Union needs your attention"]


def test_two_accounts_with_one_name_are_two_accounts():
    out = sf.fetch_all(KEY, fetch=_fetch([]), now=NOW)
    keys = [a.external_key for a in out.accounts if a.label == "Example Bank Everyday Checking"]
    assert len(keys) == 2 and keys[0] != keys[1]   # the store gives the second its " 2"


def test_a_v1_answer_still_reads():
    v1 = {"errors": ["Example Bank: please sign in again"],
          "accounts": [{"id": "A", "org": {"name": "Example Bank", "domain": "bank.example"}, "name": "Savings",
                        "currency": "USD", "balance": "5", "balance-date": 1767441600}]}
    out = sf.fetch_all(KEY, fetch=_fetch([], v1), now=NOW)
    assert out.accounts[0].label == "Example Bank Savings" and out.accounts[0].account_type == "savings"
    assert out.notes == ["SimpleFIN: Example Bank: please sign in again"]


def test_no_kind_in_the_name_falls_back_on_the_sign():
    answer = {"accounts": [{"id": "A", "conn_id": "C", "name": "Everyday", "currency": "USD", "balance": "-5", "balance-date": 1767441600},
                           {"id": "B", "conn_id": "C", "name": "Everyday", "currency": "EUR", "balance": "5", "balance-date": 1767441600}],
              "connections": [{"conn_id": "C", "name": "Example Bank"}], "errlist": []}
    out = sf.fetch_all(KEY, fetch=_fetch([], answer), now=NOW)
    assert [a.account_type for a in out.accounts] == ["credit_card", "other"]
    assert out.snapshots[0].cash[1].currency == "EUR"


def test_refused_lapsed_unreachable_and_not_json():
    for fail, message in [(HttpError(403, "https://u:p@h/x"), sf.REFUSED), (HttpError(402, "https://h"), sf.LAPSED),
                          (HttpError(0, "https://u:p@h/x", "ConnectionError: x"), "bridge.example.org couldn't be reached (ConnectionError)"),
                          (HttpError(500, "https://h"), "SimpleFIN answered 500")]:
        with pytest.raises(ConnectionFailed) as e:
            sf.fetch_all(KEY, fetch=_fetch([], fail=fail), now=NOW)
        assert str(e.value) == message
    with pytest.raises(ConnectionFailed) as e:
        sf.fetch_all(KEY, fetch=lambda url, headers: b"<html>", now=NOW)
    assert str(e.value) == "SimpleFIN sent something that isn't JSON"
```

- [ ] **Step 2: Run them to see them fail.** `.venv/Scripts/python.exe -m pytest tests/test_simplefin.py -q` — expected: FAIL with `ModuleNotFoundError`.

- [ ] **Step 3: Write the fetcher** — `src/financial_data_collector/connections/simplefin.py`:

```python
"""SimpleFIN, balances only: one balance per account per day. Transactions are never requested, so they never
reach this computer. The access address carries its own user name and password and is a secret throughout."""
from __future__ import annotations

import base64
import binascii
import json
import re
from datetime import datetime, timezone
from typing import Any
from urllib.parse import unquote, urlsplit

from ..http import HttpError
from ..models import AccountRef, CashRow, Snapshot
from . import names
from .base import ConnectionFailed, Fetch, Fetched, Post, num, unreachable
from .keys import SimpleFinKey

ORIGIN = "simplefin"
SITE = "https://beta-bridge.simplefin.org/"
BAD_TOKEN = "this doesn't look like a SimpleFIN setup token: copy it again from SimpleFIN's site"
USED = "this setup token has been used: make a new one on SimpleFIN's site"
REFUSED = "SimpleFIN refused the saved token: run fdc connect simplefin again"
LAPSED = "SimpleFIN says the subscription has lapsed: renew it on SimpleFIN's site"
_CODE = re.compile(r"^[A-Z]{3}$")


def _host(url: str) -> str:
    return urlsplit(url).hostname or "SimpleFIN"


def claim(setup_token: str, *, post: Post) -> str:
    """Exchange the one-time setup token for the access address. The token is base64 for an https claim address,
    which is sent one POST. Nothing is retried: the token is spent by the first attempt."""
    try:
        claim_url = base64.b64decode(setup_token.strip().encode("ascii"), validate=True).decode("utf-8").strip()
    except (binascii.Error, UnicodeError, ValueError):
        raise ConnectionFailed(BAD_TOKEN) from None
    if not claim_url.startswith("https://"):
        raise ConnectionFailed(BAD_TOKEN)
    try:
        status, body = post(claim_url, {"Content-Length": "0"})
    except HttpError as e:
        raise unreachable(_host(claim_url), e) from None
    if status == 403:
        raise ConnectionFailed(USED)
    if status != 200:
        raise ConnectionFailed(f"SimpleFIN answered {status} to the setup token")
    access = body.decode("utf-8", errors="replace").strip()
    parts = urlsplit(access)
    if parts.scheme != "https" or not parts.username or not parts.password:
        raise ConnectionFailed("SimpleFIN's answer wasn't an access address")
    return access


def _request(key: SimpleFinKey) -> tuple[str, dict[str, str]]:
    """The address without its credentials, and the header that carries them instead."""
    parts = urlsplit(key.access_url)
    port = f":{parts.port}" if parts.port else ""
    base = f"https://{parts.hostname}{port}{parts.path.rstrip('/')}"
    pair = f"{unquote(parts.username or '')}:{unquote(parts.password or '')}".encode("utf-8")
    auth = base64.b64encode(pair).decode("ascii")
    return f"{base}/accounts?balances-only=1&version=2", {"Authorization": f"Basic {auth}", "Accept": "application/json"}


def _failure(host: str, e: HttpError) -> ConnectionFailed:
    if e.status == 403:
        return ConnectionFailed(REFUSED)
    if e.status == 402:
        return ConnectionFailed(LAPSED)
    if e.status == 0:
        return unreachable(host, e)
    return ConnectionFailed(f"SimpleFIN answered {e.status}")


def _day(stamp: Any, now: datetime) -> str:
    seconds = num(stamp)
    if not seconds or seconds <= 0:
        return now.astimezone(timezone.utc).strftime("%Y-%m-%d")
    when = datetime.fromtimestamp(seconds, tz=timezone.utc)
    if when > now.astimezone(timezone.utc):
        when = now.astimezone(timezone.utc)
    return when.strftime("%Y-%m-%d")


def _notes(payload: dict, connections: dict[str, str]) -> list[str]:
    notes = []
    for err in payload.get("errlist") or []:
        if not isinstance(err, dict):
            continue
        msg = str(err.get("msg") or err.get("code") or "").strip()
        who = connections.get(str(err.get("conn_id") or ""))
        notes.append(f"{who} needs attention on SimpleFIN's site: {msg}" if who else f"SimpleFIN: {msg}")
    for err in payload.get("errors") or []:   # the older protocol: plain strings
        if isinstance(err, str) and err.strip():
            notes.append(f"SimpleFIN: {err.strip()}")
    return notes


def fetch_all(key: SimpleFinKey, *, fetch: Fetch, now: datetime) -> Fetched:
    url, headers = _request(key)
    host = _host(url)
    try:
        body = fetch(url, headers)
    except HttpError as e:
        raise _failure(host, e) from None
    try:
        payload = json.loads(body)
    except ValueError:
        raise ConnectionFailed("SimpleFIN sent something that isn't JSON") from None
    if not isinstance(payload, dict):
        raise ConnectionFailed("SimpleFIN sent something that isn't JSON")
    connections = {str(c.get("conn_id")): str(c.get("name") or "") for c in payload.get("connections") or []
                   if isinstance(c, dict)}
    out = Fetched()
    by_day: dict[str, list[CashRow]] = {}
    for raw in payload.get("accounts") or []:
        if not isinstance(raw, dict) or not raw.get("id"):
            continue
        org = raw.get("org") if isinstance(raw.get("org"), dict) else {}
        institution = str(connections.get(str(raw.get("conn_id") or "")) or org.get("name") or org.get("domain") or "")
        name = str(raw.get("name") or "")
        label = names.build_label(institution, name)
        balance = num(raw.get("balance"))
        if balance is None:
            out.notes.append(f"{label}: no balance in SimpleFIN's answer; skipped")
            continue
        kind = names.match_kind(name) or ("credit_card" if balance < 0 else "other")
        ref = AccountRef(label=label, slug=names.slug_for(kind), institution=names.institution_code(institution),
                         account_type=kind, external_key=names.external_key(ORIGIN, str(raw["id"])), origin=ORIGIN,
                         kind_confirmed=False, flows="balance")
        out.accounts.append(ref)
        code = str(raw.get("currency") or "USD").upper()
        by_day.setdefault(_day(raw.get("balance-date"), now), []).append(
            CashRow(ref, balance, code if _CODE.match(code) else "XXX", num(raw.get("available-balance"))))
    stamp = now.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    for day in sorted(by_day):
        out.snapshots.append(Snapshot(as_of_date=day, source=ORIGIN, positions=[], cash=by_day[day], fetched_at=stamp))
    out.notes += _notes(payload, connections)
    return out
```

- [ ] **Step 4: Run the tests.** `.venv/Scripts/python.exe -m pytest tests/test_simplefin.py -q` — expected: PASS.

- [ ] **Step 5: Commit.**

```bash
git add src/financial_data_collector/connections/simplefin.py tests/test_simplefin.py
git commit -m "Connections: SimpleFIN, balances only"
```

---

### Task 9: The service layer

**Files:**
- Create: `src/financial_data_collector/connections/service.py`
- Test: `tests/test_connections_service.py`

**Interfaces:**
- Consumes: everything above.
- Produces: `connect_snaptrade(store, home, keys, *, fetch, now) -> (accounts, notes)`, `connect_simplefin(store, home, setup_token, *, post, fetch, now) -> (accounts, notes)`, `disconnect(store, home, name, *, now) -> bool`, `overview(store) -> list[Overview]`, `run(store, home, *, min_hours, fetch, now, progress=...) -> (rows, message, status)`, `NAMES`, `TITLES`, `DOUBLE_COUNT`.

- [ ] **Step 1: Write the failing tests** — `tests/test_connections_service.py`:

```python
"""fdc connect, disconnect and the sync step, with fake services. No network."""
import base64
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from financial_data_collector.connections import keys as K
from financial_data_collector.connections import service
from financial_data_collector.connections.base import ConnectionFailed
from financial_data_collector.http import HttpError
from financial_data_collector.models import AccountRef
from financial_data_collector.store import Store
from tests.test_simplefin import ACCESS, ANSWER
from tests.test_snaptrade_connection import _fetch as snaptrade_fetch

NOW = datetime(2026, 1, 3, 12, 0, tzinfo=timezone.utc)
TOKEN = base64.b64encode(b"https://bridge.example.org/simplefin/claim/abc").decode()


@pytest.fixture
def store(tmp_path: Path) -> Store:
    s = Store.open(tmp_path / "w.db")
    yield s
    s.close()


@pytest.fixture
def home(tmp_path: Path) -> K.KeyHome:
    return K.KeyHome(K.MemoryKeyStore(), K.EnvFile(tmp_path / ".env", environ={}))


def _post(url, headers):
    return 200, ACCESS.encode()


def _both(url, headers):
    """One fake fetch that answers for both services."""
    if "simplefin" in url:
        return json.dumps(ANSWER).encode()
    return snaptrade_fetch([])(url, headers)


def test_connect_snaptrade_checks_saves_and_lists(store: Store, home: K.KeyHome):
    accounts, notes = service.connect_snaptrade(store, home, K.SnapTradeKeys("c", "k"), fetch=_both, now=NOW)
    assert [a.label for a in accounts] == ["Example Brokerage Sample Roth IRA", "Example Brokerage Everyday"]
    row = store.connection("snaptrade")
    assert row and row["key_ref"] != K.ENV_REF and home.load("snaptrade", row["key_ref"]) == K.SnapTradeKeys("c", "k")
    assert store.accounts_of("snaptrade") == 2 and notes == []


def test_a_failed_check_leaves_the_old_key(store: Store, home: K.KeyHome):
    service.connect_snaptrade(store, home, K.SnapTradeKeys("c", "k"), fetch=_both, now=NOW)
    old_ref = store.connection("snaptrade")["key_ref"]

    def refused(url, headers):
        raise HttpError(401, url)
    with pytest.raises(ConnectionFailed):
        service.connect_snaptrade(store, home, K.SnapTradeKeys("c2", "k2"), fetch=refused, now=NOW)
    assert store.connection("snaptrade")["key_ref"] == old_ref
    assert home.load("snaptrade", old_ref) == K.SnapTradeKeys("c", "k")
    # a good replacement forgets the old entry
    service.connect_snaptrade(store, home, K.SnapTradeKeys("c3", "k3"), fetch=_both, now=NOW)
    new_ref = store.connection("snaptrade")["key_ref"]
    assert new_ref != old_ref and home.store.get(old_ref) is None


def test_connect_simplefin_saves_the_address_the_moment_it_has_it(store: Store, home: K.KeyHome):
    def broken_after_claim(url, headers):
        raise HttpError(500, url)
    with pytest.raises(ConnectionFailed) as e:
        service.connect_simplefin(store, home, TOKEN, post=_post, fetch=broken_after_claim, now=NOW)
    assert str(e.value) == "SimpleFIN answered 500"
    row = store.connection("simplefin")
    assert row and home.load("simplefin", row["key_ref"]) == K.SimpleFinKey(ACCESS)   # the token is spent: the address is kept
    accounts, notes = service.connect_simplefin(store, home, TOKEN, post=_post, fetch=_both, now=NOW)
    assert len(accounts) == 2 and any("needs attention" in n for n in notes)


def test_connect_warns_when_file_accounts_exist(store: Store, home: K.KeyHome):
    store.upsert_account(AccountRef("Sample Brokerage", "brokerage", "fidelity", "brokerage"), "2026-01-02")
    _, notes = service.connect_snaptrade(store, home, K.SnapTradeKeys("c", "k"), fetch=_both, now=NOW)
    assert service.DOUBLE_COUNT in notes


def test_disconnect_forgets_the_key_and_keeps_the_rows(store: Store, home: K.KeyHome):
    service.connect_snaptrade(store, home, K.SnapTradeKeys("c", "k"), fetch=_both, now=NOW)
    ref = store.connection("snaptrade")["key_ref"]
    assert service.disconnect(store, home, "snaptrade", now=NOW)
    assert home.store.get(ref) is None and store.connections(active_only=True) == []
    assert store.accounts_of("snaptrade") == 2
    assert not service.disconnect(store, home, "snaptrade", now=NOW) and not service.disconnect(store, home, "nope", now=NOW)


def test_run_fetches_every_connection_and_writes(store: Store, home: K.KeyHome):
    service.connect_snaptrade(store, home, K.SnapTradeKeys("c", "k"), fetch=_both, now=NOW)
    service.connect_simplefin(store, home, TOKEN, post=_post, fetch=_both, now=NOW)
    rows, message, status = service.run(store, home, min_hours=6, fetch=_both, now=NOW)
    assert status == "ok" and rows > 0
    assert message.startswith("simplefin: 2 accounts") and "snaptrade: 2 accounts" in message
    assert "needs attention" in message
    assert store.query("SELECT COUNT(*) FROM position_snapshots")[0][0] == 2
    assert store.query("SELECT COUNT(*) FROM cash_balances")[0][0] == 5     # 2 SnapTrade + 3 SimpleFIN rows
    assert store.query("SELECT COUNT(*) FROM transactions")[0][0] == 2
    for name in ("snaptrade", "simplefin"):
        row = store.connection(name)
        assert row["last_ok_at"] == "2026-01-03T12:00:00Z" and row["last_error"] == ""
    # too soon: nothing fetched, nothing changed, still ok
    later = NOW + timedelta(hours=2)
    rows2, message2, status2 = service.run(store, home, min_hours=6, fetch=_both, now=later)
    assert (rows2, status2) == (0, "ok") and "fetched 2 hours ago" in message2
    # after min_hours: fetched again with nothing new, no data row changes
    counts = store.counts()
    rows3, _, status3 = service.run(store, home, min_hours=6, fetch=_both, now=NOW + timedelta(hours=7))
    assert status3 == "ok"
    same = {k: v for k, v in store.counts().items() if k != "sync_runs"}
    assert same == {k: v for k, v in counts.items() if k != "sync_runs"}


def test_run_isolates_a_failing_connection_and_redacts(store: Store, home: K.KeyHome):
    service.connect_snaptrade(store, home, K.SnapTradeKeys("client-id-value", "consumer-key-value"), fetch=_both, now=NOW)
    service.connect_simplefin(store, home, TOKEN, post=_post, fetch=_both, now=NOW)

    def snaptrade_down(url, headers):
        if "snaptrade" in url:
            raise HttpError(0, url, "ConnectionError: client-id-value consumer-key-value")
        return _both(url, headers)
    rows, message, status = service.run(store, home, min_hours=6, fetch=snaptrade_down, now=NOW)
    assert status == "ok" and rows > 0
    assert "snaptrade: api.snaptrade.com couldn't be reached (ConnectionError)" in message
    assert "client-id-value" not in message and "consumer-key-value" not in message
    row = store.connection("snaptrade")
    assert row["last_error"].startswith("api.snaptrade.com couldn't be reached") and row["last_ok_at"] is None
    assert store.connection("simplefin")["last_ok_at"] == "2026-01-03T12:00:00Z"

    def everything_down(url, headers):
        raise HttpError(0, url, "ConnectionError: x")
    _, _, status = service.run(store, home, min_hours=0, fetch=everything_down, now=NOW + timedelta(hours=1))
    assert status == "error"


def test_run_with_no_connections_is_skipped_and_env_keys_count(store: Store, tmp_path: Path):
    home = K.KeyHome(K.MemoryKeyStore(), K.EnvFile(tmp_path / ".env", environ={}))
    assert service.run(store, home, min_hours=6, fetch=_both, now=NOW) == (
        0, "no connections: run fdc connect snaptrade or fdc connect simplefin", "skipped")
    env_home = K.KeyHome(K.MemoryKeyStore(), K.EnvFile(tmp_path / ".env", environ={"FDC_SIMPLEFIN_ACCESS_URL": ACCESS}))
    rows, message, status = service.run(store, env_home, min_hours=6, fetch=_both, now=NOW)
    assert status == "ok" and store.connection("simplefin")["key_ref"] == K.ENV_REF


def test_a_missing_key_is_a_message_not_a_crash(store: Store, home: K.KeyHome):
    store.save_connection("snaptrade", "gone", "2026-01-01T00:00:00Z")
    rows, message, status = service.run(store, home, min_hours=6, fetch=_both, now=NOW)
    assert status == "error" and message == "snaptrade: no SnapTrade key saved: run fdc connect snaptrade"


def test_an_unexpected_error_is_reported_and_redacted(store: Store, home: K.KeyHome):
    service.connect_snaptrade(store, home, K.SnapTradeKeys("client-id-value", "consumer-key-value"), fetch=_both, now=NOW)

    def odd(url, headers):
        raise ValueError("boom client-id-value")
    rows, message, status = service.run(store, home, min_hours=6, fetch=odd, now=NOW)
    assert status == "error" and message == "snaptrade: ValueError: boom ***"


def test_an_account_that_vanishes_keeps_its_rows(store: Store, home: K.KeyHome):
    service.connect_simplefin(store, home, TOKEN, post=_post, fetch=_both, now=NOW)
    service.run(store, home, min_hours=0, fetch=_both, now=NOW)
    before = store.query("SELECT COUNT(*) FROM cash_balances")[0][0]
    only_first = dict(ANSWER["accounts"][0], **{"balance-date": 1767528000})   # 2026-01-04 12:00Z
    fewer = dict(ANSWER, accounts=[only_first])
    rows, message, status = service.run(store, home, min_hours=0, fetch=lambda u, h: json.dumps(fewer).encode(),
                                        now=NOW + timedelta(days=1))
    assert status == "ok"
    assert store.query("SELECT COUNT(*) FROM cash_balances")[0][0] == before + 1
    assert store.accounts_of("simplefin") == 2


def test_overview(store: Store, home: K.KeyHome):
    service.connect_snaptrade(store, home, K.SnapTradeKeys("c", "k"), fetch=_both, now=NOW)
    ov = service.overview(store)
    assert [o.name for o in ov] == ["snaptrade"] and ov[0].accounts == 2 and ov[0].last_ok_at is None
```

- [ ] **Step 2: Run them to see them fail.** `.venv/Scripts/python.exe -m pytest tests/test_connections_service.py -q` — expected: FAIL with `ImportError` (no `service`).

- [ ] **Step 3: Write the service** — `src/financial_data_collector/connections/service.py`:

```python
"""What fdc connect, fdc connections, fdc disconnect and the sync step do. The commands are a thin layer over
these functions, and the setup wizard (part 2) calls them directly. Every message returned is safe to show."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Callable

from ..models import AccountRef
from ..store import Store
from . import simplefin, snaptrade
from .base import ConnectionFailed, Fetch, Fetched, Post
from .keys import ENV_REF, KeyHome, Keys, SimpleFinKey, SnapTradeKeys
from .redact import redact

NAMES = ("snaptrade", "simplefin")
TITLES = {"snaptrade": "SnapTrade", "simplefin": "SimpleFIN"}
DOUBLE_COUNT = ("an account you import from files and also connect here is counted twice: "
                "use one or the other for each account")


def _stamp(now: datetime) -> str:
    return now.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _day(now: datetime) -> str:
    return now.astimezone(timezone.utc).strftime("%Y-%m-%d")


def _parse(stamp: str) -> datetime:
    return datetime.strptime(stamp, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)


def _ago(stamp: str, now: datetime) -> str:
    minutes = int((now - _parse(stamp)).total_seconds() // 60)
    if minutes < 60:
        return f"{minutes} minute{'s' if minutes != 1 else ''} ago"
    hours = minutes // 60
    return f"{hours} hour{'s' if hours != 1 else ''} ago"


def _remember(store: Store, home: KeyHome, name: str, keys: Keys, now: datetime) -> None:
    """Save the key, record the connection, and forget the key it replaces."""
    old = store.connection(name)
    ref = home.save(name, keys)
    store.save_connection(name, ref, _stamp(now))
    if old is not None and old["key_ref"] not in (ref, ENV_REF):
        home.forget(name, old["key_ref"])


def _register(store: Store, accounts: list[AccountRef], now: datetime) -> list[str]:
    for ref in accounts:
        store.upsert_account(ref, _day(now))
    return [DOUBLE_COUNT] if accounts and store.accounts_of("file") else []


def connect_snaptrade(store: Store, home: KeyHome, keys: SnapTradeKeys, *, fetch: Fetch,
                      now: datetime) -> tuple[list[AccountRef], list[str]]:
    """Checks the key by listing accounts, then saves it. A failed check leaves any older key in place."""
    accounts = snaptrade.fetch_accounts(keys, fetch=fetch, now=now)
    _remember(store, home, "snaptrade", keys, now)
    return accounts, _register(store, accounts, now)


def connect_simplefin(store: Store, home: KeyHome, setup_token: str, *, post: Post, fetch: Fetch,
                      now: datetime) -> tuple[list[AccountRef], list[str]]:
    """Exchanges the one-time token and saves the address it gives before anything else can fail: the token is
    spent by then. Then lists the accounts."""
    key = SimpleFinKey(simplefin.claim(setup_token, post=post))
    _remember(store, home, "simplefin", key, now)
    fetched = simplefin.fetch_all(key, fetch=fetch, now=now)
    return fetched.accounts, _register(store, fetched.accounts, now) + fetched.notes


def disconnect(store: Store, home: KeyHome, name: str, *, now: datetime) -> bool:
    row = store.connection(name)
    if row is None or row["removed_at"]:
        return False
    home.forget(name, row["key_ref"])
    return store.remove_connection(name, _stamp(now))


@dataclass
class Overview:
    name: str
    created_at: str
    last_ok_at: str | None
    last_error: str
    accounts: int


def overview(store: Store) -> list[Overview]:
    return [Overview(r["name"], r["created_at"], r["last_ok_at"], r["last_error"], store.accounts_of(r["name"]))
            for r in store.connections(active_only=True)]


def _fetch_snaptrade(keys: SnapTradeKeys, store: Store, *, fetch: Fetch, now: datetime) -> Fetched:
    return snaptrade.fetch_all(keys, fetch=fetch, now=now, since=store.newest_activity_dates("snaptrade"))


def _fetch_simplefin(keys: SimpleFinKey, store: Store, *, fetch: Fetch, now: datetime) -> Fetched:
    return simplefin.fetch_all(keys, fetch=fetch, now=now)


FETCHERS: dict[str, Callable[..., Fetched]] = {"snaptrade": _fetch_snaptrade, "simplefin": _fetch_simplefin}


def _write(store: Store, fetched: Fetched, now: datetime) -> int:
    for ref in fetched.accounts:
        store.upsert_account(ref, _day(now))
    rows = sum(store.write_snapshot(snap) for snap in fetched.snapshots)
    return rows + store.write_transactions(fetched.transactions)


def run(store: Store, home: KeyHome, *, min_hours: float, fetch: Fetch, now: datetime,
        progress: Callable[[str], None] = lambda detail: None) -> tuple[int, str, str]:
    """The sync step: every active connection, each on its own. Returns (rows, message, status)."""
    for name in NAMES:   # a key put in .env by hand, on a computer with no key store, is a connection too
        row = store.connection(name)
        if (row is None or row["removed_at"]) and home.in_env(name) is not None:
            store.save_connection(name, ENV_REF, _stamp(now))
    active = store.connections(active_only=True)
    if not active:
        return 0, "no connections: run fdc connect snaptrade or fdc connect simplefin", "skipped"
    rows, messages, ran, failed = 0, [], 0, 0
    for row in active:
        name = row["name"]
        if row["last_fetch_at"] and now - _parse(row["last_fetch_at"]) < timedelta(hours=min_hours):
            messages.append(f"{name}: fetched {_ago(row['last_fetch_at'], now)}")
            continue
        ran += 1
        progress(name)
        keys = home.load(name, row["key_ref"])
        secrets = keys.secrets() if keys else []
        try:
            if keys is None:
                raise ConnectionFailed(f"no {TITLES[name]} key saved: run fdc connect {name}")
            fetched = FETCHERS[name](keys, store, fetch=fetch, now=now)
            written = _write(store, fetched, now)
        except ConnectionFailed as e:
            failed += 1
            error = redact(str(e), secrets)
            store.mark_connection(name, fetched_at=_stamp(now), ok=False, error=error)
            messages.append(f"{name}: {error}")
            continue
        except Exception as e:  # anything else: reported for this connection, and the other one still runs
            failed += 1
            error = redact(f"{type(e).__name__}: {e}", secrets)
            store.mark_connection(name, fetched_at=_stamp(now), ok=False, error=error)
            messages.append(f"{name}: {error}")
            continue
        rows += written
        store.mark_connection(name, fetched_at=_stamp(now), ok=True)
        summary = f"{name}: {len(fetched.accounts)} accounts, {written} rows"
        for note in fetched.notes:
            summary += "; " + redact(note, secrets)
        messages.append(summary)
    status = "error" if ran and failed == ran else "ok"
    return rows, "; ".join(messages), status
```

- [ ] **Step 4: Run the tests.** `.venv/Scripts/python.exe -m pytest tests/test_connections_service.py -q` — expected: PASS.

- [ ] **Step 5: Commit.**

```bash
git add src/financial_data_collector/connections/service.py tests/test_connections_service.py
git commit -m "Connections: connect, disconnect, overview and the fetch that the sync step runs"
```

---

### Task 10: The sync step and the setting

**Files:**
- Modify: `src/financial_data_collector/config.py`
- Modify: `src/financial_data_collector/sync.py`
- Modify: `config.example.toml`, `.env.example`
- Modify: `tests/test_sync.py`, `tests/test_config.py`

**Interfaces:**
- Consumes: `service.run`, `KeyHome.for_root`.
- Produces: `Config.connections_min_hours: float` (default 6); `STEPS = ("connections", "ingest", "prices", "sec", "derive", "export")`; `run_sync(..., keys: KeyHome | None = None)`.

- [ ] **Step 1: Write the failing tests.** In `tests/test_sync.py`:

  1. Add the imports `from financial_data_collector.connections import keys as K` and `from financial_data_collector.connections.service import DOUBLE_COUNT` is not needed; add only the first.
  2. Change every expected step list: `["ingest", "prices", "sec", "derive", "export"]` becomes `["connections", "ingest", "prices", "sec", "derive", "export"]` (in `test_full_sync_then_noop` and `test_dry_run_touches_nothing`), `["ingest", "prices", "derive", "export"]` becomes `["connections", "ingest", "prices", "derive", "export"]`.
  3. In `test_full_sync_then_noop`: statuses become `["skipped", "ok", "ok", "ok", "ok", "skipped"]`; `c1["sync_runs"] == 5` becomes `== 6`; `rep.steps[1].message` becomes `rep.steps[2].message`; `rep2.steps[0].rows` becomes `rep2.steps[1].rows`; `rep2.steps[2].message` becomes `rep2.steps[3].message`.
  4. Append:

```python
def test_connections_step_runs_the_connections(project, fixtures: Path, tmp_path: Path):
    from tests.test_simplefin import ACCESS, ANSWER
    import json as _json

    home = K.KeyHome(K.MemoryKeyStore(), K.EnvFile(tmp_path / ".env", environ={"FDC_SIMPLEFIN_ACCESS_URL": ACCESS}))

    def fetch(url, headers):
        if "simplefin" in url:
            return _json.dumps(ANSWER).encode()
        return _fetch(fixtures)(url, headers)
    rep = sync.run_sync(project, only=["connections", "derive"], fetch=fetch, now=NOW, yf=lambda s, d: [],
                        sleep=lambda s: None, keys=home)
    by = {s.step: s for s in rep.steps}
    assert by["connections"].status == "ok" and "simplefin: 2 accounts" in by["connections"].message
    s = Store.open(project.db_path, migrate=False)
    assert s.query("SELECT COUNT(*) FROM accounts WHERE origin = 'simplefin'")[0][0] == 2
    assert s.query("SELECT COUNT(*) FROM cash_daily")[0][0] > 0
    assert s.query("SELECT COUNT(*) FROM reconciliation")[0][0] == 0
    s.close()


def test_a_skipped_connections_step_leaves_the_exit_code_to_the_other_network_steps(project):
    def down(url, headers):
        raise HttpError(500, url)
    rep = sync.run_sync(project, fetch=down, now=NOW, yf=lambda s, d: [], sleep=lambda s: None)
    by = {s.step: s for s in rep.steps}
    assert by["connections"].status == "skipped" and rep.exit_code == 1
```

  5. In `tests/test_config.py` append:

```python
def test_connections_min_hours(tmp_path):
    from financial_data_collector import config as C
    C.init_project(tmp_path)
    assert C.load_config(tmp_path).connections_min_hours == 6.0
    (tmp_path / "config.toml").write_text("[connections]\nmin_hours = 0.5\n")
    assert C.load_config(tmp_path).connections_min_hours == 0.5
    (tmp_path / "config.toml").write_text("[connections]\nmin_hours = -1\n")
    try:
        C.load_config(tmp_path)
        assert False
    except C.ConfigError as e:
        assert "min_hours" in str(e)
```

- [ ] **Step 2: Run them to see them fail.** `.venv/Scripts/python.exe -m pytest tests/test_sync.py tests/test_config.py -q` — expected: FAIL (step lists differ; no `connections_min_hours`).

- [ ] **Step 3: The setting.** In `src/financial_data_collector/config.py`:

  1. In `CONFIG_EXAMPLE`, after the `[sec]` block add:

```
[connections]
min_hours = 6                      # ask a connected service (fdc connect) at most this often
```

  2. In `ENV_EXAMPLE`, at the end add:

```
# A connection (fdc connect) keeps its key in your computer's key store. Only on a computer without one
# does fdc connect write it here instead (never paste a key here yourself unless there is no key store):
# FDC_SNAPTRADE_CLIENT_ID=
# FDC_SNAPTRADE_CONSUMER_KEY=
# FDC_SIMPLEFIN_ACCESS_URL=
```

  3. Add the field `connections_min_hours: float = 6.0` to `Config` (after `sec_max_age_hours`).
  4. In `load_config`, after `sec = raw.get("sec", {})` add `connections = raw.get("connections", {})`; then before `return Config(` add:

```python
    min_hours = connections.get("min_hours", 6)
    if not isinstance(min_hours, (int, float)) or isinstance(min_hours, bool) or min_hours < 0:
        raise ConfigError(f"{cfg_path}: [connections] min_hours must be a number of hours, 0 or more")
```

     and pass `connections_min_hours=float(min_hours),` to `Config`.
  5. Copy the two constants to their files so `tests/test_privacy.py::test_examples_match_package_constants` stays green: `.venv/Scripts/python.exe -c "from financial_data_collector import config as C; open('config.example.toml','w',encoding='utf-8',newline='').write(C.CONFIG_EXAMPLE); open('.env.example','w',encoding='utf-8',newline='').write(C.ENV_EXAMPLE)"`.

- [ ] **Step 4: The step.** In `src/financial_data_collector/sync.py`:

  1. Add the imports `from .connections.keys import KeyHome` and `from .connections.service import run as run_connections`.
  2. Change to `STEPS = ("connections", "ingest", "prices", "sec", "derive", "export")` and `NETWORK_STEPS = {"connections", "prices", "sec"}`.
  3. Replace `exit_code`'s first line with:

```python
        network = [s for s in self.steps if s.step in NETWORK_STEPS
                   and not (s.step == "connections" and s.status == "skipped")]
```

  4. Before `_step_ingest` add:

```python
def _step_connections(store: Store, cfg: Config, *, fetch, now, progress: Progress, keys=None, **_) -> tuple[int, str, str]:
    home = keys or KeyHome.for_root(cfg.root)
    return run_connections(store, home, min_hours=cfg.connections_min_hours, fetch=fetch, now=now, progress=progress)
```

  5. Add `"connections": _step_connections,` first in `_RUNNERS`.
  6. Add the parameter `keys: KeyHome | None = None,` to `run_sync` (after `progress`) and pass `keys=keys` in the `_RUNNERS[step](...)` call. `_step_prices` has no `**_` today: add one to its signature (`..., progress: Progress, **_)`), since every runner now receives `keys`.

- [ ] **Step 5: Run the tests.** `.venv/Scripts/python.exe -m pytest -q` — expected: PASS (including `tests/test_cli.py::test_sync_dry_run`, which only checks names that still appear).

- [ ] **Step 6: Commit.**

```bash
git add src/financial_data_collector/config.py src/financial_data_collector/sync.py config.example.toml .env.example tests/test_sync.py tests/test_config.py
git commit -m "fdc sync starts by fetching from every connection"
```

---

### Task 11: The commands

**Files:**
- Modify: `src/financial_data_collector/cli.py`
- Test: `tests/test_cli.py`

**Interfaces:**
- Consumes: `service.*`, `KeyHome.for_root`, `names.KINDS`, `http.post`, `http.fetch`, `simplefin.SITE`.
- Produces: `fdc connect snaptrade|simplefin [--with-user] [--no-browser]`, `fdc connections`, `fdc disconnect <name>`, `fdc accounts`, `fdc accounts set <label> [--kind K] [--limit N] [--rate P]`; module attributes `_ask` (hidden input) and `_open` (browser) for tests.

- [ ] **Step 1: Write the failing tests.** Append to `tests/test_cli.py`:

```python
import base64
import json

import pytest

from financial_data_collector.connections import keys as K


def _root(tmp_path, monkeypatch):
    cli.main(["--root", str(tmp_path), "init"])
    monkeypatch.setattr(cli, "_open", lambda url: True)
    return str(tmp_path)


def test_connect_snaptrade_prompts_hidden_and_lists_accounts(tmp_path, monkeypatch, capsys):
    from tests.test_snaptrade_connection import _fetch
    root = _root(tmp_path, monkeypatch)
    answers = iter([" synthetic-client \n", "synthetic-consumer-key"])
    monkeypatch.setattr(cli, "_ask", lambda prompt: next(answers))
    monkeypatch.setattr(cli.http, "fetch", _fetch([]))
    rc = cli.main(["--root", root, "connect", "snaptrade"])
    out = capsys.readouterr().out
    assert rc == 0
    assert "Example Brokerage Sample Roth IRA" in out and "roth_ira" in out and "guessed" in out
    assert "synthetic-client" not in out and "synthetic-consumer-key" not in out
    assert "fdc sync" in out
    rc = cli.main(["--root", root, "connections"])
    out = capsys.readouterr().out
    assert rc == 0 and "snaptrade" in out and "2" in out and "synthetic" not in out


def test_connect_snaptrade_refused_key_is_one_line(tmp_path, monkeypatch, capsys):
    from financial_data_collector.http import HttpError
    root = _root(tmp_path, monkeypatch)
    monkeypatch.setattr(cli, "_ask", lambda prompt: "x")

    def refused(url, headers):
        raise HttpError(401, url)
    monkeypatch.setattr(cli.http, "fetch", refused)
    assert cli.main(["--root", root, "connect", "snaptrade"]) == 1
    err = capsys.readouterr().err
    assert "error:" in err and "refused the key" in err and "Traceback" not in err


def test_connect_simplefin_and_disconnect(tmp_path, monkeypatch, capsys):
    from tests.test_simplefin import ACCESS, ANSWER
    root = _root(tmp_path, monkeypatch)
    token = base64.b64encode(b"https://bridge.example.org/simplefin/claim/abc").decode()
    monkeypatch.setattr(cli, "_ask", lambda prompt: token)
    monkeypatch.setattr(cli.http, "post", lambda url, headers: (200, ACCESS.encode()))
    monkeypatch.setattr(cli.http, "fetch", lambda url, headers: json.dumps(ANSWER).encode())
    assert cli.main(["--root", root, "connect", "simplefin"]) == 0
    out = capsys.readouterr().out
    assert "Example Bank Everyday Checking" in out and "needs attention" in out and ACCESS not in out
    assert cli.main(["--root", root, "accounts"]) == 0
    out = capsys.readouterr().out
    assert "Example Bank Cash Rewards Visa" in out and "credit_card" in out and "guessed" in out
    assert cli.main(["--root", root, "accounts", "set", "Example Bank Cash Rewards Visa", "--kind", "credit_card",
                     "--limit", "5000", "--rate", "24.9"]) == 0
    assert cli.main(["--root", root, "accounts"]) == 0
    out = capsys.readouterr().out
    assert "5,000" in out and "24.9" in out
    assert cli.main(["--root", root, "accounts", "set", "Nobody", "--kind", "checking"]) == 1
    with pytest.raises(SystemExit) as e:   # argparse itself refuses a kind that isn't one of KINDS
        cli.main(["--root", root, "accounts", "set", "Example Bank Cash Rewards Visa", "--kind", "spaceship"])
    assert e.value.code == 2
    assert cli.main(["--root", root, "accounts", "set", "Example Bank Cash Rewards Visa", "--rate", "150"]) == 2
    capsys.readouterr()
    assert cli.main(["--root", root, "disconnect", "simplefin"]) == 0
    assert cli.main(["--root", root, "disconnect", "simplefin"]) == 1
    assert cli.main(["--root", root, "connections"]) == 0
    assert "no connections" in capsys.readouterr().out


def test_connect_without_a_key_store_says_where_the_key_went(tmp_path, monkeypatch, capsys, no_real_key_store):
    from tests.test_snaptrade_connection import _fetch
    root = _root(tmp_path, monkeypatch)
    no_real_key_store.broken = True
    monkeypatch.setattr(cli, "_ask", lambda prompt: "v")
    monkeypatch.setattr(cli.http, "fetch", _fetch([]))
    assert cli.main(["--root", root, "connect", "snaptrade"]) == 0
    assert "no key store on this computer: the key is in .env instead" in capsys.readouterr().out
    assert "FDC_SNAPTRADE_CLIENT_ID=v" in (tmp_path / ".env").read_text()


def test_connect_with_user_asks_for_four_values(tmp_path, monkeypatch):
    from tests.test_snaptrade_connection import _fetch
    root = _root(tmp_path, monkeypatch)
    asked = []
    monkeypatch.setattr(cli, "_ask", lambda prompt: asked.append(prompt) or "v")
    monkeypatch.setattr(cli.http, "fetch", _fetch([]))
    assert cli.main(["--root", root, "connect", "snaptrade", "--with-user", "--no-browser"]) == 0
    assert len(asked) == 4
```

  Also change the last line of `test_init_prints_next_steps_for_a_stranger` to:

```python
    assert "investing" not in out                                  # the owner-only source is not mentioned on a fresh clone
    assert "fdc connect" in out
```

- [ ] **Step 2: Run them to see them fail.** `.venv/Scripts/python.exe -m pytest tests/test_cli.py -q` — expected: FAIL (`argparse` rejects `connect`).

- [ ] **Step 3: Add the commands.** In `src/financial_data_collector/cli.py`:

  1. Add the imports:

```python
import getpass
import webbrowser

from . import http
from .connections import service, simplefin
from .connections.base import ConnectionFailed
from .connections.keys import ENV_REF, KeyHome, SnapTradeKeys
from .connections.names import KINDS
```

  2. Add after `STALE_HOURS`:

```python
SNAPTRADE_SITE = "https://dashboard.snaptrade.com/"
SNAPTRADE_STEPS = (
    "1. Sign in at SnapTrade (free for personal use) and connect each brokerage there.\n"
    "2. Create a personal API key: a client id and a consumer key.\n"
    "3. Paste them below. What you type stays hidden and is checked with SnapTrade right away."
)
SIMPLEFIN_STEPS = (
    "1. Sign in at SimpleFIN Bridge ($15 a year) and connect each bank and card there.\n"
    "2. Choose New app and copy the setup token it shows. It works once.\n"
    "3. Paste it below. What you type stays hidden."
)
_ask = getpass.getpass      # hidden input; tests replace it
_open = webbrowser.open     # tests replace it
```

  3. In `_parser`, before `return p` add:

```python
    c = sub.add_parser("connect", help="connect a brokerage (snaptrade) or a bank (simplefin) with your own key")
    c.add_argument("service", choices=service.NAMES)
    c.add_argument("--with-user", action="store_true", help="a SnapTrade key that has a registered user (four values)")
    c.add_argument("--no-browser", action="store_true", help="don't open the service's site")
    sub.add_parser("connections", help="every connection: when it last worked, its accounts, its last error")
    d = sub.add_parser("disconnect", help="forget a connection's key; stored history stays")
    d.add_argument("name", choices=service.NAMES)
    a = sub.add_parser("accounts", help="every account, its kind, limit and rate; 'accounts set' changes them")
    asub = a.add_subparsers(dest="action")
    s2 = asub.add_parser("set", help="confirm or correct an account's kind, set a card's limit or a yearly rate")
    s2.add_argument("label")
    s2.add_argument("--kind", choices=KINDS)
    s2.add_argument("--limit", type=float, help="a card's or line's credit limit")
    s2.add_argument("--rate", type=float, help="yearly rate in percent: what cash earns or debt costs")
```

  4. In `cmd_init`, in `steps`, change the second line to `"2. Connect a brokerage: fdc connect snaptrade   (or a bank: fdc connect simplefin) — or download a Fidelity \"Portfolio Positions\" export and drop it in inbox/."`.

  5. Before `def main` add:

```python
def _accounts_table(rows) -> object:
    body = [[r.label, r.institution, r.account_type + ("" if r.kind_confirmed else " (guessed)")] for r in rows]
    return ui.table(["account", "institution", "kind"], body, title="accounts found")


def cmd_connect(root: Path, args) -> int:
    cfg = load_config(root)
    store = Store.open(cfg.db_path, backup_dir=cfg.backup_dir)
    home = KeyHome.for_root(cfg.root)
    now = datetime.now(timezone.utc)
    try:
        if args.service == "snaptrade":
            ui.console.print(ui.panel(SNAPTRADE_STEPS, "Connect your brokerages"))
            if not args.no_browser:
                _open(SNAPTRADE_SITE)
            client_id = _ask("Client id: ").strip()
            consumer_key = _ask("Consumer key: ").strip()
            user_id = user_secret = None
            if args.with_user:
                user_id = _ask("User id: ").strip()
                user_secret = _ask("User secret: ").strip()
            if not client_id or not consumer_key or (args.with_user and not (user_id and user_secret)):
                ui.error("every value is needed")
                return 2
            found, notes = service.connect_snaptrade(
                store, home, SnapTradeKeys(client_id, consumer_key, user_id or None, user_secret or None),
                fetch=http.fetch, now=now)
        else:
            ui.console.print(ui.panel(SIMPLEFIN_STEPS, "Connect your banks and cards"))
            if not args.no_browser:
                _open(simplefin.SITE)
            token = _ask("Setup token: ").strip()
            if not token:
                ui.error("a setup token is needed")
                return 2
            found, notes = service.connect_simplefin(store, home, token, post=http.post, fetch=http.fetch, now=now)
        if store.connection(args.service)["key_ref"] == ENV_REF:
            ui.hint("no key store on this computer: the key is in .env instead")
    except ConnectionFailed as e:
        ui.error(str(e))
        return 1
    finally:
        store.close()
    if found:
        ui.console.print(_accounts_table(found))
    else:
        ui.console.print(Text("connected, but no accounts yet: add them on the service's site", style="yellow"))
    for note in notes:
        ui.hint(note)
    ui.hint("run fdc sync to fetch balances, holdings and activity")
    return 0


def cmd_connections(root: Path) -> int:
    cfg = load_config(root)
    store = Store.open(cfg.db_path, backup_dir=cfg.backup_dir)
    try:
        rows = service.overview(store)
    finally:
        store.close()
    if not rows:
        ui.console.print("no connections")
        ui.hint("fdc connect snaptrade   or   fdc connect simplefin")
        return 0
    body = [[r.name, r.created_at[:10], r.last_ok_at or "never", r.accounts, r.last_error] for r in rows]
    ui.console.print(ui.table(["connection", "since", "last worked", "accounts", "last error"], body))
    return 0


def cmd_disconnect(root: Path, args) -> int:
    cfg = load_config(root)
    store = Store.open(cfg.db_path, backup_dir=cfg.backup_dir)
    try:
        gone = service.disconnect(store, KeyHome.for_root(cfg.root), args.name, now=datetime.now(timezone.utc))
    finally:
        store.close()
    if not gone:
        ui.error(f"no {args.name} connection")
        return 1
    ui.console.print(Text(f"{args.name} disconnected; its accounts and history stay", style="green"))
    return 0


def cmd_accounts(root: Path, args) -> int:
    cfg = load_config(root)
    store = Store.open(cfg.db_path, backup_dir=cfg.backup_dir)
    try:
        if args.action == "set":
            if args.limit is not None and args.limit < 0:
                ui.error("--limit is an amount, 0 or more")
                return 2
            if args.rate is not None and not 0 <= args.rate <= 100:
                ui.error("--rate is a yearly percentage between 0 and 100")
                return 2
            if args.kind is None and args.limit is None and args.rate is None:
                ui.error("nothing to set: give --kind, --limit or --rate")
                return 2
            if not store.set_account(args.label, kind=args.kind, credit_limit=args.limit, rate_pct=args.rate):
                ui.error(f"no account called {args.label!r}")
                ui.hint("fdc accounts lists them")
                return 1
            ui.console.print(Text(f"{args.label}: saved", style="green"))
            return 0
        rows = store.accounts_overview()
    finally:
        store.close()
    body = [[r["label"], r["institution"], r["account_type"] + ("" if r["kind_confirmed"] else " (guessed)"),
             r["credit_limit"], r["rate_pct"], r["origin"]] for r in rows]
    ui.console.print(ui.table(["account", "institution", "kind", "limit", "rate %", "from"], body))
    if any(not r["kind_confirmed"] for r in rows):
        ui.hint('confirm a guessed kind: fdc accounts set "<account>" --kind checking')
    return 0
```

  6. In `main`, before `except ConfigError`, add:

```python
        if args.cmd == "connect":
            return cmd_connect(root, args)
        if args.cmd == "connections":
            return cmd_connections(root)
        if args.cmd == "disconnect":
            return cmd_disconnect(root, args)
        if args.cmd == "accounts":
            return cmd_accounts(root, args)
```

     `argparse` exits with status 2 on its own for an unknown `--kind` (the `choices` check), which is what the test expects.

- [ ] **Step 4: Run the tests.** `.venv/Scripts/python.exe -m pytest tests/test_cli.py -q` — expected: PASS. Then everything: `.venv/Scripts/python.exe -m pytest -q` — expected: PASS.

- [ ] **Step 5: Try the command by hand, with no key.** `.venv/Scripts/fdc.exe --root <a scratch folder> init` then `.venv/Scripts/fdc.exe --root <the same folder> connections` — expected: `no connections` and the hint. Delete the scratch folder.

- [ ] **Step 6: Commit.**

```bash
git add src/financial_data_collector/cli.py tests/test_cli.py
git commit -m "fdc connect, connections, disconnect and accounts"
```

---

### Task 12: Docs, the privacy gate, the trial checklist

**Files:**
- Create: `docs/connections.md`
- Modify: `README.md`, `AGENTS.md`, `CHANGELOG.md`, `tests/test_privacy.py`

- [ ] **Step 1: Extend the privacy gate.** Append to `tests/test_privacy.py`:

```python
def test_no_key_value_and_no_worktree_is_tracked():
    import re
    key_line = re.compile(r"^\s*FDC_(SNAPTRADE|SIMPLEFIN)_[A-Z_]+\s*=\s*\S", re.M)
    for f in _tracked():
        assert not f.lower().startswith(".worktrees/"), f
        if f.startswith("tests/"):
            continue   # synthetic values under test are fine
        if f.endswith((".md", ".toml", ".py", ".example", ".txt", ".ps1", ".yml", ".yaml")):
            text = (ROOT / f).read_text(encoding="utf-8", errors="replace")
            assert not key_line.search(text), f"{f} holds a connection key value"
```

  Run `.venv/Scripts/python.exe -m pytest tests/test_privacy.py -q` — expected: PASS.

- [ ] **Step 2: Write `docs/connections.md`:**

````markdown
# Connections

`fdc connect` links a service you hold your own key for. The collector then fetches from it on every `fdc sync`.
Nothing here is written back to any service, and no key is ever kept in the database, `config.toml`, a log or a
message.

| Command | What it does |
|---|---|
| `fdc connect snaptrade` | Your brokerages, through [SnapTrade](https://snaptrade.com/) (free for personal use). Connect each brokerage on SnapTrade's site, create a personal API key there, and paste its client id and consumer key. A key that has a registered user (four values) works too: `fdc connect snaptrade --with-user`. |
| `fdc connect simplefin` | Your banks, cards and loans, through [SimpleFIN Bridge](https://beta-bridge.simplefin.org/) ($15 a year, up to 25 institutions). Connect them on its site, choose *New app*, copy the setup token and paste it. It works once. |
| `fdc connections` | Each connection: since when, when it last worked, how many accounts, its last error. |
| `fdc disconnect snaptrade` | Forgets the key. The accounts and their history stay. |
| `fdc accounts` | Every account with its kind (marked *guessed* until you confirm it), credit limit and rate. |
| `fdc accounts set "Example Bank Visa" --kind credit_card --limit 5000 --rate 24.9` | Confirms or corrects a kind, sets a card's limit or a yearly rate. A sync never changes what you set. |

Use one route per account: an account you import from a CSV and also connect is counted twice.

## What is fetched and stored

| From | Fetched | Stored |
|---|---|---|
| SnapTrade | accounts, positions, cash balances, activity, and which brokerage logins need repair | the same tables a Fidelity export fills (`position_snapshots`, `cash_balances`, `transactions`) |
| SimpleFIN | institution, account name, balance, available balance, balance date | one `cash_balances` row per account per day; a debt is below zero. Transactions are never requested (`balances-only=1`), so they never reach this computer |

Every SnapTrade request is a signed GET to one of five read paths; the collector holds no code that can place,
change or cancel an order, and `tests/test_snaptrade_connection.py` pins that.

**Accounts.** A connected account is known by a hash of the service's own id, never by its name, so a rename at
the service keeps its history. Its label is *institution + name*, fixed when first seen; digits that look like
part of an account number are stripped first (`Checking ...1234` becomes `Checking`), and a second account with
the same name gets ` 2`. Its kind comes from SnapTrade's own text when there is one, else from its name
(`Cash Rewards Visa` is a card); a SimpleFIN account with nothing in its name is a card when its balance is below
zero. `fdc accounts` shows which kinds are guesses. A SimpleFIN account is marked `flows = balance`: it has no
transactions to explain its changes, so a reader counts every change as money moved, never growth.

**Keys** live in your computer's key store (Windows Credential Manager, the macOS Keychain, the Linux Secret
Service), one entry per connection per warehouse. On a computer without one, `fdc connect` writes the key to
`.env` beside `config.toml` and says so; a key put there by hand (the variable `FDC_SIMPLEFIN_ACCESS_URL`) is a
connection too.

**Sync.** `connections` is the first step of `fdc sync`. A connection is asked at most every `[connections]
min_hours` (6 by default). One failing connection never stops the other; what happened is in `fdc connections`
and in `sync_runs`.

## When something fails

| The message | What to do |
|---|---|
| `no SnapTrade key saved: run fdc connect snaptrade` | Connect (again). |
| `SnapTrade refused the key: …` | Make a new personal key on SnapTrade's site and connect again. |
| `<brokerage> needs reconnecting on SnapTrade's site` | Sign in to that brokerage again through SnapTrade's site. The other accounts still update. |
| `SimpleFIN refused the saved token: …` | Make a new setup token on SimpleFIN's site and connect again. |
| `this setup token has been used: …` | A token works once: make a new one. |
| `SimpleFIN says the subscription has lapsed: …` | Renew it on SimpleFIN's site. |
| `<institution> needs attention on SimpleFIN's site: …` | SimpleFIN's own words: usually a bank wants you to sign in again there. |
| `<host> couldn't be reached (…)` | The service or your network is down; the next sync tries again. |
| `no key store on this computer: the key is in .env instead` | Not an error. Keep `.env` where only you can read it. |

## Trying it in a separate warehouse

The first real use is a trial in its own root, so a working setup is never touched:

```bash
fdc --root ..\fdc-trial init
fdc --root ..\fdc-trial connect snaptrade
fdc --root ..\fdc-trial connect simplefin
fdc --root ..\fdc-trial sync --only connections,derive
fdc --root ..\fdc-trial accounts
fdc --root ..\fdc-trial query "select account, as_of_date, total from account_values_daily order by 1, 2"
```

What to confirm, and to note in the spec's Appendix A as shapes and behaviour only, never a value:

1. A personal SnapTrade key lists accounts, positions, balances and activity on its own (no `--with-user`).
2. Which fields carry the institution, the name and the kind, and that `fdc accounts` shows no account number.
3. How a brokerage whose login expired shows up in `fdc connections` after a sync.
4. That SimpleFIN accepts `balances-only=1` and `version=2`, how a card's balance is signed, and whether names
   carried digits (the labels in `fdc accounts` say).
5. How often each service may be asked (a refusal shows as its status in the message).
````

- [ ] **Step 3: README.** In `README.md`:
  1. In *Quick start*, replace item 2 with: `2. Connect a brokerage (`fdc connect snaptrade`) or a bank (`fdc connect simplefin`) with your own key: see [docs/connections.md](docs/connections.md). Or, on fidelity.com: Accounts, Positions, Download, and save the CSV into `inbox/`. Optional: Accounts, Activity and Orders, Download, for transaction history; if that file has no `Account` column, import it explicitly: `fdc import path\to\History.csv --account "My Brokerage"`.`
  2. Replace the *Other brokers and sources* section body with:

```markdown
`fdc connect snaptrade` covers 35+ brokerages and `fdc connect simplefin` covers banks, cards and loans, each
with a key you hold yourself ([docs/connections.md](docs/connections.md)). Fidelity's positions CSV is the
file route. A second adapter ingests the JSON produced by a SnapTrade-based export (see `config.example.toml`,
section `sources.snaptrade`); it is skipped when the folder does not exist. Adding another file format means
writing one adapter that returns `models.Snapshot`; see `src/financial_data_collector/adapters/`.
```

  3. In *Privacy*, append: `Connection keys live in your computer's key store, never in the repo; account numbers and service ids from a connection are never stored, and bank transactions are never requested.`

- [ ] **Step 4: AGENTS.md.** In *Layout*, add after the `adapters/` line:

```
  connections/      names.py (labels, kinds, external keys), keys.py (key store / .env), snaptrade.py + simplefin.py (read-only fetchers -> models), service.py (connect / disconnect / run), redact.py
```

  change the `cli.py` line to `fdc init | sync | import | status | query | export | mcp | connect | connections | disconnect | accounts`, the `sync.py` line to `runs steps connections -> ingest -> prices -> sec -> derive -> export, ...`, and `migrations/*.sql` to end with `0006 connections`. In *Tables and views* add `connections` to the last bullet. In *Rules* add: `- **A key is never in the repo, the database, a log or a message.** Connections keep keys in the key store (or .env); every message from a fetcher passes through connections/redact.py. Tests use MemoryKeyStore; conftest keeps them off the real one.`

- [ ] **Step 5: CHANGELOG.** Under `## Unreleased` add first:

```markdown
- Connections: `fdc connect snaptrade` (brokerages, a personal key) and `fdc connect simplefin` (banks, cards
  and loans, balances only), `fdc connections`, `fdc disconnect`, `fdc accounts` and `fdc accounts set`. Keys
  live in the operating system's key store, or in `.env` on a computer without one. `fdc sync` gains a first
  step, `connections`. Migration 0006 adds the `connections` table, an account's `external_key`, `origin`,
  `kind_confirmed`, `credit_limit`, `rate_pct` and `flows`, and `cash_balances.available`. A connected account
  is known by a hashed key, so a rename keeps its history. Spec: `docs/superpowers/specs/2026-09-29-connections-design.md`.
```

- [ ] **Step 6: Run everything.** `.venv/Scripts/python.exe -m pytest -q` — expected: PASS.

- [ ] **Step 7: Commit.**

```bash
git add docs/connections.md README.md AGENTS.md CHANGELOG.md tests/test_privacy.py
git commit -m "Docs: connections, the trial checklist, and the privacy gate on keys"
```

---

## Self-review

- **Spec coverage:** §4 commands → Task 11; §5 keys → Task 4 (and the `.env` amendment in Task 1); §6.1 → Task 7; §6.2 → Task 8; §7 → Task 1 and Task 6; §8 → Tasks 2 and 6; §9 → Tasks 9 and 10; §10 messages → Tasks 7, 8, 9, 11; §11 → conftest, redact, the read-set test, the privacy test; §12 tests → each task's tests; §15 docs → Task 12; §3 trial → `docs/connections.md`. §14 (kestrel's reader) is a separate spec by design.
- **Placeholders:** none; every step carries its code.
- **Type consistency:** `KeyHome.save/load/forget/in_env`, `service.run(store, home, *, min_hours, fetch, now, progress)`, `Fetched.accounts/snapshots/transactions/notes`, `Store.set_account(label, *, kind, credit_limit, rate_pct)` and `Store.accounts_overview()` are used with the same names in Tasks 9, 10 and 11 as defined in Tasks 4, 6 and 9.
- **Review Focus:** (1) Task 9 `test_connect_warns_when_file_accounts_exist`; (2) Task 9 `test_an_account_that_vanishes_keeps_its_rows`; (3) Task 8 `test_accounts_become_balances_by_day_with_kinds_guessed` (the `None` balance); (4) Task 6 `test_a_connected_account_is_known_by_its_key_and_its_label_is_fixed`; (5) Task 4 `test_parse_strips_and_requires` and Task 11 `test_connect_snaptrade_prompts_hidden_and_lists_accounts` (the padded client id).
