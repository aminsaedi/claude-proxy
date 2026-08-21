"""SQLite persistence layer — the single source of truth for tokens, virtual
keys, config, and usage.

Design: SQLite is only touched at startup (load), by the debounced usage
flusher, and by admin/CRUD calls — never inline on the request hot path (the
proxy serves from in-memory caches). WAL mode makes it safe for the app and a
`manage.py` process to share the DB file concurrently. All functions are
synchronous; async callers wrap writes in ``asyncio.to_thread``.
"""

from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from pathlib import Path

from .paths import DB_FILE

_SCHEMA = """
CREATE TABLE IF NOT EXISTS tokens (
    name       TEXT PRIMARY KEY,
    token      TEXT NOT NULL,
    is_default INTEGER NOT NULL DEFAULT 0,
    position   INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS virtual_keys (
    name TEXT PRIMARY KEY,
    key  TEXT NOT NULL UNIQUE
);
CREATE TABLE IF NOT EXISTS config (
    id   INTEGER PRIMARY KEY CHECK (id = 1),
    json TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS usage (
    key_name       TEXT NOT NULL,
    model          TEXT NOT NULL,
    input_tokens   INTEGER NOT NULL DEFAULT 0,
    output_tokens  INTEGER NOT NULL DEFAULT 0,
    cache_read_input_tokens     INTEGER NOT NULL DEFAULT 0,
    cache_creation_input_tokens INTEGER NOT NULL DEFAULT 0,
    requests       INTEGER NOT NULL DEFAULT 0,
    cost_usd       REAL NOT NULL DEFAULT 0,
    PRIMARY KEY (key_name, model)
);
-- Time series behind the 1d/3d/7d views and the spend limits. One row per
-- (UTC hour, key, model); hourly granularity is fine because every limit
-- window we support starts on an hour boundary in the configured timezone.
CREATE TABLE IF NOT EXISTS usage_hourly (
    hour_start     INTEGER NOT NULL,   -- UTC epoch seconds, floored to the hour
    key_name       TEXT NOT NULL,
    model          TEXT NOT NULL,
    input_tokens   INTEGER NOT NULL DEFAULT 0,
    output_tokens  INTEGER NOT NULL DEFAULT 0,
    cache_read_input_tokens     INTEGER NOT NULL DEFAULT 0,
    cache_creation_input_tokens INTEGER NOT NULL DEFAULT 0,
    requests       INTEGER NOT NULL DEFAULT 0,
    cost_usd       REAL NOT NULL DEFAULT 0,
    PRIMARY KEY (hour_start, key_name, model)
);
CREATE INDEX IF NOT EXISTS usage_hourly_key_idx ON usage_hourly (key_name, hour_start);
-- Spend caps. A key may have several rows (e.g. $3/hour *and* $10/day); every
-- one of them is enforced, and the tightest breach is the one that blocks.
CREATE TABLE IF NOT EXISTS key_limits (
    key_name  TEXT NOT NULL,
    period    TEXT NOT NULL,          -- hour | day | week | month
    limit_usd REAL NOT NULL,
    PRIMARY KEY (key_name, period)
);
"""

_USAGE_FIELDS = (
    "input_tokens", "output_tokens",
    "cache_read_input_tokens", "cache_creation_input_tokens", "requests",
)
# Same set plus the money column; cost is a float, the rest are counters.
_USAGE_COLUMNS = (*_USAGE_FIELDS, "cost_usd")

# Columns added after v2.0 shipped — applied to existing DBs on startup.
_ADDED_COLUMNS = (
    ("usage", "cost_usd", "REAL NOT NULL DEFAULT 0"),
)


def connect(path: Path | None = None) -> sqlite3.Connection:
    p = Path(path) if path is not None else DB_FILE
    p.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(p, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=5000")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


@contextmanager
def cursor(path: Path | None = None):
    conn = connect(path)
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def init_schema(path: Path | None = None) -> None:
    with cursor(path) as conn:
        conn.executescript(_SCHEMA)
        _add_missing_columns(conn)


def _add_missing_columns(conn: sqlite3.Connection) -> None:
    """Bring a pre-existing DB up to the current schema (additive only)."""
    for table, column, decl in _ADDED_COLUMNS:
        cols = {r["name"] for r in conn.execute(f"PRAGMA table_info({table})")}
        if cols and column not in cols:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {decl}")


# --- tokens ---------------------------------------------------------------

def list_tokens(path: Path | None = None) -> list[dict]:
    with cursor(path) as conn:
        rows = conn.execute(
            "SELECT name, token, is_default FROM tokens ORDER BY position, rowid"
        ).fetchall()
    return [{"name": r["name"], "token": r["token"], "default": bool(r["is_default"])} for r in rows]


def add_token(name: str, token: str, is_default: bool = False, path: Path | None = None) -> None:
    with cursor(path) as conn:
        pos = conn.execute("SELECT COALESCE(MAX(position), -1) + 1 AS p FROM tokens").fetchone()["p"]
        if is_default:
            conn.execute("UPDATE tokens SET is_default = 0")
        conn.execute(
            "INSERT INTO tokens (name, token, is_default, position) VALUES (?, ?, ?, ?)",
            (name, token, int(is_default), pos),
        )


def delete_token(name: str, path: Path | None = None) -> None:
    with cursor(path) as conn:
        conn.execute("DELETE FROM tokens WHERE name = ?", (name,))


def set_default_token(name: str, path: Path | None = None) -> None:
    with cursor(path) as conn:
        conn.execute("UPDATE tokens SET is_default = 0")
        conn.execute("UPDATE tokens SET is_default = 1 WHERE name = ?", (name,))


def update_token(
    name: str,
    token: str | None = None,
    is_default: bool | None = None,
    path: Path | None = None,
) -> bool:
    """Update a token's secret and/or promote it to default. Returns False if unknown.

    ``token=None`` keeps the current secret; ``is_default`` only acts when True
    (promoting one token demotes the rest) — pass ``set_default_token`` semantics.
    """
    with cursor(path) as conn:
        if not conn.execute("SELECT 1 FROM tokens WHERE name = ?", (name,)).fetchone():
            return False
        if token is not None:
            conn.execute("UPDATE tokens SET token = ? WHERE name = ?", (token, name))
        if is_default:
            conn.execute("UPDATE tokens SET is_default = 0")
            conn.execute("UPDATE tokens SET is_default = 1 WHERE name = ?", (name,))
        return True


# --- virtual keys ---------------------------------------------------------

def list_virtual_keys(path: Path | None = None) -> list[dict]:
    with cursor(path) as conn:
        rows = conn.execute("SELECT name, key FROM virtual_keys ORDER BY rowid").fetchall()
    return [{"name": r["name"], "key": r["key"]} for r in rows]


def add_virtual_key(name: str, key: str, path: Path | None = None) -> None:
    with cursor(path) as conn:
        conn.execute("INSERT INTO virtual_keys (name, key) VALUES (?, ?)", (name, key))


def delete_virtual_key(name: str, path: Path | None = None) -> None:
    """Remove a key and its spend limits. Usage history is deliberately kept."""
    with cursor(path) as conn:
        conn.execute("DELETE FROM virtual_keys WHERE name = ?", (name,))
        conn.execute("DELETE FROM key_limits WHERE key_name = ?", (name,))


def set_virtual_key(name: str, key: str, path: Path | None = None) -> bool:
    """Replace a virtual key's secret value (rotation). Name and usage are kept."""
    with cursor(path) as conn:
        cur = conn.execute("UPDATE virtual_keys SET key = ? WHERE name = ?", (key, name))
        return cur.rowcount > 0


def rename_virtual_key(old: str, new: str, path: Path | None = None) -> bool:
    """Rename a virtual key, migrating its usage rows so history follows the client.

    Returns False if ``old`` is unknown. Raises ``sqlite3.IntegrityError`` if
    ``new`` already exists (PRIMARY KEY collision).
    """
    with cursor(path) as conn:
        if not conn.execute("SELECT 1 FROM virtual_keys WHERE name = ?", (old,)).fetchone():
            return False
        conn.execute("UPDATE virtual_keys SET name = ? WHERE name = ?", (new, old))
        conn.execute("UPDATE usage SET key_name = ? WHERE key_name = ?", (new, old))
        conn.execute("UPDATE usage_hourly SET key_name = ? WHERE key_name = ?", (new, old))
        conn.execute("UPDATE key_limits SET key_name = ? WHERE key_name = ?", (new, old))
        return True


# --- config ---------------------------------------------------------------

def get_config_json(path: Path | None = None) -> dict | None:
    with cursor(path) as conn:
        row = conn.execute("SELECT json FROM config WHERE id = 1").fetchone()
    return json.loads(row["json"]) if row else None


def set_config_json(data: dict, path: Path | None = None) -> None:
    with cursor(path) as conn:
        conn.execute(
            "INSERT INTO config (id, json) VALUES (1, ?) "
            "ON CONFLICT(id) DO UPDATE SET json = excluded.json",
            (json.dumps(data),),
        )


# --- usage ----------------------------------------------------------------

def load_usage(path: Path | None = None) -> dict:
    """Return the nested {key_name: {model: {field: number}}} shape the app uses."""
    with cursor(path) as conn:
        rows = conn.execute("SELECT * FROM usage").fetchall()
    out: dict[str, dict[str, dict[str, float]]] = {}
    for r in rows:
        out.setdefault(r["key_name"], {})[r["model"]] = {f: r[f] for f in _USAGE_COLUMNS}
    return out


def add_usage(rows: list[tuple], path: Path | None = None) -> None:
    """Apply lifetime *deltas*: ``(key_name, model, *_USAGE_COLUMNS)``.

    Additive rather than a whole-table rewrite, which matters twice over. It
    makes a flush cost O(what changed) instead of O(all history), and — the
    reason it is written this way — it stays correct when two processes share
    the file. A rolling deploy overlaps an old and a new pod for a few seconds;
    with a replace-from-my-snapshot write the second flush would silently erase
    whatever the other process had counted. Two additive writers just both land.
    """
    if not rows:
        return
    sets = ", ".join(f"{c} = {c} + excluded.{c}" for c in _USAGE_COLUMNS)
    with cursor(path) as conn:
        conn.executemany(
            "INSERT INTO usage (key_name, model, input_tokens, output_tokens, "
            "cache_read_input_tokens, cache_creation_input_tokens, requests, cost_usd) "
            f"VALUES (?, ?, ?, ?, ?, ?, ?, ?) "
            f"ON CONFLICT(key_name, model) DO UPDATE SET {sets}",
            rows,
        )


def read_usage(pairs: list[tuple[str, str]], path: Path | None = None) -> dict:
    """Authoritative lifetime totals for specific ``(key_name, model)`` pairs.

    Read back after a flush so a process that shares the DB with another one
    converges on the true totals instead of drifting on its own local view.
    """
    if not pairs:
        return {}
    out: dict[tuple[str, str], dict[str, float]] = {}
    with cursor(path) as conn:
        for key_name, model in pairs:
            row = conn.execute(
                "SELECT * FROM usage WHERE key_name = ? AND model = ?", (key_name, model)
            ).fetchone()
            if row is not None:
                out[(key_name, model)] = {f: row[f] for f in _USAGE_COLUMNS}
    return out


# --- hourly usage (time series) -------------------------------------------

def load_usage_hourly(since: int = 0, path: Path | None = None) -> list[dict]:
    """Rows at or after ``since`` (UTC epoch seconds), oldest first."""
    with cursor(path) as conn:
        rows = conn.execute(
            "SELECT * FROM usage_hourly WHERE hour_start >= ? ORDER BY hour_start",
            (int(since),),
        ).fetchall()
    return [
        {"hour_start": r["hour_start"], "key_name": r["key_name"], "model": r["model"],
         **{f: r[f] for f in _USAGE_COLUMNS}}
        for r in rows
    ]


def add_usage_hourly(rows: list[tuple], path: Path | None = None) -> None:
    """Apply bucket *deltas*: ``(hour_start, key_name, model, *_USAGE_COLUMNS)``.

    Additive for the same reason as :func:`add_usage` — see the note there on
    why a shared file rules out replace-from-snapshot writes.
    """
    if not rows:
        return
    sets = ", ".join(f"{c} = {c} + excluded.{c}" for c in _USAGE_COLUMNS)
    with cursor(path) as conn:
        conn.executemany(
            "INSERT INTO usage_hourly (hour_start, key_name, model, input_tokens, "
            "output_tokens, cache_read_input_tokens, cache_creation_input_tokens, "
            "requests, cost_usd) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?) "
            f"ON CONFLICT(hour_start, key_name, model) DO UPDATE SET {sets}",
            rows,
        )


def read_usage_hourly(keys: list[tuple[int, str, str]], path: Path | None = None) -> dict:
    """Authoritative bucket totals for specific ``(hour, key_name, model)`` keys."""
    if not keys:
        return {}
    out: dict[tuple[int, str, str], dict[str, float]] = {}
    with cursor(path) as conn:
        for hour, key_name, model in keys:
            row = conn.execute(
                "SELECT * FROM usage_hourly WHERE hour_start = ? AND key_name = ? AND model = ?",
                (int(hour), key_name, model),
            ).fetchone()
            if row is not None:
                out[(hour, key_name, model)] = {f: row[f] for f in _USAGE_COLUMNS}
    return out


def prune_usage_hourly(before: int, path: Path | None = None) -> int:
    """Drop buckets older than ``before``; returns the number of rows removed."""
    with cursor(path) as conn:
        cur = conn.execute("DELETE FROM usage_hourly WHERE hour_start < ?", (int(before),))
        return cur.rowcount


# --- spend limits ---------------------------------------------------------

def list_key_limits(path: Path | None = None) -> dict[str, dict[str, float]]:
    """All limits as {key_name: {period: limit_usd}}."""
    with cursor(path) as conn:
        rows = conn.execute("SELECT key_name, period, limit_usd FROM key_limits").fetchall()
    out: dict[str, dict[str, float]] = {}
    for r in rows:
        out.setdefault(r["key_name"], {})[r["period"]] = float(r["limit_usd"])
    return out


def set_key_limits(key_name: str, limits: dict[str, float], path: Path | None = None) -> None:
    """Replace *all* limits for one key. An empty dict clears them."""
    with cursor(path) as conn:
        conn.execute("DELETE FROM key_limits WHERE key_name = ?", (key_name,))
        conn.executemany(
            "INSERT INTO key_limits (key_name, period, limit_usd) VALUES (?, ?, ?)",
            [(key_name, period, float(v)) for period, v in sorted(limits.items())],
        )
