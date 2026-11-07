import asyncio
from typing import Any

from debugger_mcp import __version__
from debugger_mcp.rpc import DaemonClient


def test_version_mismatch_forces_replacement_even_with_active_vscode(monkeypatch):
    client = DaemonClient(spawn_if_missing=True, workspace_root="/workspace/project")
    shutdown_calls: list[tuple[str, dict[str, Any], float]] = []
    spawned = False

    async def fake_call_once(method: str, params: dict[str, Any], timeout: float = 0.0):
        shutdown_calls.append((method, params, timeout))
        return {"shutting_down": True}

    async def fake_wait_for_ports_closed(timeout: float) -> bool:
        return True

    async def fake_ping():
        if spawned:
            return {"version": __version__, "pid": 9999}
        return None

    def fake_spawn_daemon() -> None:
        nonlocal spawned
        spawned = True

    monkeypatch.setattr(client, "_call_once", fake_call_once)
    monkeypatch.setattr(client, "_wait_for_daemon_ports_closed", fake_wait_for_ports_closed)
    monkeypatch.setattr(client, "ping", fake_ping)
    monkeypatch.setattr(client, "_spawn_daemon", fake_spawn_daemon)

    result = asyncio.run(
        client._handle_version(
            {
                "version": "0.5.2",
                "pid": 1234,
                "vscode_connected": True,
                "session_count": 3,
            }
        )
    )

    assert result["version"] == __version__
    assert spawned is True
    assert shutdown_calls == [("__shutdown__", {"force": True}, 2.0)]


def test_version_replacement_terminates_unresponsive_daemon(monkeypatch):
    client = DaemonClient(spawn_if_missing=True, workspace_root="/workspace/project")
    waits = iter([False, True])
    terminations: list[tuple[int, bool]] = []
    spawned = False

    async def failing_shutdown(method: str, params: dict[str, Any], timeout: float = 0.0):
        raise RuntimeError("old daemon ignored shutdown")

    async def fake_wait_for_ports_closed(timeout: float) -> bool:
        return next(waits)

    async def fake_ping():
        if spawned:
            return {"version": __version__, "pid": 9999}
        return None

    def fake_terminate(pid: int, force: bool) -> None:
        terminations.append((pid, force))

    def fake_spawn_daemon() -> None:
        nonlocal spawned
        spawned = True

    monkeypatch.setattr(client, "_call_once", failing_shutdown)
    monkeypatch.setattr(client, "_wait_for_daemon_ports_closed", fake_wait_for_ports_closed)
    monkeypatch.setattr(client, "ping", fake_ping)
    monkeypatch.setattr(client, "_terminate_daemon_process", fake_terminate)
    monkeypatch.setattr(client, "_spawn_daemon", fake_spawn_daemon)

    result = asyncio.run(
        client._handle_version(
            {"version": "0.5.2", "pid": "4321", "vscode_connected": True}
        )
    )

    assert result["version"] == __version__
    assert terminations == [(4321, False)]
    assert spawned is True


def test_version_replacement_reuses_daemon_started_by_another_frontend(monkeypatch):
    client = DaemonClient(spawn_if_missing=True, workspace_root="/workspace/project")
    spawned = False

    async def fake_shutdown(method: str, params: dict[str, Any], timeout: float = 0.0):
        return {"shutting_down": True}

    async def fake_wait_for_ports_closed(timeout: float) -> bool:
        return False

    async def fake_ping():
        return {"version": __version__, "pid": 9999}

    def fake_spawn_daemon() -> None:
        nonlocal spawned
        spawned = True

    monkeypatch.setattr(client, "_call_once", fake_shutdown)
    monkeypatch.setattr(client, "_wait_for_daemon_ports_closed", fake_wait_for_ports_closed)
    monkeypatch.setattr(client, "ping", fake_ping)
    monkeypatch.setattr(client, "_terminate_daemon_process", lambda pid, force: None)
    monkeypatch.setattr(client, "_spawn_daemon", fake_spawn_daemon)

    result = asyncio.run(
        client._handle_version(
            {"version": "0.5.2", "pid": 1234, "vscode_connected": True}
        )
    )

    assert result["version"] == __version__
    assert spawned is False
