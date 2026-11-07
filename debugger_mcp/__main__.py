from __future__ import annotations

import asyncio
import logging
import sys

from . import config
from .mcp_server import MinimalMCPServer
from .rpc import DaemonClient


def configure_logging() -> None:
    logging.basicConfig(
        level=getattr(logging, config.LOG_LEVEL.upper(), logging.INFO),
        format="[%(asctime)s] %(levelname)s %(name)s: %(message)s",
        stream=sys.stderr,
    )


async def async_main() -> None:
    configure_logging()
    client = DaemonClient()
    # Start/reuse the daemon early so VS Code can press F5 immediately. Keep the
    # MCP stdio frontend alive even if startup fails; tool calls will return the
    # actionable daemon error instead of making /mcp show a generic failure.
    try:
        await client.ensure_daemon()
    except Exception as exc:
        logging.getLogger(__name__).warning("Shared daemon is not ready yet: %s", exc)
    await MinimalMCPServer(client).run()


def main() -> None:
    asyncio.run(async_main())


if __name__ == "__main__":
    main()
