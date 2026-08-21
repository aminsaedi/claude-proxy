"""Graceful shutdown wiring — the half of zero-downtime the process owns.

The bug these guard against is specific and was live in this codebase: two
uvicorn servers share one process, each installs its own SIGTERM handler, the
second registration wins, and SIGTERM stops exactly one of them. The process
then hangs holding the other open until the kubelet gives up and sends SIGKILL,
which severs every in-flight request — the opposite of a graceful drain.
"""

from __future__ import annotations

import asyncio
import signal

import pytest
import uvicorn

from claude_proxy.app import AppState, _disarm_uvicorn_signals, _install_shutdown


class _FakeServer:
    """Stands in for uvicorn.Server with the two signal hooks it has had."""

    def __init__(self) -> None:
        self.should_exit = False
        self.captured = False

    def capture_signals(self):  # noqa: ANN201
        self.captured = True
        raise AssertionError("capture_signals must not run once disarmed")

    def install_signal_handlers(self) -> None:
        raise AssertionError("install_signal_handlers must not run once disarmed")


def test_disarm_neutralises_both_uvicorn_signal_hooks():
    srv = _FakeServer()
    _disarm_uvicorn_signals(srv)
    with srv.capture_signals():      # now a no-op context manager
        pass
    srv.install_signal_handlers()    # now a no-op callable
    assert srv.captured is False


def test_disarm_works_on_a_real_uvicorn_server():
    """Pinned against the real class, so a uvicorn API change fails here."""
    srv = uvicorn.Server(uvicorn.Config(app=lambda *a: None, log_config=None))
    assert hasattr(srv, "capture_signals"), "uvicorn no longer has capture_signals"
    _disarm_uvicorn_signals(srv)
    with srv.capture_signals():
        # If this were uvicorn's own implementation it would have replaced the
        # process SIGTERM disposition; assert it did not.
        assert signal.getsignal(signal.SIGTERM) is not srv.handle_exit


async def test_sigterm_fails_readiness_first_then_stops_every_server():
    state = AppState()
    servers = [_FakeServer(), _FakeServer()]
    for s in servers:
        _disarm_uvicorn_signals(s)
    _install_shutdown(state, servers, lead=0.05)

    assert state.draining is False
    signal.raise_signal(signal.SIGTERM)
    await asyncio.sleep(0.01)

    # Readiness must drop immediately — that is what removes the pod from the
    # ingress — while the servers keep listening through the lead.
    assert state.draining is True
    assert not any(s.should_exit for s in servers), "servers stopped before draining"

    await asyncio.sleep(0.15)
    assert all(s.should_exit for s in servers), "a server was left running"


async def test_a_second_signal_does_not_restart_the_shutdown():
    state = AppState()
    servers = [_FakeServer()]
    _disarm_uvicorn_signals(servers[0])
    _install_shutdown(state, servers, lead=0.05)
    signal.raise_signal(signal.SIGTERM)
    signal.raise_signal(signal.SIGTERM)
    await asyncio.sleep(0.2)
    assert servers[0].should_exit is True


@pytest.fixture(autouse=True)
def _restore_signal_handlers():
    """Leave the interpreter's signal disposition as we found it."""
    saved = {s: signal.getsignal(s) for s in (signal.SIGTERM, signal.SIGINT)}
    yield
    loop = None
    try:
        loop = asyncio.get_event_loop_policy().get_event_loop()
    except Exception:  # noqa: BLE001
        pass
    for sig, handler in saved.items():
        try:
            if loop is not None and not loop.is_closed():
                loop.remove_signal_handler(sig)
        except Exception:  # noqa: BLE001
            pass
        signal.signal(sig, handler)
