from __future__ import annotations

import asyncio
import builtins
import gc
import json
import os
import sys
import weakref
from pathlib import Path

from debugger_mcp.god_runner.launch import load_launch_config
from debugger_mcp.frame_eval import FrameSelector
from debugger_mcp.god_runner.recipe import (
    capture_expression,
    direct_execution_expression,
    execution_expression,
    normalize_breakpoints,
)
from debugger_mcp.god_runner.runner import GodRunner
from debugger_mcp.dap import read_dap_message, write_dap_message


def test_launch_jsonc_reuses_named_configuration(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("GOD_TEST_VALUE", "visible")
    vscode = tmp_path / ".vscode"
    vscode.mkdir()
    (vscode / "launch.json").write_text('''
    {
      // Existing VS Code format, comments and trailing commas included.
      "version": "0.2.0",
      "configurations": [{
        "name": "infer",
        "type": "debugpy",
        "request": "launch",
        "program": "${workspaceFolder}/infer.py",
        "console": "integratedTerminal",
        "debugServer": 4711,
        "env": {"VALUE": "${env:GOD_TEST_VALUE}",},
      }],
    }
    ''', encoding="utf-8")
    config = load_launch_config(tmp_path, "infer")
    assert config["program"] == str(tmp_path / "infer.py")
    assert config["console"] == "integratedTerminal"
    assert config["env"]["VALUE"] == "visible"
    assert config["env"]["DEBUGGER_MCP_WORKSPACE"] == str(tmp_path)
    assert "debugServer" not in config


def test_breakpoints_keep_dap_shape_and_remove_only_god_fields(tmp_path: Path) -> None:
    groups = [{
        "source": {"path": "model.py"},
        "breakpoints": [{
            "line": 8,
            "condition": "step > 2",
            "hitCondition": "% 3",
            "name": "dump",
            "action": "result = tensor.cpu()",
        }],
    }]
    [(arguments, actions)] = normalize_breakpoints(groups, tmp_path)
    assert arguments["source"]["path"] == str((tmp_path / "model.py").resolve())
    assert arguments["breakpoints"] == [{"line": 8, "condition": "step > 2", "hitCondition": "% 3"}]
    assert actions[0].name == "dump"
    assert actions[0].code == "result = tensor.cpu()"
    assert actions[0].frame_selectors == ()


def test_recipe_extracts_literal_selectors_and_rejects_dynamic_ones(tmp_path: Path) -> None:
    groups = [{
        "source": {"path": "model.py"},
        "breakpoints": [{
            "line": 8,
            "action": '''
a = frame_eval(expr, "loader.py", 20, "load")
b = frame_eval("other", file="loader.py", line=20, function="load")
''',
        }],
    }]
    [(_, [action])] = normalize_breakpoints(groups, tmp_path)
    assert action.frame_selectors == (FrameSelector("loader.py", 20, "load"),)

    groups[0]["breakpoints"][0]["action"] = (
        'frame_eval("x", target_file, 20, "load")'
    )
    try:
        normalize_breakpoints(groups, tmp_path)
    except ValueError as exc:
        message = str(exc)
    else:
        raise AssertionError("recipe must reject a dynamic frame selector")
    assert "action: frame_eval file must be a non-empty string literal" in message

    groups[0]["breakpoints"][0]["action"] = "if broken syntax"
    try:
        normalize_breakpoints(groups, tmp_path)
    except ValueError as exc:
        assert "action:" in str(exc)
    else:
        raise AssertionError("recipe must reject invalid Python before launch")


def test_god_runner_captures_only_unique_target_frames(tmp_path: Path) -> None:
    class FakeClient:
        def __init__(self) -> None:
            self.calls: list[tuple[str, dict, float | None]] = []

        async def request(self, command, arguments=None, timeout=None):
            self.calls.append((command, arguments or {}, timeout))
            return {"result": "None"}

    runner = GodRunner(
        workspace=tmp_path,
        launch={"name": "fake", "request": "launch", "program": "train.py"},
        breakpoints=[{
            "source": {"path": "top.py"},
            "breakpoints": [{
                "line": 40,
                "name": "selective",
                "action": '''
a = frame_eval("tensor", "caller.py", 20, "caller")
b = frame_eval("tensor", file="caller.py", line=20, function="caller")
''',
            }],
        }],
        artifacts_dir=tmp_path / "artifacts",
        continue_on_error=False,
    )
    [(_, [action])] = runner.breakpoint_groups
    frames = [
        {"id": 5, "name": "top", "line": 40, "source": {"path": str(tmp_path / "top.py")}},
        {"id": 8, "name": "caller", "line": 20, "source": {"path": str(tmp_path / "caller.py")}},
        {"id": 13, "name": "other", "line": 7, "source": {"path": str(tmp_path / "other.py")}},
    ]
    client = FakeClient()
    asyncio.run(runner._execute_action(client, 1, frames, action))
    assert [arguments["frameId"] for _, arguments, _ in client.calls] == [8, 5]
    assert "_god_runner_frames" in client.calls[0][1]["expression"]
    assert "<god_runner:selective>" in client.calls[1][1]["expression"]


def test_dynamic_frame_eval_returns_real_cross_frame_objects(tmp_path: Path) -> None:
    token = "stop-1"
    caller_tensor = {"real": [1, 2, 3]}
    selector = FrameSelector("pipeline.py", 20, "caller")

    def caller() -> dict:
        tensor = caller_tensor
        caller_frame = {
            "source": {"path": str(tmp_path / "pipeline.py")},
            "line": 20,
            "name": "caller",
        }
        eval(
            capture_expression(token, selector, 1, caller_frame, tmp_path),
            globals(),
            locals(),
        )
        return current()

    def current() -> dict:
        marker = "top"
        code = '''
items = [frame_eval("tensor", "pipeline.py", 20, "caller") for _ in range(2)]
assert items[0] is items[1]
assert items[0] is frame_eval("tensor", "pipeline.py", 20, "caller")
items[0]["mutated"] = marker
'''
        expression = execution_expression(token, code, "dynamic")
        assert "def frame_eval(expression, file, line, function):" in expression
        assert "evaluate = frame_eval" not in expression
        eval(expression, globals(), locals())
        return caller_tensor

    result = caller()
    assert result["mutated"] == "top"
    import builtins
    assert token not in getattr(builtins, "_god_runner_frames", {})


def test_recipe_traceback_uses_probe_filename(tmp_path: Path) -> None:
    try:
        eval(
            direct_execution_expression("raise RuntimeError('boom')", "probe-x"),
            globals(),
            locals(),
        )
    except RuntimeError as exc:
        import traceback
        rendered = "".join(traceback.format_exception(exc))
    else:
        raise AssertionError("recipe should fail")
    assert "<god_runner:probe-x>" in rendered
    assert "RuntimeError: boom" in rendered


def test_direct_recipe_action_uses_current_breakpoint_locals() -> None:
    target: dict[str, str] = {}
    marker = "top-frame"
    eval(
        direct_execution_expression('target["value"] = marker', "direct"),
        globals(),
        locals(),
    )
    assert target == {"value": "top-frame"}


def test_direct_recipe_action_shares_top_level_names_with_nested_scopes() -> None:
    cases = {
        "lambda": "data = [1, 2, 3]\nread = lambda: data\ntarget['value'] = read()",
        "nested-def": "data = [1, 2, 3]\ndef read():\n    return data\ntarget['value'] = read()",
        "generator": "scale = 10\ntarget['value'] = list(x * scale for x in [1, 2, 3])",
        "comprehension": "scale = 10\ntarget['value'] = [x * scale for x in [1, 2, 3]]",
    }
    for name, code in cases.items():
        target: dict[str, object] = {}
        eval(direct_execution_expression(code, name), globals(), locals())
        expected = [1, 2, 3] if name in {"lambda", "nested-def"} else [10, 20, 30]
        assert target == {"value": expected}


class _ActionPayload:
    pass


def _action_expression(mode: str, token: str, code: str) -> str:
    if mode == "direct":
        return direct_execution_expression(code, mode)
    builtins.__dict__.setdefault("_god_runner_frames", {})[token] = {}
    return execution_expression(token, code, mode)


def _run_action_with_payload(mode: str, index: int, code: str):
    token = f"namespace-{mode}-{index}"
    payload = _ActionPayload()
    payload_ref = weakref.ref(payload)
    frame_globals = {"__builtins__": builtins.__dict__}
    merged = dict(frame_globals)
    merged.update({"payload": payload, "globals": lambda: frame_globals})
    error_type = error_message = None
    try:
        eval(_action_expression(mode, token, code), merged, None)
    except Exception as exc:
        error_type, error_message = type(exc), str(exc)
    finally:
        builtins.__dict__.get("_god_runner_frames", {}).pop(token, None)
        del merged
        del payload
    return payload_ref, error_type, error_message


def test_action_namespaces_release_frame_locals_without_cyclic_gc() -> None:
    code = "def read():\n    return payload\nassert read() is payload"
    gc.collect()
    was_enabled = gc.isenabled()
    gc.disable()
    try:
        for mode in ("direct", "frame_eval"):
            results = [_run_action_with_payload(mode, index, code) for index in range(100)]
            assert all(error_type is None for _, error_type, _ in results)
            refs = [ref for ref, _, _ in results]
            assert all(ref() is None for ref in refs)
    finally:
        if was_enabled:
            gc.enable()
        gc.collect()


def test_action_namespaces_release_frame_locals_on_error() -> None:
    code = "def read():\n    return payload\nraise RuntimeError('expected')"
    for mode in ("direct", "frame_eval"):
        payload_ref, error_type, error_message = _run_action_with_payload(mode, 0, code)
        assert error_type is RuntimeError
        assert error_message == "expected"
        assert payload_ref() is None


def test_headless_dap_lifecycle_executes_and_continues(tmp_path: Path) -> None:
    async def scenario() -> None:
        seen: list[str] = []

        async def fake_adapter(reader, writer) -> None:
            lock = asyncio.Lock()
            launch_request = None
            seq = 100

            async def response(request, body=None):
                nonlocal seq
                seq += 1
                await write_dap_message(writer, {
                    "seq": seq, "type": "response", "request_seq": request["seq"],
                    "command": request["command"], "success": True, "body": body or {},
                }, lock)

            async def event(name, body=None):
                nonlocal seq
                seq += 1
                await write_dap_message(writer, {"seq": seq, "type": "event", "event": name, "body": body or {}}, lock)

            try:
                while True:
                    request = await read_dap_message(reader)
                    command = request["command"]
                    seen.append(command)
                    if command == "initialize":
                        await response(request)
                    elif command == "launch":
                        launch_request = request
                        await event("initialized")
                    elif command == "setBreakpoints":
                        await response(request, {"breakpoints": [{"id": 42, "verified": True, "line": 3}]})
                    elif command == "configurationDone":
                        await response(request)
                        assert launch_request is not None
                        await response(launch_request)
                        await event("process", {"name": "fake", "systemProcessId": 123})
                        await event("stopped", {"reason": "breakpoint", "threadId": 7, "hitBreakpointIds": [42]})
                    elif command == "stackTrace":
                        await response(request, {"stackFrames": [{
                            "id": 9, "name": "work", "line": 3,
                            "source": {"path": str(tmp_path / "program.py")},
                        }], "totalFrames": 1})
                    elif command == "evaluate":
                        await response(request, {"result": "None"})
                    elif command == "continue":
                        await response(request, {"allThreadsContinued": True})
                        await event("exited", {"exitCode": 0})
                        return
            finally:
                writer.close()
                await writer.wait_closed()

        server = await asyncio.start_server(fake_adapter, "127.0.0.1", 0)
        port = server.sockets[0].getsockname()[1]
        runner = GodRunner(
            workspace=tmp_path,
            launch={"name": "fake", "request": "launch", "program": str(tmp_path / "program.py")},
            breakpoints=[{
                "source": {"path": str(tmp_path / "program.py")},
                "breakpoints": [{"line": 3, "name": "capture", "action": "observed = value"}],
            }],
            artifacts_dir=tmp_path / "artifacts",
            continue_on_error=False,
        )
        try:
            await runner._run_session("127.0.0.1", port, dict(runner.launch), root=True)
        finally:
            server.close()
            await server.wait_closed()
        assert seen == ["initialize", "launch", "setBreakpoints", "configurationDone", "stackTrace", "evaluate", "continue"]
        [record] = [json.loads(line) for line in (tmp_path / "artifacts" / "god_runner_manifest.jsonl").read_text().splitlines()]
        assert record["probe"] == "capture"
        assert record["status"] == "ok"
        assert record["process"]["systemProcessId"] == 123

    asyncio.run(scenario())


def test_launch_run_in_terminal_is_answered_and_inherits_output(tmp_path: Path) -> None:
    async def scenario() -> None:
        marker = tmp_path / "terminal-result.txt"
        launch_request: dict | None = None
        reverse_seq = 500
        reverse_response: dict | None = None

        async def fake_adapter(reader, writer) -> None:
            nonlocal launch_request, reverse_response
            lock = asyncio.Lock()
            seq = 100

            async def response(request, body=None):
                nonlocal seq
                seq += 1
                await write_dap_message(writer, {
                    "seq": seq, "type": "response", "request_seq": request["seq"],
                    "command": request["command"], "success": True, "body": body or {},
                }, lock)

            async def event(name, body=None):
                nonlocal seq
                seq += 1
                await write_dap_message(writer, {
                    "seq": seq, "type": "event", "event": name, "body": body or {},
                }, lock)

            try:
                while True:
                    message = await read_dap_message(reader)
                    if message.get("type") == "response":
                        if message.get("request_seq") == reverse_seq:
                            reverse_response = message
                            assert launch_request is not None
                            await response(launch_request)
                            await event("exited", {"exitCode": 0})
                            return
                        continue
                    command = message["command"]
                    if command == "initialize":
                        await response(message)
                    elif command == "launch":
                        launch_request = message
                        assert message["arguments"]["console"] == "integratedTerminal"
                        await event("initialized")
                    elif command == "setBreakpoints":
                        await response(message, {"breakpoints": []})
                    elif command == "configurationDone":
                        await response(message)
                        script = (
                            "from pathlib import Path; import os; "
                            f"Path({str(marker)!r}).write_text(os.environ['GOD_RIT_TEST'])"
                        )
                        await write_dap_message(writer, {
                            "seq": reverse_seq,
                            "type": "request",
                            "command": "runInTerminal",
                            "arguments": {
                                "kind": "integrated",
                                "cwd": str(tmp_path),
                                "args": [sys.executable, "-c", script],
                                "env": {"GOD_RIT_TEST": "visible"},
                            },
                        }, lock)
            finally:
                writer.close()
                await writer.wait_closed()

        server = await asyncio.start_server(fake_adapter, "127.0.0.1", 0)
        port = server.sockets[0].getsockname()[1]
        runner = GodRunner(
            workspace=tmp_path,
            launch={
                "name": "terminal",
                "request": "launch",
                "program": str(tmp_path / "program.py"),
                "console": "integratedTerminal",
            },
            breakpoints=[],
            artifacts_dir=tmp_path / "artifacts",
            continue_on_error=False,
        )
        try:
            await runner._run_session("127.0.0.1", port, dict(runner.launch), root=True)
            for _ in range(100):
                if marker.exists():
                    break
                await asyncio.sleep(0.01)
        finally:
            server.close()
            await server.wait_closed()
        assert reverse_response is not None
        assert reverse_response["success"] is True
        assert isinstance(reverse_response["body"]["processId"], int)
        assert marker.read_text(encoding="utf-8") == "visible"

    asyncio.run(scenario())


def test_terminal_cleanup_kills_worker_after_launcher_exits(tmp_path: Path) -> None:
    if os.name != "posix" or not Path("/proc").is_dir():
        return

    async def scenario() -> None:
        class FakeClient:
            def __init__(self) -> None:
                self.responses: list[tuple[bool, dict | None]] = []

            async def respond(self, request, success, message=None, body=None) -> None:
                self.responses.append((success, body))

        child_pid_path = tmp_path / "worker.pid"
        worker_code = "import time; time.sleep(60)"
        launcher_code = (
            "from pathlib import Path; import subprocess, sys; "
            f"worker = subprocess.Popen([sys.executable, '-c', {worker_code!r}]); "
            f"Path({str(child_pid_path)!r}).write_text(str(worker.pid))"
        )
        runner = GodRunner(
            workspace=tmp_path,
            launch={"name": "fake", "request": "launch", "program": "train.py"},
            breakpoints=[],
            artifacts_dir=tmp_path / "artifacts",
            continue_on_error=False,
        )
        runner.TERMINATE_GRACE_SECONDS = 0.5
        runner.KILL_GRACE_SECONDS = 0.5
        client = FakeClient()
        await runner._run_in_terminal(client, {
            "type": "request",
            "seq": 1,
            "command": "runInTerminal",
            "arguments": {
                "cwd": str(tmp_path),
                "args": [sys.executable, "-c", launcher_code],
            },
        }, 1)
        terminal = runner._terminal_processes[0]
        try:
            await terminal.process.wait()
            for _ in range(100):
                if child_pid_path.exists():
                    break
                await asyncio.sleep(0.01)
            child_pid = int(child_pid_path.read_text(encoding="utf-8"))
            assert os.getpgid(child_pid) == terminal.process_group_id
            assert terminal.process.returncode == 0
            assert runner._terminal_alive(terminal)
            await runner._terminate_terminal_processes()
            for _ in range(100):
                stat = Path(f"/proc/{child_pid}/stat")
                if not stat.exists() or stat.read_text(encoding="utf-8").split()[2] == "Z":
                    break
                await asyncio.sleep(0.01)
            stat = Path(f"/proc/{child_pid}/stat")
            assert not stat.exists() or stat.read_text(encoding="utf-8").split()[2] == "Z"
            assert runner._terminal_processes == []
        finally:
            if runner._terminal_processes:
                await runner._terminate_terminal_processes()

    asyncio.run(scenario())


def test_runner_always_cleans_terminal_groups(tmp_path: Path) -> None:
    class FakeDaemon:
        async def ensure_daemon(self):
            return {"dap_host": "127.0.0.1", "dap_port": 4711}

    class LifecycleRunner(GodRunner):
        def __init__(self, *, fail: bool) -> None:
            super().__init__(
                workspace=tmp_path,
                launch={"name": "fake", "request": "launch", "program": "train.py"},
                breakpoints=[],
                artifacts_dir=tmp_path / "artifacts",
                continue_on_error=False,
            )
            self._daemon_client = FakeDaemon()
            self.fail = fail
            self.cleaned = False

        async def _run_session(self, host, port, launch, *, root) -> None:
            if self.fail:
                raise RuntimeError("launch failed")

        async def _terminate_terminal_processes(self) -> None:
            self.cleaned = True

    async def scenario() -> None:
        success = LifecycleRunner(fail=False)
        await success.run()
        assert success.cleaned

        failure = LifecycleRunner(fail=True)
        try:
            await failure.run()
        except RuntimeError as exc:
            assert str(exc) == "launch failed"
        else:
            raise AssertionError("failed launch must propagate")
        assert failure.cleaned

        cancelled = LifecycleRunner(fail=False)

        async def cancel_session(host, port, launch, *, root) -> None:
            raise asyncio.CancelledError

        cancelled._run_session = cancel_session
        try:
            await cancelled.run()
        except asyncio.CancelledError:
            pass
        else:
            raise AssertionError("cancelled launch must propagate")
        assert cancelled.cleaned

    asyncio.run(scenario())


def test_child_start_debugging_consumes_debug_server_and_surfaces_errors(tmp_path: Path) -> None:
    async def scenario() -> None:
        class FakeClient:
            def __init__(self) -> None:
                self.responses: list[tuple[bool, dict | None]] = []

            async def respond(self, request, success, message=None, body=None) -> None:
                self.responses.append((success, body))

        class FailingChildRunner(GodRunner):
            def __init__(self, **kwargs) -> None:
                super().__init__(**kwargs)
                self.child_configuration: dict | None = None

            async def _run_session(self, host, port, launch, *, root) -> None:
                self.child_configuration = launch
                raise RuntimeError("child attach failed")

        runner = FailingChildRunner(
            workspace=tmp_path,
            launch={"name": "root", "request": "launch", "program": "train.py"},
            breakpoints=[],
            artifacts_dir=tmp_path / "artifacts",
            continue_on_error=False,
        )
        client = FakeClient()
        await runner._handle_reverse_request(client, {
            "seq": 9,
            "type": "request",
            "command": "startDebugging",
            "arguments": {
                "configuration": {
                    "request": "attach",
                    "debugServer": 4711,
                    "subProcessId": 321,
                },
            },
        }, "127.0.0.1", 4711, 1)
        [task] = list(runner._children)
        await asyncio.gather(task, return_exceptions=True)
        error = await asyncio.wait_for(runner._background_errors.get(), 1.0)
        assert runner.child_configuration == {
            "request": "attach",
            "subProcessId": 321,
        }
        assert client.responses == [(True, {})]
        assert str(error) == "child attach failed"

    asyncio.run(scenario())
