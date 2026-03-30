#!/usr/bin/env python3
"""
Claude Proxy TUI Manager

Run inside the container:
    docker compose exec -it proxy python manage.py
"""

import os
import secrets
import signal
from pathlib import Path

import yaml
from textual import on
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Container, Horizontal, Vertical
from textual.coordinate import Coordinate
from textual.screen import ModalScreen
from textual.widgets import (
    Button,
    DataTable,
    Footer,
    Header,
    Input,
    Label,
    Static,
    TabbedContent,
    TabPane,
)

BASE = Path(__file__).parent
VKEYS_FILE = BASE / "virtual_keys.yaml"
TOKENS_FILE = BASE / "tokens.yaml"


# ---------------------------------------------------------------------------
# YAML helpers
# ---------------------------------------------------------------------------

def load_virtual_keys() -> list[dict]:
    if not VKEYS_FILE.exists():
        return []
    data = yaml.safe_load(VKEYS_FILE.read_text()) or {}
    return data.get("virtual_keys", [])


def save_virtual_keys(keys: list[dict]) -> None:
    VKEYS_FILE.write_text(yaml.dump({"virtual_keys": keys}, default_flow_style=False, allow_unicode=True))


def load_tokens() -> list[dict]:
    if not TOKENS_FILE.exists():
        return []
    data = yaml.safe_load(TOKENS_FILE.read_text()) or {}
    return data.get("tokens", [])


def save_tokens(tokens: list[dict]) -> None:
    TOKENS_FILE.write_text(yaml.dump({"tokens": tokens}, default_flow_style=False, allow_unicode=True))


def mask(s: str, show: int = 12) -> str:
    if len(s) <= show + 3:
        return s
    return s[:show] + "…"


def gen_key() -> str:
    return "vk-" + secrets.token_urlsafe(24)


def _row_name(table: DataTable) -> str | None:
    """Return the name (row key) of the currently selected DataTable row."""
    if table.row_count == 0:
        return None
    try:
        cell_key = table.coordinate_to_cell_key(Coordinate(table.cursor_row, 0))
        return str(cell_key.row_key.value)
    except Exception:
        return None


# ---------------------------------------------------------------------------
# Modal — confirm action
# ---------------------------------------------------------------------------

class ConfirmModal(ModalScreen[bool]):
    DEFAULT_CSS = """
    ConfirmModal {
        align: center middle;
    }
    ConfirmModal > Vertical {
        width: 52;
        height: auto;
        border: thick $error 60%;
        background: $surface;
        padding: 1 2;
    }
    ConfirmModal Label {
        margin-bottom: 1;
        color: $text;
    }
    ConfirmModal Horizontal {
        height: auto;
        align: right middle;
        gap: 1;
    }
    """
    BINDINGS = [Binding("escape", "dismiss_false", "Cancel")]

    def __init__(self, message: str) -> None:
        super().__init__()
        self._message = message

    def compose(self) -> ComposeResult:
        with Vertical():
            yield Label(self._message)
            with Horizontal():
                yield Button("Confirm", variant="error", id="yes")
                yield Button("Cancel", id="no")

    @on(Button.Pressed, "#yes")
    def do_yes(self) -> None:
        self.dismiss(True)

    @on(Button.Pressed, "#no")
    def action_dismiss_false(self) -> None:
        self.dismiss(False)


# ---------------------------------------------------------------------------
# Modal — reveal secret
# ---------------------------------------------------------------------------

class RevealModal(ModalScreen):
    DEFAULT_CSS = """
    RevealModal {
        align: center middle;
    }
    RevealModal > Vertical {
        width: 70;
        height: auto;
        border: thick $primary 60%;
        background: $surface;
        padding: 1 2;
    }
    RevealModal #secret {
        background: $panel;
        border: solid $primary-darken-2;
        padding: 0 1;
        color: $success;
        margin-bottom: 1;
    }
    RevealModal Label.hint {
        color: $text-muted;
        margin-bottom: 1;
    }
    """
    BINDINGS = [Binding("escape", "dismiss", "Close")]

    def __init__(self, title: str, value: str) -> None:
        super().__init__()
        self._title = title
        self._value = value

    def compose(self) -> ComposeResult:
        with Vertical():
            yield Label(f"{self._title}", id="rtitle")
            yield Label(self._value, id="secret")
            yield Label("Press Esc to close", classes="hint")

    def on_mount(self) -> None:
        self.query_one("#secret", Label).focus()


# ---------------------------------------------------------------------------
# Modal — add virtual key
# ---------------------------------------------------------------------------

class AddVirtualKeyModal(ModalScreen[dict | None]):
    DEFAULT_CSS = """
    AddVirtualKeyModal {
        align: center middle;
    }
    AddVirtualKeyModal > Vertical {
        width: 60;
        height: auto;
        border: thick $primary 60%;
        background: $surface;
        padding: 1 2;
    }
    AddVirtualKeyModal Label {
        color: $text-muted;
        margin-bottom: 0;
    }
    AddVirtualKeyModal Input {
        margin-bottom: 1;
    }
    AddVirtualKeyModal Horizontal {
        height: auto;
        align: right middle;
        gap: 1;
        margin-top: 1;
    }
    AddVirtualKeyModal #err {
        color: $error;
        height: 1;
    }
    """
    BINDINGS = [Binding("escape", "dismiss_none", "Cancel")]

    def compose(self) -> ComposeResult:
        with Vertical():
            yield Label("Add Virtual Key")
            yield Label("Name")
            yield Input(placeholder="e.g. alice", id="name")
            yield Label("Key  (leave blank to auto-generate)")
            yield Input(placeholder="vk-...", id="key")
            yield Label("", id="err")
            with Horizontal():
                yield Button("Add", variant="primary", id="add")
                yield Button("Cancel", id="cancel")

    def on_mount(self) -> None:
        self.query_one("#name", Input).focus()

    @on(Button.Pressed, "#add")
    @on(Input.Submitted)
    def do_add(self) -> None:
        name = self.query_one("#name", Input).value.strip()
        key = self.query_one("#key", Input).value.strip() or gen_key()
        if not name:
            self.query_one("#err", Label).update("Name is required")
            self.query_one("#name", Input).focus()
            return
        # Check for duplicate names
        existing = [vk["name"] for vk in load_virtual_keys()]
        if name in existing:
            self.query_one("#err", Label).update(f'Name "{name}" already exists')
            self.query_one("#name", Input).focus()
            return
        self.dismiss({"name": name, "key": key})

    @on(Button.Pressed, "#cancel")
    def action_dismiss_none(self) -> None:
        self.dismiss(None)


# ---------------------------------------------------------------------------
# Modal — add upstream token
# ---------------------------------------------------------------------------

class AddTokenModal(ModalScreen[dict | None]):
    DEFAULT_CSS = """
    AddTokenModal {
        align: center middle;
    }
    AddTokenModal > Vertical {
        width: 62;
        height: auto;
        border: thick $primary 60%;
        background: $surface;
        padding: 1 2;
    }
    AddTokenModal Label {
        color: $text-muted;
        margin-bottom: 0;
    }
    AddTokenModal Input {
        margin-bottom: 1;
    }
    AddTokenModal Horizontal {
        height: auto;
        align: right middle;
        gap: 1;
        margin-top: 1;
    }
    AddTokenModal #err {
        color: $error;
        height: 1;
    }
    """
    BINDINGS = [Binding("escape", "dismiss_none", "Cancel")]

    def compose(self) -> ComposeResult:
        with Vertical():
            yield Label("Add Upstream OAuth Token")
            yield Label("Name")
            yield Input(placeholder="e.g. personal", id="name")
            yield Label("Token  (sk-ant-oat-...)")
            yield Input(placeholder="sk-ant-oat-...", password=True, id="token")
            yield Label("", id="err")
            with Horizontal():
                yield Button("Add + set default", variant="success", id="add-default")
                yield Button("Add", variant="primary", id="add")
                yield Button("Cancel", id="cancel")

    def on_mount(self) -> None:
        self.query_one("#name", Input).focus()

    def _validate(self) -> tuple[str, str] | None:
        name = self.query_one("#name", Input).value.strip()
        token = self.query_one("#token", Input).value.strip()
        if not name:
            self.query_one("#err", Label).update("Name is required")
            self.query_one("#name", Input).focus()
            return None
        if not token:
            self.query_one("#err", Label).update("Token is required")
            self.query_one("#token", Input).focus()
            return None
        existing = [t["name"] for t in load_tokens()]
        if name in existing:
            self.query_one("#err", Label).update(f'Name "{name}" already exists')
            self.query_one("#name", Input).focus()
            return None
        return name, token

    @on(Button.Pressed, "#add-default")
    def do_add_default(self) -> None:
        result = self._validate()
        if result:
            self.dismiss({"name": result[0], "token": result[1], "default": True})

    @on(Button.Pressed, "#add")
    def do_add(self) -> None:
        result = self._validate()
        if result:
            self.dismiss({"name": result[0], "token": result[1], "default": False})

    @on(Button.Pressed, "#cancel")
    def action_dismiss_none(self) -> None:
        self.dismiss(None)


# ---------------------------------------------------------------------------
# Virtual Keys pane
# ---------------------------------------------------------------------------

class VirtualKeysPane(Container):
    DEFAULT_CSS = """
    VirtualKeysPane {
        padding: 1;
        height: 1fr;
    }
    VirtualKeysPane #toolbar {
        height: 3;
        margin-bottom: 1;
        gap: 1;
    }
    VirtualKeysPane DataTable {
        height: 1fr;
    }
    """

    def compose(self) -> ComposeResult:
        with Horizontal(id="toolbar"):
            yield Button("+ Add", variant="success", id="add")
            yield Button("Reveal", id="reveal")
            yield Button("Delete", variant="error", id="delete")
        yield DataTable(id="table", cursor_type="row", zebra_stripes=True)

    def on_mount(self) -> None:
        t = self.query_one("#table", DataTable)
        t.add_columns("Name", "Key (masked)")
        self._reload()

    def _reload(self) -> None:
        t = self.query_one("#table", DataTable)
        t.clear()
        for vk in load_virtual_keys():
            t.add_row(vk["name"], mask(vk["key"]), key=vk["name"])

    @on(Button.Pressed, "#add")
    def do_add(self) -> None:
        def handle(result: dict | None) -> None:
            if not result:
                return
            keys = load_virtual_keys()
            keys.append({"name": result["name"], "key": result["key"]})
            save_virtual_keys(keys)
            self._reload()
            self.app.notify(f'Added key "{result["name"]}" — key auto-saved to virtual_keys.yaml')
        self.app.push_screen(AddVirtualKeyModal(), handle)

    @on(Button.Pressed, "#reveal")
    def do_reveal(self) -> None:
        name = _row_name(self.query_one("#table", DataTable))
        if not name:
            return
        keys = {vk["name"]: vk["key"] for vk in load_virtual_keys()}
        key = keys.get(name, "")
        self.app.push_screen(RevealModal(f'Virtual key: {name}', key))

    @on(Button.Pressed, "#delete")
    def do_delete(self) -> None:
        name = _row_name(self.query_one("#table", DataTable))
        if not name:
            return
        def handle(confirmed: bool) -> None:
            if not confirmed:
                return
            save_virtual_keys([vk for vk in load_virtual_keys() if vk["name"] != name])
            self._reload()
            self.app.notify(f'Deleted key "{name}"')
        self.app.push_screen(ConfirmModal(f'Delete virtual key "{name}"?\n\nThis cannot be undone.'), handle)


# ---------------------------------------------------------------------------
# Upstream Tokens pane
# ---------------------------------------------------------------------------

class TokensPane(Container):
    DEFAULT_CSS = """
    TokensPane {
        padding: 1;
        height: 1fr;
    }
    TokensPane #toolbar {
        height: 3;
        margin-bottom: 1;
        gap: 1;
    }
    TokensPane DataTable {
        height: 1fr;
    }
    """

    def compose(self) -> ComposeResult:
        with Horizontal(id="toolbar"):
            yield Button("+ Add", variant="success", id="add")
            yield Button("Reveal", id="reveal")
            yield Button("Set Default", id="set-default")
            yield Button("Delete", variant="error", id="delete")
        yield DataTable(id="table", cursor_type="row", zebra_stripes=True)

    def on_mount(self) -> None:
        t = self.query_one("#table", DataTable)
        t.add_columns("Name", "Token (masked)", "Default")
        self._reload()

    def _reload(self) -> None:
        t = self.query_one("#table", DataTable)
        t.clear()
        for tk in load_tokens():
            t.add_row(
                tk["name"],
                mask(tk["token"]),
                "✓" if tk.get("default") else "",
                key=tk["name"],
            )

    @on(Button.Pressed, "#add")
    def do_add(self) -> None:
        def handle(result: dict | None) -> None:
            if not result:
                return
            tokens = load_tokens()
            if result["default"]:
                for tk in tokens:
                    tk.pop("default", None)
            entry: dict = {"name": result["name"], "token": result["token"]}
            if result["default"]:
                entry["default"] = True
            tokens.append(entry)
            save_tokens(tokens)
            self._reload()
            self.app.notify(f'Added token "{result["name"]}"')
        self.app.push_screen(AddTokenModal(), handle)

    @on(Button.Pressed, "#reveal")
    def do_reveal(self) -> None:
        name = _row_name(self.query_one("#table", DataTable))
        if not name:
            return
        tokens = {tk["name"]: tk["token"] for tk in load_tokens()}
        token = tokens.get(name, "")
        self.app.push_screen(RevealModal(f'Upstream token: {name}', token))

    @on(Button.Pressed, "#set-default")
    def do_set_default(self) -> None:
        name = _row_name(self.query_one("#table", DataTable))
        if not name:
            return
        tokens = load_tokens()
        for tk in tokens:
            tk.pop("default", None)
        for tk in tokens:
            if tk["name"] == name:
                tk["default"] = True
                break
        save_tokens(tokens)
        self._reload()
        self.app.notify(f'"{name}" set as default token')

    @on(Button.Pressed, "#delete")
    def do_delete(self) -> None:
        name = _row_name(self.query_one("#table", DataTable))
        if not name:
            return
        def handle(confirmed: bool) -> None:
            if not confirmed:
                return
            save_tokens([tk for tk in load_tokens() if tk["name"] != name])
            self._reload()
            self.app.notify(f'Deleted token "{name}"')
        self.app.push_screen(ConfirmModal(f'Delete upstream token "{name}"?\n\nThis cannot be undone.'), handle)


# ---------------------------------------------------------------------------
# Main app
# ---------------------------------------------------------------------------

APP_CSS = """
Screen {
    background: $surface-darken-1;
}

#status-bar {
    height: 3;
    dock: top;
    background: $primary-darken-3;
    padding: 0 2;
    align: left middle;
    border-bottom: solid $primary-darken-2;
}

#status-label {
    width: 1fr;
    color: $text-muted;
}

#restart-btn {
    margin-left: 1;
}

TabbedContent {
    height: 1fr;
}

TabPane {
    height: 1fr;
}
"""


class ProxyManager(App):
    TITLE = "Claude Proxy Manager"
    CSS = APP_CSS
    BINDINGS = [
        Binding("q", "quit", "Quit"),
        Binding("r", "restart", "Restart proxy"),
    ]

    def compose(self) -> ComposeResult:
        yield Header()
        with Horizontal(id="status-bar"):
            yield Static("", id="status-label")
            yield Button("↺  Restart Proxy", id="restart-btn", variant="warning")
        with TabbedContent():
            with TabPane("Virtual Keys"):
                yield VirtualKeysPane()
            with TabPane("Upstream Tokens"):
                yield TokensPane()
        yield Footer()

    def on_mount(self) -> None:
        self._refresh_status()

    def _refresh_status(self) -> None:
        try:
            # Check if PID 1 is alive (proxy process)
            os.kill(1, 0)
            self.query_one("#status-label", Static).update(
                "[green]● Proxy running[/green]  — Restart will drop this session; reconnect with: docker compose exec -it proxy python manage.py"
            )
        except ProcessLookupError:
            self.query_one("#status-label", Static).update("[red]○ Proxy not running[/red]")
        except PermissionError:
            # PID 1 exists but we don't own it — still running
            self.query_one("#status-label", Static).update(
                "[green]● Proxy running[/green]  — Restart will drop this session; reconnect with: docker compose exec -it proxy python manage.py"
            )

    @on(Button.Pressed, "#restart-btn")
    def action_restart(self) -> None:
        def handle(confirmed: bool) -> None:
            if not confirmed:
                return
            self.notify("Sending SIGTERM to proxy process — container will restart shortly…", severity="warning")
            # Small delay so the notification renders before the session drops
            self.set_timer(0.5, self._do_restart)
        self.app.push_screen(
            ConfirmModal(
                "Restart the proxy container?\n\n"
                "This sends SIGTERM to PID 1. Docker will restart the container\n"
                "and this session will be disconnected."
            ),
            handle,
        )

    def _do_restart(self) -> None:
        try:
            os.kill(1, signal.SIGTERM)
        except Exception as e:
            self.notify(f"Restart failed: {e}", severity="error")


if __name__ == "__main__":
    ProxyManager().run()
