from __future__ import annotations

import asyncio
import os

from debugger_mcp.dap import DAPSession


class DummyWriter:
    def close(self) -> None:
        pass


def test_run_to_line_suppresses_current_user_breakpoint_until_resume(tmp_path) -> None:
    async def scenario() -> None:
        writer = DummyWriter()
        session = DAPSession(
            session_id=1,
            frontend_reader=None,
            frontend_writer=writer,
            backend_reader=None,
            backend_writer=writer,
            proxy_host="127.0.0.1",
            proxy_port=4711,
        )
        session.state.status = "stopped"
        source_path = str(tmp_path / "model.py")
        key = os.path.normcase(os.path.normpath(source_path))
        session._user_breakpoint_args[key] = {
            "source": {"path": source_path},
            "breakpoints": [{"line": 9}, {"line": 10}],
            "sourceModified": False,
        }
        requests: list[list[dict]] = []

        async def request(command, arguments=None, timeout=None):
            assert command == "setBreakpoints"
            breakpoints = list(arguments["breakpoints"])
            requests.append(breakpoints)
            return {
                "breakpoints": [
                    {"id": index + 1, "verified": True, "line": item["line"]}
                    for index, item in enumerate(breakpoints)
                ]
            }

        session.request = request
        target = await session.install_temporary_breakpoint(
            source_path,
            11,
            resume_line=10,
        )
        assert target["line"] == 11
        assert requests[0] == [{"line": 9}, {"line": 11}]

        await session.remove_temporary_breakpoint()
        assert requests[1] == [{"line": 9}, {"line": 10}]
        assert session._temporary_breakpoints == {}

    asyncio.run(scenario())
