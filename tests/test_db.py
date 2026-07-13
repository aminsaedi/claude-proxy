from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from claude_proxy import db


@pytest.fixture
def dbpath(tmp_path: Path) -> Path:
    p = tmp_path / "t.db"
    db.init_schema(p)
    return p


def test_token_crud_and_default(dbpath):
    db.add_token("x", "sk-x", is_default=True, path=dbpath)
    db.add_token("y", "sk-y", path=dbpath)
    toks = db.list_tokens(path=dbpath)
    assert [t["name"] for t in toks] == ["x", "y"]  # insertion order preserved
    assert toks[0]["default"] is True and toks[1]["default"] is False
    db.set_default_token("y", path=dbpath)
    toks = db.list_tokens(path=dbpath)
    assert {t["name"]: t["default"] for t in toks} == {"x": False, "y": True}
    db.delete_token("x", path=dbpath)
    assert [t["name"] for t in db.list_tokens(path=dbpath)] == ["y"]


def test_virtual_key_unique(dbpath):
    db.add_virtual_key("alice", "vk-1", path=dbpath)
    with pytest.raises(sqlite3.IntegrityError):  # UNIQUE on key
        db.add_virtual_key("bob", "vk-1", path=dbpath)
    db.delete_virtual_key("alice", path=dbpath)
    assert db.list_virtual_keys(path=dbpath) == []


def test_config_roundtrip(dbpath):
    assert db.get_config_json(path=dbpath) is None
    db.set_config_json({"a": 1, "nested": {"b": 2}}, path=dbpath)
    assert db.get_config_json(path=dbpath) == {"a": 1, "nested": {"b": 2}}
    db.set_config_json({"a": 9}, path=dbpath)  # single-row upsert
    assert db.get_config_json(path=dbpath) == {"a": 9}


def test_usage_replace_and_load(dbpath):
    stats = {"alice": {"claude-opus-4-8": {
        "input_tokens": 10, "output_tokens": 20,
        "cache_read_input_tokens": 5, "cache_creation_input_tokens": 1, "requests": 2,
    }}}
    db.replace_usage(stats, path=dbpath)
    assert db.load_usage(path=dbpath) == stats
    db.replace_usage({}, path=dbpath)
    assert db.load_usage(path=dbpath) == {}
