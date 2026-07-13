from __future__ import annotations

import pytest
from pydantic import ValidationError

from claude_proxy.config import load_config, save_config
from claude_proxy.models import AppConfig


def test_load_config_defaults_and_roundtrip():
    cfg = load_config()
    assert cfg.upstream_timeout_seconds == 600
    cfg.auto_rotation.enabled = True
    cfg.auto_rotation.threshold_5h = 0.42
    save_config(cfg)
    reloaded = load_config()
    assert reloaded.auto_rotation.enabled is True
    assert reloaded.auto_rotation.threshold_5h == 0.42


def test_validation_rejects_bad_ratio():
    with pytest.raises(ValidationError):
        AppConfig(auto_rotation={"threshold_5h": 1.5})


def test_validation_rejects_low_interval():
    with pytest.raises(ValidationError):
        AppConfig(auto_rotation={"check_interval_seconds": 1})
    with pytest.raises(ValidationError):
        AppConfig(active_probe_interval_seconds=5)
