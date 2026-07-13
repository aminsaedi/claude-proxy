"""Point the package at a throwaway data dir *before* it is imported."""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path

_TMP = Path(tempfile.mkdtemp(prefix="claude-proxy-test-"))
os.environ["CLAUDE_PROXY_DATA_DIR"] = str(_TMP)

(_TMP / "tokens.yaml").write_text(
    "tokens:\n"
    "  - name: a\n    token: sk-a\n    default: true\n"
    "  - name: b\n    token: sk-b\n"
)
(_TMP / "virtual_keys.yaml").write_text(
    "virtual_keys:\n  - name: alice\n    key: vk-alice\n  - name: bob\n    key: vk-bob\n"
)
(_TMP / "config.yaml").write_text(
    "auto_rotation:\n  enabled: false\n  threshold_5h: 0.9\n  target_max_util_5h: 0.5\n"
    "  check_interval_seconds: 30\n  probe_before_switch: false\n  cooldown_seconds: 0\n"
    "  notify_only: false\n"
    "health_probe_interval_seconds: 60\nactive_probe_interval_seconds: 300\n"
    "upstream_timeout_seconds: 600\n"
)
(_TMP / "usage_stats.json").write_text(json.dumps({}))
