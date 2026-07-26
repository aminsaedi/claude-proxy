from __future__ import annotations

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from claude_proxy import db
from claude_proxy.usage import UsageTracker, hour_of

TORONTO = ZoneInfo("America/Toronto")


class _FlatPricing:
    """$1 per 1000 tokens of anything — keeps the arithmetic checkable by eye."""

    def cost(self, model, input_tokens=0, output_tokens=0, cache_read=0, cache_creation=0):
        return (input_tokens + output_tokens + cache_read + cache_creation) / 1000


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


# --- cost + time series ---------------------------------------------------
#
# Timestamps are relative to the current local day: the tracker only holds a
# rolling window of the series in memory, so fixed calendar dates would start
# failing once they aged out.

def _midnight(days_ago: int = 0) -> float:
    """Local midnight, ``days_ago`` days back."""
    today = datetime.now(TORONTO).replace(hour=0, minute=0, second=0, microsecond=0)
    return (today - timedelta(days=days_ago)).timestamp()


def _at(days_ago: int = 0, hour: int = 12) -> float:
    """A point inside a local day, far from any midnight boundary."""
    return _midnight(days_ago) + hour * 3600


def _date(days_ago: int) -> str:
    return datetime.fromtimestamp(_midnight(days_ago), TORONTO).strftime("%Y-%m-%d")


async def test_cost_is_priced_at_ingest_on_both_shapes():
    u = UsageTracker(pricing=_FlatPricing())
    await u.record("carol", "claude-opus-5", input_tokens=1000, output_tokens=500,
                   cache_read=2000, cache_creation=500, now=_at(0, 10))
    snap = await u.snapshot()
    assert snap["carol"]["claude-opus-5"]["cost_usd"] == 4.0
    bucket = u.totals_between("carol", _midnight(0), _midnight(-1))
    assert bucket["cost_usd"] == 4.0
    assert bucket["requests"] == 1


async def test_no_pricing_table_still_records_tokens():
    u = UsageTracker()  # pricing=None
    await u.record("carol", "claude-opus-5", input_tokens=10)
    assert (await u.snapshot())["carol"]["claude-opus-5"]["cost_usd"] == 0.0


async def test_windows_only_count_their_own_hours():
    u = UsageTracker(pricing=_FlatPricing())
    for days_ago, tokens in ((0, 1000), (1, 2000), (5, 4000)):
        await u.record("dave", "m", input_tokens=tokens, now=_at(days_ago, 9))
    end = _midnight(-1)   # tomorrow's midnight
    assert u.spend_between("dave", _midnight(0), end) == 1.0
    assert u.spend_between("dave", _midnight(1), end) == 3.0
    assert u.spend_between("dave", _midnight(6), end) == 7.0
    # a window that closes before the traffic sees nothing
    assert u.spend_between("dave", _midnight(30), _midnight(6)) == 0.0
    # models_between splits the same window by model
    assert set(u.models_between("dave", _midnight(6), end)) == {"m"}


async def test_window_ending_mid_hour_still_counts_that_hour():
    """The console asks for "today up to now" — the in-progress hour must count.

    ``_buckets_between`` picks between two scan strategies by comparing the
    window's length to the number of buckets held, so both are exercised here:
    a narrow window over a busy key takes the lookup path, a wide one scans.
    """
    u = UsageTracker(pricing=_FlatPricing())
    now = _midnight(0) + 9 * 3600 + 1800   # 09:30 local, mid-bucket
    for days_ago in range(30):             # enough buckets to trip the lookup path
        await u.record("jane", "m", input_tokens=1000, now=_at(days_ago, 9) + 1800)

    narrow_start = hour_of(now)
    assert len(u._hourly["jane"]) > (int(now + 1) - narrow_start) // 3600 + 1
    assert u.spend_between("jane", narrow_start, now + 1) == 1.0      # lookup path
    assert u.spend_between("jane", _midnight(60), now + 1) == 30.0    # scan path
    # a window that closes before the bucket's traffic still excludes it
    assert u.spend_between("jane", _midnight(0), narrow_start) == 0.0


async def test_hourly_buckets_are_per_utc_hour():
    u = UsageTracker(pricing=_FlatPricing())
    base = hour_of(_at(0, 9))
    await u.record("erin", "m", input_tokens=1000, now=base + 60)
    await u.record("erin", "m", input_tokens=1000, now=base + 3540)   # same hour
    await u.record("erin", "m", input_tokens=1000, now=base + 3700)   # next hour
    hours = sorted(u._hourly["erin"])
    assert hours == [base, base + 3600]
    assert u._hourly["erin"][base]["m"]["requests"] == 2


async def test_daily_series_fills_quiet_days():
    u = UsageTracker(pricing=_FlatPricing())
    now = _at(0, 15)
    await u.record("frank", "m", input_tokens=1000, now=now)
    await u.record("frank", "m", input_tokens=3000, now=_at(2, 11))
    series = u.daily_series("frank", TORONTO, 5, now)
    assert [d["date"] for d in series] == [_date(4), _date(3), _date(2), _date(1), _date(0)]
    assert [d["cost_usd"] for d in series] == [0, 0, 3.0, 0, 1.0]
    assert series[-1]["requests"] == 1


async def test_hourly_series_survives_a_restart():
    u = UsageTracker(pricing=_FlatPricing())
    await u.record("grace", "m", input_tokens=5000, now=_at(0, 12))
    await u.flush()
    assert any(r["key_name"] == "grace" for r in db.load_usage_hourly())
    # a fresh tracker rebuilds the window sums from disk
    u2 = UsageTracker(pricing=_FlatPricing())
    assert u2.spend_between("grace", _midnight(0), _midnight(-1)) == 5.0


async def test_flush_is_idempotent_per_bucket():
    u = UsageTracker(pricing=_FlatPricing())
    now = _at(0, 13)
    await u.record("heidi", "m", input_tokens=1000, now=now)
    await u.flush()
    await u.record("heidi", "m", input_tokens=1000, now=now)
    await u.flush()   # same bucket rewritten, not double-counted
    rows = [r for r in db.load_usage_hourly() if r["key_name"] == "heidi"]
    assert len(rows) == 1 and rows[0]["input_tokens"] == 2000


def test_prune_memory_drops_only_ancient_buckets():
    u = UsageTracker(pricing=_FlatPricing(), memory_days=2)
    now = _at(0, 12)
    u._hourly["ivan"] = {hour_of(now): {}, hour_of(now - 5 * 86400): {}}
    u.prune_memory(now)
    assert list(u._hourly["ivan"]) == [hour_of(now)]
