"""The admin app: dark dashboard + token control, bound to Tailscale only.

Optional HTTP Basic auth protects the dashboard and all mutating endpoints when
``ADMIN_PASSWORD`` is set (``/metrics`` stays open so Prometheus can scrape).
"""

from __future__ import annotations

import logging
import os
import secrets

from fastapi import Depends, FastAPI, HTTPException, Request, status
from fastapi.responses import HTMLResponse, JSONResponse, Response
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from prometheus_client import CONTENT_TYPE_LATEST, REGISTRY, generate_latest
from pydantic import ValidationError

from . import health
from .config import save_config
from .models import AppConfig
from .paths import STATIC_DIR

log = logging.getLogger("claude_proxy.admin")

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
            "headers": {n: state.tokens.health[n].headers for n in state.tokens.names()},
            "health": state.tokens.state_payload(),
            "virtual_keys": [
                {"name": n, "usage": usage.get(n, {})} for n in state.vkeys.names()
            ],
            "config": state.config.model_dump(),
            "rotation_log": state.rotation_log,
            "auth_enabled": admin_auth_enabled(),
        })

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
