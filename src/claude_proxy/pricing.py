"""Model pricing — per-token USD rates used to turn token counts into dollars.

Rates come from LiteLLM's community-maintained ``model_prices_and_context_window.json``
(MIT, updated within hours of Anthropic price changes), which is the closest thing
to an open, machine-readable price list for the Claude models. The table is:

1. fetched at startup and every ``pricing.refresh_hours`` thereafter,
2. cached on disk (``$CLAUDE_PROXY_DATA_DIR/model_prices.json``) so a restart
   without network still costs correctly,
3. backed by a small bundled fallback so a brand-new install with no network at
   all still produces sane numbers for the current Claude line-up.

Costs are computed **at ingest** and stored alongside the token counts, so a
later price change never rewrites history — a dollar figure always reflects the
rate that was in force when the request was served.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import time
from dataclasses import dataclass
from pathlib import Path

import httpx

from .paths import PRICING_CACHE_FILE

log = logging.getLogger("claude_proxy.pricing")

LITELLM_URL = (
    "https://raw.githubusercontent.com/BerriAI/litellm/main/"
    "model_prices_and_context_window.json"
)

# Anthropic's long-context surcharge kicks in once a request's prompt exceeds
# this many tokens; LiteLLM exposes the higher rates as ``*_above_200k_tokens``.
LONG_CONTEXT_THRESHOLD = 200_000

_DATE_SUFFIX = re.compile(r"-\d{8}$")

# Bundled last-resort rates (USD per token), only used when neither the network
# nor the on-disk cache is available. Deliberately short — the online table is
# the real source; this just keeps a fresh, offline install from reporting $0.
_FALLBACK: dict[str, dict[str, float]] = {
    "claude-opus-5":    {"input_cost_per_token": 5e-06,  "output_cost_per_token": 2.5e-05,
                         "cache_read_input_token_cost": 5e-07, "cache_creation_input_token_cost": 6.25e-06},
    "claude-sonnet-5":  {"input_cost_per_token": 2e-06,  "output_cost_per_token": 1e-05,
                         "cache_read_input_token_cost": 2e-07, "cache_creation_input_token_cost": 2.5e-06},
    "claude-fable-5":   {"input_cost_per_token": 1e-05,  "output_cost_per_token": 5e-05,
                         "cache_read_input_token_cost": 1e-06, "cache_creation_input_token_cost": 1.25e-05},
    "claude-opus-4-5":  {"input_cost_per_token": 5e-06,  "output_cost_per_token": 2.5e-05,
                         "cache_read_input_token_cost": 5e-07, "cache_creation_input_token_cost": 6.25e-06},
    "claude-sonnet-4-5": {"input_cost_per_token": 3e-06, "output_cost_per_token": 1.5e-05,
                          "cache_read_input_token_cost": 3e-07, "cache_creation_input_token_cost": 3.75e-06},
    "claude-haiku-4-5": {"input_cost_per_token": 1e-06,  "output_cost_per_token": 5e-06,
                         "cache_read_input_token_cost": 1e-07, "cache_creation_input_token_cost": 1.25e-06},
    "claude-3-7-sonnet": {"input_cost_per_token": 3e-06, "output_cost_per_token": 1.5e-05,
                          "cache_read_input_token_cost": 3e-07, "cache_creation_input_token_cost": 3.75e-06},
    "claude-3-haiku":   {"input_cost_per_token": 2.5e-07, "output_cost_per_token": 1.25e-06,
                         "cache_read_input_token_cost": 3e-08, "cache_creation_input_token_cost": 3e-07},
}


@dataclass(frozen=True)
class Rates:
    """USD per token for one model, with the optional long-context tier."""

    input: float = 0.0
    output: float = 0.0
    cache_read: float = 0.0
    cache_write: float = 0.0
    input_long: float | None = None
    output_long: float | None = None
    cache_read_long: float | None = None
    cache_write_long: float | None = None

    @property
    def known(self) -> bool:
        return any((self.input, self.output, self.cache_read, self.cache_write))

    def cost(
        self,
        input_tokens: int = 0,
        output_tokens: int = 0,
        cache_read: int = 0,
        cache_creation: int = 0,
    ) -> float:
        # Anthropic prices the whole request at the long-context rate once the
        # *prompt* (fresh input + everything read from or written to cache)
        # crosses the threshold.
        prompt = input_tokens + cache_read + cache_creation
        long = prompt > LONG_CONTEXT_THRESHOLD

        def pick(base: float, hi: float | None) -> float:
            return hi if (long and hi is not None) else base

        return (
            input_tokens * pick(self.input, self.input_long)
            + output_tokens * pick(self.output, self.output_long)
            + cache_read * pick(self.cache_read, self.cache_read_long)
            + cache_creation * pick(self.cache_write, self.cache_write_long)
        )


_ZERO = Rates()


def _rates_from_entry(e: dict) -> Rates:
    def f(key: str) -> float:
        v = e.get(key)
        return float(v) if isinstance(v, int | float) else 0.0

    def opt(key: str) -> float | None:
        v = e.get(key)
        return float(v) if isinstance(v, int | float) else None

    # NB: we always use the 5-minute cache-write rate. The usage block doesn't
    # tell us which TTL a client asked for, and 5m is the default.
    return Rates(
        input=f("input_cost_per_token"),
        output=f("output_cost_per_token"),
        cache_read=f("cache_read_input_token_cost"),
        cache_write=f("cache_creation_input_token_cost"),
        input_long=opt("input_cost_per_token_above_200k_tokens"),
        output_long=opt("output_cost_per_token_above_200k_tokens"),
        cache_read_long=opt("cache_read_input_token_cost_above_200k_tokens"),
        cache_write_long=opt("cache_creation_input_token_cost_above_200k_tokens"),
    )


class PricingTable:
    """Model-name → :class:`Rates`, with resolution of the many name variants."""

    def __init__(self, cache_file: Path | None = None, url: str = LITELLM_URL) -> None:
        self.cache_file = Path(cache_file) if cache_file else PRICING_CACHE_FILE
        self.url = url
        self.source = "fallback"
        self.fetched_at: float = 0.0
        self.last_error: str | None = None
        self._raw: dict[str, dict] = {}
        self._resolved: dict[str, Rates] = {}
        self._unpriced: set[str] = set()
        self.load()

    # --- loading ----------------------------------------------------------

    def load(self) -> None:
        """Seed from the on-disk cache, falling back to the bundled table."""
        try:
            blob = json.loads(self.cache_file.read_text())
            self._adopt(blob.get("prices", blob), source="cache",
                        fetched_at=float(blob.get("fetched_at", 0.0)))
            return
        except FileNotFoundError:
            pass
        except Exception as e:  # noqa: BLE001 - a broken cache must not stop startup
            log.warning("Unusable pricing cache %s: %s", self.cache_file, e)
        self._adopt(_FALLBACK, source="fallback", fetched_at=0.0)

    def _adopt(self, prices: dict, source: str, fetched_at: float) -> None:
        self._raw = {k: v for k, v in prices.items() if isinstance(v, dict)}
        self._resolved = {}
        self._unpriced = set()
        self.source = source
        self.fetched_at = fetched_at
        log.info("Pricing table loaded from %s (%d models)", source, len(self._raw))

    async def refresh(self, client: httpx.AsyncClient | None = None) -> bool:
        """Fetch the online table; keep the current one on any failure."""
        own = client is None
        client = client or httpx.AsyncClient(timeout=20.0)
        try:
            r = await client.get(self.url, timeout=20.0)
            r.raise_for_status()
            prices = r.json()
            if not isinstance(prices, dict) or len(prices) < 50:
                raise ValueError(f"unexpected payload ({type(prices).__name__})")
            now = time.time()
            self._adopt(prices, source="online", fetched_at=now)
            self._write_cache(prices, now)
            self.last_error = None
            return True
        except Exception as e:  # noqa: BLE001 - pricing is best-effort
            self.last_error = str(e)
            log.warning("Pricing refresh failed (%s) — keeping %s table", e, self.source)
            return False
        finally:
            if own:
                await client.aclose()

    def _write_cache(self, prices: dict, fetched_at: float) -> None:
        """Atomically replace the cache file (same dir → rename is atomic)."""
        try:
            self.cache_file.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.cache_file.with_suffix(".tmp")
            tmp.write_text(json.dumps({"fetched_at": fetched_at, "prices": prices}))
            os.replace(tmp, self.cache_file)
        except Exception as e:  # noqa: BLE001
            log.warning("Couldn't cache pricing table: %s", e)

    # --- lookup -----------------------------------------------------------

    def _candidates(self, model: str) -> list[str]:
        m = model.strip()
        out = [m, m.lower()]
        base = m.lower()
        # Claude Code appends a context-window marker, e.g. "claude-opus-5[1m]".
        base = base.split("[", 1)[0]
        out.append(base)
        if "/" in base:  # "anthropic/claude-…" style
            out.append(base.split("/", 1)[1])
        else:
            out.append(f"anthropic/{base}")
        stripped = _DATE_SUFFIX.sub("", out[-2] if "/" in base else base)
        out += [stripped, f"anthropic/{stripped}"]
        seen: set[str] = set()
        uniq = []
        for c in out:
            if c and c not in seen:
                seen.add(c)
                uniq.append(c)
        return uniq

    def _fuzzy(self, name: str) -> dict | None:
        """Match a name the exact candidates missed.

        Two shapes show up in practice: a dated snapshot of a family the table
        only lists undated (``claude-opus-4-9-20260901`` → ``claude-opus-4-9``),
        and a model the table only carries under a provider-qualified key
        (``claude-3-5-sonnet-20240620`` → ``anthropic.claude-3-5-sonnet-…-v1:0``).
        Prefer the first: it's an exact family match rather than a substring.
        """
        prefix = ""
        for key in self._raw:
            if name.startswith(key) and len(key) > len(prefix):
                prefix = key
        if prefix:
            return self._raw[prefix]
        # Shortest containing key = the least extra qualification around the name.
        best = min((k for k in self._raw if name in k), key=len, default="")
        return self._raw.get(best) if best else None

    def rates(self, model: str) -> Rates:
        """Resolve a model name to rates, memoized. Unknown → all-zero rates."""
        if not model:
            return _ZERO
        hit = self._resolved.get(model)
        if hit is not None:
            return hit
        entry = None
        for cand in self._candidates(model):
            entry = self._raw.get(cand)
            if entry is not None:
                break
        if entry is None:
            entry = self._fuzzy(model.lower().split("[", 1)[0])
        rates = _rates_from_entry(entry) if entry else _ZERO
        if not rates.known and model not in self._unpriced:
            self._unpriced.add(model)
            log.warning("No pricing for model %r — its usage will cost $0.00", model)
        self._resolved[model] = rates
        return rates

    def cost(
        self,
        model: str,
        input_tokens: int = 0,
        output_tokens: int = 0,
        cache_read: int = 0,
        cache_creation: int = 0,
    ) -> float:
        return self.rates(model).cost(input_tokens, output_tokens, cache_read, cache_creation)

    def status(self) -> dict:
        return {
            "source": self.source,
            "url": self.url,
            "fetched_at": self.fetched_at,
            "models": len(self._raw),
            "unpriced_models": sorted(self._unpriced),
            "last_error": self.last_error,
        }


async def refresh_loop(state) -> None:  # noqa: ANN001
    """Refresh the pricing table on startup, then every ``refresh_hours``."""
    while True:
        interval = max(1, int(state.config.pricing.refresh_hours)) * 3600
        if state.config.pricing.online:
            await state.pricing.refresh(state.probe_client)
            state.notify()
        await asyncio.sleep(interval)
