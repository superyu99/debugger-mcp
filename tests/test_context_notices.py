from pathlib import Path

from debugger_mcp.daemon import DebugContextSnapshot, SharedDebuggerDaemon


def _snapshot(
    session_id: int,
    stop_id: int,
    workspace: Path,
    relative_path: str,
    line: int,
    function: str,
    process: str = "train.py",
):
    return DebugContextSnapshot(
        session_id=session_id,
        stop_id=stop_id,
        workspace_root=str(workspace),
        process_name=process,
        source_path=str(workspace / relative_path),
        line=line,
        function_name=function,
    )


def test_location_change_notice_uses_workspace_relative_paths(tmp_path: Path):
    old = _snapshot(1, 3, tmp_path, "models/head.py", 10, "forward")
    new = _snapshot(1, 4, tmp_path, "data/transform.py", 27, "__call__")
    notice = SharedDebuggerDaemon._format_context_notice(old, new, str(tmp_path))
    assert notice == (
        "[Debug location changed] models/head.py:10 forward -> "
        "data/transform.py:27 __call__"
    )


def test_same_location_new_pause_has_explicit_notice(tmp_path: Path):
    old = _snapshot(1, 3, tmp_path, "loop.py", 20, "step")
    new = _snapshot(1, 4, tmp_path, "loop.py", 20, "step")
    notice = SharedDebuggerDaemon._format_context_notice(old, new, str(tmp_path))
    assert notice.startswith("[Debug pause changed] new pause at loop.py:20 step")


def test_process_switch_notice_includes_workspace_names(tmp_path: Path):
    workspace_a = tmp_path / "workspace_a"
    workspace_b = tmp_path / "workspace_b"
    old = _snapshot(1, 3, workspace_a, "model.py", 10, "forward", "/tmp/rank0.py")
    new = _snapshot(2, 1, workspace_b, "loss.py", 8, "compute", "/tmp/rank1.py")
    notice = SharedDebuggerDaemon._format_context_notice(old, new, str(workspace_a))
    assert notice == (
        "[Debug context changed] workspace_a/model.py:10 forward -> "
        "workspace_b/loss.py:8 compute"
    )


def test_notice_is_prepended_to_compact_text_result():
    value = {"__mcp_text__": "42"}
    updated = SharedDebuggerDaemon._prepend_context_notice(value, "[Debug location changed] a.py:1 f -> b.py:2 g")
    assert updated == {
        "__mcp_text__": "[Debug location changed] a.py:1 f -> b.py:2 g\n\n42"
    }
