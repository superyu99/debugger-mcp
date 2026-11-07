from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from debugger_mcp.dap import DAPProxy, DAPSession
from debugger_mcp.workspace import (
    WORKSPACE_ENV_KEY,
    normalize_workspace_root,
)


def _fake_session(session_id: int, workspace: Path | None, stop_sequence: int):
    return SimpleNamespace(
        session_id=session_id,
        connected=True,
        workspace_root=normalize_workspace_root(str(workspace)) if workspace else None,
        launch_cwd=normalize_workspace_root(str(workspace)) if workspace else None,
        last_stop_sequence=stop_sequence,
        last_activity_monotonic=float(stop_sequence),
        session_kind="root",
        state=SimpleNamespace(status="stopped"),
    )


def test_workspace_scoped_latest_stop_selection(tmp_path: Path):
    workspace_a = tmp_path / "a"
    workspace_b = tmp_path / "b"
    proxy = DAPProxy(port=0)
    a_old = _fake_session(1, workspace_a, 1)
    b_latest = _fake_session(2, workspace_b, 4)
    a_latest = _fake_session(3, workspace_a, 3)
    proxy._sessions = {1: a_old, 2: b_latest, 3: a_latest}  # type: ignore[assignment]

    assert proxy.select_session(workspace_root=str(workspace_a), require_stopped=True) is a_latest
    assert proxy.select_session(workspace_root=str(workspace_b), require_stopped=True) is b_latest
    assert proxy.select_session(require_stopped=True) is b_latest

    # Explicit session_id intentionally bypasses workspace scoping.
    assert proxy.select_session(session_id=2, workspace_root=str(workspace_a)) is b_latest


def test_unmarked_session_is_not_silently_selected_for_a_workspace(tmp_path: Path):
    proxy = DAPProxy(port=0)
    unmarked = _fake_session(1, None, 1)
    proxy._sessions = {1: unmarked}  # type: ignore[assignment]

    assert proxy.select_session(workspace_root=str(tmp_path), require_stopped=True) is None
    assert proxy.select_session(require_stopped=True) is unmarked


def test_launch_workspace_is_consumed_and_inherited_by_child(tmp_path: Path):
    session = DAPSession(
        session_id=1,
        frontend_reader=None,  # type: ignore[arg-type]
        frontend_writer=None,  # type: ignore[arg-type]
        backend_reader=None,  # type: ignore[arg-type]
        backend_writer=None,  # type: ignore[arg-type]
        proxy_host="127.0.0.1",
        proxy_port=4711,
    )
    launch = {
        "arguments": {
            "name": "workspace launch",
            "env": {WORKSPACE_ENV_KEY: str(tmp_path)},
        }
    }
    session._capture_start_request("launch", launch)

    assert session.workspace_root == normalize_workspace_root(str(tmp_path))
    assert "env" not in launch["arguments"]

    reverse_request = {"arguments": {"configuration": {"subProcessId": 123}}}
    session._prepare_child_start_request(reverse_request)
    configuration = reverse_request["arguments"]["configuration"]
    assert configuration["env"][WORKSPACE_ENV_KEY] == session.workspace_root
    assert configuration["debugServer"] == 4711


def test_cwd_is_diagnostic_only_and_cannot_bind_workspace(tmp_path: Path):
    session = DAPSession(
        session_id=2,
        frontend_reader=None,  # type: ignore[arg-type]
        frontend_writer=None,  # type: ignore[arg-type]
        backend_reader=None,  # type: ignore[arg-type]
        backend_writer=None,  # type: ignore[arg-type]
        proxy_host="127.0.0.1",
        proxy_port=4711,
    )
    launch = {
        "arguments": {
            "name": "unscoped launch",
            "cwd": str(tmp_path),
        }
    }
    session._capture_start_request("launch", launch)

    assert session.workspace_root is None
    assert session.launch_cwd == normalize_workspace_root(str(tmp_path))

    proxy = DAPProxy(port=0)
    session.state.status = "stopped"
    session.last_stop_sequence = 1
    proxy._sessions = {session.session_id: session}  # type: ignore[assignment]
    assert proxy.select_session(workspace_root=str(tmp_path), require_stopped=True) is None


def test_legacy_top_level_workspace_field_is_ignored(tmp_path: Path):
    session = DAPSession(
        session_id=3,
        frontend_reader=None,  # type: ignore[arg-type]
        frontend_writer=None,  # type: ignore[arg-type]
        backend_reader=None,  # type: ignore[arg-type]
        backend_writer=None,  # type: ignore[arg-type]
        proxy_host="127.0.0.1",
        proxy_port=4711,
    )
    launch = {
        "arguments": {
            "name": "legacy launch",
            "debuggerMcpWorkspace": str(tmp_path),
            "cwd": str(tmp_path),
        }
    }
    session._capture_start_request("launch", launch)

    assert session.workspace_root is None
    assert session.launch_cwd == normalize_workspace_root(str(tmp_path))
    assert "debuggerMcpWorkspace" not in launch["arguments"]


def test_workspace_marker_is_consumed_but_other_env_is_preserved(tmp_path: Path):
    session = DAPSession(
        session_id=4,
        frontend_reader=None,  # type: ignore[arg-type]
        frontend_writer=None,  # type: ignore[arg-type]
        backend_reader=None,  # type: ignore[arg-type]
        backend_writer=None,  # type: ignore[arg-type]
        proxy_host="127.0.0.1",
        proxy_port=4711,
    )
    launch = {
        "arguments": {
            "name": "env launch",
            "env": {
                WORKSPACE_ENV_KEY: str(tmp_path),
                "PYTHONWARNINGS": "ignore",
            },
        }
    }
    session._capture_start_request("launch", launch)

    assert session.workspace_root == normalize_workspace_root(str(tmp_path))
    assert launch["arguments"]["env"] == {"PYTHONWARNINGS": "ignore"}
