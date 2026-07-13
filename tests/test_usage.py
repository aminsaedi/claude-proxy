from __future__ import annotations

from claude_proxy import db
from claude_proxy.usage import UsageTracker


async def test_record_accumulates_cache_tokens():
    u = UsageTracker()
    await u.record("alice", "claude-opus-4-8", input_tokens=10, output_tokens=20,
                   cache_read=100, cache_creation=5)
    await u.record("alice", "claude-opus-4-8", input_tokens=1, output_tokens=2,
                   cache_read=3, cache_creation=4)
    snap = await u.snapshot()
    m = snap["alice"]["claude-opus-4-8"]
    assert m["input_tokens"] == 11
    assert m["output_tokens"] == 22
    assert m["cache_read_input_tokens"] == 103
    assert m["cache_creation_input_tokens"] == 9
    assert m["requests"] == 2


async def test_record_ignores_empty():
    u = UsageTracker()
    await u.record("alice", "m", 0, 0, 0, 0)
    await u.record("", "m", 5, 5)
    assert await u.snapshot() == {}


async def test_flush_persists_to_db_and_reloads():
    u = UsageTracker()
    await u.record("bob", "claude-haiku-4-5", input_tokens=7, output_tokens=8)
    await u.flush()
    on_disk = db.load_usage()
    assert on_disk["bob"]["claude-haiku-4-5"]["input_tokens"] == 7
    # a fresh tracker loads what was flushed from the DB
    u2 = UsageTracker()
    snap = await u2.snapshot()
    assert snap["bob"]["claude-haiku-4-5"]["output_tokens"] == 8
