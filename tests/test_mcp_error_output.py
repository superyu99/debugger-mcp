import asyncio

from debugger_mcp.mcp_server import MinimalMCPServer


class _DummyClient:
    pass


def _capture_result(value, is_error=True):
    server = MinimalMCPServer(_DummyClient())
    captured = {}

    async def fake_send_result(request_id, result):
        captured["request_id"] = request_id
        captured["result"] = result

    server._send_result = fake_send_result
    asyncio.run(server._send_tool_result(7, value, is_error=is_error))
    return captured["result"]


def test_multiline_error_is_raw_text_without_structured_content():
    traceback_text = (
        'Traceback (most recent call last):\n'
        '  File "<string>", line 1, in <module>\n'
        'RuntimeError: No valid yaml_stream found in the stamped calibration list.\n'
    )
    result = _capture_result(traceback_text)
    assert result == {
        "content": [{"type": "text", "text": traceback_text}],
        "isError": True,
    }
    assert "structuredContent" not in result
    assert "\n" in result["content"][0]["text"]


def test_long_error_is_not_truncated():
    traceback_text = "Traceback\n" + ("frame detail\n" * 3000) + "RuntimeError: final critical line"
    assert len(traceback_text) > 30000
    result = _capture_result(traceback_text)
    assert result["content"][0]["text"] == traceback_text
    assert result["content"][0]["text"].endswith("RuntimeError: final critical line")
    assert "structuredContent" not in result


def test_legacy_error_dict_is_unwrapped_if_passed():
    result = _capture_result({"error": "ValueError: bad value\nsecond line"})
    assert result["content"][0]["text"] == "ValueError: bad value\nsecond line"
    assert result["isError"] is True


def test_normal_text_result_has_no_structured_content_duplicate():
    result = _capture_result({"__mcp_text__": "compact output"}, is_error=False)
    assert result == {
        "content": [{"type": "text", "text": "compact output"}],
        "isError": False,
    }
    assert "structuredContent" not in result


def test_normal_dict_result_has_no_structured_content_duplicate():
    result = _capture_result({"a": 1, "b": "two"}, is_error=False)
    assert result["isError"] is False
    assert '"a": 1' in result["content"][0]["text"]
    assert "structuredContent" not in result


def test_unexpected_tool_exception_returns_full_server_traceback():
    class FailingClient:
        workspace_source = "explicit"
        workspace_is_explicit = True
        mcp_roots = []

        async def call(self, method, arguments):
            raise RuntimeError("Z" * 2000 + "SERVER_FINAL_MARKER")

    server = MinimalMCPServer(FailingClient())
    captured = {}

    async def fake_send_result(request_id, result):
        captured["result"] = result

    server._send_result = fake_send_result
    asyncio.run(
        server._handle_message(
            {
                "jsonrpc": "2.0",
                "id": 9,
                "method": "tools/call",
                "params": {"name": "evaluate", "arguments": {"expression": "1"}},
            }
        )
    )
    result = captured["result"]
    text = result["content"][0]["text"]
    assert result["isError"] is True
    assert text.startswith("Traceback (most recent call last):")
    assert text.endswith("SERVER_FINAL_MARKER\n")
    assert "Z" * 2000 in text
    assert "structuredContent" not in result
