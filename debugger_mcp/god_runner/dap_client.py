from __future__ import annotations

import asyncio
from typing import Any, Optional

from ..dap import DAPError, read_dap_message, write_dap_message


class DAPClient:
    def __init__(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        self.reader, self.writer = reader, writer
        self._lock, self._seq = asyncio.Lock(), 0
        self._pending: dict[int, asyncio.Future[dict[str, Any]]] = {}
        self.messages: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
        self.task = asyncio.create_task(self._read_loop())

    async def _read_loop(self) -> None:
        try:
            while True:
                message = await read_dap_message(self.reader)
                if message.get("type") == "response":
                    future = self._pending.pop(int(message.get("request_seq", -1)), None)
                    if future is not None and not future.done():
                        future.set_result(message)
                else:
                    await self.messages.put(message)
        except (EOFError, ConnectionError, asyncio.IncompleteReadError) as exc:
            await self.messages.put({"type": "closed", "error": str(exc)})

    async def request(self, command: str, arguments: Optional[dict[str, Any]] = None, timeout: float = 60.0) -> dict[str, Any]:
        self._seq += 1
        seq = self._seq
        future = asyncio.get_running_loop().create_future()
        self._pending[seq] = future
        await write_dap_message(
            self.writer,
            {"seq": seq, "type": "request", "command": command, "arguments": arguments or {}},
            self._lock,
        )
        try:
            response = await asyncio.wait_for(future, timeout)
        except asyncio.TimeoutError as exc:
            self._pending.pop(seq, None)
            raise DAPError(f"god_runner DAP request timed out after {timeout:g}s: {command}") from exc
        if not response.get("success", False):
            body = response.get("body") or {}
            error = body.get("error") if isinstance(body, dict) else None
            detail = error.get("format") if isinstance(error, dict) else None
            raise DAPError(detail or response.get("message") or f"DAP request failed: {command}")
        return response.get("body") or {}

    async def respond(
        self,
        request: dict[str, Any],
        success: bool,
        message: Optional[str] = None,
        body: Optional[dict[str, Any]] = None,
    ) -> None:
        self._seq += 1
        response: dict[str, Any] = {
            "seq": self._seq,
            "type": "response",
            "request_seq": request["seq"],
            "command": request["command"],
            "success": success,
        }
        if message:
            response["message"] = message
        if body is not None:
            response["body"] = body
        await write_dap_message(self.writer, response, self._lock)

    async def next_message(self, timeout: Optional[float] = None) -> dict[str, Any]:
        return await asyncio.wait_for(self.messages.get(), timeout) if timeout else await self.messages.get()

    async def close(self) -> None:
        self.writer.close()
        try:
            await self.writer.wait_closed()
        except Exception:
            pass
        self.task.cancel()
        await asyncio.gather(self.task, return_exceptions=True)
