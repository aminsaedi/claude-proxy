"""Filesystem locations for data files.

All paths default to the current working directory (``/app`` in the container,
where the YAML/JSON files are bind-mounted) and can be overridden with the
``CLAUDE_PROXY_DATA_DIR`` env var — handy for tests.
"""

from __future__ import annotations

import os
from pathlib import Path

DATA_DIR = Path(os.environ.get("CLAUDE_PROXY_DATA_DIR", ".")).resolve()

# SQLite is the single source of truth for tokens, virtual keys, config, and usage.
DB_FILE = Path(os.environ.get("CLAUDE_PROXY_DB", str(DATA_DIR / "claude_proxy.db")))

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
