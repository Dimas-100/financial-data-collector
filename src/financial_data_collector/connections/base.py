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
