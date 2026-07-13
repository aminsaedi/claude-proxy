"""Point the package at a throwaway SQLite DB *before* it is imported, and seed it."""

from __future__ import annotations

import os
import tempfile
from pathlib import Path

_TMP = Path(tempfile.mkdtemp(prefix="claude-proxy-test-"))
os.environ["CLAUDE_PROXY_DATA_DIR"] = str(_TMP)
os.environ["CLAUDE_PROXY_DB"] = str(_TMP / "test.db")

from claude_proxy import db  # noqa: E402

db.init_schema()
db.add_token("a", "sk-a", is_default=True)
db.add_token("b", "sk-b")
db.add_virtual_key("alice", "vk-alice")
db.add_virtual_key("bob", "vk-bob")
db.set_config_json({
    "auto_rotation": {
        "enabled": False, "threshold_5h": 0.9, "target_max_util_5h": 0.5,
        "check_interval_seconds": 30, "probe_before_switch": False,
        "cooldown_seconds": 0, "notify_only": False,
    },
    "health_probe_interval_seconds": 60,
    "active_probe_interval_seconds": 300,
    "upstream_timeout_seconds": 600,
})
