from __future__ import annotations

import asyncio
import json
import logging
import sys
import traceback
from typing import Any, Optional

from . import __version__
from .dap import DAPError
from .rpc import DaemonClient
from .workspace import workspace_from_mcp_root_uri

log = logging.getLogger(__name__)
JsonObject = dict[str, Any]


TOOLS: list[JsonObject] = [
    {
        "name": "attach_debugpy",
        "description": (
            "Return compact connection/workspace routing status. Use full=true for detailed session/MCP Roots metadata or "
            "to identify the active root launch configuration from launch_name and root_session_id."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {"full": {"type": "boolean", "default": False}},
            "additionalProperties": False,
        },
    },
    {
        "name": "get_threads",
        "description": (
            "List threads in the selected debug process. Default output is compact text; full=true adds session diagnostics."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "session_id": {"type": ["integer", "null"]},
                "full": {"type": "boolean", "default": False},
            },
            "additionalProperties": False,
        },
    },
    {
        "name": "get_stack",
        "description": (
            "Get the current live stack. By default return every frame from start_frame as compact lines like "
            "#0 workspace-relative/path.py:line function. The #N value is stack depth, not a DAP frame ID. Use the exact "
            "file, line, and function values from this output when inspect_structure selects a caller or an "
            "evaluate expression uses frame_eval() to access active-stack objects. Pass literal file/line/function values "
            "to frame_eval; its expression argument may be dynamic. Only pass levels when an explicitly bounded stack "
            "slice is desired. full=true returns raw DAP stack-frame objects plus "
            "session diagnostics."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "session_id": {"type": ["integer", "null"]},
                "thread_id": {"type": ["integer", "null"]},
                "start_frame": {"type": "integer", "minimum": 0, "default": 0},
                "levels": {"type": ["integer", "null"], "minimum": 1},
                "full": {"type": "boolean", "default": False},
            },
            "additionalProperties": False,
        },
    },
    {
        "name": "get_locals",
        "description": (
            "Get every local variable in the current paused frame. Default output keeps the complete local-name set while "
            "using compact value metadata: type, container length, Tensor/ndarray shape, dtype/device, and scalar values. "
            "No locals are silently omitted. Set full=true for the complete raw debugpy variables response without field or "
            "length truncation. Omitted session_id stays inside this MCP frontend's workspace."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "session_id": {"type": ["integer", "null"]},
                "frame_id": {"type": ["integer", "null"]},
                "full": {"type": "boolean", "default": False},
            },
            "additionalProperties": False,
        },
    },
    {
        "name": "evaluate",
        "description": (
            "Execute Python in the current workspace's latest stopped top frame. Treat evaluate as a live runtime REPL/power "
            "tool: inspect, compute, call functions, "
            "run multi-statement code, transform or mutate runtime objects, save artifacts, perform visualization, or run debugging experiments. "
            "A single expression returns its value, and comma-separated values are one tuple expression. Statement suites "
            "execute for side effects and may return empty/(omitted); query a produced value separately when needed. "
            "Pass multi-statement Python directly instead of wrapping it in exec/eval/compile; use dynamic execution only "
            "when the task requires it, and keep every frame_eval call outside the dynamic string. "
            "To access one or more other frames still present in the current active call stack, first call get_stack(), then "
            "call the injected frame_eval(expression, file, line, function) inside the evaluate expression. Its file, line, "
            "and function must be literals copied from get_stack; its expression may be dynamic. frame_eval returns the real "
            "in-process object, uses the same syntax as a god_runner action, and cannot access a frame that has returned "
            "or another thread/process/rank/session. "
            "When copying CUDA work into a repeatedly hit god_runner action, move results to CPU early and add finally "
            "cleanup for large temporaries: del, gc.collect(), and when needed torch.cuda.empty_cache(). "
            "The default timeout is 60 seconds; pass timeout_seconds only when the operation is expected to take longer. The "
            "complete expression result is returned without DAP/session metadata or debugger-mcp truncation. Python/runtime "
            "failures are returned as complete multiline error text without JSON wrapping, summarization, or truncation."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "expression": {"type": "string", "minLength": 1},
                "timeout_seconds": {
                    "type": "number",
                    "minimum": 1,
                    "description": "Optional. Omit for the 60-second default; use only for work expected to take longer.",
                },
            },
            "required": ["expression"],
            "additionalProperties": False,
        },
    },
    {
        "name": "inspect_structure",
        "description": (
            "Return a bounded structural summary for one expression in the current paused runtime. Normally inspect the "
            "latest stopped top frame. When the object exists only in a caller frame, first call get_stack(), then pass "
            "file + line + function together exactly from that current stack line. The selector is strict and all-or-none; "
            "debugger-mcp never guesses and does not expose DAP frame IDs for inspect_structure. Use profile='summary' first, "
            "then normal/deep or a narrower subtree only when more detail is needed. Hard limits on depth, width, nodes, "
            "characters, value length, and runtime prevent huge outputs."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "expression": {"type": "string", "minLength": 1},
                "profile": {"type": "string", "enum": ["summary", "normal", "deep"], "default": "summary"},
                "name": {"type": ["string", "null"]},
                "file": {
                    "type": "string",
                    "minLength": 1,
                    "description": "Caller-frame backtracking only. Copy the file path exactly from get_stack(); requires line and function too.",
                },
                "line": {
                    "type": "integer",
                    "minimum": 1,
                    "description": "Caller-frame backtracking only. Copy the line exactly from get_stack(); requires file and function too.",
                },
                "function": {
                    "type": "string",
                    "minLength": 1,
                    "description": "Caller-frame backtracking only. Copy the function exactly from get_stack(); requires file and line too.",
                },
                "max_depth": {"type": ["integer", "null"], "minimum": 1, "maximum": 8},
                "max_children": {"type": ["integer", "null"], "minimum": 1, "maximum": 30},
                "max_nodes": {"type": ["integer", "null"], "minimum": 1, "maximum": 500},
                "max_chars": {"type": ["integer", "null"], "minimum": 256, "maximum": 30000}
            },
            "required": ["expression"],
            "additionalProperties": False
        }
    },
    {
        "name": "step_over",
        "description": (
            "Execute one step-over and return the new source location plus actual stop reason and thread. Use full=true only for raw step/session diagnostics."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "session_id": {"type": ["integer", "null"]},
                "thread_id": {"type": ["integer", "null"]},
                "full": {"type": "boolean", "default": False},
            },
            "additionalProperties": False,
        },
    },
    {
        "name": "step_into",
        "description": "Execute one step-into and return the new source location plus actual stop reason and thread. Use full=true only for raw diagnostics.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "session_id": {"type": ["integer", "null"]},
                "thread_id": {"type": ["integer", "null"]},
                "full": {"type": "boolean", "default": False},
            },
            "additionalProperties": False,
        },
    },
    {
        "name": "step_out",
        "description": "Execute one step-out and return the new source location plus actual stop reason and thread. Use full=true only for raw diagnostics.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "session_id": {"type": ["integer", "null"]},
                "thread_id": {"type": ["integer", "null"]},
                "full": {"type": "boolean", "default": False},
            },
            "additionalProperties": False,
        },
    },
    {
        "name": "run_to_line",
        "description": (
            "Set an internal temporary breakpoint, continue the current process session, wait for the actual stop, "
            "then restore the user's breakpoints. The final stopped line is visible in VS Code. Before calling, "
            "briefly reason that the target is reachable; prefer intermediate targets when control flow is uncertain. "
            "A target outside the current source file is rejected unless the user explicitly authorized it and "
            "cross_file_authorized=true is supplied."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "file": {"type": "string", "minLength": 1},
                "line": {"type": "integer", "minimum": 1},
                "reason": {"type": "string", "minLength": 8, "maxLength": 300},
                "condition": {"type": ["string", "null"]},
                "wait_seconds": {"type": "number", "minimum": 1, "maximum": 120, "default": 30},
                "cross_file_authorized": {"type": "boolean", "default": False},
                "session_id": {"type": ["integer", "null"]},
                "thread_id": {"type": ["integer", "null"]}
            },
            "required": ["file", "line", "reason"],
            "additionalProperties": False
        }
    },
]


class MinimalMCPServer:
    def __init__(self, client: DaemonClient) -> None:
        self.client = client
        self._write_lock = asyncio.Lock()
        self._client_supports_roots = False
        self._client_roots_list_changed = False
        self._client_initialized = False
        self._roots_ready = asyncio.Event()
        self._roots_refresh_lock = asyncio.Lock()
        self._outbound_request_id = 0
        self._pending_client_requests: dict[str, asyncio.Future[Any]] = {}

    async def run(self) -> None:
        while True:
            raw = await asyncio.to_thread(sys.stdin.buffer.readline)
            if not raw:
                return
            try:
                message = json.loads(raw.decode("utf-8"))
            except Exception:
                log.exception("Ignoring malformed MCP JSON")
                continue
            if not isinstance(message, dict):
                continue
            asyncio.create_task(self._handle_message(message))

    async def _handle_message(self, message: JsonObject) -> None:
        method = message.get("method")
        request_id = message.get("id")

        # Responses to server-initiated requests (notably roots/list) have an
        # id but no method. They must be consumed here instead of being treated
        # as unknown client requests.
        if method is None and request_id is not None:
            if self._resolve_client_response(message):
                return
            log.debug("Ignoring unmatched MCP response id=%r", request_id)
            return

        # MCP notifications intentionally have no id.
        if request_id is None:
            await self._handle_notification(str(method or ""), message)
            return
        try:
            if method == "initialize":
                initialize_params = message.get("params") or {}
                requested_version = initialize_params.get("protocolVersion", "2025-06-18")
                client_capabilities = initialize_params.get("capabilities") or {}
                roots_capability = client_capabilities.get("roots")
                self._client_supports_roots = isinstance(roots_capability, dict)
                self._client_roots_list_changed = bool(
                    roots_capability.get("listChanged")
                ) if isinstance(roots_capability, dict) else False
                result = {
                    "protocolVersion": requested_version,
                    "capabilities": {"tools": {"listChanged": False}},
                    "serverInfo": {"name": "debugger-mcp", "version": __version__},
                    "instructions": (
                        "Debugger MCP is a powerful runtime evidence source. Actively use it when the answer depends on "
                        "actual execution state, values, shapes, branches, or intermediate results; do not use it when "
                        "static analysis is already sufficient. Multiple MCP hosts/projects share one daemon, but omitted "
                        "session_id values are scoped to the MCP frontend's current workspace. When the MCP client exposes "
                        "one filesystem Root, debugger-mcp uses it as the frontend workspace automatically (notably Cursor); "
                        "an explicit DEBUGGER_MCP_WORKSPACE override still wins. Each VS Code/Cursor launch env "
                        "must explicitly include DEBUGGER_MCP_WORKSPACE=${workspaceFolder}; DAP cwd is diagnostic only. "
                        "Use get_locals for variable inventory, inspect_structure for bounded structural summaries, and "
                        "evaluate as the live runtime Python execution channel for computation, experiments, mutation, "
                        "artifact creation, and visualization. Normally call evaluate with expression only so it uses the latest "
                        "stopped top frame. To access any other frame still present in the current active call stack, first refresh "
                        "get_stack and call frame_eval(expression, file, line, function) inside the evaluate expression. Keep "
                        "file/line/function as literals copied from get_stack; the expression argument may be dynamic. The same "
                        "frame_eval call can be copied into a god_runner action. It cannot read a frame that has returned. "
                        "inspect_structure supports "
                        "the same exact caller selector when a nested object lives in a caller frame; "
                        "first use get_stack and pass file + line + function together. When this frontend has "
                        "already observed one pause and execution later stops elsewhere, the next debugger tool result "
                        "includes one compact debug-location change notice; it is tracked independently per frontend."
                    ),
                }
                await self._send_result(request_id, result)
            elif method == "ping":
                await self._send_result(request_id, {})
            elif method == "tools/list":
                await self._send_result(request_id, {"tools": TOOLS})
            elif method == "tools/call":
                params = message.get("params") or {}
                name = params.get("name")
                arguments = params.get("arguments") or {}
                if name not in {tool["name"] for tool in TOOLS}:
                    await self._send_error(request_id, -32602, f"Unknown tool: {name}")
                    return
                try:
                    await self._ensure_roots_ready()
                    if (
                        self.client.workspace_source == "mcp_roots_ambiguous"
                        and name != "attach_debugpy"
                        and arguments.get("session_id") is None
                    ):
                        roots = ", ".join(self.client.mcp_roots)
                        raise DAPError(
                            "The MCP client exposed multiple workspace roots, so automatic debugger routing is ambiguous: "
                            f"{roots}. Open a single-root workspace or set DEBUGGER_MCP_WORKSPACE explicitly for this MCP frontend."
                        )
                    value = await self.client.call(str(name), arguments)
                    if name == "attach_debugpy" and isinstance(value, dict):
                        value = self._augment_attach_status(value, bool(arguments.get("full", False)))
                    await self._send_tool_result(request_id, value, is_error=False)
                except (DAPError, TypeError, ValueError) as exc:
                    # Tool/debuggee errors are deliberately returned as raw multiline
                    # text. Do not wrap them in JSON or structuredContent: traceback
                    # newlines must stay readable, and error output must never pass
                    # through the normal compact/full truncation path.
                    await self._send_tool_result(request_id, str(exc), is_error=True)
                except Exception:
                    log.exception("Tool %s failed", name)
                    # Unexpected server-side failures also keep the complete Python
                    # traceback. Error output is never summarized or truncated.
                    await self._send_tool_result(
                        request_id,
                        traceback.format_exc(),
                        is_error=True,
                    )
            else:
                await self._send_error(request_id, -32601, f"Method not found: {method}")
        except Exception:
            log.exception("MCP request handling failed")
            await self._send_error(request_id, -32603, traceback.format_exc())

    async def _handle_notification(self, method: str, message: JsonObject) -> None:
        if method == "notifications/initialized":
            self._client_initialized = True
            if self._client_supports_roots and not self.client.workspace_is_explicit:
                await self._refresh_client_roots()
            else:
                self._roots_ready.set()
            return
        if method == "notifications/roots/list_changed":
            if self._client_supports_roots and self._client_roots_list_changed:
                await self._refresh_client_roots()

    async def _ensure_roots_ready(self) -> None:
        """Wait briefly for initial client Roots discovery before tool routing."""
        if not self._client_supports_roots or self.client.workspace_is_explicit:
            return
        if not self._client_initialized:
            # A conforming MCP client sends notifications/initialized before any
            # tool call. Do not invent a pre-initialization roots/list request.
            return
        if not self._roots_ready.is_set():
            try:
                await asyncio.wait_for(self._roots_ready.wait(), timeout=2.5)
            except asyncio.TimeoutError:
                # Keep the existing cwd fallback. This preserves compatibility
                # with clients that advertise Roots but fail to answer roots/list.
                pass

    async def _refresh_client_roots(self) -> None:
        if not self._client_supports_roots or self.client.workspace_is_explicit:
            self._roots_ready.set()
            return

        async with self._roots_refresh_lock:
            self._roots_ready.clear()
            try:
                result = await self._send_client_request("roots/list", timeout=2.0)
                raw_roots = result.get("roots", []) if isinstance(result, dict) else []
                roots: list[str] = []
                if isinstance(raw_roots, list):
                    for item in raw_roots:
                        if not isinstance(item, dict):
                            continue
                        root = workspace_from_mcp_root_uri(item.get("uri"))
                        if root is not None and root not in roots:
                            roots.append(root)
                self.client.apply_mcp_roots(roots)
                log.info(
                    "MCP Roots resolved workspace_source=%s workspace=%s roots=%s",
                    self.client.workspace_source,
                    self.client.workspace_root,
                    roots,
                )
            except Exception as exc:
                self.client.mcp_roots_error = str(exc)
                log.warning("MCP roots/list failed; keeping workspace fallback %r: %s", self.client.workspace_root, exc)
            finally:
                self._roots_ready.set()

    async def _send_client_request(
        self,
        method: str,
        params: Optional[JsonObject] = None,
        timeout: float = 2.0,
    ) -> Any:
        self._outbound_request_id += 1
        request_id = f"debugger-mcp:{self._outbound_request_id}"
        loop = asyncio.get_running_loop()
        future: asyncio.Future[Any] = loop.create_future()
        self._pending_client_requests[request_id] = future
        request: JsonObject = {
            "jsonrpc": "2.0",
            "id": request_id,
            "method": method,
        }
        if params:
            request["params"] = params
        await self._write(request)
        try:
            return await asyncio.wait_for(future, timeout=timeout)
        finally:
            self._pending_client_requests.pop(request_id, None)

    def _resolve_client_response(self, message: JsonObject) -> bool:
        request_id = message.get("id")
        future = self._pending_client_requests.get(str(request_id))
        if future is None:
            return False
        if future.done():
            return True
        error = message.get("error")
        if error:
            if isinstance(error, dict):
                detail = error.get("message") or error
            else:
                detail = error
            future.set_exception(DAPError(f"Client request failed: {detail}"))
        else:
            future.set_result(message.get("result") or {})
        return True

    def _augment_attach_status(self, value: JsonObject, full: bool) -> JsonObject:
        result = dict(value)
        result["client_workspace_source"] = self.client.workspace_source
        result["mcp_root_count"] = len(self.client.mcp_roots)
        if full:
            result["mcp_roots"] = list(self.client.mcp_roots)
            result["mcp_roots_error"] = self.client.mcp_roots_error
            return result

        source = self.client.workspace_source
        if source == "mcp_roots_ambiguous":
            workspace_text = f"<ambiguous MCP roots: {len(self.client.mcp_roots)}>"
        else:
            workspace_text = str(result.get("client_workspace") or "<unknown>")
        workspace_sessions = int(result.get("workspace_session_count") or 0)
        total_sessions = int(result.get("session_count") or 0)
        stopped = int(result.get("workspace_stopped_count") or 0)
        selected_id = result.get("selected_session_id")
        selected_name = str(result.get("selected_process_name") or "")
        selected_state = str(result.get("selected_debug_state") or "")
        if selected_id is None:
            selected = "none"
        else:
            selected = f"{selected_id}"
            if selected_name:
                selected += f" {selected_name}"
            if selected_state:
                selected += f" ({selected_state})"
        text = (
            f"workspace={workspace_text} [{source}]\n"
            f"sessions={workspace_sessions}/{total_sessions}, stopped={stopped}, selected={selected}"
        )
        unscoped = int(result.get("unscoped_session_count") or 0)
        if unscoped:
            text += f"\nunscoped_sessions={unscoped}"
        return {"__mcp_text__": text}

    async def _send_tool_result(self, request_id: Any, value: Any, is_error: bool) -> None:
        if is_error:
            # Error output is sacred: complete original text, original newlines,
            # no JSON wrapper, no summary, no truncation, no structured duplicate.
            if isinstance(value, str):
                text = value
            elif isinstance(value, dict) and "error" in value:
                text = str(value.get("error") or "")
            else:
                text = str(value)
            await self._send_result(
                request_id,
                {
                    "content": [{"type": "text", "text": text}],
                    "isError": True,
                },
            )
            return

        if isinstance(value, dict) and "__mcp_text__" in value:
            text = str(value.get("__mcp_text__") or "")
        else:
            text = json.dumps(value, ensure_ascii=False, indent=2, default=str)
        # Tool results are for the model to read as text. Do not send the same
        # payload again as structuredContent; that can duplicate context cost.
        await self._send_result(
            request_id,
            {
                "content": [{"type": "text", "text": text}],
                "isError": False,
            },
        )

    async def _send_result(self, request_id: Any, result: Any) -> None:
        await self._write({"jsonrpc": "2.0", "id": request_id, "result": result})

    async def _send_error(self, request_id: Any, code: int, message: str) -> None:
        await self._write({"jsonrpc": "2.0", "id": request_id, "error": {"code": code, "message": message}})

    async def _write(self, message: JsonObject) -> None:
        raw = (json.dumps(message, ensure_ascii=False, separators=(",", ":")) + "\n").encode("utf-8")
        async with self._write_lock:
            await asyncio.to_thread(self._write_sync, raw)

    @staticmethod
    def _write_sync(raw: bytes) -> None:
        sys.stdout.buffer.write(raw)
        sys.stdout.buffer.flush()
