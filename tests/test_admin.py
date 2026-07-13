"""Admin CRUD + rotate endpoints, exercised through the ASGI app.

These run against the shared seeded test DB (see conftest: tokens a/b, keys
alice/bob), so every test operates on throwaway entities and restores the DB in
a ``finally`` block — the proxy/rotation tests depend on alice/bob being intact.
"""

from __future__ import annotations

import httpx

from claude_proxy import db
from claude_proxy.admin_app import build_admin_app
from claude_proxy.app import AppState


def _asgi(app):
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://admin")


async def test_token_crud_reveal_and_default():
    state = AppState()
    app = build_admin_app(state)
    async with _asgi(app) as ac:
        try:
            assert (await ac.post("/tokens", json={"name": "ztok", "token": "sk-z"})).status_code == 200
            # live store picked it up without a restart
            assert "ztok" in state.tokens.names()
            # duplicate name rejected
            assert (await ac.post("/tokens", json={"name": "ztok", "token": "sk-q"})).status_code == 409
            # missing fields rejected
            assert (await ac.post("/tokens", json={"name": "  "})).status_code == 400
            # surfaced in /state (masked preview), full value via reveal
            body = (await ac.get("/state")).json()
            assert "ztok" in body["tokens"] and body["token_previews"]["ztok"].startswith("sk-z")
            assert (await ac.post("/tokens/ztok/reveal")).json()["token"] == "sk-z"
            # rotate the secret + promote to default; health resets so it re-probes
            r = await ac.patch("/tokens/ztok", json={"token": "sk-z2", "default": True})
            assert r.status_code == 200 and r.json()["default"] == "ztok"
            assert (await ac.post("/tokens/ztok/reveal")).json()["token"] == "sk-z2"
            assert state.tokens.health["ztok"].status == "unchecked"
            # unknown token 404s on patch/delete/reveal
            assert (await ac.patch("/tokens/ghost", json={"token": "x"})).status_code == 404
            assert (await ac.delete("/tokens/ghost")).status_code == 404
            # delete removes it from the live store
            assert (await ac.delete("/tokens/ztok")).status_code == 200
            assert "ztok" not in state.tokens.names()
        finally:
            db.delete_token("ztok")
            db.set_default_token("a")


async def test_token_delete_last_is_blocked():
    state = AppState()
    app = build_admin_app(state)
    async with _asgi(app) as ac:
        # a and b are both seeded, so deleting one is fine but never the last
        try:
            assert (await ac.delete("/tokens/b")).status_code == 200
            r = await ac.delete("/tokens/a")
            assert r.status_code == 400 and "last" in r.json()["error"].lower()
        finally:
            if "b" not in {t["name"] for t in db.list_tokens()}:
                db.add_token("b", "sk-b")


async def test_vkey_crud_rotate_rename_reveal():
    state = AppState()
    app = build_admin_app(state)
    async with _asgi(app) as ac:
        try:
            # create with auto-generated value
            r = await ac.post("/virtual-keys", json={"name": "zclient"})
            assert r.status_code == 200
            key1 = r.json()["key"]
            assert key1.startswith("vk-") and state.vkeys.resolve(key1) == "zclient"
            # duplicate name + missing name
            assert (await ac.post("/virtual-keys", json={"name": "zclient"})).status_code == 409
            assert (await ac.post("/virtual-keys", json={"name": ""})).status_code == 400
            # reveal returns the exact value
            assert (await ac.post("/virtual-keys/zclient/reveal")).json()["key"] == key1
            # rotate: new value resolves, old value is dead immediately
            key2 = (await ac.post("/virtual-keys/zclient/rotate")).json()["key"]
            assert key2 != key1
            assert state.vkeys.resolve(key2) == "zclient"
            assert state.vkeys.resolve(key1) is None
            # rename: new name live, old gone
            assert (await ac.patch("/virtual-keys/zclient", json={"name": "zclient2"})).status_code == 200
            assert "zclient2" in state.vkeys.names() and "zclient" not in state.vkeys.names()
            # rename onto an existing name is rejected
            assert (await ac.patch("/virtual-keys/zclient2", json={"name": "alice"})).status_code == 409
            # unknown key 404s
            assert (await ac.post("/virtual-keys/ghost/rotate")).status_code == 404
            assert (await ac.delete("/virtual-keys/ghost")).status_code == 404
            # delete
            assert (await ac.delete("/virtual-keys/zclient2")).status_code == 200
            assert "zclient2" not in state.vkeys.names()
        finally:
            for n in ("zclient", "zclient2"):
                db.delete_virtual_key(n)


async def test_vkey_delete_last_is_blocked():
    state = AppState()
    app = build_admin_app(state)
    async with _asgi(app) as ac:
        try:
            assert (await ac.delete("/virtual-keys/bob")).status_code == 200
            r = await ac.delete("/virtual-keys/alice")
            assert r.status_code == 400 and "last" in r.json()["error"].lower()
        finally:
            if "bob" not in {v["name"] for v in db.list_virtual_keys()}:
                db.add_virtual_key("bob", "vk-bob")
