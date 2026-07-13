"""The admin app: dark dashboard + token control, bound to Tailscale only.

Optional HTTP Basic auth protects the dashboard and all mutating endpoints when
``ADMIN_PASSWORD`` is set (``/metrics`` stays open so Prometheus can scrape).
"""

from __future__ import annotations

import asyncio
import logging
import os
import secrets
import sqlite3

from fastapi import Depends, FastAPI, HTTPException, Request, status
from fastapi.responses import HTMLResponse, JSONResponse, Response
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from prometheus_client import CONTENT_TYPE_LATEST, REGISTRY, generate_latest
from pydantic import ValidationError

from . import db, health, metrics
from .config import save_config
from .models import AppConfig
from .paths import STATIC_DIR
from .stores import TokenHealth

log = logging.getLogger("claude_proxy.admin")


def _gen_vk() -> str:
    """Generate a fresh downstream virtual key (matches manage.py)."""
    return "vk-" + secrets.token_urlsafe(24)

_ADMIN_USER = os.environ.get("ADMIN_USER", "admin")
_ADMIN_PASSWORD = os.environ.get("ADMIN_PASSWORD", "")
_basic = HTTPBasic(auto_error=True)


def admin_auth_enabled() -> bool:
    return bool(_ADMIN_PASSWORD)


async def _require_auth(credentials: HTTPBasicCredentials = Depends(_basic)) -> None:
    ok_user = secrets.compare_digest(credentials.username, _ADMIN_USER)
    ok_pass = secrets.compare_digest(credentials.password, _ADMIN_PASSWORD)
    if not (ok_user and ok_pass):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid admin credentials",
            headers={"WWW-Authenticate": "Basic"},
        )


def build_admin_app(state) -> FastAPI:  # noqa: ANN001
    app = FastAPI(title="claude-proxy admin")
    protected = [Depends(_require_auth)] if admin_auth_enabled() else []

    @app.get("/", response_class=HTMLResponse, dependencies=protected)
    async def index() -> str:
        return (STATIC_DIR / "admin.html").read_text()

    @app.get("/state", dependencies=protected)
    async def get_state() -> JSONResponse:
        usage = await state.usage.snapshot()
        return JSONResponse({
            "tokens": state.tokens.names(),
            "active": state.tokens.active,
            "default_token": state.tokens.default,
            "token_previews": {n: state.tokens.preview(n) for n in state.tokens.names()},
            "headers": {n: state.tokens.health[n].headers for n in state.tokens.names()},
            "health": state.tokens.state_payload(),
            "virtual_keys": [
                {"name": n, "preview": state.vkeys.preview(n), "usage": usage.get(n, {})}
                for n in state.vkeys.names()
            ],
            "config": state.config.model_dump(),
            "rotation_log": state.rotation_log,
            "auth_enabled": admin_auth_enabled(),
        })

    # --- upstream tokens: CRUD -------------------------------------------
    @app.post("/tokens", dependencies=protected)
    async def create_token(request: Request) -> JSONResponse:
        body = await request.json()
        name = (body.get("name") or "").strip()
        token = (body.get("token") or "").strip()
        if not name or not token:
            return JSONResponse(status_code=400, content={"error": "name and token are required"})
        if name in state.tokens:
            return JSONResponse(status_code=409, content={"error": f"Token {name!r} already exists"})
        await asyncio.to_thread(db.add_token, name, token, bool(body.get("default")))
        state.tokens.reload()
        metrics.TOKEN_HEALTHY.labels(token_name=name).set(1)
        log.info("Token added via admin API: %s", name)
        return JSONResponse({"ok": True, "name": name})

    @app.patch("/tokens/{name}", dependencies=protected)
    async def update_token(name: str, request: Request) -> JSONResponse:
        if name not in state.tokens:
            return JSONResponse(status_code=404, content={"error": f"Unknown token: {name!r}"})
        body = await request.json()
        token = body.get("token")
        if token is not None:
            token = token.strip()
            if not token:
                return JSONResponse(status_code=400, content={"error": "token cannot be empty"})
        make_default = True if body.get("default") else None
        await asyncio.to_thread(db.update_token, name, token, make_default)
        state.tokens.reload()
        if token is not None:  # secret changed — old health/rate-limit no longer applies
            state.tokens.health[name] = TokenHealth()
        log.info("Token updated via admin API: %s", name)
        return JSONResponse({"ok": True, "name": name, "default": state.tokens.default})

    @app.delete("/tokens/{name}", dependencies=protected)
    async def delete_token(name: str) -> JSONResponse:
        if name not in state.tokens:
            return JSONResponse(status_code=404, content={"error": f"Unknown token: {name!r}"})
        if len(state.tokens.names()) <= 1:
            return JSONResponse(status_code=400, content={"error": "Can't delete the last upstream token"})
        await asyncio.to_thread(db.delete_token, name)
        state.tokens.reload()
        log.info("Token deleted via admin API: %s", name)
        return JSONResponse({"ok": True, "active": state.tokens.active})

    @app.post("/tokens/{name}/reveal", dependencies=protected)
    async def reveal_token(name: str) -> JSONResponse:
        if name not in state.tokens:
            return JSONResponse(status_code=404, content={"error": f"Unknown token: {name!r}"})
        return JSONResponse({"name": name, "token": state.tokens.secret(name)})

    # --- downstream virtual keys: CRUD + rotate --------------------------
    @app.post("/virtual-keys", dependencies=protected)
    async def create_vkey(request: Request) -> JSONResponse:
        body = await request.json()
        name = (body.get("name") or "").strip()
        key = (body.get("key") or "").strip() or _gen_vk()
        if not name:
            return JSONResponse(status_code=400, content={"error": "name is required"})
        if name in state.vkeys:
            return JSONResponse(status_code=409, content={"error": f"Key {name!r} already exists"})
        try:
            await asyncio.to_thread(db.add_virtual_key, name, key)
        except sqlite3.IntegrityError:
            return JSONResponse(status_code=409, content={"error": "That key value is already in use"})
        state.vkeys.reload_if_changed()
        log.info("Virtual key added via admin API: %s", name)
        return JSONResponse({"ok": True, "name": name, "key": key})

    @app.patch("/virtual-keys/{name}", dependencies=protected)
    async def rename_vkey(name: str, request: Request) -> JSONResponse:
        if name not in state.vkeys:
            return JSONResponse(status_code=404, content={"error": f"Unknown key: {name!r}"})
        new = ((await request.json()).get("name") or "").strip()
        if not new:
            return JSONResponse(status_code=400, content={"error": "new name is required"})
        if new == name:
            return JSONResponse({"ok": True, "name": name})
        if new in state.vkeys:
            return JSONResponse(status_code=409, content={"error": f"Key {new!r} already exists"})
        try:
            await asyncio.to_thread(db.rename_virtual_key, name, new)
        except sqlite3.IntegrityError:
            return JSONResponse(status_code=409, content={"error": f"Key {new!r} already exists"})
        state.vkeys.reload_if_changed()
        log.info("Virtual key renamed via admin API: %s -> %s", name, new)
        return JSONResponse({"ok": True, "name": new})

    @app.delete("/virtual-keys/{name}", dependencies=protected)
    async def delete_vkey(name: str) -> JSONResponse:
        if name not in state.vkeys:
            return JSONResponse(status_code=404, content={"error": f"Unknown key: {name!r}"})
        if len(state.vkeys.names()) <= 1:
            return JSONResponse(status_code=400, content={"error": "Can't delete the last virtual key"})
        await asyncio.to_thread(db.delete_virtual_key, name)
        state.vkeys.reload_if_changed()
        log.info("Virtual key deleted via admin API: %s", name)
        return JSONResponse({"ok": True})

    @app.post("/virtual-keys/{name}/rotate", dependencies=protected)
    async def rotate_vkey(name: str) -> JSONResponse:
        if name not in state.vkeys:
            return JSONResponse(status_code=404, content={"error": f"Unknown key: {name!r}"})
        new_key = _gen_vk()
        await asyncio.to_thread(db.set_virtual_key, name, new_key)
        state.vkeys.reload_if_changed()
        log.info("Virtual key rotated via admin API: %s", name)
        return JSONResponse({"ok": True, "name": name, "key": new_key})

    @app.post("/virtual-keys/{name}/reveal", dependencies=protected)
    async def reveal_vkey(name: str) -> JSONResponse:
        if name not in state.vkeys:
            return JSONResponse(status_code=404, content={"error": f"Unknown key: {name!r}"})
        return JSONResponse({"name": name, "key": state.vkeys.secret(name)})

    @app.post("/select", dependencies=protected)
    async def select(request: Request) -> JSONResponse:
        name = (await request.json()).get("name", "")
        if name not in state.tokens:
            return JSONResponse(status_code=400, content={"error": f"Unknown token: {name!r}"})
        state.tokens.set_active(name)
        log.info("Token switched to: %s", name)
        return JSONResponse({"active": state.tokens.active})

    @app.post("/probe", dependencies=protected)
    async def probe_token(request: Request) -> JSONResponse:
        name = (await request.json()).get("name", "")
        if name not in state.tokens:
            return JSONResponse(status_code=400, content={"error": f"Unknown token: {name!r}"})
        ok = await health.probe(state.tokens, state.probe_client, name)
        h = state.tokens.health[name]
        return JSONResponse({"healthy": ok, "status": h.status, "error_count": h.error_count})

    @app.get("/config", dependencies=protected)
    async def get_config() -> JSONResponse:
        return JSONResponse(state.config.model_dump())

    @app.post("/config", dependencies=protected)
    async def set_config(request: Request) -> JSONResponse:
        body = await request.json()
        merged = state.config.model_dump()
        ar = body.get("auto_rotation")
        if isinstance(ar, dict):
            merged["auto_rotation"].update(ar)
        for k in ("health_probe_interval_seconds", "active_probe_interval_seconds",
                  "upstream_timeout_seconds"):
            if k in body:
                merged[k] = body[k]
        try:
            cfg = AppConfig.model_validate(merged)
        except ValidationError as e:
            msg = "; ".join(f"{'.'.join(map(str, err['loc']))}: {err['msg']}" for err in e.errors())
            return JSONResponse(status_code=400, content={"error": msg})
        state.config = cfg
        save_config(cfg)
        log.info("Config updated via admin API")
        return JSONResponse({"ok": True, "config": cfg.model_dump()})

    @app.get("/metrics")
    async def metrics_endpoint() -> Response:
        return Response(generate_latest(REGISTRY), media_type=CONTENT_TYPE_LATEST)

    return app
