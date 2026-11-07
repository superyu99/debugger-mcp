"""Edit these values directly; no command-line arguments are required."""

from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

# VS Code connects here through launch.json: "debugServer": 4711.
DAP_PROXY_HOST = os.getenv("DEBUGGER_MCP_DAP_HOST", "127.0.0.1")
DAP_PROXY_PORT = int(os.getenv("DEBUGGER_MCP_DAP_PORT", "4711"))

# Every Claude MCP stdio process connects to one shared local daemon here.
CONTROL_HOST = os.getenv("DEBUGGER_MCP_CONTROL_HOST", "127.0.0.1")
CONTROL_PORT = int(os.getenv("DEBUGGER_MCP_CONTROL_PORT", "4712"))

# Python used by the daemon to start ``python -m debugpy.adapter``.
DEBUGPY_PYTHON = os.getenv("DEBUGGER_MCP_DEBUGPY_PYTHON", sys.executable)

DAP_REQUEST_TIMEOUT_SECONDS = 15.0
# evaluate can legitimately run heavier runtime experiments/visualization.
EVALUATE_TIMEOUT_SECONDS = 60.0
STEP_TIMEOUT_SECONDS = 30.0
RUN_TO_LINE_MAX_WAIT_SECONDS = 120.0
RPC_REQUEST_TIMEOUT_SECONDS = 135.0
DAEMON_START_TIMEOUT_SECONDS = 8.0
LOG_LEVEL = os.getenv("DEBUGGER_MCP_LOG_LEVEL", "INFO")
DAP_AUDIT_ENABLED = os.getenv("DEBUGGER_MCP_DAP_AUDIT", "1").strip().lower() not in {
    "0", "false", "no", "off"
}
DAEMON_LOG_PATH = Path(
    os.getenv(
        "DEBUGGER_MCP_DAEMON_LOG",
        str(Path(tempfile.gettempdir()) / "debugger-mcp-daemon.log"),
    )
)
