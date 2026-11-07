from __future__ import annotations

import os
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

from ..frame_eval import FrameSelector, extract_frame_eval_selectors


@dataclass(frozen=True)
class Action:
    source_path: str
    requested_line: int
    code: str
    name: str
    frame_selectors: tuple[FrameSelector, ...]


def normalize_breakpoints(groups: Iterable[dict[str, Any]], workspace: Path) -> list[tuple[dict[str, Any], list[Action]]]:
    normalized: list[tuple[dict[str, Any], list[Action]]] = []
    for group_index, raw_group in enumerate(groups):
        group = dict(raw_group)
        source = dict(group.get("source") or {})
        source_path = str(source.get("path") or "").replace("${workspaceFolder}", str(workspace))
        if not source_path:
            raise ValueError(f"BREAKPOINTS[{group_index}].source.path is required")
        if not os.path.isabs(source_path):
            source_path = str((workspace / source_path).resolve())
        source["path"] = source_path
        clean_breakpoints, actions = [], []
        for item_index, raw_item in enumerate(group.get("breakpoints") or []):
            item = dict(raw_item)
            code = item.pop("action", None)
            line = item.get("line")
            if isinstance(line, bool) or not isinstance(line, int) or line <= 0:
                raise ValueError(f"BREAKPOINTS[{group_index}].breakpoints[{item_index}].line must be positive")
            if not isinstance(code, str) or not code.strip():
                raise ValueError(f"BREAKPOINTS[{group_index}].breakpoints[{item_index}].action is required")
            name = str(item.pop("name", None) or f"{Path(source_path).name}:{line}")
            try:
                compile(code, f"<god_runner:{name}>", "exec")
                frame_selectors = extract_frame_eval_selectors(code)
            except (SyntaxError, ValueError) as exc:
                raise ValueError(
                    f"BREAKPOINTS[{group_index}].breakpoints[{item_index}].action: {exc}"
                ) from exc
            clean_breakpoints.append(item)
            actions.append(Action(source_path, line, code, name, frame_selectors))
        if not actions:
            raise ValueError(f"BREAKPOINTS[{group_index}] has no action breakpoints")
        group["source"] = source
        group["breakpoints"] = clean_breakpoints
        group.setdefault("sourceModified", False)
        normalized.append((group, actions))
    return normalized


def display_path(path: str, workspace: Path) -> str:
    try:
        relative = os.path.relpath(os.path.normpath(path), str(workspace))
        if relative != os.pardir and not relative.startswith(os.pardir + os.sep):
            return relative
    except (OSError, ValueError):
        pass
    return os.path.normpath(path)


def capture_expression(
    token: str,
    selector: FrameSelector,
    stack_index: int,
    frame: dict[str, Any],
    workspace: Path,
) -> str:
    source_path = str((frame.get("source") or {}).get("path") or "")
    metadata = {
        "file": display_path(source_path, workspace),
        "absolute_file": os.path.normpath(source_path),
        "line": frame.get("line"),
        "function": str(frame.get("name") or ""),
        "stack_index": stack_index,
    }
    return (
        "__import__('builtins').__dict__.setdefault('_god_runner_frames', {})"
        f".setdefault({token!r}, {{}}).__setitem__({selector.key!r}, "
        f"(globals(), locals(), {metadata!r}))"
    )


def cleanup_expression(token: str) -> str:
    return (
        "__import__('builtins').__dict__.get('_god_runner_frames', {})"
        f".pop({token!r}, None)"
    )


def direct_execution_expression(code: str, action_name: str) -> str:
    wrapper = f'''
_god_namespace = {{}}
try:
    _god_namespace.update(globals())
    _god_namespace.update(locals())
    exec(compile({code!r}, {f'<god_runner:{action_name}>'!r}, "exec"), _god_namespace, _god_namespace)
finally:
    _god_namespace.clear()
'''
    return f"exec(compile({wrapper!r}, '<god_runner:dispatch>', 'exec'), globals(), locals())"


def execution_expression(token: str, code: str, action_name: str) -> str:
    helper = r'''
def frame_eval(expression, file, line, function):
    if not isinstance(expression, str) or not expression.strip():
        raise ValueError("frame_eval expression must be a non-empty string")
    key = (file.replace("\\", "/"), line, function)
    captured = _god_frames.get(key)
    if captured is None:
        available = "\n".join(
            f'#{frame[2]["stack_index"]} {frame[2]["file"]}:{frame[2]["line"]} {frame[2]["function"]}'
            for frame in _god_frames.values()
        )
        raise RuntimeError(
            f"frame_eval selector was not captured: {file}:{line} {function}"
            f"\nCaptured target frames:\n{available or '<none>'}"
        )
    frame_globals, frame_locals, _ = captured
    return eval(expression, frame_globals, frame_locals)

'''
    wrapper = f'''
import builtins as _god_builtins
_god_frames = _god_builtins.__dict__["_god_runner_frames"][{token!r}]
_god_namespace = {{}}
try:
    _god_namespace.update(globals())
    _god_namespace.update(locals())
    _god_namespace["_god_frames"] = _god_frames
    exec(compile({helper!r}, "<god_runner:frame_eval>", "exec"), _god_namespace, _god_namespace)
    exec(compile({code!r}, {f'<god_runner:{action_name}>'!r}, "exec"), _god_namespace, _god_namespace)
finally:
    _god_namespace.clear()
    _god_builtins.__dict__.get("_god_runner_frames", {{}}).pop({token!r}, None)
'''
    return f"exec(compile({wrapper!r}, '<god_runner:dispatch>', 'exec'), globals(), locals())"


def new_stop_token() -> str:
    return uuid.uuid4().hex
