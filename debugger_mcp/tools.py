from __future__ import annotations

import ast
import asyncio
import json
import os
from contextlib import contextmanager
from contextvars import ContextVar
from pathlib import Path
from typing import Any, Iterator, Optional

from . import config
from .dap import DAPError, DAPProxy, DAPSession
from .frame_eval import (
    FrameSelector,
    capture_frame_expression,
    cleanup_frame_expression,
    execute_with_frames_expression,
    extract_frame_eval_selectors,
    new_frame_token,
)
from .workspace import normalize_workspace_root


_INSPECT_PROFILES: dict[str, dict[str, Any]] = {
    "summary": {
        "max_depth": 2,
        "max_children": 8,
        "head": 6,
        "tail": 2,
        "max_nodes": 60,
        "max_chars": 6_000,
        "max_value_chars": 80,
        "max_seconds": 0.75,
    },
    "normal": {
        "max_depth": 4,
        "max_children": 12,
        "head": 8,
        "tail": 4,
        "max_nodes": 160,
        "max_chars": 14_000,
        "max_value_chars": 120,
        "max_seconds": 1.25,
    },
    "deep": {
        "max_depth": 6,
        "max_children": 20,
        "head": 12,
        "tail": 8,
        "max_nodes": 400,
        "max_chars": 28_000,
        "max_value_chars": 160,
        "max_seconds": 2.0,
    },
}

_INSPECT_HARD_LIMITS: dict[str, float] = {
    "max_depth": 8,
    "max_children": 30,
    "head": 20,
    "tail": 10,
    "max_nodes": 500,
    "max_chars": 30_000,
    "max_value_chars": 200,
    "max_seconds": 3.0,
}


class DebuggerTools:
    def __init__(self, proxy: DAPProxy) -> None:
        self.proxy = proxy
        self._inspect_source = Path(__file__).with_name("inspect_runtime.py").read_text(encoding="utf-8")
        self._inspect_helper_globals: set[tuple[int, str]] = set()
        self._client_workspace: ContextVar[Optional[str]] = ContextVar(
            "debugger_mcp_client_workspace", default=None
        )

    @contextmanager
    def client_workspace_scope(self, workspace_root: Optional[str]) -> Iterator[None]:
        token = self._client_workspace.set(normalize_workspace_root(workspace_root))
        try:
            yield
        finally:
            self._client_workspace.reset(token)

    @property
    def client_workspace(self) -> Optional[str]:
        return self._client_workspace.get()

    def _session(
        self,
        session_id: Optional[int] = None,
        require_stopped: bool = False,
        frame_id: Optional[int] = None,
    ) -> DAPSession:
        if session_id is None and frame_id is not None:
            matched = self.proxy.find_session_for_frame(frame_id, workspace_root=self.client_workspace)
            if matched is not None:
                if require_stopped and matched.state.status != "stopped":
                    raise DAPError(
                        f"The session owning frame_id={frame_id} is {matched.state.status!r}, not stopped"
                    )
                return matched

        session = self.proxy.select_session(
            session_id=session_id,
            require_stopped=require_stopped,
            workspace_root=self.client_workspace,
        )
        if session is None:
            active = self.proxy.sessions
            workspace = self.client_workspace
            if active and workspace is not None:
                matching = self.proxy.sessions_for_workspace(workspace)
                stopped_matching = [item for item in matching if item.state.status == "stopped"]
                state_word = "stopped " if require_stopped else ""
                raise DAPError(
                    f"No {state_word}debug session matches the current MCP workspace {workspace!r}. "
                    f"active_total={len(active)}, workspace_matches={len(matching)}, "
                    f"workspace_stopped={len(stopped_matching)}, "
                    f"unscoped={sum(1 for item in active if item.workspace_root is None)}. "
                    f"Add \"DEBUGGER_MCP_WORKSPACE\": \"${{workspaceFolder}}\" under this project's launch.json \"env\" object. "
                    f"Call attach_debugpy(full=true) only when detailed session diagnosis is needed; "
                    f"DAP cwd is diagnostic only and never assigns ownership."
                )
            if active and require_stopped:
                stopped = sum(1 for item in active if item.state.status == "stopped")
                raise DAPError(
                    f"No stopped debug session is available. active_total={len(active)}, stopped_total={stopped}. "
                    f"Call attach_debugpy(full=true) only when detailed session diagnosis is needed."
                )
            raise DAPError(
                f"No VS Code debug session is connected to the shared DAP proxy at "
                f"{self.proxy.host}:{self.proxy.port}. Start the matching launch.json configuration first."
            )
        return session

    @staticmethod
    def _meta(session: DAPSession) -> dict[str, Any]:
        return session.metadata()

    async def attach_debugpy(self, full: bool = False) -> dict[str, Any]:
        sessions = self.proxy.sessions
        workspace = self.client_workspace
        workspace_sessions = self.proxy.sessions_for_workspace(workspace)
        selected = self.proxy.select_session(workspace_root=workspace)
        compact = {
            "proxy_host": self.proxy.host,
            "proxy_port": self.proxy.port,
            "vscode_connected": bool(sessions),
            "session_count": len(sessions),
            "client_workspace": workspace,
            "workspace_connected": bool(workspace_sessions),
            "workspace_session_count": len(workspace_sessions),
            "workspace_stopped_count": sum(1 for item in workspace_sessions if item.state.status == "stopped"),
            "unscoped_session_count": sum(1 for item in sessions if item.workspace_root is None),
            "selected_session_id": selected.session_id if selected is not None else None,
            "selected_debug_state": selected.state.status if selected is not None else "disconnected",
            "selected_process_name": selected.process_name if selected is not None else None,
            "selected_stop_id": selected.state.stop_id if selected is not None else None,
            "selected_stopped_thread_id": selected.state.stopped_thread_id if selected is not None else None,
        }
        if not full:
            return compact
        return {
            **compact,
            "selected_session": selected.metadata() if selected is not None else None,
            "sessions": [session.metadata() for session in sessions],
            "note": (
                "Detailed diagnostic mode. Ordinary tools select the latest stopped session inside the current "
                "workspace in the daemon and do not return or require this session list. launch.json must include "
                "env.DEBUGGER_MCP_WORKSPACE=${workspaceFolder}; DAP cwd is diagnostic only."
            ),
        }

    async def get_threads(
        self,
        session_id: Optional[int] = None,
        full: bool = False,
    ) -> dict[str, Any]:
        session = self._session(session_id=session_id)
        body = await session.request("threads")
        threads = body.get("threads", [])
        if full:
            return {
                **self._meta(session),
                "threads": threads,
            }
        lines = []
        for thread in threads:
            thread_id = thread.get("id")
            name = str(thread.get("name") or "")
            lines.append(f"[{thread_id}] {name}".rstrip())
        return {"__mcp_text__": "\n".join(lines) if lines else "<no threads>"}

    async def get_stack(
        self,
        thread_id: Optional[int] = None,
        start_frame: int = 0,
        levels: Optional[int] = None,
        session_id: Optional[int] = None,
        full: bool = False,
    ) -> dict[str, Any]:
        """Return the requested stack without silently dropping frames.

        Default ``levels=None`` means the complete stack from ``start_frame``.
        A bounded result is produced only when the caller explicitly supplies
        ``levels``. The default text format is compact, but every returned frame
        is preserved.
        """
        session = self._session(session_id=session_id, require_stopped=True)
        selected_thread = thread_id or session.state.stopped_thread_id
        if selected_thread is None:
            raise DAPError("No stopped thread is known. Provide thread_id explicitly.")

        body = await session.stack_trace(selected_thread, start_frame=start_frame, levels=levels)
        raw_frames = list(body.get("stackFrames") or [])

        # Some adapters may still return a partial first response while exposing
        # totalFrames. When the caller asked for the default complete stack, fetch
        # any remainder rather than emitting an implicit "... more" summary.
        total = body.get("totalFrames")
        if levels is None and isinstance(total, int):
            next_start = start_frame + len(raw_frames)
            while next_start < total:
                remainder = await session.stack_trace(
                    selected_thread,
                    start_frame=next_start,
                    levels=total - next_start,
                )
                more = list(remainder.get("stackFrames") or [])
                if not more:
                    break
                raw_frames.extend(more)
                next_start += len(more)

        if full:
            return {
                **self._meta(session),
                "thread_id": selected_thread,
                "start_frame": start_frame,
                "requested_levels": levels,
                "total_frames": total,
                "stack_frames": raw_frames,
            }

        lines = [
            self._format_frame_line(session, frame, stack_index=start_frame + offset)
            for offset, frame in enumerate(raw_frames)
        ]
        # Only report omitted frames when the caller explicitly requested a
        # bounded slice. Default get_stack() never truncates the stack.
        if levels is not None and isinstance(total, int):
            shown_through = start_frame + len(raw_frames)
            if total > shown_through:
                lines.append(f"... +{total - shown_through} more frames (explicit levels={levels})")
        return {"__mcp_text__": "\n".join(lines) if lines else "<no stack frames>"}

    async def _resolve_frame_id(self, session: DAPSession, frame_id: Optional[int]) -> int:
        if frame_id is not None:
            session.validate_frame_id(frame_id)
            return frame_id

        selected_thread = session.state.stopped_thread_id
        if selected_thread is None:
            raise DAPError("No stopped thread is known. Stop at a breakpoint first.")

        cached = session.cached_top_frame(selected_thread)
        if cached is not None and isinstance(cached.get("id"), int):
            return int(cached["id"])

        body = await session.stack_trace(selected_thread, start_frame=0, levels=1)
        frames = body.get("stackFrames", [])
        if not frames:
            raise DAPError("The stopped thread has no stack frames")
        return int(frames[0]["id"])

    async def _top_frame(self, session: DAPSession) -> dict[str, Any]:
        thread_id = session.state.stopped_thread_id
        if thread_id is None:
            raise DAPError("No stopped thread is known")
        body = await session.stack_trace(thread_id, start_frame=0, levels=1)
        frames = body.get("stackFrames") or []
        if not frames:
            raise DAPError("The stopped thread has no stack frames")
        return frames[0]

    async def get_locals(
        self,
        frame_id: Optional[int] = None,
        session_id: Optional[int] = None,
        full: bool = False,
    ) -> dict[str, Any]:
        """Return every local variable, using a compact representation by default.

        Compact mode summarizes each value (type/shape/dtype/device/len/scalar)
        but does not drop local-variable names. ``full=True`` returns the raw
        debugpy variables response without field or length truncation.
        """
        session = self._session(session_id=session_id, require_stopped=True, frame_id=frame_id)
        selected_frame = await self._resolve_frame_id(session, frame_id)

        if not full:
            await self._ensure_inspect_helper(session, selected_frame)
            body = await session.request(
                "evaluate",
                {
                    "expression": "_debugger_mcp_locals_summary_json(locals())",
                    "frameId": selected_frame,
                    "context": "repl",
                },
            )
            payload = self._parse_json_payload(
                body.get("result"), operation="get_locals summary"
            )
            if payload.get("error"):
                raise DAPError(str(payload["error"]))
            return {
                "__mcp_text__": self._format_locals_summary_text(payload),
            }

        scopes_body = await session.request("scopes", {"frameId": selected_frame})
        scopes = scopes_body.get("scopes", [])
        locals_scope = next(
            (scope for scope in scopes if str(scope.get("name", "")).lower() == "locals"),
            None,
        )
        if locals_scope is None:
            locals_scope = next(
                (scope for scope in scopes if not scope.get("expensive", False)),
                None,
            )
        if locals_scope is None:
            raise DAPError("No readable local scope was returned by debugpy")

        # Omitting DAP start/count requests the complete variable collection.
        variables_body = await session.request(
            "variables",
            {"variablesReference": locals_scope["variablesReference"]},
        )
        return {
            **self._meta(session),
            "frame_id": selected_frame,
            "mode": "full",
            "scope": locals_scope,
            "variables_response": variables_body,
        }

    async def evaluate(
        self,
        expression: str,
        timeout_seconds: Optional[float] = None,
    ) -> dict[str, Any]:
        """Execute Python in the current paused runtime.

        The expression runs in the current workspace's latest paused top frame.
        Use ``frame_eval(expression, file, line, function)`` inside the expression
        to access another frame still present in the current active call stack.
        DAP frame IDs remain an internal implementation detail.
        """
        if timeout_seconds is None:
            timeout = config.EVALUATE_TIMEOUT_SECONDS
        else:
            if isinstance(timeout_seconds, bool):
                raise ValueError("evaluate timeout_seconds must be a positive number")
            timeout = float(timeout_seconds)
            if timeout <= 0:
                raise ValueError("evaluate timeout_seconds must be > 0")

        frame_selectors = extract_frame_eval_selectors(expression)
        session = self._session(require_stopped=True)
        if frame_selectors:
            return await self._evaluate_with_frame_eval(
                session, expression, timeout, frame_selectors
            )
        selected_frame = await self._resolve_frame_id(session, None)

        body = await session.request(
            "evaluate",
            {"expression": expression, "frameId": selected_frame, "context": "repl"},
            timeout_seconds=timeout,
        )
        value = body.get("result")
        # The expression result is the entire public result of evaluate(). Keep it
        # complete: no DAP/session envelope and no debugger-mcp truncation.
        return {"__mcp_text__": "" if value is None else str(value)}

    async def _evaluate_with_frame_eval(
        self,
        session: DAPSession,
        expression: str,
        timeout: float,
        selectors: tuple[FrameSelector, ...],
    ) -> dict[str, Any]:
        """Run one evaluate expression with real objects from active frames.

        ``frame_eval(expression, file, line, function)`` is injected only into
        this expression. It evaluates against one exact frame from the current
        stopped thread and returns the real in-process object to the expression.
        Frames no longer present in that stack, other processes, and other
        sessions are intentionally outside this operation.
        """
        thread_id = session.state.stopped_thread_id
        if thread_id is None:
            raise DAPError("No stopped thread is known. Stop at a breakpoint first.")
        frames = await self._complete_stack(session, thread_id)
        if not frames or not isinstance(frames[0].get("id"), int):
            raise DAPError("The stopped thread has no executable top frame")

        top_frame_id = int(frames[0]["id"])
        targets = [
            (
                selector,
                *self._match_frame_selector(
                    session, frames, selector, operation="frame_eval"
                ),
            )
            for selector in selectors
        ]
        token = new_frame_token()
        try:
            for selector, stack_index, frame in targets:
                frame_id = frame.get("id")
                if not isinstance(frame_id, int):
                    raise DAPError(
                        f"Matched frame_eval target has no valid DAP frame id: "
                        f"{selector.file}:{selector.line} {selector.function}"
                    )
                source_path = str((frame.get("source") or {}).get("path") or "")
                metadata = {
                    "file": self._display_path(session, source_path),
                    "absolute_file": os.path.normpath(source_path),
                    "line": frame.get("line"),
                    "function": str(frame.get("name") or ""),
                    "stack_index": stack_index,
                }
                await session.request(
                    "evaluate",
                    {
                        "expression": capture_frame_expression(token, selector, metadata),
                        "frameId": frame_id,
                        "context": "repl",
                    },
                )
        except Exception:
            try:
                await session.request(
                    "evaluate",
                    {
                        "expression": cleanup_frame_expression(token),
                        "frameId": top_frame_id,
                        "context": "repl",
                    },
                    timeout_seconds=min(timeout, 5.0),
                )
            except Exception:
                pass
            raise

        try:
            body = await session.request(
                "evaluate",
                {
                    "expression": execute_with_frames_expression(token, expression),
                    "frameId": top_frame_id,
                    "context": "repl",
                },
                timeout_seconds=timeout,
            )
        except Exception:
            try:
                await session.request(
                    "evaluate",
                    {
                        "expression": cleanup_frame_expression(token),
                        "frameId": top_frame_id,
                        "context": "repl",
                    },
                    timeout_seconds=min(timeout, 5.0),
                )
            except Exception:
                pass
            raise
        value = body.get("result")
        return {"__mcp_text__": "" if value is None else str(value)}

    async def _complete_stack(
        self,
        session: DAPSession,
        thread_id: int,
    ) -> list[dict[str, Any]]:
        body = await session.stack_trace(thread_id, start_frame=0, levels=None)
        frames = list(body.get("stackFrames") or [])
        total = body.get("totalFrames")
        if isinstance(total, int):
            next_start = len(frames)
            while next_start < total:
                remainder = await session.stack_trace(
                    thread_id, start_frame=next_start, levels=total - next_start
                )
                more = list(remainder.get("stackFrames") or [])
                if not more:
                    break
                frames.extend(more)
                next_start += len(more)
        return frames

    def _match_frame_selector(
        self,
        session: DAPSession,
        frames: list[dict[str, Any]],
        selector: FrameSelector,
        *,
        operation: str,
    ) -> tuple[int, dict[str, Any]]:
        selector_file = self._normalize_stack_selector_path(selector.file)
        matches: list[tuple[int, dict[str, Any]]] = []
        for stack_index, frame in enumerate(frames):
            display_file = self._display_path(session, (frame.get("source") or {}).get("path"))
            if (
                self._normalize_stack_selector_path(display_file) == selector_file
                and frame.get("line") == selector.line
                and str(frame.get("name") or "") == selector.function
            ):
                matches.append((stack_index, frame))

        target = f"{selector.file}:{selector.line} {selector.function}"
        if not matches:
            current = "\n".join(
                self._format_frame_line(session, frame, stack_index=index)
                for index, frame in enumerate(frames)
            ) or "<no stack frames>"
            raise DAPError(
                f"No current stack frame exactly matches {operation} selector: {target}\n"
                "Call get_stack() again and copy the current file, line, and function exactly.\n"
                f"Current stack:\n{current}"
            )
        if len(matches) > 1:
            candidates = "\n".join(
                self._format_frame_line(session, frame, stack_index=index)
                for index, frame in matches
            )
            raise DAPError(
                f"Multiple current stack frames exactly match {operation} selector: {target}\n"
                "debugger-mcp will not guess between recursive/duplicate frames.\n"
                f"Matching frames:\n{candidates}"
            )
        return matches[0]

    async def _resolve_frame_selector(
        self,
        session: DAPSession,
        *,
        file: str,
        line: int,
        function: str,
        operation: str,
    ) -> int:
        """Resolve a human-readable stack selector against the current pause.

        The selector is deliberately strict. ``file`` is compared with the exact
        workspace-relative/absolute display path produced by ``get_stack()``
        (normalizing only path separators), while line and function must match
        exactly. Zero or multiple matches are errors; debugger-mcp never guesses.
        """
        thread_id = session.state.stopped_thread_id
        if thread_id is None:
            raise DAPError("No stopped thread is known. Stop at a breakpoint first.")

        frames = await self._complete_stack(session, thread_id)
        selector = FrameSelector(file, line, function)
        _, frame = self._match_frame_selector(
            session, frames, selector, operation=operation
        )
        frame_id = frame.get("id")
        if not isinstance(frame_id, int):
            raise DAPError(
                f"Matched stack frame has no valid DAP frame id: "
                f"{file}:{line} {function}"
            )
        return frame_id

    @staticmethod
    def _normalize_stack_selector_path(value: Any) -> str:
        return str(value or "").replace("\\", "/")

    async def inspect_structure(
        self,
        expression: str,
        profile: str = "summary",
        name: Optional[str] = None,
        file: Optional[str] = None,
        line: Optional[int] = None,
        function: Optional[str] = None,
        max_depth: Optional[int] = None,
        max_children: Optional[int] = None,
        max_nodes: Optional[int] = None,
        max_chars: Optional[int] = None,
    ) -> dict[str, Any]:
        """Return a bounded structural summary in the current or an exact caller frame.

        With no selector, inspect the latest paused top frame. For precise
        caller-frame backtracking, ``file + line + function`` must be supplied
        together and copied from the current ``get_stack()`` output.
        """
        if profile not in _INSPECT_PROFILES:
            raise ValueError(f"Unknown profile={profile!r}; expected one of {sorted(_INSPECT_PROFILES)}")

        selector_values = (file, line, function)
        selector_count = sum(value is not None for value in selector_values)
        if selector_count not in (0, 3):
            raise ValueError(
                "inspect_structure frame backtracking requires file, line, and function together; "
                "omit all three for the current top frame"
            )
        if file is not None and not str(file).strip():
            raise ValueError("inspect_structure file must be a non-empty path from get_stack()")
        if function is not None and not str(function).strip():
            raise ValueError(
                "inspect_structure function must be a non-empty function name from get_stack()"
            )
        if line is not None:
            if isinstance(line, bool) or not isinstance(line, int) or line <= 0:
                raise ValueError("inspect_structure line must be a positive integer from get_stack()")

        session = self._session(require_stopped=True)
        if selector_count == 0:
            selected_frame = await self._resolve_frame_id(session, None)
        else:
            assert file is not None and line is not None and function is not None
            selected_frame = await self._resolve_frame_selector(
                session,
                file=str(file),
                line=line,
                function=str(function),
                operation="inspect_structure",
            )

        settings = dict(_INSPECT_PROFILES[profile])
        overrides = {
            "max_depth": max_depth,
            "max_children": max_children,
            "max_nodes": max_nodes,
            "max_chars": max_chars,
        }
        for key, value in overrides.items():
            if value is None:
                continue
            if int(value) < 1:
                raise ValueError(f"{key} must be >= 1")
            settings[key] = min(int(value), int(_INSPECT_HARD_LIMITS[key]))
        for key, hard_limit in _INSPECT_HARD_LIMITS.items():
            settings[key] = min(settings[key], hard_limit)
        settings["head"] = min(settings["head"], settings["max_children"])
        settings["tail"] = min(
            settings["tail"], max(0, settings["max_children"] - settings["head"])
        )

        await self._ensure_inspect_helper(session, selected_frame)
        expression_call = (
            "_debugger_mcp_inspect_json((%s), name=%r, %s)"
            % (
                expression,
                name or expression,
                ", ".join(f"{key}={value!r}" for key, value in settings.items()),
            )
        )
        body = await session.request(
            "evaluate",
            {"expression": expression_call, "frameId": selected_frame, "context": "repl"},
        )
        raw = body.get("result")
        payload = self._parse_json_payload(raw, operation="inspect_structure")
        if payload.get("error"):
            raise DAPError(str(payload["error"]))
        rows = payload.get("rows") or []
        text = self._format_structure_text(rows, payload)
        return {"__mcp_text__": text}

    async def _ensure_inspect_helper(self, session: DAPSession, frame_id: int) -> None:
        # Helpers live in the selected frame's module globals, not in a
        # session-wide namespace. Caller-frame backtracking can cross modules, so
        # cache installation by the identity of globals() within the session.
        identity_body = await session.request(
            "evaluate",
            {"expression": "id(globals())", "frameId": frame_id, "context": "repl"},
        )
        globals_identity = str(identity_body.get("result"))
        cache_key = (session.session_id, globals_identity)
        if cache_key in self._inspect_helper_globals:
            return

        expression = "exec(%r, globals(), globals())" % self._inspect_source
        await session.request(
            "evaluate",
            {"expression": expression, "frameId": frame_id, "context": "repl"},
        )
        self._inspect_helper_globals.add(cache_key)

    @staticmethod
    def _parse_json_payload(raw: Any, operation: str) -> dict[str, Any]:
        if not isinstance(raw, str):
            raise DAPError(f"{operation} returned a non-text result: {type(raw).__name__}")
        candidates = [raw]
        try:
            unquoted = ast.literal_eval(raw)
            if isinstance(unquoted, str):
                candidates.append(unquoted)
        except Exception:
            pass
        for candidate in candidates:
            try:
                value = json.loads(candidate)
                if isinstance(value, dict):
                    return value
            except json.JSONDecodeError:
                continue
        raise DAPError(f"Could not decode {operation} result: {raw}")

    @staticmethod
    def _format_locals_summary_text(payload: dict[str, Any]) -> str:
        lines: list[str] = []
        for item in payload.get("variables") or []:
            name = str(item.get("name") or "")
            if not name:
                continue
            # Python module/runtime bookkeeping variables are rarely useful to
            # the Agent. Keep them discoverable without spending tokens on type,
            # length, or value metadata.
            if name.startswith("__"):
                lines.append(name)
                continue

            parts = [name]
            type_name = str(item.get("type") or "")
            if type_name:
                parts.append(type_name)

            if "shape" in item:
                shape = item.get("shape")
                if isinstance(shape, list):
                    shape_text = json.dumps(shape, ensure_ascii=False, separators=(",", ":"))
                else:
                    shape_text = str(shape)
                parts.append(f"shape={shape_text}")

            dtype = str(item.get("dtype") or "")
            if dtype:
                if dtype.startswith("torch."):
                    dtype = dtype[6:]
                parts.append(dtype)

            device = str(item.get("device") or "")
            if device:
                parts.append(device)

            if item.get("requires_grad") is True:
                parts.append("requires_grad")

            if "len" in item:
                parts.append(f"len={item.get('len')}")
            elif "value" in item:
                value = item.get("value")
                parts.append("None" if value is None else str(value))

            lines.append(", ".join(parts))

        total = payload.get("total")
        returned = payload.get("returned")
        if payload.get("has_more") and isinstance(total, int) and isinstance(returned, int):
            lines.append(f"... +{max(0, total - returned)} more")
        return "\n".join(lines) if lines else "<no locals>"

    @staticmethod
    def _format_structure_text(
        rows: list[dict[str, Any]],
        payload: dict[str, Any],
    ) -> str:
        lines: list[str] = []
        for row in rows:
            path = str(row.get("path") or "")
            type_name = str(row.get("type") or "")
            meta_parts = []
            if row.get("shape"):
                meta_parts.append(f"shape={row['shape']}")
            if row.get("dtype"):
                meta_parts.append(f"dtype={row['dtype']}")
            if row.get("device"):
                meta_parts.append(f"device={row['device']}")
            note = str(row.get("note") or "")
            right = " ".join(part for part in [type_name, *meta_parts, note] if part)
            lines.append(f"{path}: {right}".rstrip())
        if payload.get("truncated"):
            lines.append(f"<truncated: {payload.get('truncation_reason') or 'limit'}>")
        # inspect_structure is intentionally bounded by its own explicit
        # depth/children/node/character limits. Do not apply a second hidden
        # truncation layer after the helper has already reported its limit.
        return "\n".join(lines) if lines else "<empty structure>"

    async def _step(
        self,
        command: str,
        thread_id: Optional[int] = None,
        session_id: Optional[int] = None,
        full: bool = False,
    ) -> dict[str, Any]:
        session = self._session(session_id=session_id, require_stopped=True)
        selected_thread = thread_id or session.state.stopped_thread_id
        if selected_thread is None:
            raise DAPError("No stopped thread is known. Provide thread_id explicitly.")

        session.prepare_for_step()
        await session.request(command, {"threadId": selected_thread})
        state = await session.wait_until_stopped(config.STEP_TIMEOUT_SECONDS)

        stopped_thread = state.stopped_thread_id or selected_thread
        body = await session.stack_trace(stopped_thread, start_frame=0, levels=1)
        raw_top = body.get("stackFrames", [None])[0] if body.get("stackFrames") else None
        if not isinstance(raw_top, dict):
            raise DAPError("The stopped thread has no stack frames after stepping")
        if full:
            return {
                **self._meta(session),
                "operation": command,
                "thread_id": stopped_thread,
                "stop_event": state.last_event,
                "top_frame": raw_top,
            }
        reason = state.stop_reason or "unknown"
        return {
            "__mcp_text__": (
                f"{self._format_frame_location(session, raw_top)} | "
                f"reason={reason} thread={stopped_thread}"
            )
        }

    async def step_over(
        self,
        thread_id: Optional[int] = None,
        session_id: Optional[int] = None,
        full: bool = False,
    ) -> dict[str, Any]:
        return await self._step("next", thread_id, session_id, full)

    async def step_into(
        self,
        thread_id: Optional[int] = None,
        session_id: Optional[int] = None,
        full: bool = False,
    ) -> dict[str, Any]:
        return await self._step("stepIn", thread_id, session_id, full)

    async def step_out(
        self,
        thread_id: Optional[int] = None,
        session_id: Optional[int] = None,
        full: bool = False,
    ) -> dict[str, Any]:
        return await self._step("stepOut", thread_id, session_id, full)

    async def run_to_line(
        self,
        file: str,
        line: int,
        reason: str,
        condition: Optional[str] = None,
        wait_seconds: float = 30.0,
        cross_file_authorized: bool = False,
        session_id: Optional[int] = None,
        thread_id: Optional[int] = None,
    ) -> dict[str, Any]:
        del reason  # Required by the tool contract to make the agent state its reachability reasoning.
        session = self._session(session_id=session_id, require_stopped=True)
        selected_thread = thread_id or session.state.stopped_thread_id
        if selected_thread is None:
            raise DAPError("No stopped thread is known. Provide thread_id explicitly.")
        if int(line) < 1:
            raise ValueError("line must be >= 1")
        wait_seconds = max(1.0, min(float(wait_seconds), config.RUN_TO_LINE_MAX_WAIT_SECONDS))

        current_raw = await self._top_frame(session)
        current = self._compact_frame(current_raw)
        current_path = current.get("source_path")
        target_path = self._resolve_target_path(str(file), current_path)
        is_cross_file = not self._same_source_path(current_path, target_path)
        if is_cross_file and not cross_file_authorized:
            raise DAPError(
                "Cross-file run_to_line requires explicit user permission. "
                f"Current file={current_path!r}, target file={target_path!r}. "
                "Ask the user, then retry with cross_file_authorized=true."
            )

        resume_line = current.get("line") if not is_cross_file else None
        target = await session.install_temporary_breakpoint(
            target_path,
            int(line),
            condition,
            resume_line=resume_line if isinstance(resume_line, int) else None,
        )
        session.prepare_for_step()
        try:
            await session.request("continue", {"threadId": selected_thread})
        except Exception:
            await session.remove_temporary_breakpoint()
            raise

        state = await session.wait_for_stop_or_termination(wait_seconds)
        if state is None:
            task = asyncio.create_task(
                self._finish_timed_out_run_to_line(session),
                name=f"run-to-line-cleanup-{session.session_id}",
            )
            session.set_run_to_line_cleanup_task(task)
            target_display = self._display_path(session, target_path)
            text = (
                f"still running after {wait_seconds:g}s\n"
                f"target={target_display}:{target['line']}\n"
                "temporary breakpoint remains active until the next stop or termination"
            )
            return {"__mcp_text__": text}

        result = await self._complete_run_to_line(session, target_path, int(target["line"]))
        return result

    async def _finish_timed_out_run_to_line(self, session: DAPSession) -> None:
        try:
            await session.wait_for_stop_or_termination(24 * 60 * 60)
            await session.remove_temporary_breakpoint()
        except asyncio.CancelledError:
            raise
        except Exception:
            pass
        finally:
            session.clear_run_to_line_cleanup_task()

    async def _complete_run_to_line(
        self,
        session: DAPSession,
        target_path: str,
        target_line: int,
    ) -> dict[str, Any]:
        state = session.state
        target_display = self._display_path(session, target_path)
        if state.status == "terminated":
            await session.remove_temporary_breakpoint()
            return {
                "__mcp_text__": f"target not reached: process terminated before {target_display}:{target_line}"
            }

        raw_top = await self._top_frame(session)
        top = self._compact_frame(raw_top)
        actual_path = top.get("source_path")
        actual_line = top.get("line")
        target_hit = self._same_source_path(actual_path, target_path) and actual_line == target_line
        await session.remove_temporary_breakpoint()
        if target_hit:
            return {
                "__mcp_text__": f"target hit: {self._format_frame_location(session, raw_top)}"
            }

        actual = self._format_frame_location(session, raw_top)
        reason = session.state.stop_reason or "stopped"
        return {
            "__mcp_text__": (
                f"stopped before target: {actual} ({reason})\n"
                f"target={target_display}:{target_line}"
            )
        }

    @staticmethod
    def _resolve_target_path(target: str, current_path: Any) -> str:
        if os.path.isabs(target):
            return os.path.normpath(target)
        if isinstance(current_path, str) and current_path:
            return os.path.normpath(os.path.join(os.path.dirname(current_path), target))
        return os.path.normpath(target)

    @staticmethod
    def _same_source_path(left: Any, right: Any) -> bool:
        if not isinstance(left, str) or not isinstance(right, str):
            return False
        return os.path.normcase(os.path.normpath(left)) == os.path.normcase(os.path.normpath(right))

    @staticmethod
    def _display_path(session: DAPSession, source_path: Any) -> str:
        if not isinstance(source_path, str) or not source_path:
            return "<unknown>"
        normalized = os.path.normpath(source_path)
        workspace = normalize_workspace_root(session.workspace_root)
        if workspace is not None:
            try:
                relative = os.path.relpath(normalized, workspace)
                if relative != os.pardir and not relative.startswith(os.pardir + os.sep):
                    return relative
            except (OSError, ValueError):
                pass
        return normalized

    @classmethod
    def _format_frame_location(cls, session: DAPSession, frame: dict[str, Any]) -> str:
        source = frame.get("source") or {}
        path = cls._display_path(session, source.get("path"))
        line = frame.get("line")
        name = str(frame.get("name") or "")
        location = f"{path}:{line}" if line is not None else path
        return f"{location} {name}".rstrip()

    @classmethod
    def _format_frame_line(
        cls,
        session: DAPSession,
        frame: dict[str, Any],
        *,
        stack_index: int,
    ) -> str:
        return f"#{stack_index} {cls._format_frame_location(session, frame)}"

    @staticmethod
    def _compact_frame(frame: dict[str, Any]) -> dict[str, Any]:
        source = frame.get("source") or {}
        return {
            "id": frame.get("id"),
            "name": frame.get("name"),
            "line": frame.get("line"),
            "column": frame.get("column"),
            "endLine": frame.get("endLine"),
            "endColumn": frame.get("endColumn"),
            "source_name": source.get("name"),
            "source_path": source.get("path"),
            "module_id": frame.get("moduleId"),
            "presentation_hint": frame.get("presentationHint"),
        }
