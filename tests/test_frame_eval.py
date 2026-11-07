from __future__ import annotations

import asyncio
import builtins
from pathlib import Path
from types import SimpleNamespace

from debugger_mcp.dap import DAPError
from debugger_mcp.frame_eval import (
    FrameSelector,
    capture_frame_expression,
    execute_with_frames_expression,
    extract_frame_eval_selectors,
    new_frame_token,
)
from debugger_mcp.tools import DebuggerTools


def _metadata(file: str, line: int, function: str, stack_index: int) -> dict:
    return {
        "file": file,
        "absolute_file": file,
        "line": line,
        "function": function,
        "stack_index": stack_index,
    }


def test_frame_eval_combines_real_objects_from_active_frames() -> None:
    token = new_frame_token()
    source = {"pixels": [1, 2, 3]}
    selector = FrameSelector("pipeline.py", 20, "caller")

    def caller() -> dict:
        raw_image = source
        eval(
            capture_frame_expression(
                token, selector, _metadata("pipeline.py", 20, "caller", 1)
            ),
            globals(),
            locals(),
        )
        return current()

    def current() -> dict:
        prediction = {"score": 0.9}
        code = '''
image = frame_eval("raw_image", "pipeline.py", 20, "caller")
assert image is frame_eval("raw_image", "pipeline.py", 20, "caller")
image["prediction"] = prediction
'''
        result = eval(execute_with_frames_expression(token, code), globals(), locals())
        assert result is None
        return source

    result = caller()
    assert result == {"pixels": [1, 2, 3], "prediction": {"score": 0.9}}
    assert token not in getattr(builtins, "_debugger_mcp_frame_eval_frames", {})


def test_frame_eval_single_expression_preserves_identity() -> None:
    token = new_frame_token()
    value = object()
    selector = FrameSelector("model.py", 7, "forward")
    eval(
        capture_frame_expression(
            token, selector, _metadata("model.py", 7, "forward", 0)
        ),
        globals(),
        locals(),
    )
    result = eval(
        execute_with_frames_expression(
            token, 'frame_eval("value", "model.py", 7, "forward")'
        ),
        globals(),
        locals(),
    )
    assert result is value


def test_frame_eval_rejects_non_active_selector_and_cleans_registry() -> None:
    token = new_frame_token()
    selector = FrameSelector("loss.py", 40, "calculate_loss")
    eval(
        capture_frame_expression(
            token, selector, _metadata("loss.py", 40, "calculate_loss", 0)
        ),
        globals(),
        locals(),
    )
    try:
        eval(
            execute_with_frames_expression(
                token,
                'frame_eval("raw", "returned_loader.py", 12, "load_image")',
            ),
            globals(),
            locals(),
        )
    except RuntimeError as exc:
        message = str(exc)
    else:
        raise AssertionError("frame_eval must not resolve a frame outside the active stack")
    assert "selector was not captured" in message
    assert "#0 loss.py:40 calculate_loss" in message
    assert token not in getattr(builtins, "_debugger_mcp_frame_eval_frames", {})


def test_frame_eval_extracts_literal_selectors_and_deduplicates() -> None:
    selectors = extract_frame_eval_selectors('''
expr = "image"
a = frame_eval(expr, "model.py", 35, "forward")
b = frame_eval("meta", file="loader.py", line=18, function="load")
c = frame_eval("again", "model.py", 35, "forward")
''')
    assert selectors == (
        FrameSelector("model.py", 35, "forward"),
        FrameSelector("loader.py", 18, "load"),
    )


def test_frame_eval_rejects_dynamic_selectors() -> None:
    for code, expected in (
        ('frame_eval("x", target_file, 35, "forward")', "file must be"),
        ('frame_eval("x", "model.py", target_line, "forward")', "line must be"),
        ('frame_eval("x", "model.py", 35, target_function)', "function must be"),
    ):
        try:
            extract_frame_eval_selectors(code)
        except ValueError as exc:
            assert expected in str(exc)
        else:
            raise AssertionError("dynamic frame_eval selectors must be rejected")


class FakeSession:
    def __init__(self, workspace: Path) -> None:
        self.workspace_root = str(workspace)
        self.state = SimpleNamespace(stopped_thread_id=1)
        self.frames = [
            {
                "id": 5,
                "name": "loss",
                "line": 80,
                "source": {"path": str(workspace / "loss.py")},
            },
            {
                "id": 8,
                "name": "forward",
                "line": 35,
                "source": {"path": str(workspace / "model.py")},
            },
        ]
        self.evaluate_calls: list[tuple[dict, float | None]] = []
        self.stack_calls = 0

    def cached_top_frame(self, thread_id):
        return None

    async def stack_trace(self, thread_id, start_frame=0, levels=None):
        assert thread_id == 1
        self.stack_calls += 1
        selected = self.frames[start_frame:] if levels is None else self.frames[start_frame:start_frame + levels]
        return {"stackFrames": selected, "totalFrames": len(self.frames)}

    async def request(self, command, arguments=None, timeout_seconds=None):
        assert command == "evaluate"
        self.evaluate_calls.append((arguments, timeout_seconds))
        return {"result": "combined"}


def test_evaluate_injects_frame_eval_and_executes_on_top(tmp_path: Path) -> None:
    session = FakeSession(tmp_path)
    session.frames.extend(
        {
            "id": 100 + index,
            "name": f"unrelated_{index}",
            "line": index + 1,
            "source": {"path": str(tmp_path / f"unrelated_{index}.py")},
        }
        for index in range(25)
    )
    tools = DebuggerTools(SimpleNamespace())
    tools._session = lambda **kwargs: session
    result = asyncio.run(tools.evaluate(
        '''
first = frame_eval("image", "model.py", 35, "forward")
second = frame_eval("image", file="model.py", line=35, function="forward")
assert first is second
''',
        timeout_seconds=90,
    ))
    assert result == {"__mcp_text__": "combined"}
    assert session.stack_calls == 1
    assert [call[0]["frameId"] for call in session.evaluate_calls] == [8, 5]
    assert session.evaluate_calls[0][1] is None
    assert session.evaluate_calls[1][1] == 90
    final_expression = session.evaluate_calls[1][0]["expression"]
    assert "frame_eval" in final_expression
    assert "model.py" in final_expression


def test_ordinary_evaluate_keeps_single_frame_path(tmp_path: Path) -> None:
    session = FakeSession(tmp_path)
    tools = DebuggerTools(SimpleNamespace())
    tools._session = lambda **kwargs: session
    result = asyncio.run(tools.evaluate("prediction"))
    assert result == {"__mcp_text__": "combined"}
    assert len(session.evaluate_calls) == 1
    assert session.evaluate_calls[0][0] == {
        "expression": "prediction",
        "frameId": 5,
        "context": "repl",
    }


def test_evaluate_rejects_dynamic_frame_eval_selector_before_dap(tmp_path: Path) -> None:
    session = FakeSession(tmp_path)
    tools = DebuggerTools(SimpleNamespace())
    tools._session = lambda **kwargs: session
    try:
        asyncio.run(tools.evaluate(
            'frame_eval("image", target_file, 35, "forward")'
        ))
    except ValueError as exc:
        message = str(exc)
    else:
        raise AssertionError("dynamic frame_eval selector must fail before DAP")
    assert "file must be a non-empty string literal" in message
    assert not session.evaluate_calls


def test_evaluate_rejects_ambiguous_active_frame_before_capture(tmp_path: Path) -> None:
    session = FakeSession(tmp_path)
    session.frames.append(dict(session.frames[1], id=9))
    tools = DebuggerTools(SimpleNamespace())
    tools._session = lambda **kwargs: session
    try:
        asyncio.run(tools.evaluate(
            'frame_eval("image", "model.py", 35, "forward")'
        ))
    except Exception as exc:
        message = str(exc)
    else:
        raise AssertionError("recursive/duplicate frame selector must be rejected")
    assert "Multiple current stack frames exactly match frame_eval selector" in message
    assert not session.evaluate_calls


def test_evaluate_cleans_registry_when_target_capture_fails(tmp_path: Path) -> None:
    class FailingSession(FakeSession):
        async def request(self, command, arguments=None, timeout_seconds=None):
            assert command == "evaluate"
            self.evaluate_calls.append((arguments, timeout_seconds))
            if arguments["frameId"] == 11:
                raise DAPError("capture failed")
            return {"result": "combined"}

    session = FailingSession(tmp_path)
    session.frames.append({
        "id": 11,
        "name": "load",
        "line": 18,
        "source": {"path": str(tmp_path / "loader.py")},
    })
    tools = DebuggerTools(SimpleNamespace())
    tools._session = lambda **kwargs: session
    try:
        asyncio.run(tools.evaluate('''
a = frame_eval("image", "model.py", 35, "forward")
b = frame_eval("meta", "loader.py", 18, "load")
'''))
    except DAPError as exc:
        assert "capture failed" in str(exc)
    else:
        raise AssertionError("target capture failure must propagate")
    assert [call[0]["frameId"] for call in session.evaluate_calls] == [8, 11, 5]
    assert "pop" in session.evaluate_calls[-1][0]["expression"]
