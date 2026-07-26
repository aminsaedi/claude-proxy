"""One-time importer: legacy YAML/JSON data files -> SQLite.

Reads tokens.yaml / virtual_keys.yaml / config.yaml / usage_stats.json from the
data dir (``CLAUDE_PROXY_DATA_DIR``) and populates the SQLite DB
(``CLAUDE_PROXY_DB``). Idempotent for tokens/keys (skips names already present).

Imported usage lands as lifetime totals with ``cost_usd = 0``: the legacy files
recorded neither cost nor a timestamp, so there is nothing to price or to place
on the hourly series. Cost accrues from the first request after the migration.

    python -m claude_proxy.migrate
"""

from __future__ import annotations

import json

import yaml

from . import db
from .models import AppConfig
from .paths import CONFIG_FILE, DB_FILE, TOKENS_FILE, USAGE_FILE, VKEYS_FILE


def _yaml(path):
    return (yaml.safe_load(path.read_text()) or {}) if path.exists() else {}


def main() -> None:
    db.init_schema()
    print(f"DB: {DB_FILE}")

    existing_tokens = {t["name"] for t in db.list_tokens()}
    added_t = 0
    for t in _yaml(TOKENS_FILE).get("tokens", []):
        if t["name"] not in existing_tokens:
            db.add_token(t["name"], t["token"], bool(t.get("default")))
            added_t += 1
    print(f"tokens: +{added_t} (now {len(db.list_tokens())})")

    existing_keys = {k["name"] for k in db.list_virtual_keys()}
    added_k = 0
    for vk in _yaml(VKEYS_FILE).get("virtual_keys", []):
        if vk["name"] not in existing_keys:
            db.add_virtual_key(vk["name"], vk["key"])
            added_k += 1
    print(f"virtual_keys: +{added_k} (now {len(db.list_virtual_keys())})")

    cfg_data = _yaml(CONFIG_FILE)
    if cfg_data:
        db.set_config_json(AppConfig.model_validate(cfg_data).model_dump())
        print("config: imported")

    if USAGE_FILE.exists():
        try:
            usage = json.loads(USAGE_FILE.read_text())
        except Exception:  # noqa: BLE001
            usage = {}
        if usage:
            db.replace_usage(usage)
            print(f"usage: imported {sum(len(v) for v in usage.values())} rows")

    print("done.")


if __name__ == "__main__":
    main()
