# Cursor Skill

Copy `debugger-mcp/SKILL.md` into Cursor's supported Skill location if you want the Agent to follow debugger-mcp's runtime-first investigation and execution-control rules.

v0.9.3 tells Agents to pass multi-statement Python directly to `evaluate`, using dynamic execution only when the task requires it and keeping every `frame_eval(...)` call outside the dynamic string. The tool protocol is unchanged.

v0.9.2 removes `evaluate`'s outer `file/line/function` selector. `evaluate` always runs in the current top frame; every access to another frame still present in the current active call stack uses `frame_eval(expression, file, line, function)` inside the expression. DAP frame IDs remain internal, and the same `frame_eval` call can be copied into a god_runner action. This supersedes the older v0.7.0 outer-selector behavior described below.

v0.8.6 gives every god_runner `runInTerminal` launch its own process group and tears down the whole launcher/torchrun/rank group on normal exit, failure, or Ctrl+C without stopping the shared daemon. v0.8.5 keeps the original `evaluate` tool and the same `frame_eval(...)` syntax in interactive expressions and god_runner actions. It extracts literal file/line/function selectors before execution, captures each unique target frame once, and skips cross-frame capture entirely for recipe actions that do not use frame_eval. The expression argument may remain dynamic. No MCP tool or public evaluate parameter is added, and returned or historical frames remain inaccessible.

The MCP server itself does not require the Skill. v0.6.3 added Cursor compatibility through standard MCP Roots support in the server protocol. v0.6.5 corrects over-compaction without changing that routing model: core runtime data sets remain complete while formatting stays compact, and errors/tracebacks remain complete and untruncated.

v0.6.8 keeps the v0.6.6 runtime-first guidance and simplifies the public `evaluate` interface to `evaluate(expression[, timeout_seconds])`. v0.6.6 gives Cursor Agents the same unrestricted runtime-first `evaluate` guidance: use the paused debuggee as a live Python execution environment for computation, experiments, visualization, and artifacts instead of unnecessarily exporting runtime data to a separate plotting process.


v0.7.1 extends the same exact caller-frame backtracking to `inspect_structure`: call `get_stack()` first, then pass the current frame's exact `file + line + function` triplet. The selector is strict/all-or-none and `inspect_structure` no longer exposes `session_id` or DAP `frame_id`.

v0.7.0 adds precise caller-frame `evaluate` backtracking. Normal calls remain `evaluate(expression)`; when context lives in a caller, call `get_stack()` first and pass the exact `file + line + function` triplet from that current stack line. The selector is strict and all-or-none; DAP frame IDs remain hidden from `evaluate`. Default `get_stack()` output now uses `#0/#1/...` stack depth rather than exposing DAP frame IDs.
