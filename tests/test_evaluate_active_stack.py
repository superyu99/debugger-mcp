import asyncio
from types import SimpleNamespace

import pytest

from debugger_mcp.tools import DebuggerTools


class FakeSession:
    def __init__(self, workspace, frames):
        self.workspace_root = str(workspace)
        self.state = SimpleNamespace(stopped_thread_id=1)
        self.frames = frames
        self.evaluate_calls = []

    def cached_top_frame(self, thread_id):
        return None

    async def stack_trace(self, thread_id, start_frame=0, levels=None):
        assert thread_id == 1
        selected = self.frames[start_frame:] if levels is None else self.frames[start_frame : start_frame + levels]
        return {"stackFrames": selected, "totalFrames": len(self.frames)}

    async def request(self, command, arguments=None, timeout_seconds=None):
        assert command == "evaluate"
        self.evaluate_calls.append((arguments, timeout_seconds))
        return {"result": f"frame={arguments['frameId']}"}


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


def test_evaluate_without_frame_eval_uses_top_frame(tmp_path):
    session = FakeSession(
        tmp_path,
        [
            _frame(5, tmp_path, "pipeline.py", 30, "process_pix"),
            _frame(8, tmp_path, "pipeline.py", 20, "process_frame"),
        ],
    )
    result = asyncio.run(_tools_for(session).evaluate("x"))
    assert result == {"__mcp_text__": "frame=5"}
    assert session.evaluate_calls[0][0]["frameId"] == 5


@pytest.mark.parametrize("selector", ["file", "line", "function"])
def test_evaluate_rejects_removed_outer_frame_selectors(tmp_path, selector):
    session = FakeSession(tmp_path, [_frame(5, tmp_path, "pipeline.py", 30, "process_pix")])
    with pytest.raises(TypeError, match=f"unexpected keyword argument '{selector}'"):
        asyncio.run(_tools_for(session).evaluate("x", **{selector: "pipeline.py"}))


def test_get_stack_exposes_stack_depth_not_dap_frame_id(tmp_path):
    session = FakeSession(
        tmp_path,
        [
            _frame(501, tmp_path, "pipeline.py", 30, "process_pix"),
            _frame(9001, tmp_path, "pipeline.py", 20, "process_frame"),
        ],
    )
    result = asyncio.run(_tools_for(session).get_stack())
    assert result["__mcp_text__"].splitlines() == [
        "#0 pipeline.py:30 process_pix",
        "#1 pipeline.py:20 process_frame",
    ]
    assert "501" not in result["__mcp_text__"]
    assert "9001" not in result["__mcp_text__"]
