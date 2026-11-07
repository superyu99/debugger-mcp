from __future__ import annotations

import ast
import uuid
from dataclasses import dataclass
from typing import Any


_FRAMES_REGISTRY = "_debugger_mcp_frame_eval_frames"
_RESULTS_REGISTRY = "_debugger_mcp_frame_eval_results"
_PARAMETERS = ("expression", "file", "line", "function")


@dataclass(frozen=True)
class FrameSelector:
    file: str
    line: int
    function: str

    @property
    def key(self) -> tuple[str, int, str]:
        return (self.file.replace("\\", "/"), self.line, self.function)


def extract_frame_eval_selectors(code: str) -> tuple[FrameSelector, ...]:
    """Extract unique literal selectors from direct ``frame_eval`` calls."""
    try:
        tree = ast.parse(code, mode="exec")
    except SyntaxError:
        return ()

    selectors: list[FrameSelector] = []
    seen: set[tuple[str, int, str]] = set()
    for node in ast.walk(tree):
        if not (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "frame_eval"
        ):
            continue
        selector = _selector_from_call(node)
        if selector.key not in seen:
            seen.add(selector.key)
            selectors.append(selector)
    return tuple(selectors)


def _selector_from_call(call: ast.Call) -> FrameSelector:
    if len(call.args) > len(_PARAMETERS) or any(isinstance(arg, ast.Starred) for arg in call.args):
        raise ValueError(
            "frame_eval requires expression, file, line, and function without *args"
        )

    values: dict[str, ast.AST] = {
        _PARAMETERS[index]: value for index, value in enumerate(call.args)
    }
    for keyword in call.keywords:
        if keyword.arg is None:
            raise ValueError("frame_eval does not accept **kwargs")
        if keyword.arg not in _PARAMETERS:
            raise ValueError(f"frame_eval got an unexpected argument: {keyword.arg}")
        if keyword.arg in values:
            raise ValueError(f"frame_eval got multiple values for {keyword.arg}")
        values[keyword.arg] = keyword.value

    missing = [name for name in _PARAMETERS if name not in values]
    if missing:
        raise ValueError(
            "frame_eval requires expression, file, line, and function; "
            f"missing {', '.join(missing)}"
        )

    file = _literal(values["file"])
    line = _literal(values["line"])
    function = _literal(values["function"])
    if not isinstance(file, str) or not file.strip():
        raise ValueError("frame_eval file must be a non-empty string literal copied from get_stack()")
    if isinstance(line, bool) or not isinstance(line, int) or line <= 0:
        raise ValueError("frame_eval line must be a positive integer literal copied from get_stack()")
    if not isinstance(function, str) or not function.strip():
        raise ValueError("frame_eval function must be a non-empty string literal copied from get_stack()")
    return FrameSelector(file, line, function)


def _literal(node: ast.AST) -> Any:
    return node.value if isinstance(node, ast.Constant) else None


def capture_frame_expression(
    token: str,
    selector: FrameSelector,
    metadata: dict[str, Any],
) -> str:
    return (
        f"__import__('builtins').__dict__.setdefault({_FRAMES_REGISTRY!r}, {{}})"
        f".setdefault({token!r}, {{}}).__setitem__({selector.key!r}, "
        f"(globals(), locals(), {metadata!r}))"
    )


def cleanup_frame_expression(token: str) -> str:
    return (
        f"__import__('builtins').__dict__.get({_FRAMES_REGISTRY!r}, {{}})"
        f".pop({token!r}, None)"
    )


def execute_with_frames_expression(token: str, code: str) -> str:
    helper = r'''
def frame_eval(expression, file, line, function):
    if not isinstance(expression, str) or not expression.strip():
        raise ValueError("frame_eval expression must be a non-empty string")
    if not isinstance(file, str) or not file.strip():
        raise ValueError("frame_eval file must be copied from the current get_stack() output")
    if isinstance(line, bool) or not isinstance(line, int) or line <= 0:
        raise ValueError("frame_eval line must be a positive integer copied from get_stack()")
    if not isinstance(function, str) or not function.strip():
        raise ValueError("frame_eval function must be copied from the current get_stack() output")
    key = (file.replace("\\", "/"), line, function)
    captured = _frame_eval_frames.get(key)
    if captured is None:
        available = "\n".join(
            f'#{frame[2]["stack_index"]} {frame[2]["file"]}:{frame[2]["line"]} {frame[2]["function"]}'
            for frame in _frame_eval_frames.values()
        )
        raise RuntimeError(
            f"frame_eval selector was not captured: {file}:{line} {function}"
            f"\nCaptured target frames:\n{available or '<none>'}"
        )
    frame_globals, frame_locals, _ = captured
    return eval(expression, frame_globals, frame_locals)
'''
    wrapper = f'''
import builtins as _frame_eval_builtins
_frame_eval_registry = _frame_eval_builtins.__dict__[{_FRAMES_REGISTRY!r}]
_frame_eval_frames = _frame_eval_registry[{token!r}]
try:
    _frame_eval_namespace = dict(_frame_eval_top_globals)
    _frame_eval_namespace.update(_frame_eval_top_locals)
    _frame_eval_namespace["_frame_eval_frames"] = _frame_eval_frames
    exec(compile({helper!r}, "<debugger_mcp:frame_eval_helper>", "exec"), _frame_eval_namespace, _frame_eval_namespace)
    try:
        _frame_eval_compiled = compile({code!r}, "<debugger_mcp:evaluate:frame_eval>", "eval")
    except SyntaxError:
        exec(compile({code!r}, "<debugger_mcp:evaluate:frame_eval>", "exec"), _frame_eval_namespace, _frame_eval_namespace)
        _frame_eval_result = None
    else:
        _frame_eval_result = eval(_frame_eval_compiled, _frame_eval_namespace, _frame_eval_namespace)
    _frame_eval_builtins.__dict__.setdefault({_RESULTS_REGISTRY!r}, {{}})[{token!r}] = _frame_eval_result
finally:
    _frame_eval_registry.pop({token!r}, None)
'''
    return (
        "(lambda _frame_eval_builtins, _frame_eval_top_globals, _frame_eval_top_locals: ("
        f"exec(compile({wrapper!r}, '<debugger_mcp:frame_eval_dispatch>', 'exec'), globals(), locals()), "
        f"_frame_eval_builtins.__dict__[{_RESULTS_REGISTRY!r}].pop({token!r})"
        ")[1])(__import__('builtins'), globals(), locals())"
    )


def new_frame_token() -> str:
    return uuid.uuid4().hex
