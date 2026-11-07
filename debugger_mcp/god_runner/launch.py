from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Any, Union


def _strip_jsonc(text: str) -> str:
    """Remove JSONC comments and trailing commas without touching strings."""
    out: list[str] = []
    index, quoted, escaped = 0, False, False
    while index < len(text):
        char = text[index]
        if quoted:
            out.append(char)
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                quoted = False
            index += 1
            continue
        if char == '"':
            quoted = True
            out.append(char)
            index += 1
        elif text.startswith("//", index):
            index = text.find("\n", index)
            index = len(text) if index < 0 else index
        elif text.startswith("/*", index):
            end = text.find("*/", index + 2)
            if end < 0:
                raise ValueError("Unterminated block comment in launch.json")
            index = end + 2
        else:
            out.append(char)
            index += 1
    return re.sub(r",\s*([}\]])", r"\1", "".join(out))


def _expand(value: Any, workspace: Path) -> Any:
    if isinstance(value, list):
        return [_expand(item, workspace) for item in value]
    if isinstance(value, dict):
        return {key: _expand(item, workspace) for key, item in value.items()}
    if not isinstance(value, str):
        return value
    value = value.replace("${workspaceFolder}", str(workspace))
    return re.sub(r"\$\{env:([^}]+)}", lambda match: os.getenv(match.group(1), ""), value)


def load_launch_config(workspace: Union[str, Path], name: str) -> dict[str, Any]:
    workspace = Path(workspace).resolve()
    path = workspace / ".vscode" / "launch.json"
    if not path.is_file():
        raise FileNotFoundError(f"VS Code launch configuration not found: {path}")
    document = json.loads(_strip_jsonc(path.read_text(encoding="utf-8")))
    matches = [item for item in document.get("configurations", []) if item.get("name") == name]
    if len(matches) != 1:
        available = [item.get("name") for item in document.get("configurations", [])]
        raise ValueError(f"Expected exactly one launch configuration named {name!r}; available={available}")
    config = _expand(matches[0], workspace)
    config.pop("debugServer", None)
    env = dict(config.get("env") or {})
    env["DEBUGGER_MCP_WORKSPACE"] = str(workspace)
    config["env"] = env
    return config
