"""The request/prompt audit log: capture, retention on both axes, and reads."""

from __future__ import annotations

import os
import sqlite3
import threading
import time

import pytest

from claude_proxy import audit
from claude_proxy.audit import AuditLog, Record


def _rec(**kw) -> Record:
    base = {
        "ts": time.time(), "request_id": "abc123", "key_name": "alice",
        "method": "POST", "path": "/v1/messages", "model": "claude-opus-5",
        "status": 200, "input_tokens": 10, "output_tokens": 20, "cost_usd": 0.01,
        "latency_ms": 120.0, "ttfb_ms": 40.0,
    }
    base.update(kw)
    return Record(**base)


def _log(tmp_path, **kw) -> AuditLog:
    log = AuditLog(tmp_path / "audit.db", **kw)
    log.start()
    return log


def _drain(log: AuditLog, expect: int, timeout: float = 5.0) -> None:
    """Wait for the writer thread to catch up (it batches on a timer)."""
    deadline = time.time() + timeout
    while log.written < expect and time.time() < deadline:
        time.sleep(0.02)
    assert log.written >= expect, f"writer stalled at {log.written}/{expect}"


def test_records_round_trip_with_bodies(tmp_path):
    log = _log(tmp_path)
    try:
        log.submit(_rec(
            req_body=b'{"model":"claude-opus-5","messages":[{"role":"user","content":"hello there"}]}',
            resp_body=b"hi back",
        ))
        _drain(log, 1)
        rows = log.query()
        assert len(rows) == 1
        assert rows[0]["key_name"] == "alice"
        assert rows[0]["summary"] == "hello there"
        assert "req_blob" not in rows[0]  # the list view never carries bodies

        full = log.get(rows[0]["id"])
        assert full["request"]["messages"][0]["content"] == "hello there"
        assert full["response"] == "hi back"
    finally:
        log.stop()


def test_meta_mode_keeps_the_row_but_not_the_prompt(tmp_path):
    log = _log(tmp_path, mode="meta")
    try:
        log.submit(_rec(req_body=b'{"messages":[{"role":"user","content":"secret"}]}',
                        resp_body=b"secret reply"))
        _drain(log, 1)
        row = log.query()[0]
        assert row["cost_usd"] == pytest.approx(0.01)
        full = log.get(row["id"])
        assert full["request"] is None and full["response"] is None
    finally:
        log.stop()


def test_off_mode_records_nothing(tmp_path):
    log = AuditLog(tmp_path / "audit.db", mode="off")
    log.start()
    try:
        log.submit(_rec())
        time.sleep(0.2)
        assert log.written == 0
        assert log.query() == []
    finally:
        log.stop()


def test_a_full_queue_drops_instead_of_blocking(tmp_path):
    """Backpressure must never reach the request path — dropping is the design."""
    log = AuditLog(tmp_path / "audit.db", queue_max=2)
    log._thread = object()  # pretend a writer exists but never drains the queue
    for _ in range(10):
        log.submit(_rec())   # must not raise, must not block
    assert log.dropped == 8
    log._thread = None


def test_bodies_are_truncated_to_the_cap(tmp_path):
    log = _log(tmp_path, max_body_bytes=64)
    try:
        log.submit(_rec(req_body=b'{"messages":[{"role":"user","content":"' + b"x" * 5000 + b'"}]}'))
        _drain(log, 1)
        full = log.get(log.query()[0]["id"])
        assert full["truncated"] == 1
        # Stored as a clipped string rather than parsed JSON, since a cut body
        # is no longer valid JSON — but it must still decode as text.
        assert isinstance(full["request"], str)
        assert len(full["request"]) <= 64
    finally:
        log.stop()


def test_retention_drops_rows_past_the_age_limit(tmp_path):
    log = _log(tmp_path, retention_days=1)
    try:
        now = time.time()
        log.submit(_rec(ts=now - 3 * 86400))   # older than the window
        log.submit(_rec(ts=now))
        _drain(log, 2)
        result = log.sweep()
        assert result["removed_age"] == 1
        rows = log.query()
        assert len(rows) == 1 and rows[0]["ts"] == pytest.approx(now)
    finally:
        log.stop()


def test_retention_drops_oldest_rows_to_fit_the_size_cap(tmp_path):
    """The byte budget is enforced even when everything is inside the age window."""
    CAP = 200 * 1024
    log = _log(tmp_path, retention_days=365, max_bytes=CAP)
    try:
        now = time.time()
        for i in range(60):
            # Random bytes: a run of the same character would compress to almost
            # nothing and the DB would never reach the cap this is testing.
            log.submit(_rec(ts=now - (60 - i),
                            req_body=os.urandom(4000), resp_body=os.urandom(4000)))
        _drain(log, 60)
        before = log.stats()
        assert before["bytes"] > CAP, "test needs the DB to actually exceed the cap"

        result = log.sweep()
        assert result["removed_age"] == 0     # nothing is old enough
        assert result["removed_size"] > 0
        after = log.stats()
        assert after["bytes"] <= CAP
        assert 0 < after["rows"] < 60
        # The survivors are the newest ones.
        assert min(r["ts"] for r in log.query(limit=500)) > now - 60
    finally:
        log.stop()


def test_query_filters_and_keyset_paging(tmp_path):
    log = _log(tmp_path)
    try:
        now = time.time()
        for i in range(10):
            log.submit(_rec(ts=now - i, key_name="alice" if i % 2 else "bob",
                            outcome="error" if i == 3 else "ok",
                            req_body=b'{"messages":[{"role":"user","content":"q%d"}]}' % i))
        _drain(log, 10)

        assert len(log.query(key_name="alice")) == 5
        assert len(log.query(outcome="error")) == 1
        assert len(log.query(search="q7")) == 1

        page1 = log.query(limit=4)
        page2 = log.query(limit=4, before_id=page1[-1]["id"])
        assert len(page1) == 4 and len(page2) == 4
        assert not {r["id"] for r in page1} & {r["id"] for r in page2}

        # after_id is the live-tail cursor: only what is newer than the last seen
        newest = max(r["id"] for r in page1)
        assert log.query(after_id=newest) == []
    finally:
        log.stop()


def test_overview_reports_percentiles_and_error_counts(tmp_path):
    log = _log(tmp_path)
    try:
        now = time.time()
        for i in range(1, 101):
            log.submit(_rec(ts=now, latency_ms=float(i), ttfb_ms=float(i) / 2,
                            outcome="error" if i <= 5 else "ok"))
        _drain(log, 100)
        o = log.overview(since=now - 60)
        assert o["requests"] == 100
        assert o["forwarded"] == 100
        assert o["errors"] == 5
        assert o["latency_p50"] == pytest.approx(51, abs=2)
        assert o["latency_p95"] == pytest.approx(96, abs=2)
        assert o["ttfb_p50"] == pytest.approx(25.5, abs=1)
    finally:
        log.stop()


def test_latency_percentiles_ignore_requests_that_never_left(tmp_path):
    """A rejected key is answered locally in microseconds.

    Counting those makes latency look better the more traffic you turn away,
    and — since only forwarded requests record a TTFB — it also puts the two
    series on different populations, which is how p50(ttfb) ended up *above*
    p50(latency) in production.
    """
    log = _log(tmp_path)
    try:
        now = time.time()
        for _ in range(10):    # real work: slow, and forwarded
            log.submit(_rec(ts=now, latency_ms=5000.0, ttfb_ms=4000.0))
        for _ in range(90):    # unknown key: instant, never forwarded
            log.submit(_rec(ts=now, latency_ms=0.2, ttfb_ms=None,
                            outcome="rejected", status=401))
        _drain(log, 100)

        o = log.overview(since=now - 60)
        assert o["requests"] == 100
        assert o["forwarded"] == 10
        assert o["rejected"] == 90
        assert o["latency_p50"] == pytest.approx(5000)
        assert o["ttfb_p50"] == pytest.approx(4000)
        assert o["ttfb_p50"] < o["latency_p50"], "TTFB can never exceed total latency"
    finally:
        log.stop()


def test_top_models_ranks_by_spend(tmp_path):
    log = _log(tmp_path)
    try:
        now = time.time()
        log.submit(_rec(ts=now, model="cheap", cost_usd=0.01))
        log.submit(_rec(ts=now, model="dear", cost_usd=5.0))
        log.submit(_rec(ts=now, model="dear", cost_usd=1.0))
        _drain(log, 3)
        # A rejected request has no model and never reached upstream; it must
        # not appear as a nameless row in a table about models.
        log.submit(_rec(ts=now, model="", ttfb_ms=None, outcome="rejected", cost_usd=0.0))
        _drain(log, 4)
        rows = log.top_models(since=now - 60)
        assert [r["model"] for r in rows] == ["dear", "cheap"]
        assert rows[0]["requests"] == 2 and rows[0]["cost_usd"] == pytest.approx(6.0)
    finally:
        log.stop()


def test_purge_empties_the_log(tmp_path):
    log = _log(tmp_path)
    try:
        for _ in range(5):
            log.submit(_rec())
        _drain(log, 5)
        assert log.purge() == 5
        assert log.query() == []
    finally:
        log.stop()


# --- summary extraction ---------------------------------------------------

def test_summary_prefers_the_last_user_turn():
    body = (b'{"system":"you are helpful","messages":['
            b'{"role":"user","content":"first"},'
            b'{"role":"assistant","content":"reply"},'
            b'{"role":"user","content":[{"type":"text","text":"second question"}]}]}')
    assert audit._summarize(body) == "second question"


def test_summary_survives_odd_content_shapes():
    assert audit._summarize(b"not json") == ""
    assert audit._summarize(b'{"messages":[]}') == ""
    assert audit._summarize(b'[]') == ""
    tool = b'{"messages":[{"role":"user","content":[{"type":"tool_result","content":"42"}]}]}'
    assert audit._summarize(tool) == "42"
    img = b'{"messages":[{"role":"user","content":[{"type":"image"},{"type":"text","text":"what is this"}]}]}'
    assert audit._summarize(img) == "[image] what is this"


def test_clip_cuts_on_a_utf8_boundary():
    text = ("é" * 40).encode()          # two bytes per character
    clipped, cut = audit._clip(text, 15)
    assert cut is True
    clipped.decode("utf-8")             # must not raise
    assert len(clipped) <= 15
    assert audit._clip(b"short", 100) == (b"short", False)


# --- write contention -----------------------------------------------------
#
# Note the busy_timeout in these tests. `_connect` gives SQLite a 10s budget to
# wait out a lock on its own, so a short lock never reaches our retry loop at
# all -- a test that holds one briefly passes with or without the fix and
# proves nothing. The production window was an admin purge whose post-delete
# `incremental_vacuum` held the write lock for ~8 minutes, far past that
# budget. Dropping busy_timeout to a few milliseconds reproduces "SQLite has
# already given up" deterministically and in about a second.

def _impatient(log):
    """A writer connection that surfaces a lock instead of waiting it out."""
    conn = log._connect()
    conn.execute("PRAGMA busy_timeout=5")
    return conn


def _hold_lock_for(path, seconds):
    """Hold an exclusive write lock, released after `seconds`, in the background."""
    ready = threading.Event()

    def run():
        conn = sqlite3.connect(path, timeout=30)
        conn.execute("BEGIN EXCLUSIVE")
        ready.set()
        time.sleep(seconds)
        conn.rollback()
        conn.close()

    t = threading.Thread(target=run, daemon=True)
    t.start()
    ready.wait(5)
    return t


def test_a_record_written_while_the_db_is_locked_is_kept_not_discarded(tmp_path):
    """The failure this reproduces cost ~20 real records in production.

    The writer used to catch "database is locked", log it, and throw the batch
    away. It must instead wait for the lock to clear and still write the row.
    """
    log = _log(tmp_path)
    try:
        conn = _impatient(log)
        holder = _hold_lock_for(tmp_path / "audit.db", 1.0)
        log._write(conn, [_rec(request_id="held")])
        holder.join(10)
        assert log.write_errors == 0, "gave up on a lock that was temporary"
        assert any(r["request_id"] == "held" for r in log.query()), \
            "record written during the lock window was lost"
        conn.close()
    finally:
        log.stop()


def test_records_lost_to_a_permanent_lock_are_counted_by_row(tmp_path):
    """A batch count would understate the hole; the number has to mean records."""
    log = _log(tmp_path)
    try:
        conn = _impatient(log)
        original, audit._WRITE_DEADLINE = audit._WRITE_DEADLINE, 0.0
        holder = _hold_lock_for(tmp_path / "audit.db", 1.0)
        try:
            log._write(conn, [_rec(request_id=f"r{i}") for i in range(3)])
        finally:
            audit._WRITE_DEADLINE = original
            holder.join(10)
            conn.close()
        assert log.write_errors == 3, f"counted {log.write_errors}, expected 3 rows"
    finally:
        log.stop()
