import asyncio
import json
import logging
import time

from debugger_mcp import config
from debugger_mcp.dap import DAPSession
from debugger_mcp.rpc import DaemonClient
from debugger_mcp.tools import DebuggerTools


class _BufferWriter:
    def __init__(self):
        self.data = bytearray()
        self.closed = False

    def write(self, data):
        self.data.extend(data)

    async def drain(self):
        return None

    def close(self):
        self.closed = True

    async def wait_closed(self):
        return None

    def is_closing(self):
        return self.closed


def _packet(message):
    body = json.dumps(message, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    return f"Content-Length: {len(body)}\r\n\r\n".encode("ascii") + body


def _decode_packet(data):
    header, body = bytes(data).split(b"\r\n\r\n", 1)
    length = int(header.decode("ascii").split(":", 1)[1].strip())
    assert len(body) == length
    return json.loads(body.decode("utf-8"))


def _session_with_backend_messages(*messages):
    frontend_reader = asyncio.StreamReader()
    backend_reader = asyncio.StreamReader()
    for message in messages:
        backend_reader.feed_data(_packet(message))
    backend_reader.feed_eof()
    return DAPSession(
        session_id=6,
        frontend_reader=frontend_reader,
        frontend_writer=_BufferWriter(),
        backend_reader=backend_reader,
        backend_writer=_BufferWriter(),
        proxy_host="127.0.0.1",
        proxy_port=4711,
    )


def test_late_mcp_response_is_dropped_not_forwarded_to_ide(caplog):
    async def scenario():
        response_seq = 1_000_123
        session = _session_with_backend_messages(
            {
                "seq": 77,
                "type": "response",
                "request_seq": response_seq,
                "success": True,
                "command": "evaluate",
                "body": {"result": "late"},
            }
        )
        session._expired_mcp_requests[response_seq] = (
            "evaluate",
            1.0,
            time.monotonic() - 2.0,
        )
        with caplog.at_level(logging.INFO, logger="debugger_mcp.dap"):
            await session._backend_to_frontend()
        assert bytes(session.frontend_writer.data) == b""
        assert "route=drop_late_mcp" in caplog.text
        assert "command=evaluate" in caplog.text

    asyncio.run(scenario())


def test_unmatched_adapter_response_is_never_forwarded_to_ide(caplog):
    async def scenario():
        session = _session_with_backend_messages(
            {
                "seq": 78,
                "type": "response",
                "request_seq": 9_999_999,
                "success": True,
                "command": "evaluate",
                "body": {"result": "orphan"},
            }
        )
        with caplog.at_level(logging.INFO, logger="debugger_mcp.dap"):
            await session._backend_to_frontend()
        assert bytes(session.frontend_writer.data) == b""
        assert "route=drop_unmatched" in caplog.text

    asyncio.run(scenario())


def test_real_ide_response_is_still_remapped_and_forwarded():
    async def scenario():
        backend_seq = 1_000_321
        session = _session_with_backend_messages(
            {
                "seq": 79,
                "type": "response",
                "request_seq": backend_seq,
                "success": True,
                "command": "threads",
                "body": {"threads": []},
            }
        )
        session._frontend_requests[backend_seq] = 42
        await session._backend_to_frontend()
        forwarded = _decode_packet(session.frontend_writer.data)
        assert forwarded["type"] == "response"
        assert forwarded["request_seq"] == 42
        assert forwarded["command"] == "threads"

    asyncio.run(scenario())


def test_evaluate_default_timeout_is_60_and_override_is_forwarded():
    class _State:
        stopped_thread_id = 1

    class _FakeSession:
        workspace_root = "/tmp/workspace"
        state = _State()

        def cached_top_frame(self, thread_id):
            return None

        async def stack_trace(self, thread_id, start_frame=0, levels=None):
            return {"stackFrames": [{"id": 5, "name": "f", "line": 1}]}

        async def request(self, command, arguments=None, timeout_seconds=None):
            self.command = command
            self.arguments = arguments
            self.timeout_seconds = timeout_seconds
            return {"result": "ok"}

    async def scenario():
        session = _FakeSession()
        tools = DebuggerTools(object())
        tools._session = lambda **kwargs: session

        await tools.evaluate("1")
        assert session.command == "evaluate"
        assert session.arguments == {"expression": "1", "frameId": 5, "context": "repl"}
        assert session.timeout_seconds == config.EVALUATE_TIMEOUT_SECONDS == 60.0

        await tools.evaluate("1", timeout_seconds=240)
        assert session.timeout_seconds == 240.0

    asyncio.run(scenario())


def test_extended_evaluation_timeout_also_extends_frontend_rpc_timeout():
    async def scenario():
        client = DaemonClient(spawn_if_missing=False)
        captured = {}

        async def fake_ensure_daemon():
            return {}

        async def fake_call_once(method, params, timeout):
            captured.update(method=method, params=params, timeout=timeout)
            return "ok"

        client.ensure_daemon = fake_ensure_daemon
        client._call_once = fake_call_once
        result = await client.call("evaluate", {"expression": "1", "timeout_seconds": 300})
        assert result == "ok"
        assert captured["method"] == "evaluate"
        assert captured["timeout"] >= 315.0

    asyncio.run(scenario())


def test_actual_request_timeout_then_late_response_is_dropped(caplog):
    async def scenario():
        frontend_reader = asyncio.StreamReader()
        backend_reader = asyncio.StreamReader()
        session = DAPSession(
            session_id=9,
            frontend_reader=frontend_reader,
            frontend_writer=_BufferWriter(),
            backend_reader=backend_reader,
            backend_writer=_BufferWriter(),
            proxy_host="127.0.0.1",
            proxy_port=4711,
        )

        try:
            await session.request("evaluate", {"expression": "slow()"}, timeout_seconds=0.01)
        except Exception as exc:
            assert "timed out" in str(exc)
        else:
            raise AssertionError("request should have timed out")

        assert len(session._expired_mcp_requests) == 1
        response_seq = next(iter(session._expired_mcp_requests))
        backend_reader.feed_data(
            _packet(
                {
                    "seq": 80,
                    "type": "response",
                    "request_seq": response_seq,
                    "success": True,
                    "command": "evaluate",
                    "body": {"result": "eventually done"},
                }
            )
        )
        backend_reader.feed_eof()

        with caplog.at_level(logging.INFO, logger="debugger_mcp.dap"):
            await session._backend_to_frontend()
        assert bytes(session.frontend_writer.data) == b""
        assert "route=drop_late_mcp" in caplog.text

    asyncio.run(scenario())
