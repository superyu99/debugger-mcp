import asyncio
import json
from types import SimpleNamespace

import pytest

from debugger_mcp.dap import DAPError
from debugger_mcp.tools import DebuggerTools


class FakeInspectSession:
    def __init__(self, workspace, frames, globals_ids=None):
        self.session_id = 7
        self.workspace_root = str(workspace)
        self.state = SimpleNamespace(stopped_thread_id=1)
        self.frames = frames
        self.globals_ids = globals_ids or {frame["id"]: frame["id"] * 100 for frame in frames}
        self.evaluate_calls = []

    def cached_top_frame(self, thread_id):
        return None

    async def stack_trace(self, thread_id, start_frame=0, levels=None):
        assert thread_id == 1
        if levels is None:
            selected = self.frames[start_frame:]
        else:
            selected = self.frames[start_frame : start_frame + levels]
        return {"stackFrames": selected, "totalFrames": len(self.frames)}

    async def request(self, command, arguments=None, timeout_seconds=None):
        assert command == "evaluate"
        self.evaluate_calls.append((arguments, timeout_seconds))
        expression = arguments["expression"]
        frame_id = arguments["frameId"]
        if expression == "id(globals())":
            return {"result": str(self.globals_ids[frame_id])}
        if expression.startswith("exec("):
            return {"result": ""}
        if expression.startswith("_debugger_mcp_inspect_json"):
            return {
                "result": json.dumps(
                    {
                        "rows": [
                            {
                                "path": "target",
                                "type": "dict",
                                "note": f"frame={frame_id}",
                            }
                        ],
                        "truncated": False,
                    }
                )
            }
        raise AssertionError(f"unexpected expression: {expression}")


def _frame(frame_id, workspace, rel, line, function):
    return {
        "id": frame_id,
        "name": function,
        "line": line,
        "source": {"path": str(workspace / rel)},
    }


def _tools_for(session):
    tools = DebuggerTools(SimpleNamespace())
    tools._session = lambda **kwargs: session
    return tools


def _inspect_frame_ids(session):
    return [
        args["frameId"]
        for args, _ in session.evaluate_calls
        if args["expression"].startswith("_debugger_mcp_inspect_json")
    ]


def _helper_install_frame_ids(session):
    return [
        args["frameId"]
        for args, _ in session.evaluate_calls
        if args["expression"].startswith("exec(")
    ]


def test_inspect_without_selector_uses_top_frame(tmp_path):
    session = FakeInspectSession(
        tmp_path,
        [
            _frame(5, tmp_path, "pipeline.py", 30, "process_pix"),
            _frame(8, tmp_path, "pipeline.py", 20, "process_frame"),
        ],
    )
    result = asyncio.run(_tools_for(session).inspect_structure("target"))
    assert "frame=5" in result["__mcp_text__"]
    assert _inspect_frame_ids(session) == [5]


def test_inspect_backtracks_by_exact_file_line_function(tmp_path):
    session = FakeInspectSession(
        tmp_path,
        [
            _frame(5, tmp_path, "pipeline.py", 30, "process_pix"),
            _frame(8, tmp_path, "pipeline.py", 20, "process_frame"),
            _frame(11, tmp_path, "pipeline.py", 10, "process_sequence"),
        ],
    )
    result = asyncio.run(
        _tools_for(session).inspect_structure(
            "target",
            file="pipeline.py",
            line=20,
            function="process_frame",
        )
    )
    assert "frame=8" in result["__mcp_text__"]
    assert _inspect_frame_ids(session) == [8]


@pytest.mark.parametrize(
    "kwargs",
    [
        {"file": "pipeline.py"},
        {"line": 20},
        {"function": "process_frame"},
        {"file": "pipeline.py", "line": 20},
        {"file": "pipeline.py", "function": "process_frame"},
        {"line": 20, "function": "process_frame"},
    ],
)
def test_inspect_backtracking_selector_is_all_or_none(tmp_path, kwargs):
    session = FakeInspectSession(
        tmp_path, [_frame(5, tmp_path, "pipeline.py", 30, "process_pix")]
    )
    with pytest.raises(ValueError, match="requires file, line, and function together"):
        asyncio.run(_tools_for(session).inspect_structure("target", **kwargs))


def test_inspect_stale_selector_fails_instead_of_guessing(tmp_path):
    session = FakeInspectSession(
        tmp_path,
        [
            _frame(5, tmp_path, "pipeline.py", 31, "process_pix"),
            _frame(8, tmp_path, "pipeline.py", 21, "process_frame"),
        ],
    )
    with pytest.raises(DAPError) as exc_info:
        asyncio.run(
            _tools_for(session).inspect_structure(
                "target", file="pipeline.py", line=20, function="process_frame"
            )
        )
    text = str(exc_info.value)
    assert "No current stack frame exactly matches inspect_structure selector" in text
    assert "Call get_stack() again" in text
    assert "#0 pipeline.py:31 process_pix" in text
    assert "#1 pipeline.py:21 process_frame" in text
    assert not session.evaluate_calls


def test_inspect_duplicate_recursive_exact_match_is_error(tmp_path):
    session = FakeInspectSession(
        tmp_path,
        [
            _frame(5, tmp_path, "recursive.py", 10, "walk"),
            _frame(8, tmp_path, "recursive.py", 10, "walk"),
        ],
    )
    with pytest.raises(DAPError) as exc_info:
        asyncio.run(
            _tools_for(session).inspect_structure(
                "target", file="recursive.py", line=10, function="walk"
            )
        )
    text = str(exc_info.value)
    assert "Multiple current stack frames exactly match inspect_structure selector" in text
    assert "will not guess" in text
    assert not session.evaluate_calls


def test_inspect_helper_is_installed_per_module_globals_not_per_session(tmp_path):
    session = FakeInspectSession(
        tmp_path,
        [
            _frame(5, tmp_path, "pixel.py", 30, "process_pix"),
            _frame(8, tmp_path, "frame.py", 20, "process_frame"),
        ],
        globals_ids={5: 1001, 8: 2002},
    )
    tools = _tools_for(session)

    asyncio.run(tools.inspect_structure("target"))
    asyncio.run(
        tools.inspect_structure(
            "target", file="frame.py", line=20, function="process_frame"
        )
    )
    # Each module globals needs its own helper installation.
    assert _helper_install_frame_ids(session) == [5, 8]

    # Re-inspecting the same caller globals should reuse that installation.
    asyncio.run(
        tools.inspect_structure(
            "target", file="frame.py", line=20, function="process_frame"
        )
    )
    assert _helper_install_frame_ids(session) == [5, 8]
