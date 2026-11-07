"""Run manually: python tests/integration_workspaces.py

Launches two independent VS Code debug sessions through the same daemon and
proves that two Claude/DaemonClient frontends default to their own workspace.
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
        self.task = asyncio.create_task(self._read())

    async def _read(self):
        try:
            while True:
                message = await read_dap_message(self.reader)
                if message["type"] == "response":
                    future = self.pending.pop(message["request_seq"], None)
                    if future is not None and not future.done():
                        future.set_result(message)
                elif message["type"] == "event":
                    await self.events.put(message)
                elif message["type"] == "request" and message["command"] == "runInTerminal":
                    raise RuntimeError("workspace test uses internalConsole")
        except (EOFError, asyncio.IncompleteReadError, ConnectionError):
            return

    async def request(self, command, arguments=None, timeout=20):
        self.seq += 1
        future = asyncio.get_running_loop().create_future()
        self.pending[self.seq] = future
        await write_dap_message(
            self.writer,
            {"seq": self.seq, "type": "request", "command": command, "arguments": arguments or {}},
            self.lock,
        )
        response = await asyncio.wait_for(future, timeout)
        if not response.get("success", False):
            raise RuntimeError(response.get("message") or response)
        return response

    async def event(self, name, timeout=20):
        while True:
            event = await asyncio.wait_for(self.events.get(), timeout)
            if event.get("event") == name:
                return event

    async def close(self):
        self.writer.close()
        try:
            await self.writer.wait_closed()
        except Exception:
            pass
        self.task.cancel()
        await asyncio.gather(self.task, return_exceptions=True)


async def launch(
    daemon: SharedDebuggerDaemon,
    workspace: pathlib.Path,
    script: pathlib.Path,
) -> TinyDAPClient:
    reader, writer = await asyncio.open_connection(daemon.proxy.host, daemon.proxy.port)
    vscode = TinyDAPClient(reader, writer)
    await vscode.request(
        "initialize",
        {
            "clientID": "workspace-smoke",
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
                "name": workspace.name,
                "type": "debugpy",
                "request": "launch",
                "env": {"DEBUGGER_MCP_WORKSPACE": str(workspace)},
                "program": str(script),
                "console": "internalConsole",
                "justMyCode": True,
                "cwd": str(workspace),
                "python": sys.executable,
            },
        )
    )
    await vscode.event("initialized")
    await vscode.request(
        "setBreakpoints",
        {
            "source": {"path": str(script)},
            "breakpoints": [{"line": 3}],
            "sourceModified": False,
        },
    )
    await vscode.request("configurationDone")
    await launch_task
    await vscode.event("stopped")
    return vscode


async def main():
    daemon = SharedDebuggerDaemon(dap_port=0, control_port=0, debugpy_python=sys.executable)
    await daemon.start()

    workspace_a = ROOT / "examples" / "workspace_a"
    workspace_b = ROOT / "examples" / "workspace_b"
    client_a = DaemonClient(
        host=daemon.control_host,
        port=daemon.control_port,
        dap_host=daemon.proxy.host,
        dap_port=daemon.proxy.port,
        spawn_if_missing=False,
        workspace_root=str(workspace_a),
    )
    client_b = DaemonClient(
        host=daemon.control_host,
        port=daemon.control_port,
        dap_host=daemon.proxy.host,
        dap_port=daemon.proxy.port,
        spawn_if_missing=False,
        workspace_root=str(workspace_b),
    )
    client_unknown = DaemonClient(
        host=daemon.control_host,
        port=daemon.control_port,
        dap_host=daemon.proxy.host,
        dap_port=daemon.proxy.port,
        spawn_if_missing=False,
        workspace_root=str(ROOT / "examples" / "unknown"),
    )

    vscode_a = await launch(daemon, workspace_a, workspace_a / "main.py")
    vscode_b = await launch(daemon, workspace_b, workspace_b / "main.py")

    status_a, status_b = await asyncio.gather(
        client_a.call("attach_debugpy"),
        client_b.call("attach_debugpy"),
    )
    assert status_a["session_count"] == status_b["session_count"] == 2
    assert status_a["workspace_session_count"] == status_b["workspace_session_count"] == 1
    assert status_a["selected_session_id"] != status_b["selected_session_id"]

    value_a, value_b = await asyncio.gather(
        client_a.call("evaluate", {"expression": "project_name"}),
        client_b.call("evaluate", {"expression": "project_name"}),
    )
    assert value_a["__mcp_text__"] == "'A'", value_a
    assert value_b["__mcp_text__"] == "'B'", value_b
    
    # evaluate is intentionally workspace-local in v0.6.8: its public interface
    # no longer exposes session_id, so each frontend stays on its own latest
    # stopped top frame automatically.

    unknown_status = await client_unknown.call("attach_debugpy")
    assert unknown_status["workspace_session_count"] == 0
    assert unknown_status["selected_session_id"] is None
    try:
        await client_unknown.call("get_stack", {"levels": 1})
    except DAPError as exc:
        assert "DEBUGGER_MCP_WORKSPACE" in str(exc)
    else:
        raise AssertionError("unknown workspace unexpectedly selected another project's session")

    print("multi-workspace routing integration passed")

    for client, vscode in ((client_a, vscode_a), (client_b, vscode_b)):
        status = await client.call("attach_debugpy")
        if status.get("selected_stopped_thread_id"):
            await vscode.request("continue", {"threadId": status["selected_stopped_thread_id"]})
        await vscode.close()
    await daemon.stop()


if __name__ == "__main__":
    asyncio.run(main())
