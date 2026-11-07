from __future__ import annotations

import asyncio
import os
import shlex
import signal
import sys
import time
import traceback
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Optional, Union

from .. import config
from ..dap import DAPError
from ..frame_eval import FrameSelector
from ..rpc import DaemonClient
from .dap_client import DAPClient
from .launch import load_launch_config
from .manifest import Manifest
from .recipe import (
    Action,
    capture_expression,
    cleanup_expression,
    direct_execution_expression,
    display_path,
    execution_expression,
    new_stop_token,
    normalize_breakpoints,
)


@dataclass(frozen=True)
class TerminalProcess:
    process: asyncio.subprocess.Process
    process_group_id: Optional[int]
    session_index: int
    command: str


class GodRunner:
    HEARTBEAT_SECONDS = 15.0
    TERMINATE_GRACE_SECONDS = 5.0
    KILL_GRACE_SECONDS = 2.0

    def __init__(
        self,
        *,
        workspace: Path,
        launch: Union[str, dict[str, Any]],
        breakpoints: Iterable[dict[str, Any]],
        artifacts_dir: Path,
        continue_on_error: bool,
    ) -> None:
        self.workspace = workspace.resolve()
        self.launch = load_launch_config(self.workspace, launch) if isinstance(launch, str) else dict(launch)
        self.launch.pop("debugServer", None)
        self.launch.setdefault("cwd", str(self.workspace))
        self.breakpoint_groups = normalize_breakpoints(breakpoints, self.workspace)
        self.manifest = Manifest(artifacts_dir / "god_runner_manifest.jsonl")
        self.continue_on_error = continue_on_error
        self._children: set[asyncio.Task[None]] = set()
        self._session_index = 0
        self._hits: dict[tuple[int, str], int] = {}
        self._processes: dict[int, dict[str, Any]] = {}
        self._background_errors: asyncio.Queue[BaseException] = asyncio.Queue()
        self._terminal_processes: list[TerminalProcess] = []
        self._terminal_watchers: set[asyncio.Task[None]] = set()
        self._daemon_client = DaemonClient(workspace_root=str(self.workspace))

    async def run(self) -> None:
        ping = await self._daemon_client.ensure_daemon()
        dap_host = str(ping.get("dap_host") or config.DAP_PROXY_HOST)
        dap_port = int(ping.get("dap_port") or config.DAP_PROXY_PORT)
        print(f"[god] debugger ready at {dap_host}:{dap_port}", flush=True)
        try:
            await self._run_session(dap_host, dap_port, dict(self.launch), root=True)
            while self._children:
                tasks = list(self._children)
                await asyncio.gather(*tasks)
                self._children.difference_update(tasks)
        except BaseException:
            await self._cancel_child_sessions()
            raise
        finally:
            if self._children:
                await asyncio.gather(*list(self._children), return_exceptions=True)
            await self._terminate_terminal_processes()
        print(
            "[god] recipe finished; shared debugger daemon remains available for MCP",
            flush=True,
        )

    async def _run_session(self, host: str, port: int, launch: dict[str, Any], *, root: bool) -> None:
        self._session_index += 1
        session_index = self._session_index
        reader, writer = await asyncio.open_connection(host, port)
        client = DAPClient(reader, writer)
        action_by_id: dict[int, Action] = {}
        try:
            await client.request("initialize", {
                "clientID": "debugger-mcp-god-runner",
                "clientName": "debugger-mcp god_runner",
                "adapterID": "python",
                "pathFormat": "path",
                "linesStartAt1": True,
                "columnsStartAt1": True,
                "supportsVariableType": True,
                "supportsVariablePaging": True,
                "supportsRunInTerminalRequest": True,
                "supportsStartDebuggingRequest": True,
            })
            request_kind = str(launch.get("request") or ("launch" if root else "attach"))
            launch["request"] = request_kind
            start_task = asyncio.create_task(client.request(request_kind, launch, timeout=120.0))
            await self._wait_for_initialized(client, host, port, session_index)
            action_by_id = await self._install_breakpoints(client, session_index)
            await client.request("configurationDone")
            initial_messages = await self._wait_for_start(
                client, start_task, host, port, session_index
            )
            print(f"[god] session {session_index} started ({request_kind})", flush=True)
            await self._event_loop(
                client, host, port, session_index, action_by_id, initial_messages
            )
        except BaseException:
            try:
                await client.request("disconnect", {"terminateDebuggee": True}, timeout=5.0)
            except Exception:
                pass
            raise
        finally:
            await client.close()

    async def _wait_for_initialized(
        self,
        client: DAPClient,
        host: str,
        port: int,
        session_index: int,
    ) -> None:
        while True:
            message = await self._next_message(client, timeout=60.0)
            if message.get("type") == "event" and message.get("event") == "initialized":
                return
            await self._handle_prestart_message(
                client, message, host, port, session_index
            )

    async def _install_breakpoints(
        self,
        client: DAPClient,
        session_index: int,
    ) -> dict[int, Action]:
        action_by_id: dict[int, Action] = {}
        for arguments, actions in self.breakpoint_groups:
            body = await client.request("setBreakpoints", arguments)
            installed = list(body.get("breakpoints") or [])
            if len(installed) != len(actions):
                raise DAPError(f"debugpy returned {len(installed)} breakpoints for {len(actions)} actions")
            for result, action in zip(installed, actions):
                if result.get("verified") is False:
                    raise DAPError(result.get("message") or f"Breakpoint not verified: {action.name}")
                breakpoint_id = result.get("id")
                if not isinstance(breakpoint_id, int):
                    raise DAPError(f"debugpy returned no breakpoint id for {action.name}")
                action_by_id[breakpoint_id] = action
                actual_line = result.get("line", action.requested_line)
                print(
                    f"[god] session {session_index} breakpoint verified: "
                    f"{action.source_path}:{actual_line} ({action.name})",
                    flush=True,
                )
        return action_by_id

    async def _wait_for_start(
        self,
        client: DAPClient,
        start_task: asyncio.Task[dict[str, Any]],
        host: str,
        port: int,
        session_index: int,
    ) -> list[dict[str, Any]]:
        deferred: list[dict[str, Any]] = []
        while not start_task.done():
            message_task = asyncio.create_task(client.next_message())
            error_task = asyncio.create_task(self._background_errors.get())
            done, pending = await asyncio.wait(
                {start_task, message_task, error_task},
                timeout=self.HEARTBEAT_SECONDS,
                return_when=asyncio.FIRST_COMPLETED,
            )
            if not done:
                message_task.cancel()
                error_task.cancel()
                await asyncio.gather(message_task, error_task, return_exceptions=True)
                self._print_heartbeat(session_index, phase="starting")
                continue
            if error_task in done:
                error = error_task.result()
                message_task.cancel()
                await asyncio.gather(message_task, return_exceptions=True)
                raise error
            error_task.cancel()
            await asyncio.gather(error_task, return_exceptions=True)
            if start_task in done:
                if message_task.done() and not message_task.cancelled():
                    deferred.append(message_task.result())
                else:
                    message_task.cancel()
                    await asyncio.gather(message_task, return_exceptions=True)
                break
            message = message_task.result()
            handled = await self._handle_prestart_message(
                client, message, host, port, session_index
            )
            if not handled:
                deferred.append(message)
        await start_task
        return deferred

    async def _event_loop(
        self,
        client: DAPClient,
        host: str,
        port: int,
        session_index: int,
        action_by_id: dict[int, Action],
        initial_messages: Optional[list[dict[str, Any]]] = None,
    ) -> None:
        pending = list(initial_messages or [])
        while True:
            if pending:
                message = pending.pop(0)
            else:
                try:
                    message = await self._next_message(
                        client, timeout=self.HEARTBEAT_SECONDS
                    )
                except asyncio.TimeoutError:
                    self._print_heartbeat(session_index, phase="running")
                    continue
            if message.get("type") == "closed":
                return
            if message.get("type") == "request":
                await self._handle_reverse_request(
                    client, message, host, port, session_index
                )
                continue
            if message.get("type") != "event":
                continue
            event = message.get("event")
            if event == "stopped":
                await self._handle_stop(client, session_index, message, action_by_id)
            elif event == "process":
                self._processes[session_index] = dict(message.get("body") or {})
                process = self._processes[session_index]
                print(
                    f"[god] session {session_index} process: "
                    f"pid={process.get('systemProcessId')} name={process.get('name')}",
                    flush=True,
                )
            elif event == "output":
                output = str((message.get("body") or {}).get("output") or "")
                if output:
                    print(output, end="", file=sys.stderr, flush=True)
            elif event in {"terminated", "exited"}:
                print(f"[god] session {session_index} {event}", flush=True)
                return

    async def _handle_prestart_message(
        self,
        client: DAPClient,
        message: dict[str, Any],
        host: str,
        port: int,
        session_index: int,
    ) -> bool:
        if message.get("type") == "event" and message.get("event") == "output":
            output = str((message.get("body") or {}).get("output") or "")
            if output:
                print(output, end="", file=sys.stderr, flush=True)
            return True
        if message.get("type") == "event" and message.get("event") == "process":
            self._processes[session_index] = dict(message.get("body") or {})
            process = self._processes[session_index]
            print(
                f"[god] session {session_index} process: "
                f"pid={process.get('systemProcessId')} name={process.get('name')}",
                flush=True,
            )
            return True
        elif message.get("type") == "request":
            await self._handle_reverse_request(
                client, message, host, port, session_index
            )
            return True
        return False

    async def _handle_reverse_request(
        self,
        client: DAPClient,
        message: dict[str, Any],
        host: str,
        port: int,
        session_index: int,
    ) -> None:
        command = message.get("command")
        if command == "runInTerminal":
            await self._run_in_terminal(client, message, session_index)
            return
        if command == "startDebugging":
            configuration = dict(
                (message.get("arguments") or {}).get("configuration") or {}
            )
            configuration.pop("debugServer", None)
            subprocess_id = configuration.get("subProcessId")
            print(
                f"[god] session {session_index} startDebugging received: "
                f"subProcessId={subprocess_id}",
                flush=True,
            )
            task = asyncio.create_task(
                self._run_session(host, port, configuration, root=False)
            )
            self._track_child_task(task, session_index, subprocess_id)
            await client.respond(message, True, body={})
            return
        await client.respond(
            message, False, f"Unsupported reverse request: {command}"
        )

    async def _run_in_terminal(
        self,
        client: DAPClient,
        message: dict[str, Any],
        session_index: int,
    ) -> None:
        arguments = message.get("arguments") or {}
        command = arguments.get("args") or []
        if not isinstance(command, list) or not command:
            await client.respond(message, False, "runInTerminal provided no command")
            return
        command = [str(item) for item in command]
        env = dict(os.environ)
        for key, value in dict(arguments.get("env") or {}).items():
            if value is None:
                env.pop(str(key), None)
            else:
                env[str(key)] = str(value)
        try:
            process = await asyncio.create_subprocess_exec(
                *command,
                cwd=arguments.get("cwd") or None,
                env=env,
                start_new_session=os.name == "posix",
            )
        except Exception as exc:
            await client.respond(message, False, str(exc))
            raise
        terminal = TerminalProcess(
            process=process,
            process_group_id=process.pid if os.name == "posix" else None,
            session_index=session_index,
            command=shlex.join(command),
        )
        self._terminal_processes.append(terminal)
        watcher = asyncio.create_task(
            self._watch_terminal_process(terminal)
        )
        self._terminal_watchers.add(watcher)
        watcher.add_done_callback(self._terminal_watchers.discard)
        print(
            f"[god] session {session_index} terminal process started: "
            f"pid={process.pid} command={shlex.join(command)}",
            flush=True,
        )
        await client.respond(
            message,
            True,
            body={"processId": process.pid, "shellProcessId": process.pid},
        )

    async def _watch_terminal_process(
        self,
        terminal: TerminalProcess,
    ) -> None:
        returncode = await terminal.process.wait()
        print(
            f"[god] session {terminal.session_index} terminal process exited: "
            f"pid={terminal.process.pid} returncode={returncode}",
            flush=True,
        )

    def _track_child_task(
        self,
        task: asyncio.Task[None],
        parent_session: int,
        subprocess_id: Any,
    ) -> None:
        self._children.add(task)

        def done(child: asyncio.Task[None]) -> None:
            if child.cancelled():
                return
            error = child.exception()
            if error is not None:
                print(
                    f"[god] child session failed: parent={parent_session} "
                    f"subProcessId={subprocess_id}\n{error}",
                    file=sys.stderr,
                    flush=True,
                )
                self._background_errors.put_nowait(error)

        task.add_done_callback(done)

    async def _next_message(
        self,
        client: DAPClient,
        timeout: float,
    ) -> dict[str, Any]:
        message_task = asyncio.create_task(client.next_message())
        error_task = asyncio.create_task(self._background_errors.get())
        done, pending = await asyncio.wait(
            {message_task, error_task},
            timeout=timeout,
            return_when=asyncio.FIRST_COMPLETED,
        )
        if not done:
            for task in pending:
                task.cancel()
            await asyncio.gather(*pending, return_exceptions=True)
            raise asyncio.TimeoutError
        if error_task in done:
            error = error_task.result()
            message_task.cancel()
            await asyncio.gather(message_task, return_exceptions=True)
            raise error
        error_task.cancel()
        await asyncio.gather(error_task, return_exceptions=True)
        return message_task.result()

    def _print_heartbeat(self, session_index: int, phase: str) -> None:
        process = self._processes.get(session_index) or {}
        terminal_alive = sum(
            self._terminal_alive(terminal) for terminal in self._terminal_processes
        )
        print(
            f"[god] waiting: session={session_index} phase={phase} "
            f"pid={process.get('systemProcessId')} "
            f"child_sessions={sum(not task.done() for task in self._children)} "
            f"terminal_processes={terminal_alive}",
            flush=True,
        )

    async def _terminate_terminal_processes(self) -> None:
        terminals = list(self._terminal_processes)
        running = [terminal for terminal in terminals if self._terminal_alive(terminal)]
        if running:
            print(
                f"[god] stopping {len(running)} recipe terminal process group(s)",
                flush=True,
            )
        for terminal in running:
            self._signal_terminal(terminal, force=False)
        remaining = await self._wait_for_terminals(running, self.TERMINATE_GRACE_SECONDS)
        if remaining:
            print(
                f"[god] force-killing {len(remaining)} recipe terminal process group(s) "
                f"after {self.TERMINATE_GRACE_SECONDS:g}s grace period",
                file=sys.stderr,
                flush=True,
            )
            for terminal in remaining:
                self._signal_terminal(terminal, force=True)
            remaining = await self._wait_for_terminals(remaining, self.KILL_GRACE_SECONDS)
        for terminal in remaining:
            print(
                f"[god] warning: terminal process group still exists: "
                f"pgid={terminal.process_group_id} command={terminal.command}",
                file=sys.stderr,
                flush=True,
            )
        leaders = [
            terminal.process.wait()
            for terminal in terminals
            if terminal.process.returncode is None
        ]
        if leaders:
            try:
                await asyncio.wait_for(
                    asyncio.gather(*leaders, return_exceptions=True),
                    timeout=self.KILL_GRACE_SECONDS,
                )
            except asyncio.TimeoutError:
                pass
        if self._terminal_watchers:
            done = [watcher for watcher in self._terminal_watchers if watcher.done()]
            if done:
                await asyncio.gather(*done, return_exceptions=True)
        self._terminal_processes.clear()

    @staticmethod
    def _terminal_alive(terminal: TerminalProcess) -> bool:
        if terminal.process_group_id is None:
            return terminal.process.returncode is None
        try:
            os.killpg(terminal.process_group_id, 0)
        except ProcessLookupError:
            return False
        except PermissionError:
            return True
        return True

    @staticmethod
    def _signal_terminal(terminal: TerminalProcess, *, force: bool) -> None:
        try:
            if terminal.process_group_id is not None:
                os.killpg(
                    terminal.process_group_id,
                    signal.SIGKILL if force else signal.SIGTERM,
                )
            elif force:
                terminal.process.kill()
            else:
                terminal.process.terminate()
        except ProcessLookupError:
            pass

    async def _wait_for_terminals(
        self,
        terminals: list[TerminalProcess],
        timeout: float,
    ) -> list[TerminalProcess]:
        deadline = asyncio.get_running_loop().time() + timeout
        remaining = [terminal for terminal in terminals if self._terminal_alive(terminal)]
        while remaining and asyncio.get_running_loop().time() < deadline:
            await asyncio.sleep(0.05)
            remaining = [terminal for terminal in remaining if self._terminal_alive(terminal)]
        return remaining

    async def _cancel_child_sessions(self) -> None:
        running = [task for task in self._children if not task.done()]
        for task in running:
            task.cancel()
        if running:
            await asyncio.gather(*running, return_exceptions=True)

    async def _complete_stack(
        self,
        client: DAPClient,
        thread_id: int,
        body: Optional[dict[str, Any]] = None,
    ) -> list[dict[str, Any]]:
        if body is None:
            body = await client.request("stackTrace", {"threadId": thread_id, "startFrame": 0})
        frames = list(body.get("stackFrames") or [])
        total = body.get("totalFrames")
        if not isinstance(total, int) and len(frames) <= 1:
            body = await client.request("stackTrace", {"threadId": thread_id, "startFrame": 0})
            frames = list(body.get("stackFrames") or [])
            total = body.get("totalFrames")
        while isinstance(total, int) and len(frames) < total:
            remainder = await client.request("stackTrace", {
                "threadId": thread_id,
                "startFrame": len(frames),
                "levels": total - len(frames),
            })
            more = list(remainder.get("stackFrames") or [])
            if not more:
                break
            frames.extend(more)
        return frames

    async def _handle_stop(
        self,
        client: DAPClient,
        session_index: int,
        message: dict[str, Any],
        action_by_id: dict[int, Action],
    ) -> None:
        body = message.get("body") or {}
        thread_id = body.get("threadId")
        if not isinstance(thread_id, int):
            raise DAPError("Stopped event has no threadId")
        top_body = await client.request(
            "stackTrace", {"threadId": thread_id, "startFrame": 0, "levels": 1}
        )
        top_frames = list(top_body.get("stackFrames") or [])
        if not top_frames:
            raise DAPError("Stopped thread has no stack frames")
        hit_ids = [item for item in body.get("hitBreakpointIds") or [] if isinstance(item, int)]
        actions = [action_by_id[item] for item in hit_ids if item in action_by_id]
        if not actions:
            top = top_frames[0]
            top_path = os.path.normcase(os.path.normpath(str((top.get("source") or {}).get("path") or "")))
            top_line = top.get("line")
            actions = [
                action for action in action_by_id.values()
                if os.path.normcase(os.path.normpath(action.source_path)) == top_path
                and action.requested_line == top_line
            ]
        if not actions:
            print(f"[god] unconfigured stop ({body.get('reason')}); continuing", flush=True)
            await client.request("continue", {"threadId": thread_id})
            return
        frames = (
            await self._complete_stack(client, thread_id, top_body)
            if any(action.frame_selectors for action in actions)
            else top_frames
        )
        for action in actions:
            await self._execute_action(client, session_index, frames, action)
        await client.request("continue", {"threadId": thread_id})

    async def _execute_action(
        self,
        client: DAPClient,
        session_index: int,
        frames: list[dict[str, Any]],
        action: Action,
    ) -> None:
        hit_key = (session_index, action.name)
        hit = self._hits[hit_key] = self._hits.get(hit_key, 0) + 1
        token = None
        started = time.monotonic()
        print(f"[god] hit #{hit}: {action.name}", flush=True)
        try:
            top_frame_id = frames[0].get("id")
            if not isinstance(top_frame_id, int):
                raise DAPError("Stopped top frame has no valid DAP frame id")
            targets = [
                (selector, *self._match_frame_selector(frames, selector))
                for selector in action.frame_selectors
            ]
            if targets:
                token = new_stop_token()
            for selector, stack_index, frame in targets:
                frame_id = frame.get("id")
                if not isinstance(frame_id, int):
                    raise DAPError(
                        f"Matched frame_eval target has no valid DAP frame id: "
                        f"{selector.file}:{selector.line} {selector.function}"
                    )
                assert token is not None
                await client.request("evaluate", {
                    "expression": capture_expression(
                        token, selector, stack_index, frame, self.workspace
                    ),
                    "frameId": frame_id,
                    "context": "repl",
                })
            expression = (
                execution_expression(token, action.code, action.name)
                if token is not None
                else direct_execution_expression(action.code, action.name)
            )
            await client.request("evaluate", {
                "expression": expression,
                "frameId": top_frame_id,
                "context": "repl",
            }, timeout=config.EVALUATE_TIMEOUT_SECONDS)
        except Exception:
            if token is not None and frames and isinstance(frames[0].get("id"), int):
                try:
                    await client.request("evaluate", {
                        "expression": cleanup_expression(token),
                        "frameId": frames[0]["id"],
                        "context": "repl",
                    }, timeout=min(config.EVALUATE_TIMEOUT_SECONDS, 5.0))
                except Exception:
                    pass
            duration_ms = round((time.monotonic() - started) * 1000, 3)
            error = traceback.format_exc()
            self.manifest.write(
                probe=action.name,
                source=action.source_path,
                line=action.requested_line,
                hit=hit,
                session=session_index,
                process=self._processes.get(session_index),
                duration_ms=duration_ms,
                status="error",
                error=error,
            )
            print(f"[god] action failed: {action.name}\n{error}", file=sys.stderr, flush=True)
            if not self.continue_on_error:
                raise
        else:
            duration_ms = round((time.monotonic() - started) * 1000, 3)
            self.manifest.write(
                probe=action.name,
                source=action.source_path,
                line=action.requested_line,
                hit=hit,
                session=session_index,
                process=self._processes.get(session_index),
                duration_ms=duration_ms,
                status="ok",
            )
            print(f"[god] action completed in {duration_ms:g} ms", flush=True)

    def _match_frame_selector(
        self,
        frames: list[dict[str, Any]],
        selector: FrameSelector,
    ) -> tuple[int, dict[str, Any]]:
        selector_file = selector.file.replace("\\", "/")
        matches = [
            (index, frame)
            for index, frame in enumerate(frames)
            if display_path(
                str((frame.get("source") or {}).get("path") or ""), self.workspace
            ).replace("\\", "/") == selector_file
            and frame.get("line") == selector.line
            and str(frame.get("name") or "") == selector.function
        ]
        target = f"{selector.file}:{selector.line} {selector.function}"
        if not matches:
            available = "\n".join(
                f"#{index} {display_path(str((frame.get('source') or {}).get('path') or ''), self.workspace)}:"
                f"{frame.get('line')} {frame.get('name') or ''}"
                for index, frame in enumerate(frames)
            ) or "<no stack frames>"
            raise DAPError(
                f"No current stack frame exactly matches frame_eval selector: {target}\n"
                f"Current stack:\n{available}"
            )
        if len(matches) > 1:
            candidates = "\n".join(
                f"#{index} {display_path(str((frame.get('source') or {}).get('path') or ''), self.workspace)}:"
                f"{frame.get('line')} {frame.get('name') or ''}"
                for index, frame in matches
            )
            raise DAPError(
                f"Multiple current stack frames exactly match frame_eval selector: {target}\n"
                f"Matching frames:\n{candidates}"
            )
        return matches[0]


async def _run_async(
    *,
    launch: Union[str, dict[str, Any]],
    breakpoints: Iterable[dict[str, Any]],
    workspace: Union[str, Path],
    artifacts_dir: Union[str, Path],
    continue_on_error: bool,
) -> None:
    workspace_path = Path(workspace).resolve()
    artifacts_path = Path(artifacts_dir)
    if not artifacts_path.is_absolute():
        artifacts_path = workspace_path / artifacts_path
    await GodRunner(
        workspace=workspace_path,
        launch=launch,
        breakpoints=breakpoints,
        artifacts_dir=artifacts_path,
        continue_on_error=continue_on_error,
    ).run()


def run(
    *,
    launch: Union[str, dict[str, Any]],
    breakpoints: Iterable[dict[str, Any]],
    workspace: Union[str, Path] = ".",
    artifacts_dir: Union[str, Path] = "god_run_artifacts",
    continue_on_error: bool = False,
) -> None:
    """Run one launch.json configuration under automatic breakpoint actions."""
    asyncio.run(_run_async(
        launch=launch,
        breakpoints=breakpoints,
        workspace=workspace,
        artifacts_dir=artifacts_dir,
        continue_on_error=continue_on_error,
    ))
