# Claude Skill

`debugger-mcp/SKILL.md` is optional but recommended. It teaches the Agent that debugger-mcp is a runtime-evidence source to use proactively when runtime facts matter, while avoiding unnecessary calls when static analysis is sufficient. It preserves workspace-scoped session selection, compact inspection defaults, latest-frame evaluation, execution-control permissions, and `run_to_line` safety rules.

v0.9.3 tells Agents to pass multi-statement Python directly to `evaluate`, using dynamic execution only when the task requires it and keeping every `frame_eval(...)` call outside the dynamic string. The tool protocol is unchanged.

v0.9.2 removes `evaluate`'s outer `file/line/function` selector. `evaluate` always runs in the current top frame; every access to another frame still present in the current active call stack uses `frame_eval(expression, file, line, function)` inside the expression. DAP frame IDs remain internal, and the same `frame_eval` call can be copied into a god_runner action. This supersedes the older v0.7.0 outer-selector behavior described below.

v0.8.6 gives every god_runner `runInTerminal` launch its own process group and tears down the whole launcher/torchrun/rank group on normal exit, failure, or Ctrl+C without stopping the shared daemon. v0.8.5 keeps the original `evaluate` tool and the same `frame_eval(...)` syntax in interactive expressions and god_runner actions. It extracts literal file/line/function selectors before execution, captures each unique target frame once, and skips cross-frame capture entirely for recipe actions that do not use frame_eval. The expression argument may remain dynamic. No MCP tool or public evaluate parameter is added, and returned or historical frames remain inaccessible.

v0.6.0 added the per-frontend one-time debug-context change notice.

v0.6.1 replaces an older shared daemon unconditionally when package and daemon versions differ. Active VS Code/Cursor debug sessions are disconnected during replacement and must be restarted.

v0.6.2 preserves debuggee/runtime exceptions as complete multiline MCP error text without JSON wrapping, normal-output truncation, or duplicate structured content.

v0.6.3 added standard MCP Roots discovery for frontend workspace identity. Cursor can therefore use a global MCP configuration while debugger-mcp obtains the current single workspace root through `roots/list`. Explicit `DEBUGGER_MCP_WORKSPACE` still overrides Roots, and launch.json still owns DAP session routing.


v0.6.5 corrects over-compaction: stack frames, local names, and evaluate result text are complete by default while their representation stays compact; step results retain stop reason/thread; full raw outputs and all errors/tracebacks are untruncated.

v0.6.8 keeps the v0.6.6 runtime-first guidance and simplifies the public `evaluate` interface to `evaluate(expression[, timeout_seconds])`. v0.6.6 repositions `evaluate` as the primary live-runtime Python execution tool rather than a read-only query primitive. The Skill explicitly encourages multi-statement runtime experiments, direct visualization/artifact creation, and controlled mutation when useful. Runtime visualizations should normally be rendered/saved directly through `evaluate`; temporary `/tmp` artifacts must be reported with an absolute path.


v0.7.1 extends the same exact caller-frame backtracking to `inspect_structure`: call `get_stack()` first, then pass the current frame's exact `file + line + function` triplet. The selector is strict/all-or-none and `inspect_structure` no longer exposes `session_id` or DAP `frame_id`.

v0.7.0 adds precise caller-frame `evaluate` backtracking. Normal calls remain `evaluate(expression)`; when context lives in a caller, call `get_stack()` first and pass the exact `file + line + function` triplet from that current stack line. The selector is strict and all-or-none; DAP frame IDs remain hidden from `evaluate`. Default `get_stack()` output now uses `#0/#1/...` stack depth rather than exposing DAP frame IDs.
