import asyncio
import os

from debugger_mcp.mcp_server import MinimalMCPServer
from debugger_mcp.rpc import DaemonClient
from debugger_mcp.workspace import WORKSPACE_ENV_KEY, workspace_from_mcp_root_uri


def test_file_root_uri_to_workspace_path(tmp_path):
    project = tmp_path / "project with space"
    uri = project.as_uri()
    assert workspace_from_mcp_root_uri(uri) == os.path.normcase(os.path.normpath(str(project.resolve())))


def test_non_file_root_uri_is_ignored():
    assert workspace_from_mcp_root_uri("https://example.com/project") is None


def test_cursor_style_roots_list_replaces_cwd_workspace(monkeypatch, tmp_path):
    project = tmp_path / "fvdet_1"
    project.mkdir()
    monkeypatch.delenv(WORKSPACE_ENV_KEY, raising=False)
    monkeypatch.chdir(tmp_path)

    async def scenario():
        client = DaemonClient(spawn_if_missing=False)
        server = MinimalMCPServer(client)
        writes = []

        async def fake_write(message):
            writes.append(message)

        server._write = fake_write

        await server._handle_message(
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {
                    "protocolVersion": "2025-11-25",
                    "capabilities": {"roots": {"listChanged": True}},
                    "clientInfo": {"name": "cursor", "version": "test"},
                },
            }
        )
        assert server._client_supports_roots is True
        assert client.workspace_source == "cwd"

        initialized_task = asyncio.create_task(
            server._handle_message(
                {"jsonrpc": "2.0", "method": "notifications/initialized"}
            )
        )
        for _ in range(20):
            await asyncio.sleep(0)
            roots_requests = [item for item in writes if item.get("method") == "roots/list"]
            if roots_requests:
                break
        assert len(roots_requests) == 1
        request_id = roots_requests[0]["id"]

        await server._handle_message(
            {
                "jsonrpc": "2.0",
                "id": request_id,
                "result": {
                    "roots": [{"uri": project.as_uri(), "name": "fvdet_1"}]
                },
            }
        )
        await initialized_task

        assert client.workspace_root == os.path.normcase(os.path.normpath(str(project.resolve())))
        assert client.workspace_source == "mcp_roots"
        assert client.mcp_roots == [client.workspace_root]

    asyncio.run(scenario())


def test_explicit_env_workspace_wins_over_mcp_roots(monkeypatch, tmp_path):
    explicit = tmp_path / "explicit"
    cursor_root = tmp_path / "cursor-root"
    explicit.mkdir()
    cursor_root.mkdir()
    monkeypatch.setenv(WORKSPACE_ENV_KEY, str(explicit))

    client = DaemonClient(spawn_if_missing=False)
    client.apply_mcp_roots([str(cursor_root)])

    assert client.workspace_source == "env"
    assert client.workspace_root == os.path.normcase(os.path.normpath(str(explicit.resolve())))
    assert client.mcp_roots == [os.path.normcase(os.path.normpath(str(cursor_root.resolve())))]


def test_multiple_mcp_roots_are_not_silently_first_selected(monkeypatch, tmp_path):
    monkeypatch.delenv(WORKSPACE_ENV_KEY, raising=False)
    monkeypatch.chdir(tmp_path)
    first = tmp_path / "a"
    second = tmp_path / "b"
    first.mkdir()
    second.mkdir()

    client = DaemonClient(spawn_if_missing=False)
    original = client.workspace_root
    client.apply_mcp_roots([str(first), str(second)])

    assert client.workspace_source == "mcp_roots_ambiguous"
    assert client.workspace_root == original
    assert client.mcp_roots == [
        os.path.normcase(os.path.normpath(str(first.resolve()))),
        os.path.normcase(os.path.normpath(str(second.resolve()))),
    ]


def test_roots_list_changed_refreshes_workspace(monkeypatch, tmp_path):
    first = tmp_path / "first"
    second = tmp_path / "second"
    first.mkdir()
    second.mkdir()
    monkeypatch.delenv(WORKSPACE_ENV_KEY, raising=False)
    monkeypatch.chdir(tmp_path)

    async def scenario():
        client = DaemonClient(spawn_if_missing=False)
        server = MinimalMCPServer(client)
        server._client_supports_roots = True
        server._client_roots_list_changed = True
        server._client_initialized = True
        responses = [first.as_uri(), second.as_uri()]

        async def fake_send_client_request(method, params=None, timeout=2.0):
            assert method == "roots/list"
            return {"roots": [{"uri": responses.pop(0)}]}

        server._send_client_request = fake_send_client_request
        await server._refresh_client_roots()
        assert client.workspace_root == os.path.normcase(os.path.normpath(str(first.resolve())))
        await server._handle_notification("notifications/roots/list_changed", {})
        assert client.workspace_root == os.path.normcase(os.path.normpath(str(second.resolve())))
        assert client.workspace_source == "mcp_roots"

    asyncio.run(scenario())
