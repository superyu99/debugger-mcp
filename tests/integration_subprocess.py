"""Run manually: python tests/integration_subprocess.py

Uses real debugpy adapters to prove that a parent launch and an auto-attached
Python subprocess both reconnect through one debugger-mcp DAP port. The fake
VS Code client handles debugpy's startDebugging reverse request exactly like
VS Code: it opens a second DAP connection using the injected debugServer.
"""

from __future__ import annotations

import asyncio
import pathlib
import sys
from typing import Any, Awaitable, Callable, Optional

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from debugger_mcp.daemon import SharedDebuggerDaemon
from debugger_mcp.dap import read_dap_message, write_dap_message
from debugger_mcp.rpc import DaemonClient


ReverseHandler = Callable[[dict[str, Any]], Awaitable[dict[str, Any]]]


class TinyDAPSession:
    def __init__(self, reader, writer, reverse_handler: Optional[ReverseHandler] = None):
        self.reader = reader
        self.writer = writer
        self.reverse_handler = reverse_handler
        self.lock = asyncio.Lock()
        self.seq = 0
        self.pending: dict[int, asyncio.Future[dict[str, Any]]] = {}
        self.events: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
        self.seen_events: list[str] = []
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
                    self.seen_events.append(str(message.get("event")))
                    await self.events.put(message)
                elif message["type"] == "request":
                    asyncio.create_task(self._handle_reverse_request(message))
        except (EOFError, asyncio.IncompleteReadError, ConnectionError):
            return

    async def _handle_reverse_request(self, message: dict[str, Any]):
        command = str(message.get("command"))
        try:
            if self.reverse_handler is None:
                raise RuntimeError(f"Unhandled reverse request: {command}")
            body = await self.reverse_handler(message)
            response = {
                "seq": self._next_seq(),
                "type": "response",
                "request_seq": message["seq"],
                "success": True,
                "command": command,
                "body": body,
            }
        except Exception as exc:
            response = {
                "seq": self._next_seq(),
                "type": "response",
                "request_seq": message["seq"],
                "success": False,
                "command": command,
                "message": str(exc),
            }
        await write_dap_message(self.writer, response, self.lock)

    def _next_seq(self) -> int:
        self.seq += 1
        return self.seq

    async def request(self, command, arguments=None, timeout=20):
        seq = self._next_seq()
        future = asyncio.get_running_loop().create_future()
        self.pending[seq] = future
        await write_dap_message(
            self.writer,
            {"seq": seq, "type": "request", "command": command, "arguments": arguments or {}},
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


class TinyVSCode:
    def __init__(
        self,
        daemon: SharedDebuggerDaemon,
        breakpoint_path: pathlib.Path,
        breakpoint_line: int,
    ):
        self.daemon = daemon
        self.breakpoint_path = breakpoint_path
        self.breakpoint_line = breakpoint_line
        self.children: list[TinyDAPSession] = []
        self.terminal_processes: list[asyncio.subprocess.Process] = []
        self.child_ready: asyncio.Queue[TinyDAPSession] = asyncio.Queue()

    async def open_session(self, reverse_handler=None) -> TinyDAPSession:
        reader, writer = await asyncio.open_connection(self.daemon.proxy.host, self.daemon.proxy.port)
        return TinyDAPSession(reader, writer, reverse_handler=reverse_handler)

    async def initialize(self, session: TinyDAPSession):
        await session.request(
            "initialize",
            {
                "clientID": "subprocess-smoke",
                "adapterID": "python",
                "pathFormat": "path",
                "linesStartAt1": True,
                "columnsStartAt1": True,
                "supportsVariableType": True,
                "supportsVariablePaging": True,
                "supportsRunInTerminalRequest": True,
                "supportsStartDebuggingRequest": True,
            },
        )

    async def handle_root_reverse_request(self, message: dict[str, Any]) -> dict[str, Any]:
        command = message.get("command")
        arguments = message.get("arguments") or {}
        if command == "runInTerminal":
            cmdline = arguments.get("args") or []
            if not cmdline:
                raise RuntimeError("runInTerminal did not provide args")
            env = dict(arguments.get("env") or {})
            process = await asyncio.create_subprocess_exec(
                *cmdline,
                cwd=arguments.get("cwd") or None,
                env=env or None,
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
            )
            self.terminal_processes.append(process)
            return {"processId": process.pid, "shellProcessId": process.pid}
        if command == "startDebugging":
            asyncio.create_task(self.start_child(arguments))
            return {}
        raise RuntimeError(f"Unexpected reverse request: {command}")

    async def start_child(self, arguments: dict[str, Any]):
        configuration = dict(arguments.get("configuration") or {})
        assert configuration.get("debugServer") == self.daemon.proxy.port, configuration
        assert isinstance(configuration.get("__debuggerMcpParentSessionId"), int), configuration
        configuration.pop("debugServer", None)  # consumed by VS Code before DAP attach

        child = await self.open_session()
        self.children.append(child)
        await self.initialize(child)
        attach_task = asyncio.create_task(child.request("attach", configuration))
        await child.event("initialized")
        await child.request(
            "setBreakpoints",
            {
                "source": {"path": str(self.breakpoint_path)},
                "breakpoints": [{"line": self.breakpoint_line}],
                "sourceModified": False,
            },
        )
        await child.request("configurationDone")
        await attach_task
        await self.child_ready.put(child)


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
    vscode = TinyVSCode(
        daemon,
        ROOT / "examples" / "subprocess_child.py",
        5,
    )
    root = await vscode.open_session(reverse_handler=vscode.handle_root_reverse_request)

    await vscode.initialize(root)
    launch_task = asyncio.create_task(
        root.request(
            "launch",
            {
                "name": "subprocess parent",
                "type": "debugpy",
                "request": "launch",
                "env": {"DEBUGGER_MCP_WORKSPACE": str(ROOT)},
                "program": str(ROOT / "examples" / "subprocess_parent.py"),
                "console": "integratedTerminal",
                "justMyCode": True,
                "subProcess": True,
                "cwd": str(ROOT / "examples"),
                "python": sys.executable,
            },
        )
    )
    await root.event("initialized")
    await root.request("configurationDone")
    await launch_task

    child = await asyncio.wait_for(vscode.child_ready.get(), 25)
    await child.event("stopped", timeout=25)

    status = await client.call("attach_debugpy", {"full": True})
    assert status["session_count"] == 2, status
    roots = [item for item in status["sessions"] if item["session_kind"] == "root"]
    children = [item for item in status["sessions"] if item["session_kind"] == "subprocess"]
    assert len(roots) == 1 and len(children) == 1, status
    assert children[0]["parent_session_id"] == roots[0]["session_id"], status
    assert children[0]["root_session_id"] == roots[0]["session_id"], status
    assert roots[0]["launch_name"] == "subprocess parent", status
    assert children[0]["debug_state"] == "stopped", status
    assert status["selected_session_id"] == children[0]["session_id"], status

    # No session_id is supplied: the latest stopped process in this workspace is selected.
    evaluated = await client.call("evaluate", {"expression": "marker"})
    assert evaluated["__mcp_text__"] == "16", evaluated

    # Explicit root selection remains available.
    root_threads = await client.call("get_threads", {"session_id": roots[0]["session_id"]})
    assert "__mcp_text__" in root_threads, root_threads
    assert "session_kind" not in root_threads, root_threads

    print("real subprocess multi-session integration passed")

    stopped_thread = children[0]["stopped_thread_id"]
    await child.request("continue", {"threadId": stopped_thread})
    try:
        await child.event("terminated", timeout=10)
    except asyncio.TimeoutError:
        pass
    await asyncio.sleep(0.5)

    for session in vscode.children:
        await session.close()
    await root.close()
    for process in vscode.terminal_processes:
        if process.returncode is None:
            process.terminate()
        await process.wait()
    await daemon.stop()


if __name__ == "__main__":
    asyncio.run(main())
