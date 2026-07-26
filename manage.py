#!/usr/bin/env python3
"""Claude Proxy Manager — interactive TUI for keys, tokens, and settings.

Run inside the container:  docker compose exec -it proxy python manage.py
CRUD goes straight to the SQLite DB; live actions (switch token, probe, save
config) go through the admin API so the running proxy picks them up immediately.
Virtual-key changes are hot-reloaded by the proxy within ~5s; token changes
need a restart.
"""

from __future__ import annotations

import base64
import json
import os
import secrets
import signal
import sys
import urllib.error
import urllib.request
from pathlib import Path

# Allow running from a raw checkout (no install): put src/ on the path.
sys.path.insert(0, str(Path(__file__).parent / "src"))

from rich.console import Console  # noqa: E402
from rich.panel import Panel  # noqa: E402
from rich.table import Table  # noqa: E402

from claude_proxy import db  # noqa: E402

ADMIN_PORT = int(os.environ.get("ADMIN_PORT", "8090"))
ADMIN_URL = f"http://localhost:{ADMIN_PORT}"
_ADMIN_USER = os.environ.get("ADMIN_USER", "admin")
_ADMIN_PASSWORD = os.environ.get("ADMIN_PASSWORD", "")

console = Console()
db.init_schema()


# --- admin API helpers -----------------------------------------------------

def _headers(extra: dict | None = None) -> dict:
    h = dict(extra or {})
    if _ADMIN_PASSWORD:
        tok = base64.b64encode(f"{_ADMIN_USER}:{_ADMIN_PASSWORD}".encode()).decode()
        h["Authorization"] = f"Basic {tok}"
    return h


def _get(path: str, timeout: float = 2.0):
    try:
        req = urllib.request.Request(f"{ADMIN_URL}{path}", headers=_headers())
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read())
    except Exception:
        return None


def _post(path: str, payload: dict, timeout: float = 5.0):
    try:
        req = urllib.request.Request(
            f"{ADMIN_URL}{path}", data=json.dumps(payload).encode(),
            headers=_headers({"Content-Type": "application/json"}), method="POST",
        )
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read())
    except urllib.error.HTTPError as e:
        console.print(f"[red]API error {e.code}: {e.read().decode()[:200]}[/]")
    except Exception as e:
        console.print(f"[red]Could not reach admin API ({ADMIN_URL}): {e}[/]")
    return None


def mask(s: str, show: int = 12) -> str:
    return s[:show] + "…" if len(s) > show + 1 else s


def gen_key() -> str:
    return "vk-" + secrets.token_urlsafe(24)


# --- input helpers ---------------------------------------------------------

def ask(prompt: str, default: str = "") -> str:
    hint = f" [{default}]" if default else ""
    try:
        return console.input(f"[bold cyan]{prompt}{hint}:[/] ").strip() or default
    except (KeyboardInterrupt, EOFError):
        return default


def confirm(prompt: str) -> bool:
    try:
        return console.input(f"[bold yellow]{prompt} (y/N):[/] ").strip().lower() == "y"
    except (KeyboardInterrupt, EOFError):
        return False


def pick(prompt: str, options: list[str]) -> int | None:
    for i, opt in enumerate(options, 1):
        console.print(f"  [bold]{i}.[/] {opt}")
    console.print("  [dim]0. Back[/]")
    try:
        n = int(console.input(f"\n[bold cyan]{prompt}:[/] ").strip())
    except (ValueError, KeyboardInterrupt, EOFError):
        return None
    return n - 1 if 1 <= n <= len(options) else None


def header(title: str) -> None:
    console.clear(); console.print(); console.rule(f"[bold magenta]{title}[/]"); console.print()


def pause() -> None:
    try:
        console.input("\nPress Enter to continue…")
    except (KeyboardInterrupt, EOFError):
        pass


# --- virtual keys ----------------------------------------------------------

def menu_virtual_keys() -> None:
    while True:
        header("Virtual Keys")
        keys = db.list_virtual_keys()
        t = Table(box=None, padding=(0, 2), header_style="bold dim")
        t.add_column("#", style="dim", width=4)
        t.add_column("Name", style="bold")
        t.add_column("Key (masked)", style="green")
        for i, vk in enumerate(keys, 1):
            t.add_row(str(i), vk["name"], mask(vk["key"]))
        console.print(t if keys else "  [dim](no virtual keys)[/]")
        console.print()

        idx = pick("Action", ["Add key", "Reveal full key", "Rotate key", "Rename key", "Delete key"])
        if idx is None:
            return
        if idx == 0:
            name = ask("Name (e.g. alice)")
            if not name or any(k["name"] == name for k in keys):
                console.print("[red]Missing or duplicate name.[/]"); pause(); continue
            key = ask("Key (blank = auto-generate)") or gen_key()
            db.add_virtual_key(name, key)
            console.print(f"\n[green]Added '{name}'.[/]  [dim]{key}[/]")
            console.print("[dim](hot-reloaded within ~5s — no restart needed)[/]"); pause()
        elif idx == 1 and keys:
            n = pick("Reveal which key", [k["name"] for k in keys])
            if n is not None:
                console.print(f"\n[bold]{keys[n]['name']}[/]: [green]{keys[n]['key']}[/]"); pause()
        elif idx == 2 and keys:
            n = pick("Rotate which key", [k["name"] for k in keys])
            if n is not None and confirm(f"Rotate '{keys[n]['name']}'? The old key stops working."):
                new = gen_key()
                db.set_virtual_key(keys[n]["name"], new)
                console.print(f"\n[green]Rotated '{keys[n]['name']}'.[/]  New key: [green]{new}[/]")
                console.print("[dim](hot-reloaded within ~5s — update the client)[/]"); pause()
        elif idx == 3 and keys:
            n = pick("Rename which key", [k["name"] for k in keys])
            if n is not None:
                new = ask("New name")
                if not new or any(k["name"] == new for k in keys):
                    console.print("[red]Missing or duplicate name.[/]"); pause(); continue
                db.rename_virtual_key(keys[n]["name"], new)
                console.print(f"[green]Renamed '{keys[n]['name']}' → '{new}'.[/]"); pause()
        elif idx == 4 and keys:
            n = pick("Delete which key", [k["name"] for k in keys])
            if n is not None and confirm(f"Delete '{keys[n]['name']}'?"):
                db.delete_virtual_key(keys[n]["name"])
                console.print(f"[green]Deleted '{keys[n]['name']}'.[/]"); pause()


# --- spend limits ----------------------------------------------------------

_PERIODS = [("hour", "per hour"), ("day", "per day"), ("week", "per week"), ("month", "per month")]


def _spend_by_key() -> dict:
    """Live per-key limit status from the admin API, keyed by name (may be empty)."""
    state = _get("/state") or {}
    return {vk["name"]: vk for vk in state.get("virtual_keys", [])}


def menu_limits() -> None:
    """Spend caps. Written straight to the DB — the proxy re-reads them in ~5s."""
    while True:
        header("Spend Limits")
        keys = db.list_virtual_keys()
        limits = db.list_key_limits()
        live = _spend_by_key()
        cfg = _get("/config") or db.get_config_json() or {}
        tz = cfg.get("timezone", "America/Toronto")
        console.print(f"  [dim]Windows are calendar-aligned in [bold]{tz}[/].[/]\n")

        t = Table(box=None, padding=(0, 2), header_style="bold dim")
        t.add_column("#", style="dim", width=4)
        t.add_column("Name", style="bold")
        t.add_column("Caps", style="green")
        t.add_column("Today", justify="right")
        t.add_column("7 days", justify="right")
        for i, vk in enumerate(keys, 1):
            caps = limits.get(vk["name"], {})
            cap_txt = " + ".join(f"${v:g}/{p[0]}" for p, v in sorted(caps.items())) or "[dim]none[/]"
            over = [c for c in (live.get(vk["name"], {}).get("limits") or []) if c.get("over")]
            if over:
                cap_txt += f" [bold red]⛔ over {over[0]['period']}[/]"
            w = (live.get(vk["name"], {}).get("windows") or {})
            t.add_row(str(i), vk["name"], cap_txt,
                      f"${w.get('1d', {}).get('cost_usd', 0):.2f}" if w else "[dim]—[/]",
                      f"${w.get('7d', {}).get('cost_usd', 0):.2f}" if w else "[dim]—[/]")
        console.print(t if keys else "  [dim](no virtual keys)[/]")
        if not live:
            console.print("\n  [dim yellow]Admin API unreachable — spend figures unavailable[/]")
        console.print()

        if not keys:
            pause(); return
        n = pick("Edit caps for which key", [k["name"] for k in keys])
        if n is None:
            return
        name = keys[n]["name"]
        current = limits.get(name, {})
        console.print("\n[dim]Blank keeps the current value; 0 removes the cap.[/]")
        new: dict[str, float] = {}
        for period, label in _PERIODS:
            cur = current.get(period)
            raw = ask(f"  ${label}", "" if cur is None else f"{cur:g}")
            if raw == "":
                continue
            try:
                amount = float(raw.lstrip("$"))
            except ValueError:
                console.print(f"[red]'{raw}' is not a number — skipping {period}.[/]")
                continue
            if amount > 0:
                new[period] = amount
        db.set_key_limits(name, new)
        summary = " + ".join(f"${v:g} {label}" for p, label in _PERIODS
                             if (v := new.get(p)) is not None)
        console.print(f"\n[green]Saved:[/] {summary or 'no caps (unlimited)'}")
        console.print("[dim](picked up by the proxy within ~5s)[/]"); pause()


# --- tokens ----------------------------------------------------------------

def _health_cell(h: dict | None) -> str:
    if not h or h.get("last_checked", 0) == 0:
        return "[dim]unchecked[/]"
    status = h.get("status") or ("healthy" if h.get("healthy") else "unhealthy")
    return {
        "healthy": "[bold green]✓ healthy[/]",
        "rate_limited": "[bold yellow]⏳ rate-limited[/]",
    }.get(status, f"[bold red]✗ unhealthy ({h.get('error_count', 0)})[/]")


def menu_tokens() -> None:
    while True:
        header("Upstream Tokens")
        tokens = db.list_tokens()
        state = _get("/state")
        active = state["active"] if state else None
        health = state.get("health") if state else {}
        t = Table(box=None, padding=(0, 2), header_style="bold dim")
        t.add_column("#", style="dim", width=4)
        t.add_column("Name", style="bold")
        t.add_column("Token (masked)", style="green")
        t.add_column("Default", justify="center")
        t.add_column("Active", justify="center")
        t.add_column("Health", justify="center")
        for i, tk in enumerate(tokens, 1):
            t.add_row(str(i), tk["name"], mask(tk["token"]),
                      "✓" if tk.get("default") else "",
                      "[bold green]▶ live[/]" if tk["name"] == active else "",
                      _health_cell((health or {}).get(tk["name"])))
        console.print(t if tokens else "  [dim](no tokens)[/]")
        if active is None:
            console.print("  [dim yellow]Admin API unreachable — live state unknown[/]")
        console.print()

        actions = ["Select active (live switch)", "Run health probe", "Add token",
                   "Reveal full token", "Set default (on restart)", "Delete token"]
        idx = pick("Action", actions)
        if idx is None:
            return
        if idx == 0 and tokens:
            n = pick("Switch live traffic to", [tk["name"] for tk in tokens])
            if n is not None and _post("/select", {"name": tokens[n]["name"]}):
                console.print(f"[green]Switched to '{tokens[n]['name']}'.[/]"); pause()
        elif idx == 1 and tokens:
            n = pick("Probe which token", [tk["name"] for tk in tokens])
            if n is not None:
                console.print("[dim]Probing (up to 30s)…[/]")
                r = _post("/probe", {"name": tokens[n]["name"]}, timeout=35)
                if r:
                    color = "green" if r["healthy"] else "red"
                    console.print(f"[bold {color}]{tokens[n]['name']}: {r.get('status')}[/]")
                pause()
        elif idx == 2:
            name = ask("Name (e.g. personal)")
            if not name or any(tk["name"] == name for tk in tokens):
                console.print("[red]Missing or duplicate name.[/]"); pause(); continue
            token = ask("Token (sk-ant-oat-...)")
            if not token:
                console.print("[red]Token required.[/]"); pause(); continue
            db.add_token(name, token, is_default=confirm("Set as default?"))
            console.print(f"[green]Added '{name}'. Restart to load it.[/]"); pause()
        elif idx == 3 and tokens:
            n = pick("Reveal which token", [tk["name"] for tk in tokens])
            if n is not None:
                console.print(f"\n[bold]{tokens[n]['name']}[/]: [green]{tokens[n]['token']}[/]"); pause()
        elif idx == 4 and tokens:
            n = pick("Set default (used on next restart)", [tk["name"] for tk in tokens])
            if n is not None:
                db.set_default_token(tokens[n]["name"])
                console.print(f"[green]'{tokens[n]['name']}' is now default.[/]"); pause()
        elif idx == 5 and tokens:
            n = pick("Delete which token", [tk["name"] for tk in tokens])
            if n is not None and confirm(f"Delete '{tokens[n]['name']}'?"):
                db.delete_token(tokens[n]["name"])
                console.print(f"[green]Deleted '{tokens[n]['name']}'. Restart to apply.[/]"); pause()


# --- settings --------------------------------------------------------------

_SCHEMA = [
    ("auto_rotation.enabled", "Auto-rotation enabled", "bool"),
    ("auto_rotation.threshold_5h", "Threshold (5h)", "float"),
    ("auto_rotation.target_max_util_5h", "Target max util (5h)", "float"),
    ("auto_rotation.check_interval_seconds", "Check interval (s)", "int"),
    ("auto_rotation.probe_before_switch", "Probe before switch", "bool"),
    ("auto_rotation.cooldown_seconds", "Cooldown (s)", "int"),
    ("auto_rotation.notify_only", "Notify only", "bool"),
    ("health_probe_interval_seconds", "Health probe interval (s)", "int"),
    ("active_probe_interval_seconds", "Active probe interval (s)", "int"),
    ("upstream_timeout_seconds", "Upstream timeout (s, restart)", "int"),
    ("timezone", "Timezone (spend windows)", "str"),
    ("pricing.online", "Fetch prices online", "bool"),
    ("pricing.refresh_hours", "Price refresh (h)", "int"),
    ("usage_retention_days", "Usage history (days)", "int"),
]


def _cfg_get(cfg: dict, path: str):
    v = cfg
    for p in path.split("."):
        v = v.get(p) if isinstance(v, dict) else None
    return v


def _cfg_set(cfg: dict, path: str, val) -> None:
    parts = path.split(".")
    d = cfg
    for p in parts[:-1]:
        d = d.setdefault(p, {})
    d[parts[-1]] = val


def menu_settings() -> None:
    while True:
        header("Settings")
        cfg = _get("/config")
        via_api = cfg is not None
        if cfg is None:
            cfg = db.get_config_json() or {}
            console.print("  [dim yellow]Admin API unavailable — editing DB directly[/]\n")
        t = Table(box=None, padding=(0, 2), header_style="bold dim")
        t.add_column("#", style="dim", width=4)
        t.add_column("Setting", style="bold")
        t.add_column("Value", justify="right")
        for i, (path, label, typ) in enumerate(_SCHEMA, 1):
            v = _cfg_get(cfg, path)
            shown = ("ON" if v else "OFF") if typ == "bool" else str(v)
            t.add_row(str(i), label, shown)
        console.print(t)
        console.print()
        idx = pick("Edit which setting", [s[1] for s in _SCHEMA])
        if idx is None:
            return
        path, label, typ = _SCHEMA[idx]
        cur = _cfg_get(cfg, path)
        if typ == "bool":
            new = not cur
        else:
            raw = ask(f"New value for {label} (current: {cur})")
            if not raw:
                continue
            if typ == "str":
                new = raw
            else:
                try:
                    new = float(raw) if typ == "float" else int(raw)
                except ValueError:
                    console.print("[red]Invalid number.[/]"); pause(); continue
        _cfg_set(cfg, path, new)
        if via_api and _post("/config", cfg):
            console.print("[green]Saved (live).[/]")
        else:
            db.set_config_json(cfg)
            console.print("[yellow]Saved to DB (restart to apply).[/]")
        pause()


# --- restart ---------------------------------------------------------------

def do_restart() -> None:
    console.print("\n[yellow]Sends SIGTERM to PID 1; the orchestrator restarts the pod/container.[/]\n")
    if confirm("Restart the proxy now?"):
        try:
            os.kill(1, signal.SIGTERM)
        except Exception as e:
            console.print(f"[red]Failed: {e}[/]"); pause()


def main() -> None:
    while True:
        console.clear()
        console.print(Panel("[bold white]Claude Proxy Manager[/]",
                            border_style="magenta", padding=(1, 4)))
        console.print()
        idx = pick("Choose", ["Virtual Keys", "Spend Limits", "Upstream Tokens", "Settings",
                              "Restart Proxy", "Quit"])
        if idx is None or idx == 5:
            console.clear(); console.print("[dim]Bye.[/]"); sys.exit(0)
        (menu_virtual_keys, menu_limits, menu_tokens, menu_settings, do_restart)[idx]()


if __name__ == "__main__":
    main()
