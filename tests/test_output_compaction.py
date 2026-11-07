from types import SimpleNamespace

from debugger_mcp.tools import DebuggerTools


def _session(workspace):
    return SimpleNamespace(workspace_root=str(workspace))


def test_frame_line_is_workspace_relative_and_minimal(tmp_path):
    session = _session(tmp_path)
    frame = {
        "id": 5,
        "name": "forward",
        "line": 1297,
        "source": {"path": str(tmp_path / "projects" / "vfm" / "head.py")},
    }
    assert DebuggerTools._format_frame_line(session, frame, stack_index=0) == (
        "#0 projects/vfm/head.py:1297 forward"
    )


def test_frame_location_has_no_session_or_dap_metadata(tmp_path):
    session = _session(tmp_path)
    frame = {
        "id": 5,
        "name": "forward",
        "line": 1297,
        "column": 1,
        "endLine": None,
        "source": {"path": str(tmp_path / "head.py"), "name": "head.py"},
        "presentationHint": None,
    }
    text = DebuggerTools._format_frame_location(session, frame)
    assert text == "head.py:1297 forward"
    for noisy in ["session_id", "thread_id", "column", "presentation", "workspace_root"]:
        assert noisy not in text


def test_get_stack_default_preserves_all_frames_even_if_adapter_pages(tmp_path):
    import asyncio
    from types import SimpleNamespace

    class FakeSession:
        def __init__(self):
            self.workspace_root = str(tmp_path)
            self.state = SimpleNamespace(stopped_thread_id=1)
            self.calls = []

        def cached_top_frame(self, thread_id):
            return None

        async def stack_trace(self, thread_id, start_frame=0, levels=None):
            self.calls.append((thread_id, start_frame, levels))
            frames = [
                {
                    "id": i + 100,
                    "name": f"fn_{i}",
                    "line": i + 1,
                    "source": {"path": str(tmp_path / "pkg" / f"m{i}.py")},
                }
                for i in range(12)
            ]
            if start_frame == 0 and levels is None:
                # Simulate an adapter that returns only the first page while
                # advertising the complete depth.
                return {"stackFrames": frames[:3], "totalFrames": 12}
            end = len(frames) if levels is None else min(len(frames), start_frame + levels)
            return {"stackFrames": frames[start_frame:end], "totalFrames": 12}

    session = FakeSession()
    tools = DebuggerTools(SimpleNamespace())
    tools._session = lambda **kwargs: session
    result = asyncio.run(tools.get_stack())
    lines = result["__mcp_text__"].splitlines()
    assert len(lines) == 12
    assert lines[0] == "#0 pkg/m0.py:1 fn_0"
    assert lines[-1] == "#11 pkg/m11.py:12 fn_11"
    assert not any("more frames" in line for line in lines)
    assert session.calls == [(1, 0, None), (1, 3, 9)]


def test_get_stack_explicit_levels_may_return_requested_slice(tmp_path):
    import asyncio
    from types import SimpleNamespace

    class FakeSession:
        workspace_root = str(tmp_path)
        state = SimpleNamespace(stopped_thread_id=1)

        def cached_top_frame(self, thread_id):
            return None

        async def stack_trace(self, thread_id, start_frame=0, levels=None):
            frames = [
                {
                    "id": i,
                    "name": f"f{i}",
                    "line": i + 1,
                    "source": {"path": str(tmp_path / "x.py")},
                }
                for i in range(5)
            ]
            end = start_frame + (levels or len(frames))
            return {"stackFrames": frames[start_frame:end], "totalFrames": 5}

    tools = DebuggerTools(SimpleNamespace())
    tools._session = lambda **kwargs: FakeSession()
    result = asyncio.run(tools.get_stack(levels=2))
    assert result["__mcp_text__"].splitlines() == [
        "#0 x.py:1 f0",
        "#1 x.py:2 f1",
        "... +3 more frames (explicit levels=2)",
    ]


def test_evaluate_normal_result_is_never_truncated(tmp_path):
    import asyncio
    from types import SimpleNamespace

    long_result = "R" * 50000 + "FINAL_RESULT_MARKER"

    class FakeSession:
        workspace_root = str(tmp_path)
        state = SimpleNamespace(stopped_thread_id=1)

        def cached_top_frame(self, thread_id):
            return None

        async def stack_trace(self, thread_id, start_frame=0, levels=None):
            return {"stackFrames": [{"id": 5, "name": "f", "line": 1}]}

        async def request(self, command, arguments=None, timeout_seconds=None):
            assert command == "evaluate"
            assert arguments["frameId"] == 5
            assert arguments["context"] == "repl"
            return {"result": long_result, "type": "str", "variablesReference": 0}

    tools = DebuggerTools(SimpleNamespace())
    tools._session = lambda **kwargs: FakeSession()
    result = asyncio.run(tools.evaluate("x"))
    assert result == {"__mcp_text__": long_result}
    assert result["__mcp_text__"].endswith("FINAL_RESULT_MARKER")


def test_step_default_keeps_location_reason_and_thread(tmp_path):
    import asyncio
    from types import SimpleNamespace

    state = SimpleNamespace(
        stopped_thread_id=7,
        stop_reason="breakpoint",
        last_event={"event": "stopped", "body": {"reason": "breakpoint", "threadId": 7}},
    )

    class FakeSession:
        workspace_root = str(tmp_path)

        def __init__(self):
            self.state = state

        def prepare_for_step(self):
            pass

        async def request(self, command, arguments=None, timeout_seconds=None):
            assert command == "next"
            assert arguments == {"threadId": 7}
            return {}

        async def wait_until_stopped(self, timeout):
            return state

        def cached_top_frame(self, thread_id):
            return None

        async def stack_trace(self, thread_id, start_frame=0, levels=None):
            return {
                "stackFrames": [
                    {
                        "id": 9,
                        "name": "forward",
                        "line": 42,
                        "source": {"path": str(tmp_path / "model.py")},
                    }
                ],
                "totalFrames": 1,
            }

    tools = DebuggerTools(SimpleNamespace())
    tools._session = lambda **kwargs: FakeSession()
    result = asyncio.run(tools.step_over())
    assert result == {"__mcp_text__": "model.py:42 forward | reason=breakpoint thread=7"}


def test_get_stack_full_keeps_raw_frame_fields(tmp_path):
    import asyncio
    from types import SimpleNamespace

    raw = {
        "id": 5,
        "name": "forward",
        "line": 10,
        "column": 7,
        "endLine": 12,
        "endColumn": 9,
        "source": {"name": "head.py", "path": str(tmp_path / "head.py"), "sourceReference": 0},
        "moduleId": "module-x",
        "presentationHint": "subtle",
        "customFutureField": {"x": 1},
    }

    class FakeSession:
        workspace_root = str(tmp_path)
        state = SimpleNamespace(stopped_thread_id=1)

        def metadata(self):
            return {"session_id": 3}

        def cached_top_frame(self, thread_id):
            return None

        async def stack_trace(self, thread_id, start_frame=0, levels=None):
            return {"stackFrames": [raw], "totalFrames": 1}

    tools = DebuggerTools(SimpleNamespace())
    tools._session = lambda **kwargs: FakeSession()
    result = asyncio.run(tools.get_stack(full=True))
    assert result["stack_frames"] == [raw]
    assert result["stack_frames"][0]["customFutureField"] == {"x": 1}
