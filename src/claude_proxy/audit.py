"""Request + prompt audit log.

Every proxied request can be recorded in full — the prompt that went up, the
completion that came back, who sent it, which upstream token served it, what it
cost, and how long it took. That is a lot of bytes moving through a hot path
that must not get slower, so the design is deliberately lopsided:

**The event loop does almost nothing.** :meth:`AuditLog.submit` builds no JSON,
compresses nothing, and touches no file — it drops an already-assembled record
onto a bounded ``queue.Queue`` and returns. Serialization, compression, and the
SQLite insert all happen on a dedicated writer *thread*, so none of it competes
with request handling for the GIL-holding event loop beyond the handoff itself.

**The queue is bounded and lossy on purpose.** If the writer ever falls behind
(slow disk, huge burst), :meth:`submit` drops the record and bumps a counter
rather than applying backpressure to a live API request. Losing an audit row is
always preferable to adding latency to the thing being audited.

**Its storage is a separate database.** ``audit.db`` sits beside the main DB but
in its own file, so the write volume here — orders of magnitude above the usage
tracker's — never contends with the tokens/keys/usage tables.

Retention is enforced on two axes at once, because either one alone eventually
surprises you: rows older than ``retention_days`` are dropped, *and* if the file
still exceeds ``max_bytes`` the oldest rows are dropped until it fits. The
database is created with ``auto_vacuum=INCREMENTAL`` so freed pages are actually
returned to the filesystem without a full-rewrite ``VACUUM`` that would lock the
file for seconds.
"""

from __future__ import annotations

import json
import logging
import os
import queue
import sqlite3
import threading
import time
import zlib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

log = logging.getLogger("claude_proxy.audit")

# Recording modes, cheapest first.
#   off  — record nothing at all.
#   meta — record one row per request (who/what/cost/latency) but no bodies.
#   full — also record the prompt and the completion, capped and compressed.
MODES = ("off", "meta", "full")

# Every outcome that means the caller did not get a complete answer. Kept as one
# list because "show me what failed" must not depend on remembering which of
# these exist — a new outcome added to the proxy and not added here would be
# silently missing from the failure views, which is the exact blindness this
# whole vocabulary exists to remove.
FAILURE_OUTCOMES = ("error", "aborted", "incomplete", "blocked", "rejected")

# How many finished requests may be waiting for the writer thread. At ~1KB of
# retained Python objects per record this is a couple of MB worst case, and it
# absorbs a multi-second disk stall without dropping anything.
QUEUE_MAX = 4096

# The writer wakes at least this often to flush a partial batch.
FLUSH_INTERVAL = 0.5
BATCH_MAX = 256

_SCHEMA = """
CREATE TABLE IF NOT EXISTS requests (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    ts          REAL    NOT NULL,          -- epoch seconds, request start
    request_id  TEXT,
    key_name    TEXT,
    token_name  TEXT,                      -- upstream token that served it
    method      TEXT,
    path        TEXT,
    model       TEXT,
    status      INTEGER,
    streamed    INTEGER NOT NULL DEFAULT 0,
    -- ok         served to completion
    -- error      upstream refused, or the stream carried an error event
    -- aborted    the caller (or the edge) hung up before we finished
    -- incomplete the stream ended without message_stop — a truncated answer
    -- blocked    over a spend limit;  rejected  bad virtual key
    outcome     TEXT,
    input_tokens                INTEGER NOT NULL DEFAULT 0,
    output_tokens               INTEGER NOT NULL DEFAULT 0,
    cache_read_input_tokens     INTEGER NOT NULL DEFAULT 0,
    cache_creation_input_tokens INTEGER NOT NULL DEFAULT 0,
    cost_usd    REAL    NOT NULL DEFAULT 0,
    latency_ms  REAL,                      -- start -> last byte
    ttfb_ms     REAL,                      -- start -> upstream response headers
    attempts    INTEGER NOT NULL DEFAULT 1,
    client_ip   TEXT,
    user_agent  TEXT,
    error       TEXT,
    req_bytes   INTEGER NOT NULL DEFAULT 0,   -- uncompressed sizes, for stats
    resp_bytes  INTEGER NOT NULL DEFAULT 0,
    summary     TEXT,                      -- last user turn, trimmed: list preview
    req_blob    BLOB,                      -- zlib(utf-8 json) request body
    resp_blob   BLOB,                      -- zlib(utf-8 text) completion
    truncated   INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS requests_ts_idx  ON requests (ts DESC);
CREATE INDEX IF NOT EXISTS requests_key_idx ON requests (key_name, ts DESC);
"""

# Column order used by the INSERT; keep in step with ``Record.row()``.
_COLUMNS = (
    "ts", "request_id", "key_name", "token_name", "method", "path", "model",
    "status", "streamed", "outcome", "input_tokens", "output_tokens",
    "cache_read_input_tokens", "cache_creation_input_tokens", "cost_usd",
    "latency_ms", "ttfb_ms", "attempts", "client_ip", "user_agent", "error",
    "req_bytes", "resp_bytes", "summary", "req_blob", "resp_blob", "truncated",
)

# Columns safe (and cheap) to return in a list view — everything but the blobs.
_LIST_COLUMNS = (
    "id", "ts", "request_id", "key_name", "token_name", "method", "path",
    "model", "status", "streamed", "outcome", "input_tokens", "output_tokens",
    "cache_read_input_tokens", "cache_creation_input_tokens", "cost_usd",
    "latency_ms", "ttfb_ms", "attempts", "client_ip", "error",
    "req_bytes", "resp_bytes", "summary", "truncated",
)

SUMMARY_CHARS = 240


@dataclass
class Record:
    """One request, assembled on the hot path and finished off by the writer.

    Body capture holds *references* to bytes the proxy already had in hand — no
    copying happens on the event loop. ``resp_parts`` is a list the streaming
    passthrough appends text deltas to; the writer joins it.
    """

    ts: float
    request_id: str
    key_name: str = ""
    method: str = ""
    path: str = ""
    model: str = ""
    client_ip: str = ""
    user_agent: str = ""
    token_name: str = ""
    status: int = 0
    streamed: bool = False
    outcome: str = "ok"
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read: int = 0
    cache_creation: int = 0
    cost_usd: float = 0.0
    latency_ms: float | None = None
    ttfb_ms: float | None = None
    attempts: int = 1
    error: str = ""
    req_body: bytes | None = None
    resp_body: bytes | None = None
    resp_parts: list[str] = field(default_factory=list)
    req_bytes: int = 0
    resp_bytes: int = 0
    truncated: bool = False

    def row(self, keep_bodies: bool, max_body: int) -> tuple:
        """Compress and flatten into the INSERT tuple. Writer thread only."""
        req_blob = resp_blob = None
        summary = ""
        req = self.req_body or b""
        resp = self.resp_body if self.resp_body is not None else _join(self.resp_parts)
        self.req_bytes = self.req_bytes or len(req)
        self.resp_bytes = self.resp_bytes or len(resp)
        if req:
            summary = _summarize(req)
        if keep_bodies:
            if req:
                clipped, cut = _clip(req, max_body)
                req_blob = zlib.compress(clipped, 1)
                self.truncated = self.truncated or cut
            if resp:
                clipped, cut = _clip(resp, max_body)
                resp_blob = zlib.compress(clipped, 1)
                self.truncated = self.truncated or cut
        return (
            self.ts, self.request_id, self.key_name, self.token_name, self.method,
            self.path, self.model, self.status, int(self.streamed), self.outcome,
            self.input_tokens, self.output_tokens, self.cache_read,
            self.cache_creation, self.cost_usd, self.latency_ms, self.ttfb_ms,
            self.attempts, self.client_ip, self.user_agent, self.error,
            self.req_bytes, self.resp_bytes, summary, req_blob, resp_blob,
            int(self.truncated),
        )


def _join(parts: list[str]) -> bytes:
    if not parts:
        return b""
    return "".join(parts).encode("utf-8", "replace")


def _clip(data: bytes, limit: int) -> tuple[bytes, bool]:
    if limit <= 0 or len(data) <= limit:
        return data, False
    # Cut on a UTF-8 boundary so the stored text still decodes cleanly.
    cut = data[:limit]
    for _ in range(4):
        try:
            cut.decode("utf-8")
            break
        except UnicodeDecodeError:
            cut = cut[:-1]
    return cut, True


def _summarize(body: bytes) -> str:
    """A one-line preview of what was asked, for the request list.

    Pulled from the last user turn because that is the part an operator scans
    for — the system prompt and tool definitions are identical across a whole
    session and tell you nothing about which request this is.
    """
    try:
        data = json.loads(body)
    except Exception:  # noqa: BLE001 - a non-JSON body just has no preview
        return ""
    if not isinstance(data, dict):
        return ""
    messages = data.get("messages")
    if not isinstance(messages, list):
        return ""
    for msg in reversed(messages):
        if not isinstance(msg, dict) or msg.get("role") != "user":
            continue
        text = _text_of(msg.get("content"))
        if text:
            return text[:SUMMARY_CHARS]
    return ""


def _text_of(content: Any) -> str:
    """Flatten Anthropic's string-or-block content shape into plain text."""
    if isinstance(content, str):
        return " ".join(content.split())
    if not isinstance(content, list):
        return ""
    out = []
    for block in content:
        if isinstance(block, str):
            out.append(block)
        elif isinstance(block, dict):
            if isinstance(block.get("text"), str):
                out.append(block["text"])
            elif block.get("type") == "tool_result":
                out.append(_text_of(block.get("content")))
            elif block.get("type") == "tool_use":
                out.append(f"[tool_use {block.get('name', '')}]")
            elif block.get("type") in ("image", "document"):
                out.append(f"[{block['type']}]")
    return " ".join(" ".join(out).split())


class AuditLog:
    """Bounded queue in front of a writer thread in front of its own SQLite DB.

    Safe to construct even when recording is off — nothing is opened until the
    first :meth:`start`, and :meth:`submit` short-circuits on the mode check.
    """

    def __init__(
        self,
        path: Path,
        mode: str = "full",
        retention_days: int = 7,
        max_bytes: int = 2 * 1024**3,
        max_body_bytes: int = 256 * 1024,
        queue_max: int = QUEUE_MAX,
    ) -> None:
        self.path = Path(path)
        self.mode = mode if mode in MODES else "full"
        self.retention_days = retention_days
        self.max_bytes = max_bytes
        self.max_body_bytes = max_body_bytes
        self._q: queue.Queue[Record | None] = queue.Queue(maxsize=queue_max)
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        # Counters are plain ints touched only under the GIL; exactness under
        # concurrent increment does not matter for what they are used for.
        self.dropped = 0
        self.written = 0
        self.write_errors = 0
        self.last_error: str | None = None
        self._lock = threading.Lock()

    # --- lifecycle --------------------------------------------------------

    @property
    def enabled(self) -> bool:
        return self.mode != "off"

    @property
    def keep_bodies(self) -> bool:
        return self.mode == "full"

    def start(self) -> None:
        if self._thread is not None:
            return
        self._init_db()
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="audit-writer", daemon=True)
        self._thread.start()
        log.info("Audit log started: %s (mode=%s, %dd / %s cap)",
                 self.path, self.mode, self.retention_days, _human_bytes(self.max_bytes))

    def stop(self, timeout: float = 5.0) -> None:
        """Drain what is queued and shut the writer down."""
        if self._thread is None:
            return
        self._stop.set()
        try:
            self._q.put_nowait(None)  # wake the writer out of its timed get
        except queue.Full:
            pass
        self._thread.join(timeout=timeout)
        self._thread = None

    # --- hot path ---------------------------------------------------------

    def submit(self, record: Record) -> None:
        """Hand a finished request to the writer. Never blocks, never raises.

        This is the only method the request path calls, and it is deliberately
        four lines long: a mode check, an enqueue, and a drop counter.
        """
        if self.mode == "off" or self._thread is None:
            return
        try:
            self._q.put_nowait(record)
        except queue.Full:
            self.dropped += 1
            if self.dropped % 500 == 1:
                log.warning("Audit queue full — dropped %d record(s) so far", self.dropped)

    def depth(self) -> int:
        return self._q.qsize()

    # --- writer thread ----------------------------------------------------

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path, timeout=15)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=10000")
        # Durability is traded for throughput on purpose: an audit row lost to a
        # power cut is not worth an fsync on every batch.
        conn.execute("PRAGMA synchronous=NORMAL")
        return conn

    def _init_db(self) -> None:
        """Create the database with incremental auto-vacuum, then the schema.

        Ordering matters and is easy to get wrong: ``auto_vacuum`` is recorded
        in the file header when the first page is written, and *any* earlier
        write — including setting ``journal_mode=WAL`` — fixes it at the default
        of "none", after which the pragma silently does nothing and deleting
        rows never returns a byte to the filesystem. So it goes first, on a
        connection that has done nothing else, and a database that somehow ends
        up without it is converted with a one-time VACUUM.
        """
        self.path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(self.path, timeout=15)
        try:
            conn.execute("PRAGMA auto_vacuum=INCREMENTAL")
            if conn.execute("PRAGMA auto_vacuum").fetchone()[0] != 2:
                # Pre-existing file created without it: VACUUM rewrites the
                # header. Only ever paid once, at startup.
                conn.execute("VACUUM")
                log.info("Converted %s to incremental auto-vacuum", self.path)
            conn.execute("PRAGMA journal_mode=WAL")
            conn.executescript(_SCHEMA)
            conn.commit()
        finally:
            conn.close()

    def _run(self) -> None:
        conn = self._connect()
        next_sweep = time.time() + 60
        try:
            while True:
                batch = self._drain()
                if batch:
                    self._write(conn, batch)
                now = time.time()
                if now >= next_sweep:
                    next_sweep = now + 300
                    try:
                        self.sweep(conn)
                    except Exception as e:  # noqa: BLE001 - retention is best-effort
                        log.warning("Audit retention sweep failed: %s", e)
                if self._stop.is_set() and self._q.empty():
                    return
        finally:
            conn.close()

    def _drain(self) -> list[Record]:
        """Block briefly for one record, then scoop up whatever else is ready."""
        batch: list[Record] = []
        try:
            first = self._q.get(timeout=FLUSH_INTERVAL)
        except queue.Empty:
            return batch
        if first is not None:
            batch.append(first)
        while len(batch) < BATCH_MAX:
            try:
                item = self._q.get_nowait()
            except queue.Empty:
                break
            if item is not None:
                batch.append(item)
        return batch

    def _write(self, conn: sqlite3.Connection, batch: list[Record]) -> None:
        keep, cap = self.keep_bodies, self.max_body_bytes
        rows = []
        for rec in batch:
            try:
                rows.append(rec.row(keep, cap))
            except Exception as e:  # noqa: BLE001 - one bad record must not sink the batch
                log.warning("Skipping unencodable audit record: %s", e)
        if not rows:
            return
        placeholders = ", ".join("?" * len(_COLUMNS))
        sql = f"INSERT INTO requests ({', '.join(_COLUMNS)}) VALUES ({placeholders})"
        try:
            with conn:
                conn.executemany(sql, rows)
            self.written += len(rows)
        except Exception as e:  # noqa: BLE001 - keep serving even if audit I/O fails
            self.write_errors += 1
            self.last_error = str(e)
            log.warning("Audit write failed (%d rows): %s", len(rows), e)

    # --- retention --------------------------------------------------------

    def sweep(self, conn: sqlite3.Connection | None = None) -> dict:
        """Enforce the age cap, then the size cap. Returns what it removed.

        Age first because it is a single indexed delete; the size pass only has
        work left to do if a week of traffic genuinely exceeds the byte budget.
        """
        own = conn is None
        conn = conn or self._connect()
        try:
            removed_age = 0
            if self.retention_days > 0:
                cutoff = time.time() - self.retention_days * 86400
                with conn:
                    removed_age = conn.execute(
                        "DELETE FROM requests WHERE ts < ?", (cutoff,)
                    ).rowcount
            if removed_age:
                self._reclaim(conn)
            removed_size = self._enforce_size(conn)
            if removed_age or removed_size:
                log.info("Audit sweep: removed %d aged + %d oversize row(s), now %s",
                         removed_age, removed_size, _human_bytes(self._db_bytes(conn)))
            return {"removed_age": removed_age, "removed_size": removed_size,
                    "bytes": self._db_bytes(conn)}
        finally:
            if own:
                conn.close()

    def _enforce_size(self, conn: sqlite3.Connection) -> int:
        """Delete oldest rows until the file fits the byte budget.

        Deletes in chunks and re-measures, because how many rows a megabyte
        represents depends entirely on whether they carry bodies — a metadata
        row is a few hundred bytes, a captured 256KB prompt is a thousand times
        that, and a fixed batch size would be wrong for both.

        Each pass estimates the fraction of rows to drop from the fraction of
        bytes we are over by, and deliberately **under**-shoots (90% of the
        estimate, and never the last remaining row). Overshooting here throws
        away history nobody asked us to throw away; undershooting just costs
        another pass, and the loop converges from above in a handful of them.
        The iteration cap keeps a pathological case off the writer thread.
        """
        if self.max_bytes <= 0:
            return 0
        removed = 0
        for _ in range(64):
            size = self._db_bytes(conn)
            if size <= self.max_bytes:
                break
            total = conn.execute("SELECT COUNT(*) AS n FROM requests").fetchone()["n"]
            if total <= 1:
                break   # one record over budget is as small as this gets
            over = (size - self.max_bytes) / size
            chunk = max(1, min(total - 1, int(total * over * 0.9)))
            with conn:
                cur = conn.execute(
                    "DELETE FROM requests WHERE id IN "
                    "(SELECT id FROM requests ORDER BY ts LIMIT ?)", (chunk,)
                )
            removed += cur.rowcount
            self._reclaim(conn)
        return removed

    @staticmethod
    def _reclaim(conn: sqlite3.Connection) -> None:
        """Actually give the freed pages back to the filesystem.

        Two steps, both needed. ``incremental_vacuum`` moves free pages out of
        the database file, and the checkpoint folds the write-ahead log back in
        and truncates it — without that the -wal file keeps the deleted pages
        alive on disk, and a size budget that ignores it is a fiction.

        The ``fetchall`` is not decorative. ``PRAGMA incremental_vacuum`` does
        its work as its cursor is stepped, and Python's sqlite3 driver steps it
        lazily, so calling ``execute`` alone frees exactly *one* page: deleting
        a gigabyte of rows would shrink the file by 4KB and the size cap would
        never converge. Draining the cursor is what actually runs the vacuum.
        """
        conn.execute("PRAGMA incremental_vacuum").fetchall()
        conn.commit()
        conn.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchall()

    @staticmethod
    def _db_bytes(conn: sqlite3.Connection) -> int:
        page_count = conn.execute("PRAGMA page_count").fetchone()[0]
        page_size = conn.execute("PRAGMA page_size").fetchone()[0]
        return int(page_count) * int(page_size)

    # --- reads (admin API; runs in a worker thread) ------------------------

    def stats(self) -> dict:
        """Counts, span, and on-disk footprint — cheap enough to poll."""
        out: dict[str, Any] = {
            "mode": self.mode,
            "retention_days": self.retention_days,
            "max_bytes": self.max_bytes,
            "max_body_bytes": self.max_body_bytes,
            "queued": self.depth(),
            "queue_max": self._q.maxsize,
            "dropped": self.dropped,
            "written": self.written,
            "write_errors": self.write_errors,
            "last_error": self.last_error,
            "rows": 0, "bytes": 0, "oldest": None, "newest": None,
        }
        if self._thread is None and not self.path.exists():
            return out
        try:
            conn = self._connect()
        except Exception as e:  # noqa: BLE001
            out["last_error"] = str(e)
            return out
        try:
            row = conn.execute(
                "SELECT COUNT(*) AS n, MIN(ts) AS lo, MAX(ts) AS hi FROM requests"
            ).fetchone()
            out["rows"] = row["n"] or 0
            out["oldest"] = row["lo"]
            out["newest"] = row["hi"]
            out["bytes"] = self._db_bytes(conn)
        except Exception as e:  # noqa: BLE001
            out["last_error"] = str(e)
        finally:
            conn.close()
        return out

    def query(
        self,
        limit: int = 100,
        before_id: int | None = None,
        after_id: int | None = None,
        key_name: str | None = None,
        model: str | None = None,
        status: int | None = None,
        outcome: str | None = None,
        since: float | None = None,
        search: str | None = None,
        failed: bool = False,
    ) -> list[dict]:
        """Newest-first page of request rows, blobs excluded.

        ``before_id`` is a keyset cursor rather than an OFFSET so paging stays
        O(limit) however deep the operator scrolls, and doesn't skip or repeat
        rows when new requests land mid-scroll.

        ``failed`` selects every non-``ok`` outcome at once. It exists so that
        watching for trouble is a single query with no client-side filtering:
        polling for failures must not mean paging through every success to find
        them, or a busy proxy makes its own monitoring expensive.
        """
        where: list[str] = []
        params: list[Any] = []
        if before_id:
            where.append("id < ?")
            params.append(int(before_id))
        if after_id:  # live tail: only what has landed since the last poll
            where.append("id > ?")
            params.append(int(after_id))
        if key_name:
            where.append("key_name = ?")
            params.append(key_name)
        if model:
            where.append("model = ?")
            params.append(model)
        if status is not None:
            where.append("status = ?")
            params.append(int(status))
        if outcome:
            where.append("outcome = ?")
            params.append(outcome)
        if failed:
            # A row written before this vocabulary existed can have a NULL or
            # empty outcome; judge those on the status line instead of assuming
            # they were fine.
            placeholders = ", ".join("?" * len(FAILURE_OUTCOMES))
            where.append(
                f"(outcome IN ({placeholders}) OR status >= 400 "
                "OR (outcome IS NULL AND status >= 400))"
            )
            params += list(FAILURE_OUTCOMES)
        if since:
            where.append("ts >= ?")
            params.append(float(since))
        if search:
            where.append("(summary LIKE ? OR model LIKE ? OR path LIKE ?)")
            like = f"%{search}%"
            params += [like, like, like]
        clause = f"WHERE {' AND '.join(where)}" if where else ""
        sql = (f"SELECT {', '.join(_LIST_COLUMNS)} FROM requests {clause} "
               f"ORDER BY id DESC LIMIT ?")
        params.append(max(1, min(int(limit), 500)))
        try:
            conn = self._connect()
        except Exception:  # noqa: BLE001
            return []
        try:
            return [dict(r) for r in conn.execute(sql, params)]
        except Exception as e:  # noqa: BLE001
            log.warning("Audit query failed: %s", e)
            return []
        finally:
            conn.close()

    def get(self, row_id: int) -> dict | None:
        """One request with its bodies decompressed and parsed where possible."""
        try:
            conn = self._connect()
        except Exception:  # noqa: BLE001
            return None
        try:
            row = conn.execute("SELECT * FROM requests WHERE id = ?", (int(row_id),)).fetchone()
        except Exception as e:  # noqa: BLE001
            log.warning("Audit fetch failed: %s", e)
            return None
        finally:
            conn.close()
        if row is None:
            return None
        out = {k: row[k] for k in row.keys() if k not in ("req_blob", "resp_blob")}
        out["request"] = _inflate_json(row["req_blob"])
        out["response"] = _inflate_text(row["resp_blob"])
        return out

    def purge(self) -> int:
        """Delete every recorded request and give the space straight back."""
        try:
            conn = self._connect()
        except Exception:  # noqa: BLE001
            return 0
        try:
            with conn:
                n = conn.execute("DELETE FROM requests").rowcount
            self._reclaim(conn)
            return n
        finally:
            conn.close()

    def overview(self, since: float) -> dict:
        """Volume, error rate, and latency percentiles over a window.

        Percentiles come from an OFFSET into an ordered scan rather than an
        average, because latency is the classic long-tailed distribution where
        the mean tells you about nobody: a p95 that has doubled is a real user
        complaint, a mean that has doubled might be one slow batch job.

        They also cover *only requests that reached upstream*. A rejected key or
        a blocked budget is answered locally in about a microsecond, and letting
        those into the sample makes latency look better the more requests you
        turn away — the p50 drops purely because more of the population never
        left the building. Restricting both series to rows with a TTFB keeps
        them measuring the same population as each other, and the one an
        operator actually means.
        """
        out = {
            "since": since, "requests": 0, "errors": 0, "blocked": 0,
            "rejected": 0, "aborted": 0, "incomplete": 0,
            "forwarded": 0, "cost_usd": 0.0, "tokens": 0,
            "ttfb_p50": None, "ttfb_p95": None,
            "latency_p50": None, "latency_p95": None,
        }
        try:
            conn = self._connect()
        except Exception:  # noqa: BLE001
            return out
        try:
            row = conn.execute(
                "SELECT COUNT(*) AS n, "
                "SUM(outcome = 'error') AS errors, SUM(outcome = 'blocked') AS blocked, "
                "SUM(outcome = 'rejected') AS rejected, "
                "SUM(outcome = 'aborted') AS aborted, "
                "SUM(outcome = 'incomplete') AS incomplete, "
                "SUM(ttfb_ms IS NOT NULL) AS forwarded, "
                "SUM(cost_usd) AS cost, "
                "SUM(input_tokens + output_tokens + cache_read_input_tokens + "
                "    cache_creation_input_tokens) AS tokens "
                "FROM requests WHERE ts >= ?", (float(since),)
            ).fetchone()
            out["requests"] = row["n"] or 0
            out["errors"] = row["errors"] or 0
            out["blocked"] = row["blocked"] or 0
            out["rejected"] = row["rejected"] or 0
            out["aborted"] = row["aborted"] or 0
            out["incomplete"] = row["incomplete"] or 0
            out["forwarded"] = row["forwarded"] or 0
            out["cost_usd"] = row["cost"] or 0.0
            out["tokens"] = row["tokens"] or 0
            for column, prefix in (("ttfb_ms", "ttfb"), ("latency_ms", "latency")):
                for q, label in ((0.50, "p50"), (0.95, "p95")):
                    out[f"{prefix}_{label}"] = _percentile(conn, column, since, q)
        except Exception as e:  # noqa: BLE001
            log.warning("Audit overview failed: %s", e)
        finally:
            conn.close()
        return out

    def top_models(self, since: float, limit: int = 8) -> list[dict]:
        """Spend and volume by model, over forwarded requests only.

        Rejected requests carry no model, so including them adds a nameless row
        with zero cost and a microsecond latency to a table about models — and
        drags the average latency column with it.
        """
        try:
            conn = self._connect()
        except Exception:  # noqa: BLE001
            return []
        try:
            rows = conn.execute(
                "SELECT model, COUNT(*) AS requests, SUM(cost_usd) AS cost_usd, "
                "AVG(latency_ms) AS avg_latency_ms, AVG(ttfb_ms) AS avg_ttfb_ms "
                "FROM requests WHERE ts >= ? AND ttfb_ms IS NOT NULL "
                "GROUP BY model ORDER BY cost_usd DESC LIMIT ?",
                (float(since), int(limit)),
            ).fetchall()
            return [dict(r) for r in rows]
        except Exception:  # noqa: BLE001
            return []
        finally:
            conn.close()


# Only requests that actually reached upstream carry a TTFB, so this doubles as
# "was this forwarded" — and using it for both series keeps them comparable.
_FORWARDED = "ts >= ? AND ttfb_ms IS NOT NULL AND {column} IS NOT NULL"


def _percentile(conn: sqlite3.Connection, column: str, since: float, q: float) -> float | None:
    """The q-th percentile of ``column`` over forwarded requests, or None.

    ``column`` is never user input — it comes from a fixed tuple at the one call
    site — so interpolating it into the SQL is safe here.
    """
    where = _FORWARDED.format(column=column)
    n = conn.execute(
        f"SELECT COUNT({column}) AS n FROM requests WHERE {where}", (since,),
    ).fetchone()["n"]
    if not n:
        return None
    offset = min(n - 1, int(n * q))
    row = conn.execute(
        f"SELECT {column} AS v FROM requests WHERE {where} ORDER BY {column} LIMIT 1 OFFSET ?",
        (since, offset),
    ).fetchone()
    return row["v"] if row else None


def _inflate_json(blob: bytes | None) -> Any:
    text = _inflate_text(blob)
    if text is None:
        return None
    try:
        return json.loads(text)
    except Exception:  # noqa: BLE001 - keep the raw text if it isn't valid JSON
        return text


def _inflate_text(blob: bytes | None) -> str | None:
    if not blob:
        return None
    try:
        return zlib.decompress(blob).decode("utf-8", "replace")
    except Exception:  # noqa: BLE001
        return None


def _human_bytes(n: int) -> str:
    step = 1024.0
    value = float(n)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if value < step or unit == "TB":
            return f"{value:.0f}{unit}" if unit == "B" else f"{value:.1f}{unit}"
        value /= step
    return f"{value:.1f}TB"  # pragma: no cover - unreachable, loop returns first


def default_path() -> Path:
    from .paths import AUDIT_DB_FILE  # noqa: PLC0415 - avoids an import cycle

    return Path(os.environ.get("CLAUDE_PROXY_AUDIT_DB", str(AUDIT_DB_FILE)))
