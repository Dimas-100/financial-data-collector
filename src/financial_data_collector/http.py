"""One GET with retry/backoff. Collectors take this as an injected `fetch`."""
from __future__ import annotations

import time
from typing import Callable

import requests

Fetch = Callable[[str, dict[str, str] | None], bytes]
_RETRY_STATUSES = {403, 429}


class HttpError(Exception):
    """A non-200 answer, or (status 0) a transport failure after retries."""

    def __init__(self, status: int, url: str, detail: str | None = None):
        super().__init__(f"{detail or f'HTTP {status}'} for {url}")
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
        try:
            resp = requests.get(url, headers=headers, timeout=timeout)
        except requests.RequestException as e:  # DNS, connection reset, read timeout: retry like a 5xx
            last = HttpError(0, url, f"{type(e).__name__}: {e}")
            if attempt < retries - 1:
                sleep(2 ** attempt)
            continue
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


def post(url: str, headers: dict[str, str] | None = None, *, timeout: int = 30) -> tuple[int, bytes]:
    """One POST with an empty body and no retry: the status and the body, whatever the status. A SimpleFIN setup
    token is spent by the first attempt, so a second one would only report it used. The detail carries the
    failure's type only, never its text, which can repeat the address."""
    try:
        resp = requests.post(url, headers=headers, data=b"", timeout=timeout, allow_redirects=False)
    except requests.RequestException as e:
        raise HttpError(0, url, type(e).__name__) from None
    return resp.status_code, resp.content
