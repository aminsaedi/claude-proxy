"""In-memory stores for upstream OAuth tokens and downstream virtual keys."""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import Literal

import yaml

from .paths import TOKENS_FILE, VKEYS_FILE

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
        self.health: dict[str, TokenHealth] = {}
        self.load()

    def load(self) -> None:
        if not TOKENS_FILE.exists():
            raise RuntimeError(f"{TOKENS_FILE} not found — create it with at least one token")
        data = yaml.safe_load(TOKENS_FILE.read_text()) or {}
        entries = data.get("tokens", [])
        tokens = {t["name"]: t["token"] for t in entries}
        if not tokens:
            raise RuntimeError("tokens.yaml must contain at least one token")
        default = next((t["name"] for t in entries if t.get("default")), next(iter(tokens)))
        self._tokens = tokens
        self._order = list(tokens)
        self.active = default
        # preserve existing health across reloads; init new tokens
        self.health = {n: self.health.get(n, TokenHealth()) for n in tokens}
        log.info("Loaded %d token(s), default: %s", len(tokens), default)

    def names(self) -> list[str]:
        return list(self._order)

    def secret(self, name: str) -> str:
        return self._tokens[name]

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
    """Downstream client keys, hot-reloaded from virtual_keys.yaml on mtime change."""

    def __init__(self) -> None:
        self._by_name: dict[str, str] = {}
        self._by_key: dict[str, str] = {}
        self._mtime: float = 0.0
        self.load()

    def load(self) -> None:
        by_name: dict[str, str] = {}
        if VKEYS_FILE.exists():
            data = yaml.safe_load(VKEYS_FILE.read_text()) or {}
            by_name = {vk["name"]: vk["key"] for vk in data.get("virtual_keys", [])}
            self._mtime = VKEYS_FILE.stat().st_mtime
        if not by_name:
            raise RuntimeError("No virtual keys configured: create virtual_keys.yaml")
        self._by_name = by_name
        self._by_key = {v: k for k, v in by_name.items()}

    def reload_if_changed(self) -> bool:
        try:
            mtime = VKEYS_FILE.stat().st_mtime
        except OSError:
            return False
        if mtime == self._mtime:
            return False
        try:
            self.load()
        except Exception as e:  # noqa: BLE001 - keep serving old keys on bad file
            log.warning("Failed to reload virtual_keys.yaml: %s", e)
            return False
        log.info("Reloaded virtual_keys.yaml (%d keys)", len(self._by_name))
        return True

    def resolve(self, key: str | None) -> str | None:
        """Return the client name for a presented key, or None if unknown."""
        if not key:
            return None
        return self._by_key.get(key)

    def names(self) -> list[str]:
        return list(self._by_name)
