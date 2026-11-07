from __future__ import annotations

import asyncio
import json
import logging
import os
import signal
import subprocess
import sys
import time
import uuid
from pathlib import Path
from typing import Any, Optional

from . import __version__, config
from .dap import DAPError
from .workspace import (
    CLIENT_ID_PARAM,
    CLIENT_WORKSPACE_PARAM,
    WORKSPACE_ENV_KEY,
    detect_client_workspace,
    normalize_workspace_root,
)

log = logging.getLogger(__name__)


class DaemonClient:
    def __init__(
        self,
        host: str = config.CONTROL_HOST,
        port: int = config.CONTROL_PORT,
        dap_host: str = config.DAP_PROXY_HOST,
        dap_port: int = config.DAP_PROXY_PORT,
        python_executable: str = sys.executable,
        spawn_if_missing: bool = True,
        workspace_root: Optional[str] = None,
        client_id: Optional[str] = None,
    ) -> None:
        self.host = host
        self.port = port
        self.dap_host = dap_host
        self.dap_port = dap_port
        self.python_executable = python_executable
        self.spawn_if_missing = spawn_if_missing
        self.workspace_root = detect_client_workspace(workspace_root)
        if workspace_root is not None:
            self.workspace_source = "explicit"
        elif os.getenv(WORKSPACE_ENV_KEY):
            self.workspace_source = "env"
        else:
            self.workspace_source = "cwd"
        self.mcp_roots: list[str] = []
        self.mcp_roots_error: Optional[str] = None
        self.client_id = client_id or f"mcp-{os.getpid()}-{uuid.uuid4().hex}"
        self._request_id = 0
        self._start_lock = asyncio.Lock()

    @property
    def workspace_is_explicit(self) -> bool:
        return self.workspace_source in {"explicit", "env"}

    def apply_mcp_roots(self, roots: list[str]) -> None:
        """Update this MCP frontend's workspace from client-provided Roots.

        An explicit constructor/env workspace always wins. With exactly one
        root, automatic routing is deterministic. Multiple roots intentionally
        remain ambiguous because the MCP Roots protocol describes scope, not an
        active folder; choosing the first root could silently cross projects.
        """
        normalized: list[str] = []
        for item in roots:
            value = normalize_workspace_root(item)
            if value is not None and value not in normalized:
                normalized.append(value)
        self.mcp_roots = normalized
        self.mcp_roots_error = None

        if self.workspace_is_explicit:
            return
        if len(normalized) == 1:
            self.workspace_root = normalized[0]
            self.workspace_source = "mcp_roots"
        elif len(normalized) > 1:
            self.workspace_source = "mcp_roots_ambiguous"
        elif self.workspace_source in {"mcp_roots", "mcp_roots_ambiguous"}:
            # The client removed all roots. Fall back to the process cwd rather
            # than leaving a stale project identity behind.
            self.workspace_root = detect_client_workspace(None)
            self.workspace_source = "cwd"

    async def call(self, method: str, params: Optional[dict[str, Any]] = None) -> Any:
        await self.ensure_daemon()
        arguments = params or {}
        rpc_timeout = config.RPC_REQUEST_TIMEOUT_SECONDS
        if method == "evaluate":
            requested = arguments.get("timeout_seconds")
            dap_timeout: Optional[float] = None
            if requested is None:
                dap_timeout = config.EVALUATE_TIMEOUT_SECONDS
            elif isinstance(requested, (int, float)) and not isinstance(requested, bool):
                if float(requested) > 0:
                    dap_timeout = float(requested)
            if dap_timeout is not None:
                # The control RPC must outlive the DAP evaluate timeout; otherwise
                # a caller-requested longer evaluate could be abandoned by the
                # frontend before the daemon has a chance to return its result.
                rpc_timeout = max(rpc_timeout, dap_timeout + 15.0)
        return await self._call_once(method, arguments, timeout=rpc_timeout)

    async def ping(self) -> Optional[dict[str, Any]]:
        try:
            result = await self._call_once("__ping__", {}, timeout=1.0)
            return result if isinstance(result, dict) else None
        except Exception:
            return None

    async def ensure_daemon(self) -> dict[str, Any]:
        async with self._start_lock:
            ping = await self.ping()
            if ping is not None:
                return await self._handle_version(ping)

            if not self.spawn_if_missing:
                raise DAPError(f"Shared debugger daemon is not listening on {self.host}:{self.port}")

            # A legacy v0.2 process owns 4711 but has no shared control port.
            if await self._port_open(self.dap_host, self.dap_port):
                for _ in range(10):
                    await asyncio.sleep(0.1)
                    ping = await self.ping()
                    if ping is not None:
                        return await self._handle_version(ping)
                raise DAPError(
                    f"DAP port {self.dap_host}:{self.dap_port} is occupied, but no v0.3 shared daemon "
                    f"responds on {self.host}:{self.port}. Stop the old debugger_mcp process once, then retry."
                )

            self._spawn_daemon()
            deadline = time.monotonic() + config.DAEMON_START_TIMEOUT_SECONDS
            while time.monotonic() < deadline:
                await asyncio.sleep(0.1)
                ping = await self.ping()
                if ping is not None:
                    return await self._handle_version(ping)

            log_path = Path(config.DAEMON_LOG_PATH)
            suffix = f" See daemon log: {log_path}" if log_path else ""
            raise DAPError(
                f"Shared debugger daemon did not start on {self.host}:{self.port}.{suffix}"
            )

    async def _handle_version(self, ping: dict[str, Any]) -> dict[str, Any]:
        daemon_version = str(ping.get("version", "unknown"))
        if daemon_version == __version__:
            return ping

        # Version mismatch means the installed MCP frontend and shared daemon no
        # longer implement the same protocol. Replace the daemon unconditionally,
        # even when VS Code still has active DAP connections. Those debug sessions
        # will be disconnected and must be started again after the upgrade.
        daemon_pid = self._coerce_pid(ping.get("pid"))
        log.warning(
            "Replacing shared debugger daemon version %s with %s pid=%s; active DAP sessions will be disconnected",
            daemon_version,
            __version__,
            daemon_pid,
        )
        try:
            await self._call_once("__shutdown__", {"force": True}, timeout=2.0)
        except Exception:
            # Older or unhealthy daemons may not implement force shutdown. The
            # process-level fallback below still guarantees replacement.
            pass

        stopped = await self._wait_for_daemon_ports_closed(timeout=3.0)
        if not stopped and daemon_pid is not None:
            self._terminate_daemon_process(daemon_pid, force=False)
            stopped = await self._wait_for_daemon_ports_closed(timeout=2.0)
        if not stopped and daemon_pid is not None:
            self._terminate_daemon_process(daemon_pid, force=True)
            stopped = await self._wait_for_daemon_ports_closed(timeout=2.0)

        # Another MCP frontend may have won the replacement race while this one
        # was waiting. Reuse that daemon when it already runs the current version.
        current = await self.ping()
        if current is not None and str(current.get("version")) == __version__:
            return current

        if not stopped:
            raise DAPError(
                f"Could not stop debugger daemon {daemon_version} pid={daemon_pid}; "
                f"ports {self.dap_host}:{self.dap_port} or {self.host}:{self.port} are still active"
            )

        self._spawn_daemon()
        deadline = time.monotonic() + config.DAEMON_START_TIMEOUT_SECONDS
        while time.monotonic() < deadline:
            await asyncio.sleep(0.1)
            new_ping = await self.ping()
            if new_ping is not None and str(new_ping.get("version")) == __version__:
                return new_ping
        raise DAPError(f"Could not replace debugger daemon {daemon_version} with {__version__}")

    async def _wait_for_daemon_ports_closed(self, timeout: float) -> bool:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            control_open, dap_open = await asyncio.gather(
                self._port_open(self.host, self.port),
                self._port_open(self.dap_host, self.dap_port),
            )
            if not control_open and not dap_open:
                return True
            await asyncio.sleep(0.1)
        control_open, dap_open = await asyncio.gather(
            self._port_open(self.host, self.port),
            self._port_open(self.dap_host, self.dap_port),
        )
        return not control_open and not dap_open

    @staticmethod
    def _coerce_pid(value: Any) -> Optional[int]:
        if isinstance(value, bool):
            return None
        try:
            pid = int(value)
        except (TypeError, ValueError):
            return None
        return pid if pid > 0 else None

    @staticmethod
    def _terminate_daemon_process(pid: int, force: bool) -> None:
        if pid <= 0 or pid == os.getpid():
            return
        if os.name == "nt":
            command = ["taskkill", "/PID", str(pid), "/T"]
            if force:
                command.append("/F")
            try:
                subprocess.run(
                    command,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    check=False,
                    timeout=3.0,
                )
            except Exception:
                pass
            return

        sig = signal.SIGKILL if force else signal.SIGTERM
        try:
            process_group = os.getpgid(pid)
            if process_group == pid:
                os.killpg(process_group, sig)
            else:
                os.kill(pid, sig)
        except (ProcessLookupError, PermissionError, OSError):
            pass

    async def _call_once(
        self,
        method: str,
        params: dict[str, Any],
        timeout: float = config.RPC_REQUEST_TIMEOUT_SECONDS,
    ) -> Any:
        self._request_id += 1
        request_params = dict(params)
        request_params[CLIENT_WORKSPACE_PARAM] = self.workspace_root
        request_params[CLIENT_ID_PARAM] = self.client_id
        request = {"id": self._request_id, "method": method, "params": request_params}
        reader, writer = await asyncio.wait_for(asyncio.open_connection(self.host, self.port), timeout=timeout)
        try:
            writer.write((json.dumps(request, ensure_ascii=False, separators=(",", ":")) + "\n").encode("utf-8"))
            await writer.drain()
            raw = await asyncio.wait_for(reader.readline(), timeout=timeout)
            if not raw:
                raise DAPError("Shared debugger daemon closed the RPC connection without a response")
            response = json.loads(raw.decode("utf-8"))
            error = response.get("error")
            if error:
                raise DAPError(str(error.get("message") or error))
            return response.get("result")
        finally:
            writer.close()
            try:
                await writer.wait_closed()
            except Exception:
                pass

    def _spawn_daemon(self) -> None:
        log_path = Path(config.DAEMON_LOG_PATH)
        log_path.parent.mkdir(parents=True, exist_ok=True)
        log_file = open(log_path, "ab", buffering=0)
        kwargs: dict[str, Any] = {
            "stdin": subprocess.DEVNULL,
            "stdout": log_file,
            "stderr": subprocess.STDOUT,
            "env": {**os.environ, "PYTHONUNBUFFERED": "1"},
            "close_fds": True,
        }
        if os.name == "nt":
            kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.DETACHED_PROCESS
        else:
            kwargs["start_new_session"] = True
        try:
            subprocess.Popen(
                [self.python_executable, "-m", "debugger_mcp.daemon"],
                **kwargs,
            )
        finally:
            log_file.close()

    @staticmethod
    async def _port_open(host: str, port: int) -> bool:
        try:
            reader, writer = await asyncio.wait_for(asyncio.open_connection(host, port), timeout=0.25)
            writer.close()
            try:
                await writer.wait_closed()
            except Exception:
                pass
            return True
        except Exception:
            return False
