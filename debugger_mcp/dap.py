from __future__ import annotations

import asyncio
import copy
import json
import logging
import os
import tempfile
import time
from pathlib import Path
from dataclasses import dataclass, field
from typing import Any, Callable, Optional

from . import config
from .workspace import (
    diagnostic_debug_cwd,
    inject_workspace_into_debug_configuration,
    normalize_workspace_root,
    pop_workspace_from_debug_arguments,
)

log = logging.getLogger(__name__)

_PARENT_SESSION_KEY = "__debuggerMcpParentSessionId"
_ROOT_SESSION_KEY = "__debuggerMcpRootSessionId"

_AUDITED_IDE_COMMANDS = {
    "launch",
    "attach",
    "continue",
    "next",
    "stepIn",
    "stepOut",
    "pause",
    "goto",
    "restart",
    "terminate",
    "disconnect",
    "setExpression",
    "setVariable",
    "setBreakpoints",
}


def _audit(message: str, *args: Any, level: int = logging.INFO) -> None:
    if config.DAP_AUDIT_ENABLED:
        log.log(level, "[DAP AUDIT] " + message, *args)


class DAPError(RuntimeError):
    pass


async def read_dap_message(reader: asyncio.StreamReader) -> dict[str, Any]:
    headers: dict[str, str] = {}
    while True:
        line = await reader.readline()
        if not line:
            raise EOFError("DAP stream closed")
        if line in (b"\r\n", b"\n"):
            break
        key, sep, value = line.decode("ascii", errors="strict").partition(":")
        if not sep:
            raise DAPError(f"Malformed DAP header: {line!r}")
        headers[key.strip().lower()] = value.strip()

    length_text = headers.get("content-length")
    if length_text is None:
        raise DAPError("DAP message has no Content-Length header")
    body = await reader.readexactly(int(length_text))
    message = json.loads(body.decode("utf-8"))
    if not isinstance(message, dict):
        raise DAPError("DAP message body is not an object")
    return message


async def write_dap_message(
    writer: asyncio.StreamWriter,
    message: dict[str, Any],
    lock: asyncio.Lock,
) -> None:
    body = json.dumps(message, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    packet = f"Content-Length: {len(body)}\r\n\r\n".encode("ascii") + body
    async with lock:
        writer.write(packet)
        await writer.drain()


@dataclass
class DebugState:
    session_id: int = 0
    stop_id: int = 0
    status: str = "disconnected"
    stopped_thread_id: Optional[int] = None
    stop_reason: Optional[str] = None
    all_threads_stopped: Optional[bool] = None
    last_event: Optional[dict[str, Any]] = None


@dataclass
class DAPSession:
    session_id: int
    frontend_reader: asyncio.StreamReader
    frontend_writer: asyncio.StreamWriter
    backend_reader: asyncio.StreamReader
    backend_writer: asyncio.StreamWriter
    proxy_host: str
    proxy_port: int
    state: DebugState = field(init=False)

    parent_session_id: Optional[int] = None
    root_session_id: Optional[int] = None
    session_kind: str = "root"
    request_kind: Optional[str] = None
    launch_name: Optional[str] = None
    subprocess_id: Optional[int] = None
    process_id: Optional[int] = None
    process_name: Optional[str] = None
    process_start_method: Optional[str] = None
    workspace_root: Optional[str] = None
    launch_cwd: Optional[str] = None
    last_stop_sequence: int = 0
    stop_sequence_provider: Optional[Callable[[], int]] = None
    last_activity_monotonic: float = field(default_factory=time.monotonic)

    _backend_seq: int = 1_000_000
    _frontend_seq: int = 1
    _backend_write_lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    _frontend_write_lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    _frontend_requests: dict[int, int] = field(default_factory=dict)
    _frontend_breakpoint_counts: dict[int, int] = field(default_factory=dict)
    _reverse_requests: dict[int, int] = field(default_factory=dict)
    _mcp_requests: dict[int, asyncio.Future[dict[str, Any]]] = field(default_factory=dict)
    _mcp_request_meta: dict[int, tuple[str, float, float]] = field(default_factory=dict)
    _expired_mcp_requests: dict[int, tuple[str, float, float]] = field(default_factory=dict)
    _closed: asyncio.Event = field(default_factory=asyncio.Event)
    _stopped_event: asyncio.Event = field(default_factory=asyncio.Event)
    _tasks: list[asyncio.Task[Any]] = field(default_factory=list)

    # Shared by every Claude MCP frontend because DAPSession lives in the daemon.
    _stack_cache: dict[tuple[int, int, int, Optional[int]], dict[str, Any]] = field(default_factory=dict)
    _top_frame_cache: dict[int, dict[str, Any]] = field(default_factory=dict)
    _frame_versions: dict[int, tuple[int, int]] = field(default_factory=dict)
    stack_trace_request_count: int = 0

    # VS Code owns the persistent breakpoint list. Temporary run-to-line
    # breakpoints are merged only in the adapter-facing request, so the editor
    # does not need a companion extension and user breakpoints are preserved.
    _user_breakpoint_args: dict[str, dict[str, Any]] = field(default_factory=dict)
    _temporary_breakpoints: dict[str, list[dict[str, Any]]] = field(default_factory=dict)
    _active_run_to_line: Optional[dict[str, Any]] = None
    _run_to_line_cleanup_task: Optional[asyncio.Task[Any]] = None

    def __post_init__(self) -> None:
        self.state = DebugState(session_id=self.session_id)
        self.root_session_id = self.session_id

    def next_backend_seq(self) -> int:
        self._backend_seq += 1
        return self._backend_seq

    def next_frontend_seq(self) -> int:
        self._frontend_seq += 1
        return self._frontend_seq

    @property
    def connected(self) -> bool:
        return not self._closed.is_set()

    def metadata(self) -> dict[str, Any]:
        return {
            "session_id": self.state.session_id,
            "parent_session_id": self.parent_session_id,
            "root_session_id": self.root_session_id,
            "session_kind": self.session_kind,
            "request_kind": self.request_kind,
            "launch_name": self.launch_name,
            "subprocess_id": self.subprocess_id,
            "process_id": self.process_id,
            "process_name": self.process_name,
            "process_start_method": self.process_start_method,
            "workspace_root": self.workspace_root,
            "launch_cwd": self.launch_cwd,
            "last_stop_sequence": self.last_stop_sequence,
            "stop_id": self.state.stop_id,
            "debug_state": self.state.status,
            "stopped_thread_id": self.state.stopped_thread_id,
            "stop_reason": self.state.stop_reason,
        }

    def knows_frame_id(self, frame_id: int) -> bool:
        return frame_id in self._frame_versions

    async def run(self) -> None:
        self.state.status = "connected"
        self._touch()
        self._tasks = [
            asyncio.create_task(self._frontend_to_backend(), name=f"dap-{self.session_id}-frontend-to-backend"),
            asyncio.create_task(self._backend_to_frontend(), name=f"dap-{self.session_id}-backend-to-frontend"),
        ]
        done, _ = await asyncio.wait(self._tasks, return_when=asyncio.FIRST_COMPLETED)
        self.close("DAP session ended")
        for task in self._tasks:
            if task not in done:
                task.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)

    def close(self, reason: str) -> None:
        if self._closed.is_set():
            return
        self._closed.set()
        self.state.status = "disconnected"
        self._touch()
        self._invalidate_frame_cache()
        error = DAPError(reason)
        for future in self._mcp_requests.values():
            if not future.done():
                future.set_exception(error)
        self._mcp_requests.clear()
        self._mcp_request_meta.clear()
        self._expired_mcp_requests.clear()
        self._stopped_event.set()
        if self._run_to_line_cleanup_task is not None:
            self._run_to_line_cleanup_task.cancel()
            self._run_to_line_cleanup_task = None
        for writer in (self.frontend_writer, self.backend_writer):
            try:
                writer.close()
            except Exception:
                pass

    async def request(
        self,
        command: str,
        arguments: Optional[dict[str, Any]] = None,
        timeout_seconds: Optional[float] = None,
    ) -> dict[str, Any]:
        if not self.connected:
            raise DAPError(f"No active VS Code debug session for session_id={self.session_id}")
        timeout = (
            config.DAP_REQUEST_TIMEOUT_SECONDS
            if timeout_seconds is None
            else float(timeout_seconds)
        )
        if timeout <= 0:
            raise ValueError("DAP request timeout_seconds must be > 0")

        seq = self.next_backend_seq()
        future: asyncio.Future[dict[str, Any]] = asyncio.get_running_loop().create_future()
        self._mcp_requests[seq] = future
        self._mcp_request_meta[seq] = (command, timeout, time.monotonic())
        _audit(
            "request source=mcp session_id=%s backend_seq=%s command=%s timeout=%.3fs",
            self.session_id,
            seq,
            command,
            timeout,
        )
        await write_dap_message(
            self.backend_writer,
            {"seq": seq, "type": "request", "command": command, "arguments": arguments or {}},
            self._backend_write_lock,
        )
        try:
            response = await asyncio.wait_for(future, timeout=timeout)
        except asyncio.TimeoutError as exc:
            self._mcp_requests.pop(seq, None)
            meta = self._mcp_request_meta.pop(seq, (command, timeout, time.monotonic()))
            self._expired_mcp_requests[seq] = meta
            # Bound stale timeout bookkeeping. Unknown responses are dropped even
            # after this history entry ages out, so pruning cannot reintroduce the
            # old protocol bug.
            while len(self._expired_mcp_requests) > 4096:
                self._expired_mcp_requests.pop(next(iter(self._expired_mcp_requests)))
            _audit(
                "timeout source=mcp session_id=%s backend_seq=%s command=%s timeout=%.3fs",
                self.session_id,
                seq,
                command,
                timeout,
                level=logging.WARNING,
            )
            raise DAPError(
                f"DAP request timed out after {timeout:g}s in session_id={self.session_id}: {command}"
            ) from exc
        if not response.get("success", False):
            body = response.get("body") or {}
            detail = body.get("error", {}).get("format") if isinstance(body, dict) else None
            raise DAPError(detail or response.get("message") or f"DAP request failed: {command}")
        body = response.get("body")
        return body if isinstance(body, dict) else {}

    async def stack_trace(
        self,
        thread_id: int,
        start_frame: int = 0,
        levels: Optional[int] = None,
    ) -> dict[str, Any]:
        """Return a stack trace cached for this session's current stop generation.

        ``levels=None`` deliberately omits the DAP ``levels`` argument so the
        adapter returns the complete remaining stack. A numeric value is only
        used when the caller explicitly asks for a bounded slice.
        """
        if self.state.status != "stopped":
            raise DAPError(
                f"Stack frames require a stopped debuggee; session_id={self.session_id} "
                f"is {self.state.status!r}"
            )

        key = (self.state.stop_id, thread_id, start_frame, levels)
        cached = self._stack_cache.get(key)
        if cached is not None:
            return copy.deepcopy(cached)

        expected_session_id = self.state.session_id
        expected_stop_id = self.state.stop_id
        self.stack_trace_request_count += 1
        arguments: dict[str, Any] = {"threadId": thread_id, "startFrame": start_frame}
        if levels is not None:
            arguments["levels"] = levels
        body = await self.request("stackTrace", arguments)

        if (
            self.state.status == "stopped"
            and self.state.session_id == expected_session_id
            and self.state.stop_id == expected_stop_id
        ):
            self._stack_cache[key] = copy.deepcopy(body)
            frames = body.get("stackFrames", [])
            for frame in frames:
                frame_id = frame.get("id")
                if isinstance(frame_id, int):
                    self._frame_versions[frame_id] = (expected_session_id, expected_stop_id)
            if start_frame == 0 and frames:
                self._top_frame_cache[thread_id] = copy.deepcopy(frames[0])
        return body

    def cached_top_frame(self, thread_id: int) -> Optional[dict[str, Any]]:
        frame = self._top_frame_cache.get(thread_id)
        return copy.deepcopy(frame) if frame is not None else None

    def validate_frame_id(self, frame_id: int) -> None:
        version = self._frame_versions.get(frame_id)
        if version is None:
            return
        current = (self.state.session_id, self.state.stop_id)
        if version != current:
            raise DAPError(
                f"Stale frame_id={frame_id}. "
                "Frame IDs are valid only for the pause in which they were obtained. "
                "Omit frame_id to use the latest top frame, or call get_stack() to obtain "
                "frame IDs for the current pause."
            )

    def prepare_for_step(self) -> None:
        self._stopped_event.clear()

    async def wait_until_stopped(self, timeout: float) -> DebugState:
        try:
            await asyncio.wait_for(self._stopped_event.wait(), timeout=timeout)
        except asyncio.TimeoutError as exc:
            raise DAPError("Timed out waiting for the debuggee to stop after stepping") from exc
        if self.state.status == "terminated":
            raise DAPError("The debuggee terminated while stepping")
        if self.state.status != "stopped":
            raise DAPError(f"Unexpected debug state after stepping: {self.state.status}")
        return self.state

    async def forward_frontend_message(self, message: dict[str, Any]) -> None:
        """Forward one already-read VS Code DAP message to this session's adapter."""
        message_type = message.get("type")
        outgoing = copy.deepcopy(message)
        if message_type == "request":
            command = str(message.get("command", ""))
            if command in {"launch", "attach"}:
                self._capture_start_request(command, outgoing)
            backend_seq = self.next_backend_seq()
            self._frontend_requests[backend_seq] = int(message["seq"])
            if command == "setBreakpoints":
                user_count = self._capture_and_merge_user_breakpoints(outgoing)
                self._frontend_breakpoint_counts[backend_seq] = user_count
            outgoing["seq"] = backend_seq
            if command in _AUDITED_IDE_COMMANDS:
                _audit(
                    "request source=ide session_id=%s frontend_seq=%s backend_seq=%s command=%s",
                    self.session_id,
                    message.get("seq"),
                    backend_seq,
                    command,
                )
        elif message_type == "response":
            frontend_request_seq = int(message.get("request_seq", -1))
            backend_request_seq = self._reverse_requests.pop(frontend_request_seq, None)
            if backend_request_seq is not None:
                outgoing["request_seq"] = backend_request_seq
            outgoing["seq"] = self.next_backend_seq()
        else:
            outgoing["seq"] = self.next_backend_seq()
        await write_dap_message(self.backend_writer, outgoing, self._backend_write_lock)

    async def _frontend_to_backend(self) -> None:
        try:
            while True:
                message = await read_dap_message(self.frontend_reader)
                await self.forward_frontend_message(message)
        except (EOFError, ConnectionError, asyncio.IncompleteReadError):
            _audit("session source=ide event=disconnected session_id=%s", self.session_id)
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("Frontend-to-backend forwarding failed for session_id=%s", self.session_id)

    async def _backend_to_frontend(self) -> None:
        try:
            while True:
                message = await read_dap_message(self.backend_reader)
                message_type = message.get("type")

                if message_type == "response":
                    request_seq = int(message.get("request_seq", -1))
                    future = self._mcp_requests.pop(request_seq, None)
                    if future is not None:
                        meta = self._mcp_request_meta.pop(request_seq, None)
                        command = meta[0] if meta is not None else str(message.get("command", ""))
                        elapsed = (time.monotonic() - meta[2]) if meta is not None else None
                        if elapsed is None:
                            _audit(
                                "response route=mcp session_id=%s backend_seq=%s command=%s",
                                self.session_id,
                                request_seq,
                                command,
                            )
                        else:
                            _audit(
                                "response route=mcp session_id=%s backend_seq=%s command=%s elapsed=%.3fs",
                                self.session_id,
                                request_seq,
                                command,
                                elapsed,
                            )
                        if not future.done():
                            future.set_result(message)
                        continue

                    frontend_request_seq = self._frontend_requests.pop(request_seq, None)
                    if frontend_request_seq is not None:
                        outgoing = copy.deepcopy(message)
                        user_count = self._frontend_breakpoint_counts.pop(request_seq, None)
                        if user_count is not None:
                            body = outgoing.get("body")
                            if isinstance(body, dict) and isinstance(body.get("breakpoints"), list):
                                body["breakpoints"] = body["breakpoints"][:user_count]
                        outgoing["seq"] = self.next_frontend_seq()
                        outgoing["request_seq"] = frontend_request_seq
                        await write_dap_message(self.frontend_writer, outgoing, self._frontend_write_lock)
                        continue

                    # A response must belong either to an MCP-injected request or
                    # to an IDE request that was remapped through _frontend_requests.
                    # Never leak an unmatched/late MCP response into VS Code/Cursor:
                    # the IDE did not issue that request_seq and may treat it as an
                    # invalid unsolicited response.
                    expired = self._expired_mcp_requests.pop(request_seq, None)
                    self._mcp_request_meta.pop(request_seq, None)
                    if expired is not None:
                        command, timeout, started = expired
                        _audit(
                            "response route=drop_late_mcp session_id=%s backend_seq=%s command=%s "
                            "timeout=%.3fs elapsed=%.3fs",
                            self.session_id,
                            request_seq,
                            command,
                            timeout,
                            time.monotonic() - started,
                            level=logging.WARNING,
                        )
                    else:
                        _audit(
                            "response route=drop_unmatched session_id=%s backend_seq=%s command=%s",
                            self.session_id,
                            request_seq,
                            message.get("command"),
                            level=logging.WARNING,
                        )
                    continue

                elif message_type == "request":
                    outgoing = copy.deepcopy(message)
                    if outgoing.get("command") == "startDebugging":
                        self._prepare_child_start_request(outgoing)
                    frontend_seq = self.next_frontend_seq()
                    self._reverse_requests[frontend_seq] = int(message["seq"])
                    outgoing["seq"] = frontend_seq
                    await write_dap_message(self.frontend_writer, outgoing, self._frontend_write_lock)
                    continue

                elif message_type == "event":
                    self._update_state(message)

                outgoing = copy.deepcopy(message)
                outgoing["seq"] = self.next_frontend_seq()
                await write_dap_message(self.frontend_writer, outgoing, self._frontend_write_lock)
        except (EOFError, ConnectionError, asyncio.IncompleteReadError):
            _audit("session source=adapter event=disconnected session_id=%s", self.session_id)
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("Backend-to-frontend forwarding failed for session_id=%s", self.session_id)


    @staticmethod
    def _source_key(source: dict[str, Any]) -> str:
        path = source.get("path")
        if isinstance(path, str) and path:
            return os.path.normcase(os.path.normpath(path))
        reference = source.get("sourceReference")
        return "sourceReference:%s" % reference

    def _capture_and_merge_user_breakpoints(self, outgoing: dict[str, Any]) -> int:
        arguments = outgoing.get("arguments")
        if not isinstance(arguments, dict):
            return 0
        source = arguments.get("source")
        if not isinstance(source, dict):
            return 0
        key = self._source_key(source)
        user_arguments = copy.deepcopy(arguments)
        user_breakpoints = user_arguments.get("breakpoints")
        if not isinstance(user_breakpoints, list):
            user_breakpoints = []
            user_arguments["breakpoints"] = user_breakpoints
        self._user_breakpoint_args[key] = user_arguments
        outgoing["arguments"] = self._merged_breakpoint_arguments(key, user_arguments)
        return len(user_breakpoints)

    def _merged_breakpoint_arguments(
        self,
        key: str,
        base_arguments: Optional[dict[str, Any]] = None,
    ) -> dict[str, Any]:
        if base_arguments is None:
            base_arguments = self._user_breakpoint_args.get(key)
        if base_arguments is None:
            path = key if not key.startswith("sourceReference:") else None
            source: dict[str, Any] = {"path": path} if path else {}
            base_arguments = {"source": source, "breakpoints": [], "sourceModified": False}
        merged = copy.deepcopy(base_arguments)
        user_breakpoints = merged.get("breakpoints")
        if not isinstance(user_breakpoints, list):
            user_breakpoints = []
        merged["breakpoints"] = user_breakpoints + copy.deepcopy(self._temporary_breakpoints.get(key, []))
        return merged

    async def install_temporary_breakpoint(
        self,
        source_path: str,
        line: int,
        condition: Optional[str] = None,
        resume_line: Optional[int] = None,
    ) -> dict[str, Any]:
        if self._active_run_to_line is not None:
            target = self._active_run_to_line
            raise DAPError(
                "A run_to_line operation is already active for %s:%s"
                % (target.get("source_path"), target.get("line"))
            )
        normalized_path = os.path.normcase(os.path.normpath(source_path))
        key = normalized_path
        breakpoint: dict[str, Any] = {"line": int(line)}
        if condition:
            breakpoint["condition"] = str(condition)
        user_args = self._user_breakpoint_args.get(key) or {}
        user_breakpoints = user_args.get("breakpoints") or []
        borrowed = any(
            isinstance(item, dict)
            and item.get("line") == int(line)
            and (item.get("condition") or None) == (condition or None)
            for item in user_breakpoints
        )
        if not borrowed:
            self._temporary_breakpoints[key] = [breakpoint]
        self._active_run_to_line = {
            "source_path": source_path,
            "normalized_path": normalized_path,
            "line": int(line),
            "condition": condition,
            "borrowed_user_breakpoint": borrowed,
        }
        if borrowed:
            return copy.deepcopy(self._active_run_to_line)
        try:
            arguments = self._merged_breakpoint_arguments(key)
            if isinstance(resume_line, int):
                breakpoints = list(arguments.get("breakpoints") or [])
                arguments["breakpoints"] = [
                    item
                    for index, item in enumerate(breakpoints)
                    if index >= len(user_breakpoints)
                    or not isinstance(item, dict)
                    or item.get("line") != resume_line
                ]
            source = arguments.get("source")
            if not isinstance(source, dict):
                source = {}
                arguments["source"] = source
            source["path"] = source_path
            body = await self.request("setBreakpoints", arguments)
            user_count = len(arguments["breakpoints"]) - 1
            returned = body.get("breakpoints") or []
            temp_result = returned[user_count] if len(returned) > user_count else None
            if not isinstance(temp_result, dict):
                raise DAPError("debugpy did not return the temporary breakpoint")
            if temp_result.get("verified") is False:
                raise DAPError(temp_result.get("message") or "Temporary breakpoint was not verified")
            actual_line = temp_result.get("line")
            if isinstance(actual_line, int):
                self._active_run_to_line["line"] = actual_line
            return copy.deepcopy(self._active_run_to_line)
        except Exception:
            self._temporary_breakpoints.pop(key, None)
            self._active_run_to_line = None
            raise

    async def remove_temporary_breakpoint(self) -> None:
        active = self._active_run_to_line
        self._active_run_to_line = None
        if active is None:
            return
        key = str(active.get("normalized_path"))
        self._temporary_breakpoints.pop(key, None)
        if active.get("borrowed_user_breakpoint"):
            return
        if not self.connected or self.state.status in {"terminated", "disconnected"}:
            return
        try:
            await self.request("setBreakpoints", self._merged_breakpoint_arguments(key))
        except Exception:
            log.exception("Could not restore user breakpoints for session_id=%s source=%s", self.session_id, key)

    def set_run_to_line_cleanup_task(self, task: asyncio.Task[Any]) -> None:
        if self._run_to_line_cleanup_task is not None and not self._run_to_line_cleanup_task.done():
            self._run_to_line_cleanup_task.cancel()
        self._run_to_line_cleanup_task = task

    def clear_run_to_line_cleanup_task(self) -> None:
        self._run_to_line_cleanup_task = None

    async def wait_for_stop_or_termination(self, timeout: float) -> Optional[DebugState]:
        try:
            await asyncio.wait_for(self._stopped_event.wait(), timeout=timeout)
        except asyncio.TimeoutError:
            return None
        return self.state

    def _capture_start_request(self, command: str, outgoing: dict[str, Any]) -> None:
        arguments = outgoing.get("arguments")
        if not isinstance(arguments, dict):
            arguments = {}
            outgoing["arguments"] = arguments

        parent = arguments.pop(_PARENT_SESSION_KEY, None)
        root = arguments.pop(_ROOT_SESSION_KEY, None)
        self.launch_cwd = diagnostic_debug_cwd(arguments.get("cwd"))
        normalized_workspace = pop_workspace_from_debug_arguments(arguments)
        if normalized_workspace is not None:
            self.workspace_root = normalized_workspace
        self.request_kind = command
        self.launch_name = str(arguments.get("name")) if arguments.get("name") is not None else None

        sub_pid = arguments.get("subProcessId")
        self.subprocess_id = int(sub_pid) if isinstance(sub_pid, int) else None
        if isinstance(parent, int) or self.subprocess_id is not None:
            self.parent_session_id = int(parent) if isinstance(parent, int) else None
            self.root_session_id = int(root) if isinstance(root, int) else self.parent_session_id
            self.session_kind = "subprocess"
        else:
            self.session_kind = "root"
            self.root_session_id = self.session_id
        self._touch()

    def _prepare_child_start_request(self, outgoing: dict[str, Any]) -> None:
        arguments = outgoing.get("arguments")
        if not isinstance(arguments, dict):
            return
        configuration = arguments.get("configuration")
        if not isinstance(configuration, dict):
            return

        # startDebugging asks VS Code to create a child debug session. Inject the
        # same debugServer so that child session reconnects through this daemon
        # instead of bypassing it with a private adapter owned only by VS Code.
        configuration["debugServer"] = self.proxy_port
        configuration[_PARENT_SESSION_KEY] = self.session_id
        configuration[_ROOT_SESSION_KEY] = self.root_session_id or self.session_id
        if self.workspace_root is not None:
            inject_workspace_into_debug_configuration(configuration, self.workspace_root)
        log.info(
            "Routed startDebugging child through %s:%s; parent_session_id=%s subprocess_id=%s",
            self.proxy_host,
            self.proxy_port,
            self.session_id,
            configuration.get("subProcessId"),
        )

    def _update_state(self, message: dict[str, Any]) -> None:
        event = message.get("event")
        body = message.get("body") or {}
        self.state.last_event = message
        self._touch()
        if event == "stopped":
            self.state.stop_id += 1
            if self.stop_sequence_provider is not None:
                self.last_stop_sequence = self.stop_sequence_provider()
            self.state.status = "stopped"
            self.state.stopped_thread_id = body.get("threadId")
            self.state.stop_reason = body.get("reason")
            self.state.all_threads_stopped = body.get("allThreadsStopped")
            self._invalidate_frame_cache()
            self._stopped_event.set()
            _audit(
                "event session_id=%s event=stopped stop_id=%s reason=%s thread_id=%s all_threads_stopped=%s",
                self.session_id,
                self.state.stop_id,
                self.state.stop_reason,
                self.state.stopped_thread_id,
                self.state.all_threads_stopped,
            )
        elif event == "continued":
            self.state.status = "running"
            self.state.stopped_thread_id = None
            self.state.stop_reason = None
            self._invalidate_frame_cache()
            self._stopped_event.clear()
            _audit(
                "event session_id=%s event=continued thread_id=%s all_threads_continued=%s",
                self.session_id,
                body.get("threadId"),
                body.get("allThreadsContinued"),
            )
        elif event == "process":
            self.process_name = body.get("name")
            process_id = body.get("systemProcessId")
            self.process_id = int(process_id) if isinstance(process_id, int) else self.process_id
            self.process_start_method = body.get("startMethod")
            _audit(
                "event session_id=%s event=process process_id=%s process_name=%s start_method=%s",
                self.session_id,
                self.process_id,
                self.process_name,
                self.process_start_method,
            )
        elif event in {"terminated", "exited"}:
            self.state.status = "terminated"
            self.state.stopped_thread_id = None
            self._invalidate_frame_cache()
            self._stopped_event.set()
            _audit(
                "event session_id=%s event=%s exit_code=%s",
                self.session_id,
                event,
                body.get("exitCode"),
            )

    def _touch(self) -> None:
        self.last_activity_monotonic = time.monotonic()

    def _invalidate_frame_cache(self) -> None:
        self._stack_cache.clear()
        self._top_frame_cache.clear()
        if len(self._frame_versions) > 4096:
            self._frame_versions = dict(list(self._frame_versions.items())[-2048:])


def _debugpy_initialize_capabilities() -> dict[str, Any]:
    """Capabilities returned by debugpy 1.8.x for DAP initialize.

    The proxy must answer VS Code's initialize request before VS Code sends the
    launch/attach request whose env contains DEBUGGER_MCP_WORKSPACE and the internal
    root-session marker. The exact same initialize request is then replayed to
    the selected per-root debugpy adapter and its duplicate response is hidden.
    """
    return {
        "supportsCompletionsRequest": True,
        "supportsConditionalBreakpoints": True,
        "supportsConfigurationDoneRequest": True,
        "supportsDebuggerProperties": True,
        "supportsDelayedStackTraceLoading": True,
        "supportsEvaluateForHovers": True,
        "supportsExceptionInfoRequest": True,
        "supportsExceptionOptions": True,
        "supportsFunctionBreakpoints": True,
        "supportsHitConditionalBreakpoints": True,
        "supportsLogPoints": True,
        "supportsModulesRequest": True,
        "supportsSetExpression": True,
        "supportsSetVariable": True,
        "supportsValueFormattingOptions": True,
        "supportsTerminateDebuggee": True,
        "supportsTerminateRequest": True,
        "supportsGotoTargetsRequest": True,
        "supportsClipboardContext": True,
        "exceptionBreakpointFilters": [
            {
                "filter": "raised",
                "label": "Raised Exceptions",
                "default": False,
                "description": "Break whenever any exception is raised.",
            },
            {
                "filter": "uncaught",
                "label": "Uncaught Exceptions",
                "default": True,
                "description": "Break when the process is exiting due to unhandled exception.",
            },
            {
                "filter": "userUnhandled",
                "label": "User Uncaught Exceptions",
                "default": False,
                "description": "Break when exception escapes into library code.",
            },
        ],
        "supportsStepInTargetsRequest": True,
    }


@dataclass
class AdapterGroup:
    """One debugpy adapter shared by one root debug run and all of its children."""

    root_session_id: int
    process: asyncio.subprocess.Process
    host: str
    port: int
    endpoints_path: Path
    session_ids: set[int] = field(default_factory=set)


class DAPProxy:
    def __init__(
        self,
        host: str = config.DAP_PROXY_HOST,
        port: int = config.DAP_PROXY_PORT,
        debugpy_python: str = config.DEBUGPY_PYTHON,
    ) -> None:
        self.host = host
        self.port = port
        self.debugpy_python = debugpy_python
        self._server: Optional[asyncio.AbstractServer] = None
        self._sessions: dict[int, DAPSession] = {}
        self._session_tasks: dict[int, asyncio.Task[Any]] = {}
        self._next_session_id = 0
        self._next_stop_sequence = 0

        # A debugpy adapter only permits one independent "launch" root session;
        # later client connections are expected to be subprocess "attach"
        # sessions. Therefore every root debug run gets its own adapter process,
        # while torchrun/debugpy child sessions are routed back to their root's
        # adapter so debugpy's in-process subProcessId registry still works.
        self._adapter_groups: dict[int, AdapterGroup] = {}
        self._adapter_group_lock = asyncio.Lock()

    @property
    def sessions(self) -> list[DAPSession]:
        return [self._sessions[key] for key in sorted(self._sessions) if self._sessions[key].connected]

    @property
    def session(self) -> Optional[DAPSession]:
        """Backward-compatible default selection."""
        return self.select_session()

    def _allocate_stop_sequence(self) -> int:
        self._next_stop_sequence += 1
        return self._next_stop_sequence

    def sessions_for_workspace(self, workspace_root: Optional[str]) -> list[DAPSession]:
        active = self.sessions
        normalized = normalize_workspace_root(workspace_root)
        if normalized is None:
            return active
        return [item for item in active if item.workspace_root == normalized]

    def select_session(
        self,
        session_id: Optional[int] = None,
        require_stopped: bool = False,
        workspace_root: Optional[str] = None,
    ) -> Optional[DAPSession]:
        active = self.sessions
        if session_id is not None:
            session = self._sessions.get(session_id)
            if session is None or not session.connected:
                available = [item.session_id for item in active]
                raise DAPError(f"Unknown or disconnected session_id={session_id}; active session_ids={available}")
            if require_stopped and session.state.status != "stopped":
                raise DAPError(
                    f"session_id={session_id} is {session.state.status!r}, not stopped. "
                    "Call attach_debugpy to inspect active sessions."
                )
            return session

        scoped = self.sessions_for_workspace(workspace_root)
        candidates = [item for item in scoped if not require_stopped or item.state.status == "stopped"]
        if not candidates:
            return None

        # Workspace filtering prevents accidental cross-project selection.
        # Within one workspace, whichever process most recently emitted a real
        # stopped event wins, regardless of root/subprocess kind.
        return max(
            candidates,
            key=lambda item: (
                1 if item.state.status == "stopped" else 0,
                item.last_stop_sequence,
                item.last_activity_monotonic,
                item.session_id,
            ),
        )

    def find_session_for_frame(
        self,
        frame_id: int,
        workspace_root: Optional[str] = None,
    ) -> Optional[DAPSession]:
        all_matches = [session for session in self.sessions if session.knows_frame_id(frame_id)]
        normalized = normalize_workspace_root(workspace_root)
        matches = (
            [session for session in all_matches if session.workspace_root == normalized]
            if normalized is not None
            else all_matches
        )
        if len(matches) == 1:
            return matches[0]
        if len(matches) > 1:
            ids = [session.session_id for session in matches]
            raise DAPError(f"frame_id={frame_id} exists in multiple sessions {ids}; provide session_id explicitly")
        if all_matches and normalized is not None:
            details = [
                {"session_id": session.session_id, "workspace_root": session.workspace_root}
                for session in all_matches
            ]
            raise DAPError(
                f"frame_id={frame_id} belongs to another workspace: {details}. "
                "Pass session_id explicitly only when cross-project access is intended."
            )
        return None

    async def start(self) -> None:
        self._server = await asyncio.start_server(self._accept_frontend, self.host, self.port)
        sockets = self._server.sockets or []
        if sockets:
            self.port = int(sockets[0].getsockname()[1])
        log.info("Multi-root DAP proxy listening on %s:%s pid=%s", self.host, self.port, os.getpid())

    async def stop(self) -> None:
        if self._server is not None:
            self._server.close()
            await self._server.wait_closed()
            self._server = None
        for session in list(self._sessions.values()):
            session.close("Debugger MCP daemon stopped")
        if self._session_tasks:
            await asyncio.gather(*list(self._session_tasks.values()), return_exceptions=True)
        self._session_tasks.clear()
        self._sessions.clear()
        for root_session_id in list(self._adapter_groups):
            await self._stop_adapter_group(root_session_id)

    async def _start_adapter_group(self, root_session_id: int) -> AdapterGroup:
        fd, endpoint_name = tempfile.mkstemp(
            prefix=f"debugger-mcp-adapter-{root_session_id}-", suffix=".json"
        )
        os.close(fd)
        endpoint_path = Path(endpoint_name)
        try:
            endpoint_path.unlink()
        except FileNotFoundError:
            pass

        env = {
            **os.environ,
            "PYTHONUNBUFFERED": "1",
            "DEBUGPY_ADAPTER_ENDPOINTS": str(endpoint_path),
        }
        # Keep adapter diagnostics in the same persistent daemon log instead of
        # discarding stdout/stderr. This is intentionally append-only so an
        # unexpected adapter exit can be correlated with the surrounding DAP
        # audit trail.
        adapter_log_path = Path(config.DAEMON_LOG_PATH)
        adapter_log_path.parent.mkdir(parents=True, exist_ok=True)
        adapter_log = open(adapter_log_path, "ab", buffering=0)
        try:
            process = await asyncio.create_subprocess_exec(
                self.debugpy_python,
                "-m",
                "debugpy.adapter",
                "--host",
                "127.0.0.1",
                "--port",
                "0",
                stdin=asyncio.subprocess.DEVNULL,
                stdout=adapter_log,
                stderr=asyncio.subprocess.STDOUT,
                env=env,
            )
        finally:
            adapter_log.close()

        deadline = time.monotonic() + config.DAEMON_START_TIMEOUT_SECONDS
        try:
            while time.monotonic() < deadline:
                if process.returncode is not None:
                    raise DAPError(
                        f"debugpy adapter for root_session_id={root_session_id} "
                        f"exited with code {process.returncode}"
                    )
                if endpoint_path.exists():
                    try:
                        endpoints = json.loads(endpoint_path.read_text(encoding="utf-8"))
                        client = endpoints.get("client") or {}
                        host = client.get("host")
                        port = client.get("port")
                        if isinstance(host, str) and isinstance(port, int):
                            group = AdapterGroup(
                                root_session_id=root_session_id,
                                process=process,
                                host=host,
                                port=port,
                                endpoints_path=endpoint_path,
                            )
                            self._adapter_groups[root_session_id] = group
                            log.info(
                                "Started adapter group root_session_id=%s pid=%s endpoint=%s:%s",
                                root_session_id,
                                process.pid,
                                host,
                                port,
                            )
                            _audit(
                                "adapter event=started root_session_id=%s pid=%s endpoint=%s:%s",
                                root_session_id,
                                process.pid,
                                host,
                                port,
                            )
                            return group
                    except (OSError, json.JSONDecodeError):
                        pass
                await asyncio.sleep(0.05)
            raise DAPError(
                f"Timed out waiting for debugpy adapter root_session_id={root_session_id}"
            )
        except Exception:
            if process.returncode is None:
                process.terminate()
                try:
                    await asyncio.wait_for(process.wait(), timeout=3.0)
                except asyncio.TimeoutError:
                    process.kill()
                    await process.wait()
            try:
                endpoint_path.unlink()
            except FileNotFoundError:
                pass
            raise

    async def _stop_adapter_group(self, root_session_id: int) -> None:
        group = self._adapter_groups.pop(root_session_id, None)
        if group is None:
            return
        process = group.process
        if process.returncode is None:
            process.terminate()
            try:
                await asyncio.wait_for(process.wait(), timeout=3.0)
            except asyncio.TimeoutError:
                process.kill()
                await process.wait()
        try:
            group.endpoints_path.unlink()
        except FileNotFoundError:
            pass
        log.info("Stopped adapter group root_session_id=%s returncode=%s", root_session_id, process.returncode)
        _audit(
            "adapter event=stopped root_session_id=%s pid=%s returncode=%s",
            root_session_id,
            process.pid,
            process.returncode,
            level=logging.WARNING if process.returncode not in (None, 0, -15) else logging.INFO,
        )

    async def _open_group_connection(
        self, group: AdapterGroup
    ) -> tuple[asyncio.StreamReader, asyncio.StreamWriter]:
        if group.process.returncode is not None:
            raise DAPError(
                f"debugpy adapter for root_session_id={group.root_session_id} has exited"
            )
        try:
            return await asyncio.open_connection(group.host, group.port)
        except (ConnectionError, OSError) as exc:
            raise DAPError(
                f"Could not connect to debugpy adapter for root_session_id={group.root_session_id}"
            ) from exc

    @staticmethod
    async def _send_frontend_response(
        writer: asyncio.StreamWriter,
        request: dict[str, Any],
        *,
        success: bool,
        body: Optional[dict[str, Any]] = None,
        message: Optional[str] = None,
        seq: int = 1,
    ) -> None:
        response: dict[str, Any] = {
            "seq": seq,
            "type": "response",
            "request_seq": int(request.get("seq", 0)),
            "success": success,
            "command": str(request.get("command", "")),
        }
        if body is not None:
            response["body"] = body
        if message:
            response["message"] = message
        await write_dap_message(writer, response, asyncio.Lock())

    async def _initialize_backend(
        self,
        group: AdapterGroup,
        initialize_request: dict[str, Any],
    ) -> tuple[asyncio.StreamReader, asyncio.StreamWriter]:
        backend_reader, backend_writer = await self._open_group_connection(group)
        outgoing = copy.deepcopy(initialize_request)
        outgoing["seq"] = 1
        try:
            await write_dap_message(backend_writer, outgoing, asyncio.Lock())
            while True:
                message = await asyncio.wait_for(
                    read_dap_message(backend_reader), timeout=config.DAP_REQUEST_TIMEOUT_SECONDS
                )
                if (
                    message.get("type") == "response"
                    and int(message.get("request_seq", -1)) == 1
                ):
                    if not message.get("success", False):
                        raise DAPError(message.get("message") or "debugpy initialize failed")
                    return backend_reader, backend_writer
                # No meaningful runtime event should precede initialize response.
                log.debug(
                    "Ignoring pre-initialize adapter message root_session_id=%s: %s",
                    group.root_session_id,
                    message.get("type"),
                )
        except Exception:
            backend_writer.close()
            try:
                await backend_writer.wait_closed()
            except Exception:
                pass
            raise

    async def _select_adapter_group(
        self,
        session_id: int,
        start_request: dict[str, Any],
    ) -> AdapterGroup:
        arguments = start_request.get("arguments")
        if not isinstance(arguments, dict):
            arguments = {}
        root_marker = arguments.get(_ROOT_SESSION_KEY)
        if isinstance(root_marker, int):
            group = self._adapter_groups.get(root_marker)
            if group is None:
                raise DAPError(
                    f"Subprocess session references missing root_session_id={root_marker}"
                )
            return group

        async with self._adapter_group_lock:
            # This is an independent root launch/attach. Each root needs its own
            # adapter because debugpy rejects a second independent launch on the
            # same adapter with '"attach" expected'.
            return await self._start_adapter_group(session_id)

    async def _accept_frontend(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        self._next_session_id += 1
        session_id = self._next_session_id
        group: Optional[AdapterGroup] = None
        session: Optional[DAPSession] = None
        start_request: Optional[dict[str, Any]] = None
        created_root_group = False
        try:
            initialize_request = await asyncio.wait_for(
                read_dap_message(reader), timeout=config.DAP_REQUEST_TIMEOUT_SECONDS
            )
            if not (
                initialize_request.get("type") == "request"
                and initialize_request.get("command") == "initialize"
            ):
                raise DAPError("The first DAP message must be an initialize request")

            # VS Code will not send launch/attach until initialize has a response,
            # but workspace/root routing information exists only in launch/attach.
            # Return debugpy's stable capabilities now, then replay initialize to
            # the selected adapter once the next request reveals the route.
            await self._send_frontend_response(
                writer,
                initialize_request,
                success=True,
                body=_debugpy_initialize_capabilities(),
                seq=1,
            )

            start_request = await asyncio.wait_for(
                read_dap_message(reader), timeout=config.DAP_REQUEST_TIMEOUT_SECONDS
            )
            if not (
                start_request.get("type") == "request"
                and start_request.get("command") in {"launch", "attach"}
            ):
                raise DAPError("Expected launch or attach immediately after initialize")

            group = await self._select_adapter_group(session_id, start_request)
            created_root_group = group.root_session_id == session_id
            backend_reader, backend_writer = await self._initialize_backend(
                group, initialize_request
            )

            session = DAPSession(
                session_id=session_id,
                frontend_reader=reader,
                frontend_writer=writer,
                backend_reader=backend_reader,
                backend_writer=backend_writer,
                proxy_host=self.host,
                proxy_port=self.port,
                stop_sequence_provider=self._allocate_stop_sequence,
            )
            self._sessions[session.session_id] = session
            group.session_ids.add(session.session_id)

            # launch/attach was consumed for routing, so prime it into the normal
            # sequence-remapping path before the forwarding tasks start.
            await session.forward_frontend_message(start_request)

            log.info(
                "VS Code connected; session_id=%s adapter_root=%s adapter_pid=%s active_sessions=%s",
                session.session_id,
                group.root_session_id,
                group.process.pid,
                len(self._sessions),
            )
            _audit(
                "session source=ide event=connected session_id=%s adapter_root=%s adapter_pid=%s",
                session.session_id,
                group.root_session_id,
                group.process.pid,
            )
            task = asyncio.create_task(
                self._run_session(session, group.root_session_id),
                name=f"dap-session-{session.session_id}",
            )
            self._session_tasks[session.session_id] = task
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log.exception("Could not establish DAP session_id=%s", session_id)
            if start_request is not None and not writer.is_closing():
                try:
                    await self._send_frontend_response(
                        writer,
                        start_request,
                        success=False,
                        message=str(exc),
                        seq=2,
                    )
                except Exception:
                    pass
            if session is not None:
                session.close("DAP setup failed")
                self._sessions.pop(session.session_id, None)
                if group is not None:
                    group.session_ids.discard(session.session_id)
            else:
                writer.close()
                try:
                    await writer.wait_closed()
                except Exception:
                    pass
            if created_root_group and group is not None and not group.session_ids:
                await self._stop_adapter_group(group.root_session_id)

    async def _run_session(self, session: DAPSession, adapter_root_session_id: int) -> None:
        try:
            await session.run()
        finally:
            self._sessions.pop(session.session_id, None)
            self._session_tasks.pop(session.session_id, None)
            group = self._adapter_groups.get(adapter_root_session_id)
            if group is not None:
                group.session_ids.discard(session.session_id)
                if not group.session_ids:
                    await self._stop_adapter_group(adapter_root_session_id)
            log.info(
                "DAP session_id=%s ended; adapter_root=%s active_sessions=%s",
                session.session_id,
                adapter_root_session_id,
                len(self._sessions),
            )
            _audit(
                "session event=ended session_id=%s adapter_root=%s active_sessions=%s",
                session.session_id,
                adapter_root_session_id,
                len(self._sessions),
            )
