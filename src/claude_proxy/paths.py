"""Filesystem locations for data files.

All paths default to the current working directory (``/app`` in the container,
where the YAML/JSON files are bind-mounted) and can be overridden with the
``CLAUDE_PROXY_DATA_DIR`` env var — handy for tests.
"""

from __future__ import annotations

import os
from pathlib import Path

DATA_DIR = Path(os.environ.get("CLAUDE_PROXY_DATA_DIR", ".")).resolve()

CONFIG_FILE = DATA_DIR / "config.yaml"
TOKENS_FILE = DATA_DIR / "tokens.yaml"
VKEYS_FILE = DATA_DIR / "virtual_keys.yaml"
USAGE_FILE = DATA_DIR / "usage_stats.json"

STATIC_DIR = Path(__file__).parent / "static"
