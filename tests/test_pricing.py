"""Pricing table: name resolution, cost math, and where the rates come from."""

from __future__ import annotations

import json

from claude_proxy.pricing import PricingTable

_TABLE = {
    "claude-opus-5": {
        "input_cost_per_token": 5e-06, "output_cost_per_token": 2.5e-05,
        "cache_read_input_token_cost": 5e-07, "cache_creation_input_token_cost": 6.25e-06,
    },
    "claude-sonnet-4-5": {
        "input_cost_per_token": 3e-06, "output_cost_per_token": 1.5e-05,
        "cache_read_input_token_cost": 3e-07, "cache_creation_input_token_cost": 3.75e-06,
        "input_cost_per_token_above_200k_tokens": 6e-06,
        "output_cost_per_token_above_200k_tokens": 2.25e-05,
        "cache_read_input_token_cost_above_200k_tokens": 6e-07,
        "cache_creation_input_token_cost_above_200k_tokens": 7.5e-06,
    },
    "anthropic/claude-haiku-4-5": {
        "input_cost_per_token": 1e-06, "output_cost_per_token": 5e-06,
    },
    "anthropic.claude-3-5-sonnet-20240620-v1:0": {
        "input_cost_per_token": 3e-06, "output_cost_per_token": 1.5e-05,
    },
    "not-a-dict": None,
}


def _table(tmp_path, prices=None, fetched_at=123.0) -> PricingTable:
    cache = tmp_path / "model_prices.json"
    cache.write_text(json.dumps({"fetched_at": fetched_at, "prices": prices or _TABLE}))
    return PricingTable(cache_file=cache)


def test_loads_from_cache_file(tmp_path):
    p = _table(tmp_path)
    assert p.source == "cache"
    assert p.fetched_at == 123.0
    assert p.status()["models"] == 4  # the None entry is skipped


def test_falls_back_when_no_cache(tmp_path):
    p = PricingTable(cache_file=tmp_path / "missing.json")
    assert p.source == "fallback"
    assert p.rates("claude-opus-5").input > 0


def test_unreadable_cache_falls_back(tmp_path):
    cache = tmp_path / "model_prices.json"
    cache.write_text("{not json")
    assert PricingTable(cache_file=cache).source == "fallback"


def test_exact_and_variant_name_resolution(tmp_path):
    p = _table(tmp_path)
    assert p.rates("claude-opus-5").input == 5e-06
    # Claude Code's context-window marker
    assert p.rates("claude-opus-5[1m]").input == 5e-06
    # provider-qualified either way round
    assert p.rates("claude-haiku-4-5").input == 1e-06
    assert p.rates("anthropic/claude-haiku-4-5").input == 1e-06
    # a dated snapshot of a family listed undated
    assert p.rates("claude-sonnet-4-5-20250929").input == 3e-06
    # only present under a provider-prefixed key
    assert p.rates("claude-3-5-sonnet-20240620").input == 3e-06


def test_unknown_model_costs_zero_and_is_reported(tmp_path):
    p = _table(tmp_path)
    assert p.cost("some-other-llm", 1000, 1000) == 0.0
    assert p.status()["unpriced_models"] == ["some-other-llm"]


def test_cost_includes_cache_tokens(tmp_path):
    p = _table(tmp_path)
    cost = p.cost("claude-opus-5", input_tokens=1000, output_tokens=500,
                  cache_read=200_00, cache_creation=1000)
    expected = 1000 * 5e-06 + 500 * 2.5e-05 + 20000 * 5e-07 + 1000 * 6.25e-06
    assert cost == expected
    # cache read is the bulk of a real Claude Code request — never drop it
    assert p.cost("claude-opus-5", cache_read=1_000_000) == 0.5


def test_long_context_tier_applies_above_200k(tmp_path):
    p = _table(tmp_path)
    base = p.cost("claude-sonnet-4-5", input_tokens=100_000, output_tokens=1000)
    assert base == 100_000 * 3e-06 + 1000 * 1.5e-05
    # the *prompt* crossing 200k switches the whole request to the higher tier
    long = p.cost("claude-sonnet-4-5", input_tokens=250_000, output_tokens=1000)
    assert long == 250_000 * 6e-06 + 1000 * 2.25e-05
    # cache tokens count toward the prompt size that triggers the tier
    mixed = p.cost("claude-sonnet-4-5", input_tokens=1000, cache_read=250_000)
    assert mixed == 1000 * 6e-06 + 250_000 * 6e-07


def test_model_without_long_tier_keeps_base_rates(tmp_path):
    p = _table(tmp_path)
    assert p.cost("claude-opus-5", input_tokens=500_000) == 500_000 * 5e-06


async def test_refresh_rejects_a_junk_payload_and_keeps_current(tmp_path):
    import httpx

    p = _table(tmp_path)
    client = httpx.AsyncClient(transport=httpx.MockTransport(
        lambda r: httpx.Response(200, json={"only": "one entry"})))
    assert await p.refresh(client) is False
    assert p.source == "cache"  # unchanged
    assert p.last_error
    await client.aclose()


async def test_refresh_adopts_and_caches_a_good_payload(tmp_path):
    import httpx

    prices = {f"model-{i}": {"input_cost_per_token": 1e-06} for i in range(60)}
    prices["claude-opus-5"] = {"input_cost_per_token": 9e-06, "output_cost_per_token": 9e-05}
    p = _table(tmp_path)
    client = httpx.AsyncClient(transport=httpx.MockTransport(
        lambda r: httpx.Response(200, json=prices)))
    assert await p.refresh(client) is True
    assert p.source == "online"
    assert p.rates("claude-opus-5").input == 9e-06
    # a restart reads the same rates back off disk
    assert PricingTable(cache_file=tmp_path / "model_prices.json").rates("claude-opus-5").input == 9e-06
    await client.aclose()
