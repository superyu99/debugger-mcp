"""Run manually: python tests/integration_smoke.py

Starts one shared daemon in-process, connects two independent daemon clients,
launches examples/demo.py through the DAP proxy, and proves that both clients
see the same state. It also verifies that the top frame is requested only once
per stop and refreshed after a manual VS Code-style step.
"""

from __future__ import annotations

import asyncio
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from debugger_mcp.daemon import SharedDebuggerDaemon
from debugger_mcp.dap import DAPError, read_dap_message, write_dap_message
from debugger_mcp.rpc import DaemonClient


class TinyDAPClient:
    def __init__(self, reader, writer):
        self.reader = reader
        self.writer = writer
        self.lock = asyncio.Lock()
        self.seq = 0
        self.pending = {}
        self.events = asyncio.Queue()
        self.seen_events = []
        self.task = asyncio.create_task(self._read())

    async def _read(self):
        while True:
            message = await read_dap_message(self.reader)
            if message["type"] == "response":
                self.pending.pop(message["request_seq"]).set_result(message)
            elif message["type"] == "event":
                self.seen_events.append(message.get("event"))
                await self.events.put(message)
            elif message["type"] == "request" and message["command"] == "runInTerminal":
                raise RuntimeError("Smoke test uses internalConsole and should not receive runInTerminal")

    async def request(self, command, arguments=None):
        self.seq += 1
        future = asyncio.get_running_loop().create_future()
        self.pending[self.seq] = future
        await write_dap_message(
            self.writer,
            {"seq": self.seq, "type": "request", "command": command, "arguments": arguments or {}},
            self.lock,
        )
        return await asyncio.wait_for(future, 15)

    async def event(self, name):
        while True:
            event = await asyncio.wait_for(self.events.get(), 15)
            if event.get("event") == name:
                return event


async def main():
    daemon = SharedDebuggerDaemon(dap_port=0, control_port=0, debugpy_python=sys.executable)
    await daemon.start()

    client_a = DaemonClient(
        host=daemon.control_host,
        port=daemon.control_port,
        dap_host=daemon.proxy.host,
        dap_port=daemon.proxy.port,
        spawn_if_missing=False,
        workspace_root=str(ROOT),
    )
    client_b = DaemonClient(
        host=daemon.control_host,
        port=daemon.control_port,
        dap_host=daemon.proxy.host,
        dap_port=daemon.proxy.port,
        spawn_if_missing=False,
        workspace_root=str(ROOT),
    )

    ping_a, ping_b = await asyncio.gather(client_a.ping(), client_b.ping())
    assert ping_a and ping_b and ping_a["pid"] == ping_b["pid"]

    reader, writer = await asyncio.open_connection(daemon.proxy.host, daemon.proxy.port)
    vscode = TinyDAPClient(reader, writer)

    await vscode.request(
        "initialize",
        {
            "clientID": "smoke",
            "adapterID": "python",
            "pathFormat": "path",
            "linesStartAt1": True,
            "columnsStartAt1": True,
            "supportsVariableType": True,
            "supportsVariablePaging": True,
            "supportsRunInTerminalRequest": False,
        },
    )
    launch_task = asyncio.create_task(
        vscode.request(
            "launch",
            {
                "name": "smoke",
                "type": "debugpy",
                "request": "launch",
                "env": {"DEBUGGER_MCP_WORKSPACE": str(ROOT)},
                "program": str(ROOT / "examples" / "demo.py"),
                "console": "internalConsole",
                "justMyCode": True,
                "cwd": str(ROOT / "examples"),
                "python": sys.executable,
            },
        )
    )
    await vscode.event("initialized")
    await vscode.request(
        "setBreakpoints",
        {
            "source": {"path": str(ROOT / "examples" / "demo.py")},
            "breakpoints": [{"line": 9}],
            "sourceModified": False,
        },
    )
    await vscode.request("configurationDone")
    await launch_task
    await vscode.event("stopped")

    status_a, status_b = await asyncio.gather(
        client_a.call("attach_debugpy"),
        client_b.call("attach_debugpy"),
    )
    assert status_a["selected_session_id"] == status_b["selected_session_id"] == 1
    assert status_a["selected_stop_id"] == status_b["selected_stop_id"] == 1

    # One get_locals resolves the top frame. Later evaluate from another Claude
    # frontend reuses the daemon's shared frame cache.
    locals_a = await client_a.call("get_locals")
    assert "values, list, len=4" in locals_a["__mcp_text__"]
    assert "scale, int, 3" in locals_a["__mcp_text__"]

    full_locals = await client_b.call("get_locals", {"full": True})
    assert full_locals["mode"] == "full"
    full_by_name = {item["name"]: item for item in full_locals["variables_response"]["variables"]}
    assert full_by_name["values"]["value"] == "[1, 2, 3, 4]"

    session = daemon.proxy.session
    assert session is not None
    requests_after_locals = session.stack_trace_request_count
    evaluated_b = await client_b.call("evaluate", {"expression": "scale + len(values)"})
    assert evaluated_b == {"__mcp_text__": "7"}
    assert session.stack_trace_request_count == requests_after_locals

    # Agent A steps into helper. Its own execution-control result establishes
    # the new stop as seen, so the next call from A does not repeat a notice.
    # Agent B previously saw stop 1 and therefore receives one lazy notice.
    step_a = await client_a.call("step_into")
    after_own_step_a = await client_a.call("get_locals")
    stack_b = await client_b.call("get_stack", {"levels": 1})
    assert "examples/demo.py:2 helper | reason=step" in step_a["__mcp_text__"], step_a
    assert not after_own_step_a["__mcp_text__"].startswith("[Debug ")
    assert stack_b["__mcp_text__"].startswith("[Debug location changed]"), stack_b
    assert "examples/demo.py:2 helper" in stack_b["__mcp_text__"], stack_b

    # v0.9.2: every non-top-frame evaluate uses frame_eval with a selector
    # copied from the current stack. transform() owns scale while helper() is top.
    stack_a = await client_a.call("get_stack")
    stack_lines = stack_a["__mcp_text__"].splitlines()
    assert stack_lines[0].startswith("#0 examples/demo.py:2 helper"), stack_a
    transform_line = next(line for line in stack_lines if line.endswith(" transform"))
    prefix, function = transform_line.rsplit(" ", 1)
    location = prefix.split(" ", 1)[1]
    file_name, line_text = location.rsplit(":", 1)
    caller_eval = await client_a.call(
        "evaluate",
        {
            "expression": f'frame_eval("scale", {file_name!r}, {int(line_text)}, {function!r})',
        },
    )
    assert caller_eval == {"__mcp_text__": "3"}, caller_eval

    # v0.8.5: evaluate runs in helper() while frame_eval returns the
    # actual values list from the still-active transform() frame. Repeated
    # retrieval preserves identity, and the object can be combined directly
    # with helper()'s top-frame value without crossing RPC.
    combined = await client_a.call(
        "evaluate",
        {
            "expression": (
                f'(frame_eval("values", {file_name!r}, {int(line_text)}, {function!r}) is '
                f'frame_eval("values", {file_name!r}, {int(line_text)}, {function!r}), '
                f'frame_eval("values", {file_name!r}, {int(line_text)}, {function!r})[0] == value)'
            )
        },
    )
    assert combined == {"__mcp_text__": "(True, True)"}, combined

    # v0.7.1: inspect_structure uses the same precise human-readable caller
    # selector and can inspect an object/value that is not visible in #0.
    caller_inspect = await client_a.call(
        "inspect_structure",
        {
            "expression": "scale",
            "file": file_name,
            "line": int(line_text),
            "function": function,
            "profile": "summary",
        },
    )
    assert "scale: int 3" in caller_inspect["__mcp_text__"], caller_inspect

    # Simulate the user manually pressing Step Over in VS Code. The daemon sees
    # continued/stopped, increments stop_id, invalidates old frames, and lazily
    # fetches the new top frame only when a Claude client asks again.
    old_stop_id = session.state.stop_id
    stopped_thread = session.state.stopped_thread_id
    assert stopped_thread is not None
    await vscode.request("next", {"threadId": stopped_thread})
    await vscode.event("stopped")
    latest = await client_a.call("get_locals")
    assert latest["__mcp_text__"].startswith("[Debug location changed]")
    assert session.state.stop_id == old_stop_id + 1
    # The notice is consumed only for client A. A second call at the same stop
    # is clean, while client B must still receive its own notice.
    latest_again_a = await client_a.call("evaluate", {"expression": "value"})
    assert latest_again_a == {"__mcp_text__": "1"}

    # evaluate without a caller selector always resolves the latest top frame
    # after a user-controlled VS Code step. Precise caller evaluation remains
    # available through the current get_stack file + line + function selector.
    latest_eval = await client_b.call("evaluate", {"expression": "value"})
    assert latest_eval["__mcp_text__"].startswith("[Debug location changed]")
    assert latest_eval["__mcp_text__"].endswith("\n\n1")
    latest_eval_again = await client_b.call("evaluate", {"expression": "value"})
    assert latest_eval_again == {"__mcp_text__": "1"}
    old_frame_id = full_locals["frame_id"]
    try:
        await client_b.call("get_locals", {"frame_id": old_frame_id})
    except DAPError as exc:
        stale_message = str(exc)
    else:
        raise AssertionError("Expected the previous stop's frame_id to be stale")
    assert stale_message.startswith(f"Stale frame_id={old_frame_id}.")

    # The frontend standing in for VS Code must see Agent-generated state events.
    assert vscode.seen_events.count("continued") >= 2, vscode.seen_events
    assert vscode.seen_events.count("stopped") >= 3, vscode.seen_events
    print("multi-client integration smoke passed")

    await vscode.request("disconnect", {"terminateDebuggee": True})
    writer.close()
    await writer.wait_closed()
    vscode.task.cancel()
    await daemon.stop()


if __name__ == "__main__":
    asyncio.run(main())
