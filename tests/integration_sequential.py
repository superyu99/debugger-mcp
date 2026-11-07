"""Run manually: python tests/integration_sequential.py

Proves that the daemon can survive one complete debug run, transparently
restart debugpy.adapter, and accept a second F5 without restarting Claude.
"""

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
        try:
            while True:
                message = await read_dap_message(self.reader)
                if message["type"] == "response":
                    future = self.pending.pop(message["request_seq"], None)
                    if future is not None:
                        future.set_result(message)
                elif message["type"] == "event":
                    await self.events.put(message)
                elif message["type"] == "request":
                    raise RuntimeError(f"Unexpected reverse request: {message.get('command')}")
        except (EOFError, asyncio.IncompleteReadError, ConnectionError):
            return

    async def request(self, command, arguments=None):
        self.seq += 1
        future = asyncio.get_running_loop().create_future()
        self.pending[self.seq] = future
        await write_dap_message(
            self.writer,
            {"seq": self.seq, "type": "request", "command": command, "arguments": arguments or {}},
            self.lock,
        )
        response = await asyncio.wait_for(future, 20)
        if not response.get("success", False):
            raise RuntimeError(response)
        return response

    async def event(self, name):
        while True:
            event = await asyncio.wait_for(self.events.get(), 20)
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


async def launch_once(daemon: SharedDebuggerDaemon, client: DaemonClient) -> int:
    reader, writer = await asyncio.open_connection(daemon.proxy.host, daemon.proxy.port)
    vscode = TinyDAPClient(reader, writer)
    await vscode.request(
        "initialize",
        {
            "clientID": "sequential-smoke",
            "adapterID": "python",
            "pathFormat": "path",
            "linesStartAt1": True,
            "columnsStartAt1": True,
            "supportsRunInTerminalRequest": False,
        },
    )
    launch_task = asyncio.create_task(
        vscode.request(
            "launch",
            {
                "name": "sequential",
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
    status = await client.call("attach_debugpy")
    session_id = status["selected_session_id"]
    await vscode.request("disconnect", {"terminateDebuggee": True})
    await vscode.close()
    return session_id


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

    first = await launch_once(daemon, client)
    await asyncio.sleep(1.0)
    second = await launch_once(daemon, client)
    assert second > first, (first, second)
    assert (await client.ping())["pid"] is not None
    print("sequential debug-run adapter restart passed")
    await daemon.stop()


if __name__ == "__main__":
    asyncio.run(main())
