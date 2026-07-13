"""Load and persist the hot-reloadable proxy configuration (config.yaml)."""

from __future__ import annotations

import logging

import yaml

from .atomicio import atomic_write_text
from .models import AppConfig
from .paths import CONFIG_FILE

log = logging.getLogger("claude_proxy.config")


def load_config() -> AppConfig:
    """Read config.yaml, falling back to defaults for any missing keys.

    Writes a default file the first time if none exists. Never raises on a
    malformed file — logs and returns defaults instead.
    """
    if CONFIG_FILE.exists():
        try:
            data = yaml.safe_load(CONFIG_FILE.read_text()) or {}
            cfg = AppConfig.model_validate(data)
            log.info("Config loaded: auto_rotation.enabled=%s", cfg.auto_rotation.enabled)
            return cfg
        except Exception as e:  # noqa: BLE001 - config must never crash startup
            log.warning("Failed to load config.yaml (%s) — using defaults", e)
            return AppConfig()
    cfg = AppConfig()
    save_config(cfg)
    return cfg


def save_config(cfg: AppConfig) -> None:
    """Persist the config atomically."""
    text = yaml.dump(cfg.model_dump(), default_flow_style=False, sort_keys=False)
    try:
        atomic_write_text(CONFIG_FILE, text)
    except Exception as e:  # noqa: BLE001
        log.warning("Failed to save config.yaml: %s", e)
