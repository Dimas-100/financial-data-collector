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
