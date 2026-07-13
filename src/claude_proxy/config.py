"""Load and persist the hot-reloadable proxy configuration (SQLite ``config`` row)."""

from __future__ import annotations

import logging

from . import db
from .models import AppConfig

log = logging.getLogger("claude_proxy.config")


def load_config() -> AppConfig:
    """Read config from the DB, falling back to (and persisting) defaults.

    Never raises on a malformed row — logs and returns defaults instead.
    """
    data = db.get_config_json()
    if data is not None:
        try:
            cfg = AppConfig.model_validate(data)
            log.info("Config loaded: auto_rotation.enabled=%s", cfg.auto_rotation.enabled)
            return cfg
        except Exception as e:  # noqa: BLE001 - config must never crash startup
            log.warning("Invalid config row (%s) — using defaults", e)
            return AppConfig()
    cfg = AppConfig()
    save_config(cfg)
    return cfg


def save_config(cfg: AppConfig) -> None:
    try:
        db.set_config_json(cfg.model_dump())
    except Exception as e:  # noqa: BLE001
        log.warning("Failed to save config: %s", e)
