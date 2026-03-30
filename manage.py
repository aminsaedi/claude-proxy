#!/usr/bin/env python3
"""
Claude Proxy Manager — interactive menu
Run: docker compose exec -it proxy python manage.py
"""

import json
import os
import secrets
import signal
import sys
import urllib.error
import urllib.request
from pathlib import Path

import yaml
from rich.console import Console
from rich.panel import Panel
from rich.table import Table

BASE = Path(__file__).parent
VKEYS_FILE = BASE / "virtual_keys.yaml"
TOKENS_FILE = BASE / "tokens.yaml"
CONFIG_FILE = BASE / "config.yaml"
ADMIN_PORT = int(os.environ.get("ADMIN_PORT", "8090"))
ADMIN_URL = f"http://localhost:{ADMIN_PORT}"

console = Console()


# ---------------------------------------------------------------------------
# YAML helpers
# ---------------------------------------------------------------------------

def load_virtual_keys() -> list[dict]:
    if not VKEYS_FILE.exists():
        return []
    data = yaml.safe_load(VKEYS_FILE.read_text()) or {}
    return data.get("virtual_keys", [])


def save_virtual_keys(keys: list[dict]) -> None:
    VKEYS_FILE.write_text(
        yaml.dump({"virtual_keys": keys}, default_flow_style=False, allow_unicode=True)
    )


def load_tokens() -> list[dict]:
    if not TOKENS_FILE.exists():
        return []
    data = yaml.safe_load(TOKENS_FILE.read_text()) or {}
    return data.get("tokens", [])


def save_tokens(tokens: list[dict]) -> None:
    TOKENS_FILE.write_text(
        yaml.dump({"tokens": tokens}, default_flow_style=False, allow_unicode=True)
    )


def mask(s: str, show: int = 12) -> str:
    return s[:show] + "…" if len(s) > show + 1 else s


def gen_key() -> str:
    return "vk-" + secrets.token_urlsafe(24)


# ---------------------------------------------------------------------------
# Admin API helpers (talks to the running proxy)
# ---------------------------------------------------------------------------

def get_state() -> dict | None:
    """Return the full /state payload from the admin API, or None on error."""
    try:
        with urllib.request.urlopen(f"{ADMIN_URL}/state", timeout=2) as r:
            return json.loads(r.read())
    except Exception:
        return None


def get_active_token() -> str | None:
    """Return the name of the currently active upstream token, or None on error."""
    s = get_state()
    return s["active"] if s else None


def probe_token(name: str) -> dict | None:
    """Call POST /probe for the given token. Returns {healthy, error_count} or None."""
    try:
        data = json.dumps({"name": name}).encode()
        req = urllib.request.Request(
            f"{ADMIN_URL}/probe",
            data=data,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=35) as r:
            return json.loads(r.read())
    except urllib.error.HTTPError as e:
        console.print(f"[red]API error {e.code}: {e.read().decode()}[/]")
    except Exception as e:
        console.print(f"[red]Could not reach admin API ({ADMIN_URL}): {e}[/]")
    return None


def get_config() -> dict | None:
    """Return the current config from the admin API, or None on error."""
    try:
        with urllib.request.urlopen(f"{ADMIN_URL}/config", timeout=2) as r:
            return json.loads(r.read())
    except Exception:
        return None


def save_config_api(cfg: dict) -> bool:
    """Save config via the admin API. Returns True on success."""
    try:
        data = json.dumps(cfg).encode()
        req = urllib.request.Request(
            f"{ADMIN_URL}/config",
            data=data,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=2) as r:
            result = json.loads(r.read())
            return result.get("ok", False)
    except urllib.error.HTTPError as e:
        body = e.read().decode()
        try:
            err = json.loads(body).get("error", body)
        except Exception:
            err = body
        console.print(f"[red]Config save failed: {err}[/]")
    except Exception as e:
        console.print(f"[red]Could not reach admin API ({ADMIN_URL}): {e}[/]")
    return False


def load_config_file() -> dict:
    """Load config directly from YAML file (when admin API is unavailable)."""
    if CONFIG_FILE.exists():
        try:
            return yaml.safe_load(CONFIG_FILE.read_text()) or {}
        except Exception:
            pass
    return {}


def save_config_file(cfg: dict) -> None:
    """Save config directly to YAML file."""
    CONFIG_FILE.write_text(
        yaml.dump(cfg, default_flow_style=False, sort_keys=False, allow_unicode=True)
    )


def set_active_token(name: str) -> bool:
    """Switch the active upstream token via the admin API. Returns True on success."""
    try:
        data = json.dumps({"name": name}).encode()
        req = urllib.request.Request(
            f"{ADMIN_URL}/select",
            data=data,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=2):
            return True
    except urllib.error.HTTPError as e:
        console.print(f"[red]API error {e.code}: {e.read().decode()}[/]")
    except Exception as e:
        console.print(f"[red]Could not reach admin API ({ADMIN_URL}): {e}[/]")
    return False


# ---------------------------------------------------------------------------
# Input helpers
# ---------------------------------------------------------------------------

def ask(prompt: str, default: str = "") -> str:
    hint = f" [{default}]" if default else ""
    try:
        val = console.input(f"[bold cyan]{prompt}{hint}:[/] ").strip()
    except (KeyboardInterrupt, EOFError):
        return default
    return val or default


def confirm(prompt: str) -> bool:
    try:
        val = console.input(f"[bold yellow]{prompt} (y/N):[/] ").strip().lower()
    except (KeyboardInterrupt, EOFError):
        return False
    return val == "y"


def pick(prompt: str, options: list[str]) -> int | None:
    """Show numbered list, return 0-based index or None for back/quit."""
    for i, opt in enumerate(options, 1):
        console.print(f"  [bold]{i}.[/] {opt}")
    console.print(f"  [dim]0. Back[/]")
    try:
        raw = console.input(f"\n[bold cyan]{prompt}:[/] ").strip()
        n = int(raw)
    except (ValueError, KeyboardInterrupt, EOFError):
        return None
    if n == 0:
        return None
    if 1 <= n <= len(options):
        return n - 1
    return None


def clear() -> None:
    console.clear()


def header(title: str) -> None:
    console.print()
    console.rule(f"[bold magenta]{title}[/]")
    console.print()


# ---------------------------------------------------------------------------
# Virtual Keys menu
# ---------------------------------------------------------------------------

def show_vkeys_table(keys: list[dict]) -> None:
    t = Table(show_header=True, header_style="bold dim", box=None, padding=(0, 2))
    t.add_column("#", style="dim", width=4)
    t.add_column("Name", style="bold")
    t.add_column("Key (masked)", style="green")
    for i, vk in enumerate(keys, 1):
        t.add_row(str(i), vk["name"], mask(vk["key"]))
    if keys:
        console.print(t)
    else:
        console.print("  [dim](no virtual keys)[/]")
    console.print()


def menu_virtual_keys() -> None:
    while True:
        clear()
        header("Virtual Keys")
        keys = load_virtual_keys()
        show_vkeys_table(keys)

        idx = pick("Action", ["Add key", "Show full key", "Delete key"])
        if idx is None:
            return

        if idx == 0:  # Add
            console.print()
            name = ask("Name (e.g. alice)")
            if not name:
                continue
            if any(k["name"] == name for k in keys):
                console.print(f"[red]Name '{name}' already exists.[/]")
                console.input("Press Enter to continue…")
                continue
            key = ask("Key (leave blank to auto-generate)")
            if not key:
                key = gen_key()
            keys.append({"name": name, "key": key})
            save_virtual_keys(keys)
            console.print(f"\n[green]Added '{name}'.[/]")
            console.print(f"[dim]Key: {key}[/]")
            console.input("\nPress Enter to continue…")

        elif idx == 1:  # Show full key
            if not keys:
                console.input("No keys to show. Press Enter…")
                continue
            console.print()
            n = pick("Which key to reveal", [k["name"] for k in keys])
            if n is None:
                continue
            console.print(f"\n[bold]{keys[n]['name']}[/]: [green]{keys[n]['key']}[/]")
            console.input("\nPress Enter to continue…")

        elif idx == 2:  # Delete
            if not keys:
                console.input("No keys to delete. Press Enter…")
                continue
            console.print()
            n = pick("Which key to delete", [k["name"] for k in keys])
            if n is None:
                continue
            name = keys[n]["name"]
            if confirm(f"Delete '{name}'?"):
                save_virtual_keys([k for k in keys if k["name"] != name])
                console.print(f"[green]Deleted '{name}'.[/]")
            console.input("Press Enter to continue…")


# ---------------------------------------------------------------------------
# Upstream Tokens menu
# ---------------------------------------------------------------------------

def _health_cell(health: dict | None) -> str:
    if health is None:
        return "[dim]?[/]"
    if health.get("last_checked", 0) == 0:
        return "[dim]unchecked[/]"
    if health.get("healthy"):
        return "[bold green]✓ healthy[/]"
    errs = health.get("error_count", 0)
    return f"[bold red]✗ unhealthy ({errs})[/]"


def show_tokens_table(tokens: list[dict], active: str | None = None,
                      health: dict | None = None) -> None:
    t = Table(show_header=True, header_style="bold dim", box=None, padding=(0, 2))
    t.add_column("#", style="dim", width=4)
    t.add_column("Name", style="bold")
    t.add_column("Token (masked)", style="green")
    t.add_column("Default", justify="center")
    t.add_column("Active", justify="center")
    t.add_column("Health", justify="center")
    for i, tk in enumerate(tokens, 1):
        is_active = active is not None and tk["name"] == active
        h = (health or {}).get(tk["name"])
        t.add_row(
            str(i),
            tk["name"],
            mask(tk["token"]),
            "✓" if tk.get("default") else "",
            "[bold green]▶ live[/]" if is_active else "",
            _health_cell(h),
        )
    if tokens:
        console.print(t)
    else:
        console.print("  [dim](no tokens)[/]")
    if active:
        console.print(f"  [dim]Active (live): [bold]{active}[/][/]")
    elif active is None:
        console.print("  [dim yellow]Admin API unreachable — active token unknown[/]")
    console.print()


def menu_tokens() -> None:
    while True:
        clear()
        header("Upstream Tokens")
        tokens = load_tokens()
        state = get_state()
        active = state["active"] if state else None
        health = state.get("health") if state else None
        show_tokens_table(tokens, active, health)

        idx = pick("Action", [
            "Select active token (live switch)",
            "Run health probe",
            "Add token",
            "Show full token",
            "Set default (on restart)",
            "Delete token",
        ])
        if idx is None:
            return

        if idx == 0:  # Select active
            if not tokens:
                console.input("No tokens. Press Enter…")
                continue
            console.print()
            n = pick("Switch live traffic to", [
                f"{tk['name']}  {'[dim](currently active)[/]' if tk['name'] == active else ''}"
                for tk in tokens
            ])
            if n is None:
                continue
            name = tokens[n]["name"]
            if name == active:
                console.print(f"[yellow]'{name}' is already active.[/]")
            elif set_active_token(name):
                console.print(f"[green]Switched live traffic to '{name}'.[/]")
            console.input("Press Enter to continue…")

        elif idx == 1:  # Run health probe
            if not tokens:
                console.input("No tokens. Press Enter…")
                continue
            console.print()
            n = pick("Probe which token", [t["name"] for t in tokens])
            if n is None:
                continue
            name = tokens[n]["name"]
            console.print(f"[dim]Probing '{name}' (may take up to 30 s)…[/]")
            result = probe_token(name)
            if result is None:
                pass  # error already printed
            elif result["healthy"]:
                console.print(f"[bold green]✓ '{name}' is healthy[/]")
            else:
                console.print(f"[bold red]✗ '{name}' probe failed (errors: {result['error_count']})[/]")
            console.input("Press Enter to continue…")

        elif idx == 2:  # Add
            console.print()
            name = ask("Name (e.g. personal)")
            if not name:
                continue
            if any(t["name"] == name for t in tokens):
                console.print(f"[red]Name '{name}' already exists.[/]")
                console.input("Press Enter to continue…")
                continue
            token = ask("Token (sk-ant-oat-...)")
            if not token:
                console.print("[red]Token is required.[/]")
                console.input("Press Enter to continue…")
                continue
            set_default = confirm("Set as default?")
            if set_default:
                for t in tokens:
                    t.pop("default", None)
            entry: dict = {"name": name, "token": token}
            if set_default:
                entry["default"] = True
            tokens.append(entry)
            save_tokens(tokens)
            console.print(f"\n[green]Added '{name}'.[/]")
            console.input("\nPress Enter to continue…")

        elif idx == 2:  # Show full token
            if not tokens:
                console.input("No tokens to show. Press Enter…")
                continue
            console.print()
            n = pick("Which token to reveal", [t["name"] for t in tokens])
            if n is None:
                continue
            console.print(f"\n[bold]{tokens[n]['name']}[/]: [green]{tokens[n]['token']}[/]")
            console.input("\nPress Enter to continue…")

        elif idx == 3:  # Set default
            if not tokens:
                console.input("No tokens. Press Enter…")
                continue
            console.print()
            n = pick("Set which token as default (used on next restart)", [t["name"] for t in tokens])
            if n is None:
                continue
            for t in tokens:
                t.pop("default", None)
            tokens[n]["default"] = True
            save_tokens(tokens)
            console.print(f"[green]'{tokens[n]['name']}' set as default.[/]")
            console.input("Press Enter to continue…")

        elif idx == 5:  # Delete
            if not tokens:
                console.input("No tokens to delete. Press Enter…")
                continue
            console.print()
            n = pick("Which token to delete", [t["name"] for t in tokens])
            if n is None:
                continue
            name = tokens[n]["name"]
            if confirm(f"Delete '{name}'?"):
                save_tokens([t for t in tokens if t["name"] != name])
                console.print(f"[green]Deleted '{name}'.[/]")
            console.input("Press Enter to continue…")


# ---------------------------------------------------------------------------
# Settings menu
# ---------------------------------------------------------------------------

_SETTINGS_SCHEMA = [
    # (key_path, label, type, hint)
    ("auto_rotation.enabled",              "Auto-rotation enabled",        "bool",  "Automatically switch tokens when 5h utilization is high"),
    ("auto_rotation.threshold_5h",         "Threshold (5h)",               "float", "Trigger when 5h util >= this (0.0-1.0)"),
    ("auto_rotation.target_max_util_5h",   "Target max util (5h)",         "float", "Only switch to tokens below this (0.0-1.0)"),
    ("auto_rotation.check_interval_seconds", "Check interval",             "int",   "Seconds between utilization checks"),
    ("auto_rotation.probe_before_switch",  "Probe before switch",          "bool",  "Health-check candidate before switching"),
    ("auto_rotation.cooldown_seconds",     "Cooldown",                     "int",   "Seconds between auto-rotations"),
    ("auto_rotation.notify_only",          "Notify only",                  "bool",  "Log events without actually switching"),
    ("health_probe_interval_seconds",      "Health probe interval",        "int",   "Seconds between unhealthy token re-probes"),
    ("active_probe_interval_seconds",      "Active probe interval",        "int",   "Seconds between active token probes"),
    ("upstream_timeout_seconds",           "Upstream timeout",             "int",   "HTTP timeout for API calls (restart req.)"),
]


def _cfg_get(cfg: dict, path: str):
    parts = path.split(".")
    v = cfg
    for p in parts:
        if isinstance(v, dict):
            v = v.get(p)
        else:
            return None
    return v


def _cfg_set(cfg: dict, path: str, val) -> None:
    parts = path.split(".")
    d = cfg
    for p in parts[:-1]:
        if p not in d or not isinstance(d[p], dict):
            d[p] = {}
        d = d[p]
    d[parts[-1]] = val


def _fmt_val(val, typ: str) -> str:
    if typ == "bool":
        return "[bold green]ON[/]" if val else "[bold red]OFF[/]"
    if typ == "float":
        return f"[bold]{val:.2f}[/]" if val is not None else "[dim]?[/]"
    return f"[bold]{val}[/]" if val is not None else "[dim]?[/]"


def menu_settings() -> None:
    while True:
        clear()
        header("Settings")

        # Try admin API first, fall back to file
        cfg = get_config()
        via_api = cfg is not None
        if cfg is None:
            cfg = load_config_file()
            console.print("  [dim yellow]Admin API unavailable — reading config.yaml directly[/]\n")

        t = Table(show_header=True, header_style="bold dim", box=None, padding=(0, 2))
        t.add_column("#", style="dim", width=4)
        t.add_column("Setting", style="bold")
        t.add_column("Value", justify="right")
        t.add_column("", style="dim")
        for i, (path, label, typ, hint) in enumerate(_SETTINGS_SCHEMA, 1):
            val = _cfg_get(cfg, path)
            t.add_row(str(i), label, _fmt_val(val, typ), hint)
        console.print(t)
        console.print()

        options = [f"Edit: {s[1]}" for s in _SETTINGS_SCHEMA]
        idx = pick("Action", options)
        if idx is None:
            return

        path, label, typ, hint = _SETTINGS_SCHEMA[idx]
        current = _cfg_get(cfg, path)
        console.print(f"\n  [bold]{label}[/]: {_fmt_val(current, typ)}")
        console.print(f"  [dim]{hint}[/]\n")

        if typ == "bool":
            new_val = not current if current is not None else True
            console.print(f"  Toggling to {_fmt_val(new_val, typ)}")
        elif typ == "float":
            raw = ask(f"New value (current: {current})")
            if not raw:
                continue
            try:
                new_val = float(raw)
            except ValueError:
                console.print("[red]Invalid number.[/]")
                console.input("Press Enter to continue…")
                continue
        elif typ == "int":
            raw = ask(f"New value (current: {current})")
            if not raw:
                continue
            try:
                new_val = int(raw)
            except ValueError:
                console.print("[red]Invalid integer.[/]")
                console.input("Press Enter to continue…")
                continue
        else:
            raw = ask(f"New value (current: {current})")
            if not raw:
                continue
            new_val = raw

        _cfg_set(cfg, path, new_val)

        if via_api:
            if save_config_api(cfg):
                console.print(f"[green]Saved (live).[/]")
            else:
                # Fall back to file
                save_config_file(cfg)
                console.print(f"[yellow]Saved to file (restart needed to apply).[/]")
        else:
            save_config_file(cfg)
            console.print(f"[yellow]Saved to file (restart needed to apply).[/]")

        console.input("\nPress Enter to continue…")


# ---------------------------------------------------------------------------
# Restart
# ---------------------------------------------------------------------------

def do_restart() -> None:
    console.print()
    console.print("[yellow]This will send SIGTERM to the proxy process.[/]")
    console.print("[dim]Docker will restart the container automatically.[/]")
    console.print("[dim]This session will be disconnected — reconnect with:[/]")
    console.print("[dim]  docker compose exec -it proxy python manage.py[/]")
    console.print()
    if confirm("Restart the proxy now?"):
        console.print("[yellow]Restarting…[/]")
        try:
            os.kill(1, signal.SIGTERM)
        except Exception as e:
            console.print(f"[red]Failed: {e}[/]")
            console.input("Press Enter to continue…")


# ---------------------------------------------------------------------------
# Main menu
# ---------------------------------------------------------------------------

def main() -> None:
    while True:
        clear()
        console.print(Panel(
            "[bold white]Claude Proxy Manager[/]\n[dim]Manages virtual_keys.yaml and tokens.yaml[/]",
            border_style="magenta",
            padding=(1, 4),
        ))
        console.print()

        idx = pick("Choose", [
            "Manage Virtual Keys",
            "Manage Upstream Tokens",
            "Settings",
            "Restart Proxy",
            "Quit",
        ])

        if idx is None or idx == 4:
            clear()
            console.print("[dim]Bye.[/]")
            sys.exit(0)
        elif idx == 0:
            menu_virtual_keys()
        elif idx == 1:
            menu_tokens()
        elif idx == 2:
            menu_settings()
        elif idx == 3:
            do_restart()


if __name__ == "__main__":
    main()
