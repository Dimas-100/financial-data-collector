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
        # only a fetch that worked is worth waiting on: a failed one is tried again next time, and a new key
        # (save_connection clears both stamps) right away
        if row["last_ok_at"] and now - _parse(row["last_ok_at"]) < timedelta(hours=min_hours):
            messages.append(f"{name}: fetched {_ago(row['last_ok_at'], now)}")
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
