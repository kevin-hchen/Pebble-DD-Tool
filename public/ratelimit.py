"""Per-IP rate limiting, in memory, recording nothing about the request.

A fixed-size sliding window per address. The counter holds an address and a list
of timestamps — never a path, never a query, never a body — so the rate limiter
cannot become the place where request contents are retained by accident. That
matters here more than in most services: this one promises it does not keep what
you type, and a limiter that logged "this IP searched X three times" would break
that promise while looking like abuse prevention.

In memory rather than Redis because the service is stateless-by-design and a
shared limiter would be a second place holding per-visitor data. A restart
forgets everyone, which is the correct trade for a read-only public snapshot:
the worst case is a visitor gets a few extra requests through.
"""

from __future__ import annotations

import threading
import time
from collections import deque
from dataclasses import dataclass


@dataclass(frozen=True)
class RateDecision:
    allowed: bool
    remaining: int
    retry_after_seconds: int


class RateLimiter:
    """Sliding-window counter keyed by client address."""

    def __init__(self, limit: int, window_seconds: int) -> None:
        self.limit = max(1, limit)
        self.window = max(1, window_seconds)
        self._hits: dict[str, deque] = {}
        self._lock = threading.Lock()

    def check(self, client: str, now: float | None = None) -> RateDecision:
        now = time.monotonic() if now is None else now
        key = client or "unknown"
        with self._lock:
            hits = self._hits.setdefault(key, deque())
            cutoff = now - self.window
            while hits and hits[0] <= cutoff:
                hits.popleft()
            if len(hits) >= self.limit:
                retry = int(max(1, self.window - (now - hits[0])))
                return RateDecision(False, 0, retry)
            hits.append(now)
            # Bound the table so a spray of addresses cannot grow it without
            # limit; the oldest idle entries go first.
            if len(self._hits) > 10_000:
                self._evict(now)
            return RateDecision(True, self.limit - len(hits), 0)

    def _evict(self, now: float) -> None:
        cutoff = now - self.window
        for key in [k for k, v in self._hits.items() if not v or v[-1] <= cutoff]:
            self._hits.pop(key, None)


def client_address(request) -> str:
    """The caller's address, from the proxy header when one is trusted.

    Takes the FIRST entry of X-Forwarded-For, which is the original client;
    later entries are proxies. Falls back to the socket address. Truncated to a
    sane length so a hostile header cannot become an unbounded dictionary key.
    """
    forwarded = request.headers.get("x-forwarded-for", "")
    if forwarded:
        return forwarded.split(",")[0].strip()[:64]
    client = getattr(request, "client", None)
    return (getattr(client, "host", "") or "unknown")[:64]
