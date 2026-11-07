"""Run directly after editing the variables below; no command-line arguments."""

from pathlib import Path

from debugger_mcp.god_runner import run


WORKSPACE = Path(__file__).resolve().parent
LAUNCH = "Python: Demo through Debugger MCP"
ARTIFACTS_DIR = WORKSPACE / "god_run_artifacts"

# This is the existing DAP setBreakpoints shape. ``action`` is the only
# god_runner extension; every other field is passed unchanged to debugpy.
BREAKPOINTS = [
    {
        "source": {"path": "${workspaceFolder}/demo.py"},
        "breakpoints": [
            {
                "line": 2,
                "name": "collect-demo-values",
                "action": r'''
from pathlib import Path

caller_scale = frame_eval(
    "scale",
    "demo.py",
    9,
    "transform",
)

output = Path("god_run_artifacts") / f"values_{value}.txt"
output.parent.mkdir(parents=True, exist_ok=True)
output.write_text(f"value={value}, scale={caller_scale}\n", encoding="utf-8")
''',
            }
        ],
    }
]


if __name__ == "__main__":
    run(
        workspace=WORKSPACE,
        launch=LAUNCH,
        breakpoints=BREAKPOINTS,
        artifacts_dir=ARTIFACTS_DIR,
    )
