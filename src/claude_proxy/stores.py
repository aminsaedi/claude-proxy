"""In-memory stores for upstream OAuth tokens and downstream virtual keys."""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass, field
from typing import Literal

from . import db

log = logging.getLogger("claude_proxy.stores")

HealthStatus = Literal["unchecked", "healthy", "rate_limited", "unhealthy"]


@dataclass
class TokenHealth:
    """Runtime health of a single upstream token.

    ``rate_limited`` (HTTP 429) is deliberately distinct from ``unhealthy``
    (auth/other failure): a rate-limited token is fine and will recover, so we
    fail *over* from it temporarily rather than treating it as dead.
    """

    status: HealthStatus = "unchecked"
    error_count: int = 0
    last_checked: float = 0.0
    rate_limited_until: float = 0.0
    headers: dict[str, str] = field(default_factory=dict)

    @property
    def healthy(self) -> bool:
        # Kept for backward-compatible /state payloads.
        return self.status in ("healthy", "unchecked")

    def usable(self, now: float | None = None) -> bool:
        now = time.time() if now is None else now
        if self.status == "unhealthy":
            return False
        if self.status == "rate_limited" and now < self.rate_limited_until:
            return False
        return True

    def util_5h(self) -> float | None:
        v = self.headers.get("anthropic-ratelimit-unified-5h-utilization")
        try:
            return float(v) if v is not None else None
        except (TypeError, ValueError):
            return None


class TokenStore:
    """Holds upstream tokens, the active selection, and per-token health."""

    def __init__(self) -> None:
        self._tokens: dict[str, str] = {}
        self._order: list[str] = []
        self.active: str = ""
        self.default: str = ""
        self.health: dict[str, TokenHealth] = {}
        self.load()

    def load(self) -> None:
        entries = db.list_tokens()
        tokens = {t["name"]: t["token"] for t in entries}
        if not tokens:
            raise RuntimeError("No upstream tokens in the database — add one via manage.py")
        default = next((t["name"] for t in entries if t.get("default")), next(iter(tokens)))
        self._tokens = tokens
        self._order = list(tokens)
        self.active = default
        self.default = default
        # preserve existing health across reloads; init new tokens
        self.health = {n: self.health.get(n, TokenHealth()) for n in tokens}
        log.info("Loaded %d token(s), default: %s", len(tokens), default)

    def reload(self) -> None:
        """Re-read tokens from the DB after a CRUD change, keeping the live active
        selection if that token still exists (``load`` alone resets it to default)."""
        prev = self.active
        self.load()
        if prev in self._tokens:
            self.active = prev

    def names(self) -> list[str]:
        return list(self._order)

    def secret(self, name: str) -> str:
        return self._tokens[name]

    def preview(self, name: str, show: int = 12) -> str:
        """A masked prefix of the secret, safe to show in the console at a glance."""
        s = self._tokens.get(name, "")
        return s[:show] + "…" if len(s) > show + 1 else s

    def active_secret(self) -> str:
        return self._tokens[self.active]

    def __contains__(self, name: str) -> bool:
        return name in self._tokens

    def set_active(self, name: str) -> None:
        if name not in self._tokens:
            raise KeyError(name)
        self.active = name

    def record_headers(self, name: str, headers: dict[str, str]) -> None:
        rl = {k: v for k, v in headers.items() if k.lower().startswith("anthropic-")}
        if rl and name in self.health:
            self.health[name].headers = rl

    def mark_healthy(self, name: str) -> None:
        h = self.health[name]
        h.status = "healthy"
        h.error_count = 0
        h.last_checked = time.time()

    def mark_rate_limited(self, name: str, retry_after_seconds: float | None = None) -> None:
        h = self.health[name]
        h.status = "rate_limited"
        h.last_checked = time.time()
        # Default cooldown of 60s if upstream gave no hint.
        h.rate_limited_until = time.time() + (retry_after_seconds or 60.0)

    def mark_unhealthy(self, name: str) -> None:
        h = self.health[name]
        h.status = "unhealthy"
        h.error_count += 1
        h.last_checked = time.time()

    def failover_order(self, now: float | None = None) -> list[str]:
        """Names to try for a request, best first: active (if usable), then
        other usable tokens by ascending 5h utilization, then everything else
        (rate-limited/unhealthy) as a last resort.
        """
        now = time.time() if now is None else now

        def util(name: str) -> float:
            u = self.health[name].util_5h()
            return u if u is not None else -1.0  # unknown util → try early

        usable = [n for n in self._order if self.health[n].usable(now)]
        unusable = [n for n in self._order if not self.health[n].usable(now)]
        usable.sort(key=util)
        # Active goes first if it is usable.
        if self.active in usable:
            usable.remove(self.active)
            usable.insert(0, self.active)
        return usable + unusable

    def state_payload(self) -> dict:
        return {
            name: {
                "status": h.status,
                "healthy": h.healthy,
                "error_count": h.error_count,
                "last_checked": h.last_checked,
                "rate_limited_until": h.rate_limited_until,
            }
            for name, h in self.health.items()
        }


class VirtualKeyStore:
    """Downstream client keys, cached in memory and refreshed from the DB."""

    def __init__(self) -> None:
        self._by_name: dict[str, str] = {}
        self._by_key: dict[str, str] = {}
        self._sig: frozenset = frozenset()
        # Serialises overlapping reloads; see reload_if_changed.
        self._lock = threading.Lock()
        self.load()

    def load(self) -> None:
        by_name = {vk["name"]: vk["key"] for vk in db.list_virtual_keys()}
        if not by_name:
            raise RuntimeError("No virtual keys in the database — add one via manage.py")
        self._by_name = by_name
        self._by_key = {v: k for k, v in by_name.items()}
        self._sig = frozenset(by_name.items())

    def reload_if_changed(self) -> bool:
        """Adopt key edits from the DB, whoever made them.

        The read and the adopt are held under one lock together, and that
        pairing is the point. Two reloads can overlap — the background loop
        every few seconds, and an explicit one right after an admin write — and
        if the slow background read were allowed to finish *after* the admin's,
        it would install its older snapshot on top. For a rotation that means
        the key you just revoked starts working again until the next tick.

        Serialising read-with-adopt makes the reload that started last also
        finish last, so the freshest view of the database is the one that wins.
        The request path never takes this lock: ``resolve`` reads a dict that is
        only ever swapped in by whole-reference assignment, so it sees the old
        map or the new one, never a half-built one.
        """
        with self._lock:
            try:
                by_name = {vk["name"]: vk["key"] for vk in db.list_virtual_keys()}
            except Exception as e:  # noqa: BLE001 - keep serving cached keys on DB error
                log.warning("Failed to reload virtual keys: %s", e)
                return False
            sig = frozenset(by_name.items())
            if sig == self._sig or not by_name:
                return False
            self._by_name = by_name
            self._by_key = {v: k for k, v in by_name.items()}
            self._sig = sig
        log.info("Reloaded virtual keys from DB (%d keys)", len(by_name))
        return True

    def resolve(self, key: str | None) -> str | None:
        """Return the client name for a presented key, or None if unknown."""
        if not key:
            return None
        return self._by_key.get(key)

    def __contains__(self, name: str) -> bool:
        return name in self._by_name

    def names(self) -> list[str]:
        return list(self._by_name)

    def secret(self, name: str) -> str | None:
        return self._by_name.get(name)

    def preview(self, name: str, show: int = 12) -> str:
        s = self._by_name.get(name, "")
        return s[:show] + "…" if len(s) > show + 1 else s
