"""Per-caller throttling of invalid-key attempts.

The proxy is on the public internet and, until this existed, an invalid key
cost the caller nothing but a round trip — so a virtual key could be guessed at
indefinitely. This bounds that.

Deliberately narrow. The throttle is consulted only *after* a key has already
failed to resolve, so it can never touch an authenticated request no matter how
wrong its idea of the caller's address is. That matters more than it sounds:
every request through the Cloudflare Tunnel used to look like it came from one
of two pod IPs, and a guard applied before authentication would have blocked
every real user the moment one misconfigured client tripped it.

It is not a defence against volume. An attempt still costs a connection and a
dictionary lookup, and dropping traffic before it reaches the origin at all is
the edge's job. What this buys is a hard ceiling on guesses per source, plus a
metric that says when someone is trying.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field


@dataclass
class _Attempts:
    count: int = 0
    window_start: float = 0.0
    blocked_until: float = 0.0
    last_seen: float = field(default=0.0)


class AuthGuard:
    """A fixed-window counter of authentication failures, per caller.

    Memory is bounded by `max_tracked`: an attacker rotating source addresses
    would otherwise turn this into an unbounded dictionary, which is a better
    attack than the one it prevents. When full, the least recently seen entries
    are dropped — losing the count for an idle source is harmless, because a
    source that matters is by definition still trying.
    """

    def __init__(self, max_failures: int = 20, window_seconds: int = 300,
                 block_seconds: int = 300, max_tracked: int = 4096) -> None:
        self.max_failures = max_failures
        self.window_seconds = window_seconds
        self.block_seconds = block_seconds
        self.max_tracked = max_tracked
        self._seen: dict[str, _Attempts] = {}

    @property
    def enabled(self) -> bool:
        return self.max_failures > 0

    def configure(self, max_failures: int, window_seconds: int, block_seconds: int) -> None:
        """Adopt new settings without discarding what is already being tracked."""
        self.max_failures = max_failures
        self.window_seconds = max(1, window_seconds)
        self.block_seconds = max(1, block_seconds)

    def retry_after(self, caller: str, now: float | None = None) -> int | None:
        """Seconds until `caller` may try again, or None if it may try now."""
        if not self.enabled:
            return None
        entry = self._seen.get(caller)
        if entry is None:
            return None
        now = time.time() if now is None else now
        if entry.blocked_until > now:
            return max(1, int(entry.blocked_until - now))
        return None

    def record_failure(self, caller: str, now: float | None = None) -> int | None:
        """Count one failed attempt. Returns the retry-after if this one trips the block.

        Returning the value only on the transition keeps the caller's logging
        to one line per block rather than one per attempt.
        """
        if not self.enabled:
            return None
        now = time.time() if now is None else now
        entry = self._seen.get(caller)
        if entry is None:
            if len(self._seen) >= self.max_tracked:
                self._evict(now)
            entry = self._seen[caller] = _Attempts(window_start=now)
        entry.last_seen = now
        if now - entry.window_start > self.window_seconds:
            entry.count, entry.window_start = 0, now
        entry.count += 1
        if entry.count >= self.max_failures and entry.blocked_until <= now:
            entry.blocked_until = now + self.block_seconds
            return self.block_seconds
        return None

    def forget(self, caller: str) -> None:
        """Drop a caller's history — used when it authenticates successfully."""
        self._seen.pop(caller, None)

    def _evict(self, now: float) -> None:
        """Make room, oldest first, and take the expired entries while we are here."""
        for caller, entry in list(self._seen.items()):
            if entry.blocked_until <= now and now - entry.last_seen > self.window_seconds:
                del self._seen[caller]
        if len(self._seen) < self.max_tracked:
            return
        # Never evict a caller that is currently blocked. Flooding the table
        # from fresh addresses would otherwise be a way to clear your own
        # block, which turns the bound into the bypass.
        victims = sorted(((c, e) for c, e in self._seen.items() if e.blocked_until <= now),
                         key=lambda kv: kv[1].last_seen)
        for caller, _ in victims[: max(1, len(victims) // 4)]:
            del self._seen[caller]

    def snapshot(self, now: float | None = None) -> dict[str, object]:
        now = time.time() if now is None else now
        blocked = {c: max(1, int(e.blocked_until - now))
                   for c, e in self._seen.items() if e.blocked_until > now}
        return {"enabled": self.enabled, "tracked": len(self._seen),
                "blocked": blocked, "max_failures": self.max_failures,
                "window_seconds": self.window_seconds,
                "block_seconds": self.block_seconds}
