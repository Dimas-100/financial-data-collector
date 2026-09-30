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
