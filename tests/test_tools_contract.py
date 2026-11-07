from debugger_mcp import __version__
from debugger_mcp.mcp_server import TOOLS


def test_v093_tool_surface():
    assert __version__ == "0.9.3"
    assert [tool["name"] for tool in TOOLS] == [
        "attach_debugpy",
        "get_threads",
        "get_stack",
        "get_locals",
        "evaluate",
        "inspect_structure",
        "step_over",
        "step_into",
        "step_out",
        "run_to_line",
    ]


def test_process_session_selection_inputs():
    schemas = {tool["name"]: tool["inputSchema"] for tool in TOOLS}
    for name in [
        "get_threads",
        "get_stack",
        "get_locals",
        "step_over",
        "step_into",
        "step_out",
        "run_to_line",
    ]:
        assert "session_id" in schemas[name]["properties"]
    assert "session_id" not in schemas["evaluate"]["properties"]
    assert "session_id" not in schemas["inspect_structure"]["properties"]
    assert "frame_id" not in schemas["inspect_structure"]["properties"]


def test_run_to_line_requires_reachability_reason():
    schema = {tool["name"]: tool["inputSchema"] for tool in TOOLS}["run_to_line"]
    assert schema["required"] == ["file", "line", "reason"]
    assert schema["properties"]["cross_file_authorized"]["default"] is False


def test_inspect_structure_has_hard_schema_limits_and_precise_backtracking():
    schema = {tool["name"]: tool["inputSchema"] for tool in TOOLS}["inspect_structure"]
    assert schema["properties"]["max_depth"]["maximum"] == 8
    assert schema["properties"]["max_nodes"]["maximum"] == 500
    assert schema["properties"]["max_chars"]["maximum"] == 30000
    assert {"file", "line", "function"} <= set(schema["properties"])
    assert "session_id" not in schema["properties"]
    assert "frame_id" not in schema["properties"]
    assert schema["required"] == ["expression"]
    assert schema["additionalProperties"] is False


def test_get_locals_has_simple_summary_or_full_switch():
    schema = {tool["name"]: tool["inputSchema"] for tool in TOOLS}["get_locals"]
    assert schema["properties"]["full"] == {"type": "boolean", "default": False}
    assert set(schema["properties"]) == {"session_id", "frame_id", "full"}


def test_evaluate_public_schema_is_expression_plus_timeout():
    schema = {tool["name"]: tool["inputSchema"] for tool in TOOLS}["evaluate"]
    assert set(schema["properties"]) == {"expression", "timeout_seconds"}
    assert schema["required"] == ["expression"]
    assert schema["additionalProperties"] is False
    assert schema["properties"]["timeout_seconds"]["type"] == "number"
    assert schema["properties"]["timeout_seconds"]["minimum"] == 1
    assert "default" not in schema["properties"]["timeout_seconds"]


def test_attach_debugpy_has_compact_or_full_switch():
    schema = {tool["name"]: tool["inputSchema"] for tool in TOOLS}["attach_debugpy"]
    assert schema["properties"]["full"] == {"type": "boolean", "default": False}
    assert set(schema["properties"]) == {"full"}
    description = {tool["name"]: tool for tool in TOOLS}["attach_debugpy"]["description"]
    assert "active root launch configuration" in description
    assert "launch_name and root_session_id" in description


def test_evaluate_description_uses_frame_eval_for_active_stack_access():
    tool = {item["name"]: item for item in TOOLS}["evaluate"]
    description = tool["description"]
    lower = description.lower()
    assert "latest stopped top frame" in lower
    assert "first call get_stack" in lower
    assert "timeout_seconds" in description
    assert "frame_eval(expression, file, line, function)" in description
    assert "file, line, and function must be literals" in lower
    assert "expression may be dynamic" in lower
    assert "real in-process object" in lower
    assert "cannot access a frame that has returned" in lower
    assert "same syntax as a god_runner action" in lower
    assert "repeatedly hit god_runner action" in lower
    assert "gc.collect()" in description
    assert "torch.cuda.empty_cache()" in description
    assert "full=true" not in lower
    assert "pass multi-statement python directly" in lower
    assert "keep every frame_eval call outside the dynamic string" in lower


def test_skill_explains_current_active_stack_semantics():
    from pathlib import Path

    skill = (Path(__file__).parents[1] / "claude-skill" / "debugger-mcp" / "SKILL.md").read_text(
        encoding="utf-8"
    )
    assert "current active call stack" in skill
    assert "outer `file/line/function` selection" in skill
    assert "Exactly one current frame must match" in skill
    assert "not historical execution" in skill
    assert "The visible `#0/#1/...` prefixes are call-stack depth only" in skill
    assert "Lazy debug-context change notice" in skill
    assert "independently remembers the last paused debug context" in skill


def test_v065_read_and_step_tools_have_full_diagnostic_switch():
    schemas = {tool["name"]: tool["inputSchema"] for tool in TOOLS}
    for name in ["get_threads", "get_stack", "step_over", "step_into", "step_out"]:
        assert schemas[name]["properties"]["full"] == {"type": "boolean", "default": False}


def test_get_stack_is_complete_by_default_and_bounded_only_explicitly():
    schema = {tool["name"]: tool["inputSchema"] for tool in TOOLS}["get_stack"]
    assert "default" not in schema["properties"]["levels"]
    assert schema["properties"]["levels"]["type"] == ["integer", "null"]
    description = {tool["name"]: tool for tool in TOOLS}["get_stack"]["description"]
    assert "every frame" in description
    assert "Only pass levels" in description


def test_inspect_structure_description_exposes_precise_caller_backtracking():
    tool = {item["name"]: item for item in TOOLS}["inspect_structure"]
    lower = tool["description"].lower()
    assert "latest stopped top frame" in lower
    assert "first call get_stack" in lower
    assert "file + line + function" in lower
    assert "all-or-none" in lower
    assert "never guesses" in lower
    assert "does not expose dap frame ids" in lower
