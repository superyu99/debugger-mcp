from __future__ import annotations

import asyncio
import json
import logging
import os
import sys
import time
import traceback
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Awaitable, Callable, Optional

from . import __version__, config
from .dap import DAPError, DAPProxy
from .tools import DebuggerTools
from .workspace import CLIENT_ID_PARAM, CLIENT_WORKSPACE_PARAM, normalize_workspace_root

log = logging.getLogger(__name__)
JsonObject = dict[str, Any]


@dataclass(frozen=True)
class DebugContextSnapshot:
    session_id: int
    stop_id: int
    workspace_root: Optional[str]
    process_name: Optional[str]
    source_path: Optional[str]
    line: Optional[int]
    function_name: Optional[str]

    @property
    def key(self) -> tuple[int, int]:
        return (self.session_id, self.stop_id)


@dataclass
class SeenDebugContext:
    snapshot: DebugContextSnapshot
    updated_at: float


class SharedDebuggerDaemon:
    def __init__(
        self,
        dap_host: str = config.DAP_PROXY_HOST,
        dap_port: int = config.DAP_PROXY_PORT,
        control_host: str = config.CONTROL_HOST,
        control_port: int = config.CONTROL_PORT,
        debugpy_python: str = config.DEBUGPY_PYTHON,
    ) -> None:
        self.control_host = control_host
        self.control_port = control_port
        self.proxy = DAPProxy(dap_host, dap_port, debugpy_python)
        self.tools = DebuggerTools(self.proxy)
        self._server: Optional[asyncio.AbstractServer] = None
        self._stop_event = asyncio.Event()
        self._execution_lock = asyncio.Lock()
        self._client_contexts: dict[str, SeenDebugContext] = {}
        self._client_locks: dict[str, asyncio.Lock] = {}
        self._client_context_limit = 512
        self._handlers: dict[str, Callable[[JsonObject], Awaitable[Any]]] = {
            "attach_debugpy": lambda args: self.tools.attach_debugpy(**args),
            "get_threads": lambda args: self.tools.get_threads(**args),
            "get_stack": lambda args: self.tools.get_stack(**args),
            "get_locals": lambda args: self.tools.get_locals(**args),
            "evaluate": lambda args: self.tools.evaluate(**args),
            "inspect_structure": lambda args: self.tools.inspect_structure(**args),
            "step_over": lambda args: self.tools.step_over(**args),
            "step_into": lambda args: self.tools.step_into(**args),
            "step_out": lambda args: self.tools.step_out(**args),
            "run_to_line": lambda args: self.tools.run_to_line(**args),
        }

    async def start(self) -> None:
        # Bind DAP first. A successful control ping must imply that VS Code can connect.
        await self.proxy.start()
        try:
            self._server = await asyncio.start_server(
                self._handle_client,
                self.control_host,
                self.control_port,
            )
        except Exception:
            await self.proxy.stop()
            raise
        sockets = self._server.sockets or []
        if sockets:
            self.control_port = int(sockets[0].getsockname()[1])
        log.info(
            "Shared daemon ready: DAP=%s:%s control=%s:%s pid=%s log=%s dap_audit=%s",
            self.proxy.host,
            self.proxy.port,
            self.control_host,
            self.control_port,
            os.getpid(),
            config.DAEMON_LOG_PATH,
            config.DAP_AUDIT_ENABLED,
        )

    async def wait(self) -> None:
        await self._stop_event.wait()

    async def stop(self) -> None:
        if self._server is not None:
            self._server.close()
            await self._server.wait_closed()
            self._server = None
        await self.proxy.stop()

    async def _handle_client(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            raw = await asyncio.wait_for(reader.readline(), timeout=config.RPC_REQUEST_TIMEOUT_SECONDS)
            if not raw:
                return
            request = json.loads(raw.decode("utf-8"))
            if not isinstance(request, dict):
                raise ValueError("RPC request must be a JSON object")
            request_id = request.get("id")
            method = request.get("method")
            params = request.get("params") or {}
            if not isinstance(params, dict):
                raise ValueError("RPC params must be a JSON object")

            try:
                result = await self._dispatch(str(method), params)
                response = {"id": request_id, "result": result}
            except (DAPError, TypeError, ValueError) as exc:
                response = {"id": request_id, "error": {"type": type(exc).__name__, "message": str(exc)}}
            except Exception:
                log.exception("Daemon RPC method %s failed", method)
                response = {
                    "id": request_id,
                    "error": {"type": "Exception", "message": traceback.format_exc()},
                }
            writer.write((json.dumps(response, ensure_ascii=False, separators=(",", ":")) + "\n").encode("utf-8"))
            await writer.drain()
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("Daemon RPC connection failed")
        finally:
            writer.close()
            try:
                await writer.wait_closed()
            except Exception:
                pass

    async def _dispatch(self, method: str, params: JsonObject) -> Any:
        params = dict(params)
        client_workspace = normalize_workspace_root(params.pop(CLIENT_WORKSPACE_PARAM, None))
        raw_client_id = params.pop(CLIENT_ID_PARAM, None)
        client_id = (
            str(raw_client_id).strip()
            if isinstance(raw_client_id, str) and raw_client_id.strip()
            else f"legacy:{client_workspace or '<unknown>'}"
        )
        if method == "__ping__":
            sessions = self.proxy.sessions
            workspace_sessions = self.proxy.sessions_for_workspace(client_workspace)
            selected = self.proxy.select_session(workspace_root=client_workspace)
            return {
                "version": __version__,
                "pid": os.getpid(),
                "dap_host": self.proxy.host,
                "dap_port": self.proxy.port,
                "control_host": self.control_host,
                "control_port": self.control_port,
                "vscode_connected": bool(sessions),
                "session_count": len(sessions),
                "client_workspace": client_workspace,
                "workspace_connected": bool(workspace_sessions),
                "workspace_session_count": len(workspace_sessions),
                "unscoped_session_count": sum(1 for item in sessions if item.workspace_root is None),
                "session_id": selected.state.session_id if selected else None,
                "stop_id": selected.state.stop_id if selected else None,
                "debug_state": selected.state.status if selected else "disconnected",
                "sessions": [session.metadata() for session in sessions],
            }
        if method == "__shutdown__":
            # Daemon replacement is unconditional. Existing DAP sessions are
            # disconnected when the daemon stops and can be restarted with F5.
            asyncio.get_running_loop().call_soon(self._stop_event.set)
            return {"shutting_down": True, "pid": os.getpid()}

        handler = self._handlers.get(method)
        if handler is None:
            raise ValueError(f"Unknown daemon method: {method}")

        client_lock = self._client_locks.setdefault(client_id, asyncio.Lock())
        async with client_lock:
            with self.tools.client_workspace_scope(client_workspace):
                return await self._call_tool_with_context_notice(
                    client_id=client_id,
                    client_workspace=client_workspace,
                    method=method,
                    params=params,
                    handler=handler,
                )

    async def _call_tool_with_context_notice(
        self,
        client_id: str,
        client_workspace: Optional[str],
        method: str,
        params: JsonObject,
        handler: Callable[[JsonObject], Awaitable[Any]],
    ) -> Any:
        before = await self._snapshot_for_request(client_workspace, params)
        previous_entry = self._client_contexts.get(client_id)
        notice = None
        if previous_entry is not None and before is not None:
            if previous_entry.snapshot.key != before.key:
                notice = self._format_context_notice(
                    previous_entry.snapshot, before, client_workspace
                )

        if method in {"step_over", "step_into", "step_out", "run_to_line"}:
            # Execution-control tools are serialized globally, but each MCP frontend
            # has an independent last-seen context. If the user moved execution before
            # this Agent's step, surface that unseen change once; then mark the step's
            # final stop as seen so the next inspection does not repeat it.
            async with self._execution_lock:
                result = await handler(params)
        else:
            result = await handler(params)

        after = await self._snapshot_for_request(client_workspace, params)
        execution_method = method in {"step_over", "step_into", "step_out", "run_to_line"}
        # A manual VS Code action can finish between the pre-call snapshot and
        # the actual tool operation. For read-only tools, detect that change from
        # the post-call snapshot as well. For execution-control tools, only a
        # change already visible before the call is reported; the tool's own
        # movement is marked as seen without generating a redundant notice.
        if (
            notice is None
            and not execution_method
            and previous_entry is not None
            and after is not None
            and previous_entry.snapshot.key != after.key
        ):
            notice = self._format_context_notice(
                previous_entry.snapshot, after, client_workspace
            )

        observed = after or before
        if observed is not None:
            self._remember_client_context(client_id, observed)

        if notice is not None:
            result = self._prepend_context_notice(result, notice)
        return result

    async def _snapshot_for_request(
        self, client_workspace: Optional[str], params: JsonObject
    ) -> Optional[DebugContextSnapshot]:
        session = None
        session_id = params.get("session_id")
        frame_id = params.get("frame_id")
        try:
            if isinstance(session_id, int):
                session = self.proxy.select_session(session_id=session_id)
            elif isinstance(frame_id, int):
                session = self.proxy.find_session_for_frame(
                    frame_id, workspace_root=client_workspace
                )
            if session is None:
                session = self.proxy.select_session(
                    require_stopped=True, workspace_root=client_workspace
                )
        except DAPError:
            return None

        if session is None or session.state.status != "stopped":
            return None
        thread_id = session.state.stopped_thread_id
        if thread_id is None:
            return None

        top = session.cached_top_frame(thread_id)
        if top is None:
            try:
                body = await session.stack_trace(thread_id, start_frame=0, levels=1)
            except DAPError:
                return None
            frames = body.get("stackFrames") or []
            if not frames:
                return None
            top = frames[0]

        source = top.get("source") or {}
        line = top.get("line")
        return DebugContextSnapshot(
            session_id=session.session_id,
            stop_id=session.state.stop_id,
            workspace_root=session.workspace_root,
            process_name=session.process_name,
            source_path=source.get("path"),
            line=int(line) if isinstance(line, int) else None,
            function_name=top.get("name"),
        )

    def _remember_client_context(
        self, client_id: str, snapshot: DebugContextSnapshot
    ) -> None:
        self._client_contexts[client_id] = SeenDebugContext(
            snapshot=snapshot, updated_at=time.monotonic()
        )
        if len(self._client_contexts) <= self._client_context_limit:
            return
        overflow = len(self._client_contexts) - self._client_context_limit
        oldest = sorted(
            self._client_contexts.items(), key=lambda item: item[1].updated_at
        )[:overflow]
        for stale_client_id, _ in oldest:
            if stale_client_id == client_id:
                continue
            stale_lock = self._client_locks.get(stale_client_id)
            if stale_lock is not None and stale_lock.locked():
                continue
            self._client_contexts.pop(stale_client_id, None)
            self._client_locks.pop(stale_client_id, None)

    @classmethod
    def _format_context_notice(
        cls,
        previous: DebugContextSnapshot,
        current: DebugContextSnapshot,
        workspace_root: Optional[str],
    ) -> str:
        include_process = previous.session_id != current.session_id
        previous_location = cls._format_snapshot_location(
            previous, workspace_root, include_process=include_process
        )
        current_location = cls._format_snapshot_location(
            current, workspace_root, include_process=include_process
        )
        if include_process:
            label = "[Debug context changed]"
        elif previous_location == current_location:
            return (
                f"[Debug pause changed] new pause at {current_location} "
                "(same source location as the previous pause)"
            )
        else:
            label = "[Debug location changed]"
        return f"{label} {previous_location} -> {current_location}"

    @classmethod
    def _format_snapshot_location(
        cls,
        snapshot: DebugContextSnapshot,
        workspace_root: Optional[str],
        include_process: bool = False,
    ) -> str:
        path_root = snapshot.workspace_root or workspace_root
        path = cls._display_source_path(snapshot.source_path, path_root)
        if include_process and snapshot.workspace_root:
            workspace_name = os.path.basename(snapshot.workspace_root) or snapshot.workspace_root
            path = f"{workspace_name}/{path}"
        line = f":{snapshot.line}" if snapshot.line is not None else ""
        function_name = f" {snapshot.function_name}" if snapshot.function_name else ""
        location = f"{path}{line}{function_name}"
        if include_process and not snapshot.workspace_root and snapshot.process_name:
            process = os.path.basename(snapshot.process_name) or snapshot.process_name
            if process != os.path.basename(path):
                return f"{process} {location}"
        return location

    @staticmethod
    def _display_source_path(
        source_path: Optional[str], workspace_root: Optional[str]
    ) -> str:
        if not source_path:
            return "<unknown source>"
        normalized_source = os.path.normpath(source_path)
        if workspace_root:
            try:
                relative = os.path.relpath(normalized_source, workspace_root)
                if relative != os.pardir and not relative.startswith(os.pardir + os.sep):
                    return relative
            except (OSError, ValueError):
                pass
        return os.path.basename(normalized_source) or normalized_source

    @staticmethod
    def _prepend_context_notice(result: Any, notice: str) -> Any:
        if isinstance(result, dict):
            if "__mcp_text__" in result:
                updated = dict(result)
                text = str(updated.get("__mcp_text__") or "")
                updated["__mcp_text__"] = f"{notice}\n\n{text}" if text else notice
                return updated
            return {"debug_context_notice": notice, **result}
        return {"__mcp_text__": f"{notice}\n\n{result}"}


def configure_daemon_logging() -> None:
    path = Path(config.DAEMON_LOG_PATH)
    path.parent.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(
        level=getattr(logging, config.LOG_LEVEL.upper(), logging.INFO),
        format="[%(asctime)s] %(levelname)s %(name)s: %(message)s",
        handlers=[logging.FileHandler(path, encoding="utf-8")],
    )


async def async_main() -> None:
    configure_daemon_logging()
    daemon = SharedDebuggerDaemon()
    await daemon.start()
    try:
        await daemon.wait()
    finally:
        await daemon.stop()


def main() -> None:
    try:
        asyncio.run(async_main())
    except KeyboardInterrupt:
        pass
    except Exception:
        configure_daemon_logging()
        log.exception("Shared debugger daemon failed")
        raise


if __name__ == "__main__":
    main()
