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
