"""Run manually: python tests/integration_v040.py"""

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
        self.task = asyncio.create_task(self._read())

    async def _read(self):
        while True:
            message = await read_dap_message(self.reader)
            if message["type"] == "response":
                self.pending.pop(message["request_seq"]).set_result(message)
            elif message["type"] == "event":
                await self.events.put(message)
            elif message["type"] == "request" and message["command"] == "runInTerminal":
                raise RuntimeError("test uses internalConsole")

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
    client = DaemonClient(
        host=daemon.control_host,
        port=daemon.control_port,
        dap_host=daemon.proxy.host,
        dap_port=daemon.proxy.port,
        spawn_if_missing=False,
        workspace_root=str(ROOT),
    )

    reader, writer = await asyncio.open_connection(daemon.proxy.host, daemon.proxy.port)
    vscode = TinyDAPClient(reader, writer)
    await vscode.request(
        "initialize",
        {
            "clientID": "v040",
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
                "name": "v040",
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
    breakpoint_response = await vscode.request(
        "setBreakpoints",
        {
            "source": {"path": str(ROOT / "examples" / "demo.py")},
            "breakpoints": [{"line": 9}, {"line": 10}],
            "sourceModified": False,
        },
    )
    assert len(breakpoint_response["body"]["breakpoints"]) == 2
    await vscode.request("configurationDone")
    await launch_task
    await vscode.event("stopped")

    summary = await client.call(
        "inspect_structure",
        {
            "expression": "{'values': values, 'nested': {'a': list(range(1000))}}",
            "profile": "summary",
        },
    )
    assert "__mcp_text__" in summary
    assert len(summary["__mcp_text__"]) < 7000
    assert "__mcp_structured__" not in summary
    assert "items skipped" in summary["__mcp_text__"]

    stopped_early = await client.call(
        "run_to_line",
        {
            "file": str(ROOT / "examples" / "demo.py"),
            "line": 11,
            "reason": "Same function and forward control flow; an existing user breakpoint may stop first.",
            "wait_seconds": 10,
        },
    )
    assert stopped_early["__mcp_text__"].startswith("stopped before target:"), stopped_early
    assert "demo.py:10" in stopped_early["__mcp_text__"], stopped_early

    target = await client.call(
        "run_to_line",
        {
            "file": str(ROOT / "examples" / "demo.py"),
            "line": 11,
            "reason": "Same function, next forward statement, no return before the target.",
            "wait_seconds": 10,
        },
    )
    assert target["__mcp_text__"].startswith("target hit:"), target
    assert "demo.py:11" in target["__mcp_text__"], target

    session = daemon.proxy.session
    assert session is not None
    key = session._source_key({"path": str(ROOT / "examples" / "demo.py")})
    assert session._temporary_breakpoints.get(key) is None
    assert session._user_breakpoint_args[key]["breakpoints"] == [{"line": 9}, {"line": 10}]

    try:
        await client.call(
            "run_to_line",
            {
                "file": str(ROOT / "examples" / "subprocess_child.py"),
                "line": 1,
                "reason": "Cross-file target for permission enforcement test.",
            },
        )
    except DAPError as exc:
        assert "explicit user permission" in str(exc)
    else:
        raise AssertionError("cross-file run_to_line was not rejected")

    print("aggregate inspection and run_to_line integration passed")
    await vscode.request("disconnect", {"terminateDebuggee": True})
    writer.close()
    await writer.wait_closed()
    vscode.task.cancel()
    await daemon.stop()


if __name__ == "__main__":
    asyncio.run(main())
