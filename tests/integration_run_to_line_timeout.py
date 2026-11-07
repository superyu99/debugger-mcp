"""Run manually: python tests/integration_run_to_line_timeout.py"""

from __future__ import annotations

import asyncio
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from debugger_mcp.daemon import SharedDebuggerDaemon
from debugger_mcp.dap import read_dap_message, write_dap_message
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
            "clientID": "timeout",
            "adapterID": "python",
            "pathFormat": "path",
            "linesStartAt1": True,
            "columnsStartAt1": True,
            "supportsVariableType": True,
            "supportsVariablePaging": True,
            "supportsRunInTerminalRequest": False,
        },
    )
    program = ROOT / "examples" / "run_to_line_timeout.py"
    launch_task = asyncio.create_task(
        vscode.request(
            "launch",
            {
                "name": "timeout",
                "type": "debugpy",
                "request": "launch",
                "env": {"DEBUGGER_MCP_WORKSPACE": str(ROOT)},
                "program": str(program),
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
        {"source": {"path": str(program)}, "breakpoints": [{"line": 3}], "sourceModified": False},
    )
    await vscode.request("configurationDone")
    await launch_task
    await vscode.event("stopped")

    result = await client.call(
        "run_to_line",
        {
            "file": str(program),
            "line": 5,
            "reason": "Same module and straight-line execution after a finite sleep.",
            "wait_seconds": 1,
        },
    )
    assert result["__mcp_text__"].startswith("still running after"), result
    assert "temporary breakpoint remains active" in result["__mcp_text__"]

    # Simulate the user editing breakpoints while the temporary target is active.
    # The proxy must keep the hidden target in the adapter request, filter it out
    # of the VS Code response, and preserve the user's latest list on cleanup.
    changed = await vscode.request(
        "setBreakpoints",
        {
            "source": {"path": str(program)},
            "breakpoints": [{"line": 3}, {"line": 6}],
            "sourceModified": False,
        },
    )
    assert len(changed["body"]["breakpoints"]) == 2

    await vscode.event("stopped")
    for _ in range(30):
        session = daemon.proxy.session
        if session is not None and session._active_run_to_line is None:
            break
        await asyncio.sleep(0.05)
    session = daemon.proxy.session
    assert session is not None and session._active_run_to_line is None
    key = session._source_key({"path": str(program)})
    assert session._user_breakpoint_args[key]["breakpoints"] == [{"line": 3}, {"line": 6}]
    stack = await client.call("get_stack", {"levels": 1})
    assert ":5 " in stack["__mcp_text__"], stack
    print("run_to_line timeout background cleanup passed")

    await vscode.request("disconnect", {"terminateDebuggee": True})
    writer.close()
    await writer.wait_closed()
    vscode.task.cancel()
    await daemon.stop()


if __name__ == "__main__":
    asyncio.run(main())
