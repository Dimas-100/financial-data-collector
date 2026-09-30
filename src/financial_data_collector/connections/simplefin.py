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
    """The balance date's UTC day. A missing, absurd or future stamp (milliseconds where seconds were expected,
    a clock ahead of ours) reads as today rather than failing the whole fetch."""
    today = now.astimezone(timezone.utc)
    seconds = num(stamp)
    if not seconds or seconds <= 0 or seconds > today.timestamp():
        return today.strftime("%Y-%m-%d")
    try:
        return datetime.fromtimestamp(seconds, tz=timezone.utc).strftime("%Y-%m-%d")
    except (OverflowError, OSError, ValueError):
        return today.strftime("%Y-%m-%d")


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
