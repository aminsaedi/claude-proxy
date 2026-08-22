"""Filesystem locations for data files.

Everything hangs off ``CLAUDE_PROXY_DATA_DIR``, which defaults to the current
working directory and is set to ``/app/data`` in the image and ``/data`` in k8s.
Individual files can also be pointed elsewhere (``CLAUDE_PROXY_DB``,
``CLAUDE_PROXY_AUDIT_DB``, ``CLAUDE_PROXY_PRICING_CACHE``); mount the *directory*
rather than the files, since SQLite writes ``-wal``/``-shm`` siblings next to each DB.
"""

from __future__ import annotations

import os
from pathlib import Path

DATA_DIR = Path(os.environ.get("CLAUDE_PROXY_DATA_DIR", ".")).resolve()

# SQLite is the single source of truth for tokens, virtual keys, config, and usage.
DB_FILE = Path(os.environ.get("CLAUDE_PROXY_DB", str(DATA_DIR / "claude_proxy.db")))

# Request/prompt audit log. A separate file from the main DB on purpose: its
# write volume is orders of magnitude higher, and isolating it keeps that
# traffic off the tokens/keys/usage tables entirely (see audit.py).
AUDIT_DB_FILE = Path(
    os.environ.get("CLAUDE_PROXY_AUDIT_DB", str(DATA_DIR / "audit.db"))
)

# Cached copy of the online model price list (see pricing.py).
PRICING_CACHE_FILE = Path(
    os.environ.get("CLAUDE_PROXY_PRICING_CACHE", str(DATA_DIR / "model_prices.json"))
)

# Legacy YAML/JSON locations — read only by the one-time migration importer.
CONFIG_FILE = DATA_DIR / "config.yaml"
TOKENS_FILE = DATA_DIR / "tokens.yaml"
VKEYS_FILE = DATA_DIR / "virtual_keys.yaml"
USAGE_FILE = DATA_DIR / "usage_stats.json"

STATIC_DIR = Path(__file__).parent / "static"
