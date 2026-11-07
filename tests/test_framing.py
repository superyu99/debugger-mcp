import asyncio

from debugger_mcp.dap import read_dap_message, write_dap_message


def test_dap_round_trip():
    async def run():
        received = {}

        async def handler(reader, writer):
            received.update(await read_dap_message(reader))
            writer.close()
            await writer.wait_closed()

        server = await asyncio.start_server(handler, "127.0.0.1", 0)
        port = server.sockets[0].getsockname()[1]
        reader, writer = await asyncio.open_connection("127.0.0.1", port)
        await write_dap_message(writer, {"seq": 1, "type": "request", "command": "threads"}, asyncio.Lock())
        writer.close()
        await writer.wait_closed()
        await asyncio.sleep(0.05)
        server.close()
        await server.wait_closed()
        assert received["command"] == "threads"

    asyncio.run(run())
