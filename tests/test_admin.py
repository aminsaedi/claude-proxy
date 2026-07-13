"""Admin CRUD + rotate endpoints, exercised through the ASGI app.

These run against the shared seeded test DB (see conftest: tokens a/b, keys
alice/bob), so every test operates on throwaway entities and restores the DB in
a ``finally`` block — the proxy/rotation tests depend on alice/bob being intact.
"""

from __future__ import annotations

import asyncio
import json

import httpx
import uvicorn

from claude_proxy import db
from claude_proxy.admin_app import build_admin_app
from claude_proxy.app import AppState


def _asgi(app):
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://admin")


async def _read_event(lines) -> tuple[str | None, str]:
    """Pull one SSE frame (event:/data: lines up to the blank separator)."""
    ev, data = None, []
    async for raw in lines:
        line = raw.rstrip("\r")
        if line == "":
            if ev is not None:
                return ev, "\n".join(data)
            continue
        if line.startswith("event:"):
            ev = line[len("event:"):].strip()
        elif line.startswith("data:"):
            data.append(line[len("data:"):].lstrip())
    return ev, "\n".join(data)


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


def test_appstate_pubsub_notify():
    """subscribe/notify/unsubscribe: notify() sets every live subscriber."""
    state = AppState()
    a = state.subscribe()
    b = state.subscribe()
    assert not a.is_set() and not b.is_set()
    state.notify()
    assert a.is_set() and b.is_set()
    state.unsubscribe(a)
    a.clear()
    b.clear()
    state.notify()
    assert b.is_set() and not a.is_set()  # unsubscribed one is no longer nudged
    state.unsubscribe(b)


async def test_events_initial_state_and_push_on_mutation():
    """The SSE stream sends current state on connect, then pushes on mutation.

    httpx's ASGITransport buffers infinite responses, so SSE must be exercised
    against a real server on an ephemeral port.
    """
    state = AppState()
    app = build_admin_app(state)
    cfg = uvicorn.Config(app, host="127.0.0.1", port=0, log_level="error", lifespan="off")
    server = uvicorn.Server(cfg)
    server.install_signal_handlers = lambda: None  # type: ignore[method-assign]
    serve = asyncio.create_task(server.serve())
    try:
        while not server.started:  # noqa: ASYNC110 - poll uvicorn's bind flag
            await asyncio.sleep(0.02)
        port = server.servers[0].sockets[0].getsockname()[1]
        async with httpx.AsyncClient(base_url=f"http://127.0.0.1:{port}") as ac:
            async with ac.stream("GET", "/events") as r:
                assert r.status_code == 200
                assert r.headers["content-type"].startswith("text/event-stream")
                lines = r.aiter_lines()
                # first frame is the current state
                ev, data = await asyncio.wait_for(_read_event(lines), timeout=5)
                assert ev == "state"
                assert "alice" in {v["name"] for v in json.loads(data)["virtual_keys"]}
                # a mutation must produce a fresh state push (via notify)
                assert (await ac.post("/virtual-keys", json={"name": "zsse"})).status_code == 200
                found = False
                for _ in range(8):  # tolerate a heartbeat/unchanged frame or two
                    ev, data = await asyncio.wait_for(_read_event(lines), timeout=5)
                    if ev == "state" and "zsse" in {v["name"] for v in json.loads(data)["virtual_keys"]}:
                        found = True
                        break
                assert found, "expected a state push carrying the new key"
    finally:
        db.delete_virtual_key("zsse")
        server.should_exit = True
        await asyncio.wait_for(serve, timeout=5)
