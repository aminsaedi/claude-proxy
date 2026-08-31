"""The admin app: dark dashboard + token control, bound to Tailscale only.

Optional HTTP Basic auth protects the dashboard and all mutating endpoints when
``ADMIN_PASSWORD`` is set (``/metrics`` stays open so Prometheus can scrape).
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import secrets
import sqlite3
import time
from collections.abc import AsyncIterator

from fastapi import Depends, FastAPI, HTTPException, Request, status
from fastapi.responses import HTMLResponse, JSONResponse, Response, StreamingResponse
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from prometheus_client import CONTENT_TYPE_LATEST, REGISTRY, generate_latest
from pydantic import ValidationError

from . import __version__, budgets, db, health, metrics
from .config import save_config
from .models import AppConfig
from .paths import STATIC_DIR
from .stores import TokenHealth

log = logging.getLogger("claude_proxy.admin")


def _gen_vk() -> str:
    """Generate a fresh downstream virtual key (matches manage.py)."""
    return "vk-" + secrets.token_urlsafe(24)


# Seconds the SSE stream waits for a mutation nudge before recomputing on its
# own — bounds how long background changes (health probes, usage) take to reach
# a viewer. Also the heartbeat cadence that keeps the connection warm.
_SSE_HEARTBEAT = 3.0


# Rolling windows shown per client, as whole local calendar days back from and
# including today. "1d" is therefore today so far — which is also what the
# `day` spend limit measures, so the two numbers agree.
_WINDOW_DAYS = (("1d", 1), ("3d", 3), ("7d", 7), ("30d", 30))
_DAILY_SERIES_DAYS = 14


def _client_payload(state, name: str, lifetime: dict, now: float) -> dict:  # noqa: ANN001
    """Per-client usage: lifetime, calendar windows, a daily series, and caps."""
    tz = state.tz
    windows = {}
    for label, days in _WINDOW_DAYS:
        start = budgets.day_start(now, tz, days - 1)
        windows[label] = {
            **state.usage.totals_between(name, start, now + 1),
            "start": start,
            "models": state.usage.models_between(name, start, now + 1),
        }
    return {
        "name": name,
        "preview": state.vkeys.preview(name),
        "usage": lifetime,
        "windows": windows,
        "daily": state.usage.daily_series(name, tz, _DAILY_SERIES_DAYS, now),
        "limits": [s.payload() for s in state.limit_status(name, now)],
    }


async def _snapshot(state) -> dict:  # noqa: ANN001
    """The full dashboard state — shared by GET /state and the SSE stream.

    Deliberately carries no wall-clock field: the SSE stream only pushes when
    the snapshot hash changes, and a per-second timestamp would make every idle
    tick look like news. The console counts down against its own clock.
    """
    usage = await state.usage.snapshot()
    now = time.time()
    return {
        "tokens": state.tokens.names(),
        "active": state.tokens.active,
        "default_token": state.tokens.default,
        "token_previews": {n: state.tokens.preview(n) for n in state.tokens.names()},
        "headers": {n: state.tokens.health[n].headers for n in state.tokens.names()},
        "health": state.tokens.state_payload(),
        "virtual_keys": [
            _client_payload(state, n, usage.get(n, {}), now) for n in state.vkeys.names()
        ],
        "config": state.config.model_dump(),
        "rotation_log": state.rotation_log,
        "auth_enabled": admin_auth_enabled(),
        "pricing": state.pricing.status(),
        "timezone": state.config.timezone,
        # Live counters are read straight off the in-process object; the
        # storage figures come from the cached background reading so a frame
        # never opens the audit database.
        "audit": {
            **state.audit_stats,
            "mode": state.audit.mode,
            "queued": state.audit.depth(),
            "dropped": state.audit.dropped,
            "written": state.audit.written,
            "retention_days": state.audit.retention_days,
            "max_bytes": state.audit.max_bytes,
        },
        "started_at": state.started_at,
        "version": __version__,
    }


# Assets the console is allowed to fetch, with their content types. An explicit
# map rather than a directory listing: `name` comes from the URL, and this makes
# path traversal impossible by construction instead of by sanitising.
_ASSETS = {
    "console.css": "text/css; charset=utf-8",
    "console-dom.js": "application/javascript; charset=utf-8",
    "console.js": "application/javascript; charset=utf-8",
}

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
    async def index() -> HTMLResponse:
        # The shell itself is never cached, and it stamps the build version
        # into the asset URLs. That pairing is what lets the assets be cached
        # hard: a new version changes their URLs, so no browser can be left
        # running last release's JavaScript against this release's API.
        html = (STATIC_DIR / "console.html").read_text().replace("__BUILD__", __version__)
        return HTMLResponse(html, headers={"Cache-Control": "no-store"})

    @app.get("/assets/{name}", dependencies=protected)
    async def asset(name: str) -> Response:
        """Serve the console's CSS/JS.

        The console is split out of the HTML rather than inlined so it can be
        edited as real files and cached as real files. Assets carry the build
        version in their URL, so they can be cached hard and still turn over
        the instant a new version ships.
        """
        if name not in _ASSETS:
            raise HTTPException(status_code=404, detail="Not found")
        path = STATIC_DIR / name
        try:
            body = path.read_bytes()
        except OSError:
            raise HTTPException(status_code=404, detail="Not found") from None
        return Response(
            content=body,
            media_type=_ASSETS[name],
            headers={"Cache-Control": "public, max-age=604800, immutable"},
        )

    @app.get("/state", dependencies=protected)
    async def get_state() -> JSONResponse:
        return JSONResponse(await _snapshot(state))

    @app.get("/events", dependencies=protected)
    async def events(request: Request) -> StreamingResponse:
        """Live dashboard feed over Server-Sent Events.

        The browser opens one long-lived ``EventSource`` instead of polling. We
        push a ``state`` event only when the snapshot actually changes (compared
        by hash), so an idle dashboard never re-renders — that's what keeps
        scroll position, open panels, and in-progress forms from resetting.
        A ``ping`` event every few seconds tells the client the link is live,
        and ``state.notify()`` from a mutation wakes the loop for instant push.
        """

        async def gen() -> AsyncIterator[str]:
            ev = state.subscribe()
            last_hash: int | None = None
            try:
                # Prime the client with the current state immediately on connect.
                while True:
                    if await request.is_disconnected():
                        break
                    if state.draining:
                        # A long-lived stream would otherwise keep uvicorn's
                        # graceful shutdown waiting out its whole timeout. Tell
                        # the console to reconnect (it will land on the new pod)
                        # and let go.
                        yield "event: bye\ndata: draining\n\n"
                        break
                    ev.clear()  # clear before snapshotting so a concurrent
                    #             notify() during this pass isn't lost
                    snap = await _snapshot(state)
                    payload = json.dumps(snap, default=str, sort_keys=True)
                    digest = hash(payload)
                    if digest != last_hash:
                        last_hash = digest
                        yield f"event: state\ndata: {payload}\n\n"
                    else:
                        yield "event: ping\ndata: 1\n\n"
                    try:
                        await asyncio.wait_for(ev.wait(), timeout=_SSE_HEARTBEAT)
                    except TimeoutError:
                        pass
            except asyncio.CancelledError:  # client dropped / server shutdown
                raise
            finally:
                state.unsubscribe(ev)

        return StreamingResponse(
            gen(),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "Connection": "keep-alive",
                "X-Accel-Buffering": "no",  # defeat any proxy response buffering
            },
        )

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
        state.notify()
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
        state.notify()
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
        state.notify()
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
        state.notify()
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
        state.notify()
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
        state.notify()
        log.info("Virtual key deleted via admin API: %s", name)
        return JSONResponse({"ok": True})

    @app.post("/virtual-keys/{name}/rotate", dependencies=protected)
    async def rotate_vkey(name: str) -> JSONResponse:
        if name not in state.vkeys:
            return JSONResponse(status_code=404, content={"error": f"Unknown key: {name!r}"})
        new_key = _gen_vk()
        await asyncio.to_thread(db.set_virtual_key, name, new_key)
        state.vkeys.reload_if_changed()
        state.notify()
        log.info("Virtual key rotated via admin API: %s", name)
        return JSONResponse({"ok": True, "name": name, "key": new_key})

    @app.post("/virtual-keys/{name}/reveal", dependencies=protected)
    async def reveal_vkey(name: str) -> JSONResponse:
        if name not in state.vkeys:
            return JSONResponse(status_code=404, content={"error": f"Unknown key: {name!r}"})
        return JSONResponse({"name": name, "key": state.vkeys.secret(name)})

    # --- spend limits ----------------------------------------------------
    @app.get("/virtual-keys/{name}/limits", dependencies=protected)
    async def get_limits(name: str) -> JSONResponse:
        if name not in state.vkeys:
            return JSONResponse(status_code=404, content={"error": f"Unknown key: {name!r}"})
        return JSONResponse({
            "name": name,
            "limits": state.limits.for_key(name),
            "status": [s.payload() for s in state.limit_status(name)],
        })

    @app.put("/virtual-keys/{name}/limits", dependencies=protected)
    async def set_limits(name: str, request: Request) -> JSONResponse:
        """Replace *all* caps on a key at once.

        Replace-all rather than patch: the console edits the whole set in one
        dialog, and it makes "remove the hourly cap" a plain omission instead of
        a special delete call.
        """
        if name not in state.vkeys:
            return JSONResponse(status_code=404, content={"error": f"Unknown key: {name!r}"})
        body = await request.json()
        try:
            limits = budgets.parse_limits(body.get("limits", body))
        except ValueError as e:
            return JSONResponse(status_code=400, content={"error": str(e)})
        await asyncio.to_thread(state.limits.set, name, limits)
        state.notify()
        log.info("Spend limits for %s set to %s", name, limits or "none")
        return JSONResponse({
            "ok": True, "name": name, "limits": limits,
            "status": [s.payload() for s in state.limit_status(name)],
        })

    # --- pricing ---------------------------------------------------------
    @app.get("/pricing", dependencies=protected)
    async def get_pricing() -> JSONResponse:
        return JSONResponse(state.pricing.status())

    @app.post("/pricing/refresh", dependencies=protected)
    async def refresh_pricing() -> JSONResponse:
        ok = await state.pricing.refresh(state.probe_client)
        state.notify()
        status = state.pricing.status()
        if not ok:
            return JSONResponse(status_code=502,
                                content={"error": status["last_error"] or "refresh failed",
                                         **status})
        return JSONResponse({"ok": True, **status})

    # --- request / prompt audit ------------------------------------------
    @app.get("/requests", dependencies=protected)
    async def list_requests(request: Request) -> JSONResponse:
        """A page of recorded requests, newest first, without the bodies.

        Every query runs in a worker thread: the audit database is on the same
        disk as everything else, and a big scan must not stall the event loop
        that is simultaneously proxying traffic.
        """
        q = request.query_params
        try:
            rows = await asyncio.to_thread(
                state.audit.query,
                limit=int(q.get("limit", 100)),
                before_id=int(q["before_id"]) if q.get("before_id") else None,
                after_id=int(q["after_id"]) if q.get("after_id") else None,
                key_name=q.get("key") or None,
                model=q.get("model") or None,
                status=int(q["status"]) if q.get("status") else None,
                outcome=q.get("outcome") or None,
                since=float(q["since"]) if q.get("since") else None,
                search=q.get("q") or None,
                # One switch for "anything that did not reach the caller".
                # Spelling out the failure outcomes at each caller would
                # mean every future one has to be retro-fitted here.
                failed=q.get("failed") in ("1", "true", "yes"),
            )
        except (TypeError, ValueError) as e:
            return JSONResponse(status_code=400, content={"error": f"bad filter: {e}"})
        return JSONResponse({
            "requests": rows,
            # Cursor for the next page; absent once the last page is short.
            "next_before_id": rows[-1]["id"] if rows else None,
            "mode": state.audit.mode,
        })

    @app.get("/requests/{row_id}", dependencies=protected)
    async def get_request(row_id: int) -> JSONResponse:
        row = await asyncio.to_thread(state.audit.get, row_id)
        if row is None:
            return JSONResponse(status_code=404, content={"error": "No such request"})
        return JSONResponse(row)

    @app.get("/audit/stats", dependencies=protected)
    async def audit_stats() -> JSONResponse:
        stats = await asyncio.to_thread(state.audit.stats)
        state.audit_stats = stats
        return JSONResponse(stats)

    @app.get("/audit/overview", dependencies=protected)
    async def audit_overview(request: Request) -> JSONResponse:
        hours = float(request.query_params.get("hours", 24))
        since = time.time() - max(0.1, hours) * 3600
        return JSONResponse(await asyncio.to_thread(state.audit.overview, since))

    @app.get("/audit/models", dependencies=protected)
    async def audit_models(request: Request) -> JSONResponse:
        hours = float(request.query_params.get("hours", 24))
        since = time.time() - max(0.1, hours) * 3600
        rows = await asyncio.to_thread(state.audit.top_models, since)
        return JSONResponse({"models": rows, "since": since})

    @app.post("/audit/purge", dependencies=protected)
    async def audit_purge() -> JSONResponse:
        removed = await asyncio.to_thread(state.audit.purge)
        state.audit_stats = await asyncio.to_thread(state.audit.stats)
        state.notify()
        log.warning("Audit log purged via admin API (%d records removed)", removed)
        return JSONResponse({"ok": True, "removed": removed})

    @app.post("/audit/sweep", dependencies=protected)
    async def audit_sweep() -> JSONResponse:
        """Run retention now instead of waiting for the writer's timer."""
        result = await asyncio.to_thread(state.audit.sweep)
        state.audit_stats = await asyncio.to_thread(state.audit.stats)
        state.notify()
        return JSONResponse({"ok": True, **result})

    @app.post("/select", dependencies=protected)
    async def select(request: Request) -> JSONResponse:
        name = (await request.json()).get("name", "")
        if name not in state.tokens:
            return JSONResponse(status_code=400, content={"error": f"Unknown token: {name!r}"})
        state.tokens.set_active(name)
        state.notify()
        log.info("Token switched to: %s", name)
        return JSONResponse({"active": state.tokens.active})

    @app.post("/probe", dependencies=protected)
    async def probe_token(request: Request) -> JSONResponse:
        name = (await request.json()).get("name", "")
        if name not in state.tokens:
            return JSONResponse(status_code=400, content={"error": f"Unknown token: {name!r}"})
        ok = await health.probe(state.tokens, state.probe_client, name)
        h = state.tokens.health[name]
        state.notify()
        return JSONResponse({"healthy": ok, "status": h.status, "error_count": h.error_count})

    @app.get("/config", dependencies=protected)
    async def get_config() -> JSONResponse:
        return JSONResponse(state.config.model_dump())

    @app.post("/config", dependencies=protected)
    async def set_config(request: Request) -> JSONResponse:
        body = await request.json()
        merged = state.config.model_dump()
        for section in ("auto_rotation", "pricing", "audit"):
            sub = body.get(section)
            if isinstance(sub, dict):
                merged[section].update(sub)
        for k in ("health_probe_interval_seconds", "active_probe_interval_seconds",
                  "upstream_timeout_seconds", "sse_keepalive_seconds", "timezone",
                  "usage_retention_days"):
            if k in body:
                merged[k] = body[k]
        try:
            cfg = AppConfig.model_validate(merged)
        except ValidationError as e:
            msg = "; ".join(f"{'.'.join(map(str, err['loc']))}: {err['msg']}" for err in e.errors())
            return JSONResponse(status_code=400, content={"error": msg})
        prev_url = state.config.pricing.source_url
        state.config = cfg
        save_config(cfg)
        if cfg.pricing.source_url != prev_url:
            state.pricing.url = cfg.pricing.source_url
        state.apply_audit_config()  # mode / retention take effect immediately
        state.notify()
        log.info("Config updated via admin API")
        return JSONResponse({"ok": True, "config": cfg.model_dump()})

    @app.get("/metrics")
    async def metrics_endpoint() -> Response:
        # Window spend is a point-in-time reading of the in-memory series, so
        # it's computed on scrape rather than carried as a running counter.
        for name in state.vkeys.names():
            for s in state.limit_status(name):
                metrics.KEY_SPEND_USD.labels(key_name=name, period=s.period).set(s.spent_usd)
                metrics.KEY_LIMIT_USD.labels(key_name=name, period=s.period).set(s.limit_usd)
        return Response(generate_latest(REGISTRY), media_type=CONTENT_TYPE_LATEST)

    return app
