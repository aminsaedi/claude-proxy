"""Calendar window maths, limit evaluation, and the limit store."""

from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from claude_proxy import budgets, db

TORONTO = ZoneInfo("America/Toronto")


def _at(y, m, d, hh=0, mm=0, tz=TORONTO) -> float:
    return datetime(y, m, d, hh, mm, tzinfo=tz).timestamp()


def _local(ts: float, tz=TORONTO) -> str:
    return datetime.fromtimestamp(ts, tz).strftime("%Y-%m-%d %H:%M")


def test_zone_falls_back_on_a_bad_name():
    assert budgets.zone("America/Toronto").key == "America/Toronto"
    assert budgets.zone("Mars/Olympus").key == budgets.DEFAULT_TIMEZONE


def test_hour_window():
    start, end = budgets.window_bounds("hour", _at(2026, 7, 25, 14, 37), TORONTO)
    assert _local(start) == "2026-07-25 14:00"
    assert _local(end) == "2026-07-25 15:00"


def test_day_window_is_local_midnight_not_utc():
    start, end = budgets.window_bounds("day", _at(2026, 7, 25, 1, 0), TORONTO)
    assert _local(start) == "2026-07-25 00:00"
    assert _local(end) == "2026-07-26 00:00"
    # Local midnight is 04:00 UTC in summer, so bucketing by UTC days would put
    # an evening's traffic on the operator's *next* day.
    assert _local(start, ZoneInfo("UTC")) == "2026-07-25 04:00"
    evening = _at(2026, 7, 24, 21, 0)
    assert _local(evening, ZoneInfo("UTC")).startswith("2026-07-25")
    assert budgets.window_bounds("day", evening, TORONTO)[0] == _at(2026, 7, 24)


def test_week_window_starts_monday():
    # 2026-07-25 is a Saturday
    start, end = budgets.window_bounds("week", _at(2026, 7, 25, 12), TORONTO)
    assert _local(start) == "2026-07-20 00:00"   # Monday
    assert _local(end) == "2026-07-27 00:00"


def test_month_window_spans_the_calendar_month():
    start, end = budgets.window_bounds("month", _at(2026, 7, 25, 12), TORONTO)
    assert _local(start) == "2026-07-01 00:00"
    assert _local(end) == "2026-08-01 00:00"
    # December must roll into the next year, not month 13
    _, dec_end = budgets.window_bounds("month", _at(2026, 12, 14), TORONTO)
    assert _local(dec_end) == "2027-01-01 00:00"


def test_day_window_across_dst_is_23_or_25_hours():
    # Toronto springs forward 2026-03-08 and falls back 2026-11-01.
    _, spring_end = budgets.window_bounds("day", _at(2026, 3, 8, 12), TORONTO)
    spring_start, _ = budgets.window_bounds("day", _at(2026, 3, 8, 12), TORONTO)
    assert (spring_end - spring_start) == 23 * 3600
    fall_start, fall_end = budgets.window_bounds("day", _at(2026, 11, 1, 12), TORONTO)
    assert (fall_end - fall_start) == 25 * 3600


def test_unknown_period_is_rejected():
    with pytest.raises(ValueError, match="unknown period"):
        budgets.window_bounds("fortnight", _at(2026, 7, 25), TORONTO)


def test_day_start_walks_back_whole_local_days():
    now = _at(2026, 7, 25, 22, 30)
    assert _local(budgets.day_start(now, TORONTO, 0)) == "2026-07-25 00:00"
    assert _local(budgets.day_start(now, TORONTO, 6)) == "2026-07-19 00:00"


def test_evaluate_scores_every_cap_tightest_first():
    now = _at(2026, 7, 25, 14, 30)
    spend = {"hour": 2.50, "day": 4.00}

    def spend_between(key, start, end):
        # distinguish the windows by their length
        return spend["hour"] if end - start == 3600 else spend["day"]

    out = budgets.evaluate("alice", {"hour": 3.0, "day": 10.0}, spend_between, now, TORONTO)
    assert [s.period for s in out] == ["hour", "day"]   # 83% before 40%
    assert out[0].remaining_usd == pytest.approx(0.5)
    assert not out[0].over and not out[1].over

    spend["hour"] = 3.0   # exactly at the cap counts as over
    out = budgets.evaluate("alice", {"hour": 3.0, "day": 10.0}, spend_between, now, TORONTO)
    assert out[0].over
    assert "3.00" in out[0].message() and "per hour" in out[0].message()


def test_evaluate_skips_zero_and_missing_caps():
    now = _at(2026, 7, 25, 14, 30)
    out = budgets.evaluate("alice", {"hour": 0, "week": 5.0}, lambda *a: 1.0, now, TORONTO)
    assert [s.period for s in out] == ["week"]


def test_parse_limits_accepts_strings_and_drops_blanks():
    assert budgets.parse_limits({"hour": "3", "day": 10, "week": "", "month": None}) == {
        "hour": 3.0, "day": 10.0,
    }
    assert budgets.parse_limits({}) == {}
    assert budgets.parse_limits({"day": 0}) == {}       # 0 means "no cap"


def test_parse_limits_rejects_bad_input():
    with pytest.raises(ValueError, match="unknown period"):
        budgets.parse_limits({"fortnight": 5})
    with pytest.raises(ValueError, match="not a number"):
        budgets.parse_limits({"day": "ten dollars"})
    with pytest.raises(ValueError, match="must be positive"):
        budgets.parse_limits({"day": -1})


def test_limit_store_roundtrip_and_reload():
    store = budgets.LimitStore()
    try:
        store.set("alice", {"hour": 3.0, "day": 10.0})
        assert store.for_key("alice") == {"hour": 3.0, "day": 10.0}
        assert db.list_key_limits()["alice"]["day"] == 10.0

        # a second process (manage.py) editing the DB is picked up on reload
        other = budgets.LimitStore()
        assert other.for_key("alice") == {"hour": 3.0, "day": 10.0}
        db.set_key_limits("alice", {"day": 25.0})
        assert other.reload_if_changed() is True
        assert other.for_key("alice") == {"day": 25.0}
        assert other.reload_if_changed() is False   # no further change

        # replace-all semantics: an empty dict uncaps the key
        store.set("alice", {})
        assert store.for_key("alice") == {}
        assert "alice" not in db.list_key_limits()
    finally:
        db.set_key_limits("alice", {})


def test_deleting_a_key_drops_its_limits():
    db.add_virtual_key("zlimits", "vk-zlimits")
    db.set_key_limits("zlimits", {"day": 1.0})
    db.delete_virtual_key("zlimits")
    assert "zlimits" not in db.list_key_limits()


def test_renaming_a_key_carries_its_limits():
    db.add_virtual_key("zold", "vk-zold")
    db.set_key_limits("zold", {"day": 2.0})
    try:
        assert db.rename_virtual_key("zold", "znew") is True
        assert db.list_key_limits()["znew"] == {"day": 2.0}
        assert "zold" not in db.list_key_limits()
    finally:
        db.delete_virtual_key("znew")
        db.delete_virtual_key("zold")


def test_a_save_is_not_undone_by_a_concurrent_reload(monkeypatch):
    """The lost-update race behind 'I set a limit and it didn't stick'.

    ``LimitStore.set`` runs in a worker thread (asyncio.to_thread) while
    ``reload_if_changed`` runs on the event loop. The reload reads the DB and
    then replaces the whole cache with what it read — so a save committing
    inside that read window used to be silently rolled back, in the console
    *and* in enforcement, until some later tick happened to notice.
    """
    import threading

    from claude_proxy import budgets as budgets_mod

    store = budgets.LimitStore()
    store.set("alice", {"day": 10.0})

    real_list = budgets_mod.db.list_key_limits
    gate = threading.Event()
    started = threading.Event()

    def slow_list(*a, **kw):
        out = real_list(*a, **kw)
        started.set()
        gate.wait(3.0)      # stand in for the I/O window sqlite really has
        return out

    def save():
        started.wait(3.0)
        store.set("alice", {"day": 10.0, "hour": 5.0})
        gate.set()

    monkeypatch.setattr(budgets_mod.db, "list_key_limits", slow_list)
    saver = threading.Thread(target=save)
    saver.start()
    store.reload_if_changed()
    saver.join(5.0)
    monkeypatch.setattr(budgets_mod.db, "list_key_limits", real_list)

    try:
        assert store.for_key("alice") == {"day": 10.0, "hour": 5.0}, \
            "the reload rolled the save back"
        # And a later reload still converges on the persisted truth.
        store.reload_if_changed()
        assert store.for_key("alice") == {"day": 10.0, "hour": 5.0}
    finally:
        store.set("alice", {})


def test_reload_still_picks_up_another_process_edit():
    """The generation guard must not block genuine external changes."""
    store = budgets.LimitStore()
    try:
        store.set("alice", {"day": 10.0})
        db.set_key_limits("alice", {"day": 99.0})     # as if manage.py did it
        assert store.reload_if_changed() is True
        assert store.for_key("alice") == {"day": 99.0}
    finally:
        store.set("alice", {})
