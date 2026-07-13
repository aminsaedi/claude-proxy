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
    PRIMARY KEY (key_name, model)
);
"""

_USAGE_FIELDS = (
    "input_tokens", "output_tokens",
    "cache_read_input_tokens", "cache_creation_input_tokens", "requests",
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
    with cursor(path) as conn:
        conn.execute("DELETE FROM virtual_keys WHERE name = ?", (name,))


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
    """Return the nested {key_name: {model: {field: int}}} shape the app uses."""
    with cursor(path) as conn:
        rows = conn.execute("SELECT * FROM usage").fetchall()
    out: dict[str, dict[str, dict[str, int]]] = {}
    for r in rows:
        out.setdefault(r["key_name"], {})[r["model"]] = {f: r[f] for f in _USAGE_FIELDS}
    return out


def replace_usage(stats: dict, path: Path | None = None) -> None:
    """Overwrite the usage table from the in-memory snapshot (called by the flusher)."""
    rows = [
        (key_name, model, *(m.get(f, 0) for f in _USAGE_FIELDS))
        for key_name, models in stats.items()
        for model, m in models.items()
    ]
    with cursor(path) as conn:
        conn.execute("DELETE FROM usage")
        conn.executemany(
            "INSERT INTO usage (key_name, model, input_tokens, output_tokens, "
            "cache_read_input_tokens, cache_creation_input_tokens, requests) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            rows,
        )
