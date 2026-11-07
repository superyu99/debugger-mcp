from pathlib import Path

from debugger_mcp.mcp_server import TOOLS


ROOT = Path(__file__).resolve().parents[1]
SKILL = ROOT / "claude-skill" / "debugger-mcp" / "SKILL.md"


def _tool(name: str) -> dict:
    return next(tool for tool in TOOLS if tool["name"] == name)


def test_evaluate_tool_description_exposes_runtime_power():
    description = _tool("evaluate")["description"]
    lower = description.lower()
    assert "live runtime repl/power tool" in lower
    assert "mutate runtime objects" in lower
    assert "save artifacts" in lower
    assert "visualization" in lower
    assert "prefer read-only" not in lower
    assert "focused python" not in lower


def test_evaluate_guidance_distinguishes_expressions_from_statement_suites():
    description = _tool("evaluate")["description"]
    assert "comma-separated values are one tuple expression" in description
    assert "Statement suites execute for side effects and may return empty/(omitted)" in description

    text = SKILL.read_text(encoding="utf-8")
    assert "comma-separated values form one tuple expression" in text
    assert "an empty or `(omitted)` result is normal" in text
    assert "Pass multi-statement Python directly" in text
    assert "keep every `frame_eval(...)` call outside the dynamic string" in text
    assert "multi-statement Python through `exec(...)`" not in text
    assert "exec(...) or result_variable" not in text


def test_skill_does_not_restrict_evaluate_to_read_only_queries():
    text = SKILL.read_text(encoding="utf-8")
    lower = text.lower()
    assert "do **not** artificially restrict `evaluate` to read-only expressions" in lower
    assert "use read-only expressions by default" not in lower
    assert "without explicit user intent, do not" not in lower
    assert "prefer read-only expressions" not in lower


def test_skill_supports_direct_runtime_visualization_and_artifacts():
    text = SKILL.read_text(encoding="utf-8")
    assert "Runtime visualization and artifact creation" in text
    assert ".debugger_mcp_artifacts" in text
    assert "/tmp/debugger_mcp/" in text
    assert "Always tell the user the final saved path" in text
    assert "Do **not** default to this indirect workflow" in text


def test_packaged_skill_copies_are_identical():
    canonical = SKILL.read_text(encoding="utf-8")
    assert (ROOT / "cursor-skill" / "debugger-mcp" / "SKILL.md").read_text(encoding="utf-8") == canonical
    assert (ROOT / "debugger-mcp-v0.9.3-SKILL.md").read_text(encoding="utf-8") == canonical


def test_skill_keeps_evaluate_interface_minimal_and_uses_frame_eval():
    text = SKILL.read_text(encoding="utf-8")
    assert "evaluate(expression=...)" in text
    assert "current active call stack" in text
    assert "outer `file/line/function` selection" in text
    assert "public contract is `evaluate(expression[, timeout_seconds])`" in text
    assert "The default timeout is 60 seconds" in text
    assert "Only pass `timeout_seconds`" in text
    assert "evaluate(full=true)" not in text
    assert "Normally omit `frame_id`" not in text


def test_skill_exposes_inspect_structure_caller_backtracking():
    text = SKILL.read_text(encoding="utf-8")
    assert "pass the exact current `file + line + function` triplet to `inspect_structure`" in text
    assert "Do not transfer that concept into `evaluate` or `inspect_structure`" in text


def test_skill_explains_frame_eval_inside_existing_evaluate():
    text = SKILL.read_text(encoding="utf-8")
    assert "## `frame_eval` inside `evaluate`: access objects from the current active call stack" in text
    assert "This is part of the existing `evaluate`, not a separate MCP tool" in text
    assert "even when reading only one value" in text
    assert "`frame_eval` returns the actual object reference inside the same debuggee process" in text
    assert "Keep `file`, `line`, and `function` as literals copied from `get_stack()`" in text
    assert "The `expression` argument may be a variable" in text
    assert "captures each unique target frame once" in text
    assert "This is access to the current active call stack, not historical execution" in text
    assert "Every target must still appear in the current `get_stack()` output" in text
    assert "The `frame_eval(...)` spelling is identical in interactive `evaluate` and god_runner actions" in text


def test_skill_makes_agent_generated_god_runner_recipe_the_default_handoff():
    text = SKILL.read_text(encoding="utf-8")
    assert "Write a god_runner recipe only when the user explicitly asks for one" in text
    assert "do not merely explain the format" in text
    assert "call `attach_debugpy(full=true)`" in text
    assert "follow `selected_session.root_session_id` to the matching root session" in text
    assert "copy that root session's `launch_name`" in text
    assert "Inspect only that named configuration in `.vscode/launch.json`; never choose among configurations" in text
    assert "If no root `launch_name` is available, ask the user" in text
    assert "Copy `frame_eval` selectors from the current `get_stack()` output" in text
    assert "god_runner's injected `frame_eval(expression, file, line, function)`" in text
    assert "validated code can be copied directly into a recipe action without rewriting it" in text
    assert "Put all cross-frame reads in one action so each unique target frame is captured once" in text
    assert "Recipe actions expose no legacy `evaluate(...)` alias" in text
    assert "always use this one spelling" in text
    assert "only when the user explicitly asks for one" in text
    assert "never start, execute, wait for, or monitor a long-running train or inference job" in text
    assert "The user always launches the recipe" in text
    assert "program stdout/stderr appears in the terminal where they launch the recipe" in text
    assert "`[god] waiting` is the 15-second liveness heartbeat" in text
    assert "TCP 4711 is the shared DAP proxy rather than the stdio MCP channel" in text
    assert "stopping the recipe cleans its torchrun/rank process group" in text
    assert "leaving the shared daemon/MCP available" in text
    assert "For complex tasks, do not contort the workflow" not in text


def test_skill_cleans_repeated_cuda_recipe_actions():
    text = SKILL.read_text(encoding="utf-8")
    assert "god_runner clears its merged action namespace after every hit" in text
    assert "For repeatedly hit actions that touch CUDA tensors" in text
    assert "move results to CPU inside `frame_eval` as early as practical" in text
    assert "`finally` to `del` action-created large temporaries" in text
    assert "`gc.collect()`" in text
    assert "`torch.cuda.empty_cache()`" in text
    assert "Do not substitute `hitCondition` unless the user requests sampling" in text
