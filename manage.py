#!/usr/bin/env python3
"""
Claude Proxy Manager — interactive menu
Run: docker compose exec -it proxy python manage.py
"""

import os
import secrets
import signal
import sys
from pathlib import Path

import yaml
from rich.console import Console
from rich.panel import Panel
from rich.table import Table
from rich import print as rprint

BASE = Path(__file__).parent
VKEYS_FILE = BASE / "virtual_keys.yaml"
TOKENS_FILE = BASE / "tokens.yaml"

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

def show_tokens_table(tokens: list[dict]) -> None:
    t = Table(show_header=True, header_style="bold dim", box=None, padding=(0, 2))
    t.add_column("#", style="dim", width=4)
    t.add_column("Name", style="bold")
    t.add_column("Token (masked)", style="green")
    t.add_column("Default", justify="center")
    for i, tk in enumerate(tokens, 1):
        t.add_row(str(i), tk["name"], mask(tk["token"]), "✓" if tk.get("default") else "")
    if tokens:
        console.print(t)
    else:
        console.print("  [dim](no tokens)[/]")
    console.print()


def menu_tokens() -> None:
    while True:
        clear()
        header("Upstream Tokens")
        tokens = load_tokens()
        show_tokens_table(tokens)

        idx = pick("Action", ["Add token", "Show full token", "Set default", "Delete token"])
        if idx is None:
            return

        if idx == 0:  # Add
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

        elif idx == 1:  # Show full token
            if not tokens:
                console.input("No tokens to show. Press Enter…")
                continue
            console.print()
            n = pick("Which token to reveal", [t["name"] for t in tokens])
            if n is None:
                continue
            console.print(f"\n[bold]{tokens[n]['name']}[/]: [green]{tokens[n]['token']}[/]")
            console.input("\nPress Enter to continue…")

        elif idx == 2:  # Set default
            if not tokens:
                console.input("No tokens. Press Enter…")
                continue
            console.print()
            n = pick("Set which token as default", [t["name"] for t in tokens])
            if n is None:
                continue
            for t in tokens:
                t.pop("default", None)
            tokens[n]["default"] = True
            save_tokens(tokens)
            console.print(f"[green]'{tokens[n]['name']}' set as default.[/]")
            console.input("Press Enter to continue…")

        elif idx == 3:  # Delete
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
            "Restart Proxy",
            "Quit",
        ])

        if idx is None or idx == 3:
            clear()
            console.print("[dim]Bye.[/]")
            sys.exit(0)
        elif idx == 0:
            menu_virtual_keys()
        elif idx == 1:
            menu_tokens()
        elif idx == 2:
            do_restart()


if __name__ == "__main__":
    main()
