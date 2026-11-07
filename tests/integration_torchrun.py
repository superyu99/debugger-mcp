"""Run manually: python tests/integration_torchrun.py

Runs real torchrun --nproc_per_node=1 through an integratedTerminal-style DAP
launch and proves that the rank worker reconnects through debugger-mcp, hits a
breakpoint, and is selected by default for Agent inspection.
"""

from __future__ import annotations

import asyncio
import pathlib
import shutil
import socket
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))

from debugger_mcp.daemon import SharedDebuggerDaemon
from debugger_mcp.rpc import DaemonClient
from integration_subprocess import TinyVSCode


async def main():
    torchrun = shutil.which("torchrun")
    if torchrun is None:
        print("torchrun not installed; skipped")
        return

    worker_path = ROOT / "examples" / "torchrun_worker.py"
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        master_port = listener.getsockname()[1]
    daemon = SharedDebuggerDaemon(dap_port=0, control_port=0, debugpy_python=sys.executable)
    await daemon.start()
    client = DaemonClient(
        host=daemon.control_host,
        port=daemon.control_port,
        dap_host=daemon.proxy.host,
        dap_port=daemon.proxy.port,
        spawn_if_missing=False,
        workspace_root=str(ROOT),
    )
    vscode = TinyVSCode(daemon, worker_path, 7)
    root = await vscode.open_session(reverse_handler=vscode.handle_root_reverse_request)

    await vscode.initialize(root)
    launch_task = asyncio.create_task(
        root.request(
            "launch",
            {
                "name": "torchrun smoke",
                "type": "debugpy",
                "request": "launch",
                "program": torchrun,
                "console": "integratedTerminal",
                "justMyCode": False,
                "subProcess": True,
                "cwd": str(ROOT / "examples"),
                "python": sys.executable,
                "args": [
                    "--nnodes", "1",
                    "--node_rank", "0",
                    "--nproc_per_node", "1",
                    "--master_addr", "127.0.0.1",
                    "--master_port", str(master_port),
                    str(worker_path),
                ],
                "env": {
                    "DEBUGGER_MCP_WORKSPACE": str(ROOT),
                    "OMP_NUM_THREADS": "1",
                },
            },
        )
    )
    await root.event("initialized")
    await root.request("configurationDone")
    await launch_task

    child = await asyncio.wait_for(vscode.child_ready.get(), 35)
    await child.event("stopped", timeout=35)

    status = await client.call("attach_debugpy", {"full": True})
    stopped_workers = [
        item for item in status["sessions"]
        if item["session_kind"] == "subprocess" and item["debug_state"] == "stopped"
    ]
    assert stopped_workers, status
    worker = stopped_workers[-1]
    assert status["selected_session_id"] == worker["session_id"], status

    marker = await client.call("evaluate", {"expression": "marker"})
    rank = await client.call(
        "evaluate",
        {"expression": "__import__('os').environ.get('LOCAL_RANK')"},
    )
    workspace_env = await client.call(
        "evaluate",
        {"expression": "__import__('os').environ.get('DEBUGGER_MCP_WORKSPACE') is None"},
    )
    assert marker["__mcp_text__"] == "100", marker
    assert rank["__mcp_text__"] == "'0'", rank
    assert workspace_env["__mcp_text__"] == "True", workspace_env
    print("real torchrun rank-worker integration passed")

    await child.request("continue", {"threadId": worker["stopped_thread_id"]})
    await asyncio.sleep(1.0)
    for session in vscode.children:
        await session.close()
    await root.close()
    for process in vscode.terminal_processes:
        if process.returncode is None:
            process.terminate()
        await process.wait()
    await daemon.stop()


if __name__ == "__main__":
    asyncio.run(main())
