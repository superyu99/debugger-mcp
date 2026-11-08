**English** | [简体中文](RELEASE_NOTES.zh-CN.md)

# Release Notes

v0.9.3 only expands `evaluate` usage guidance: pass multi-statement Python directly instead of wrapping it in `exec/eval/compile`; when dynamic execution is genuinely needed, keep all `frame_eval(...)` calls outside the dynamic string. Execution, parameters, and return protocols are unchanged.

v0.9.2 unifies active-stack access for `evaluate`: its public parameters are now only `expression` and optional `timeout_seconds`; outer `file/line/function` frame selection was removed. Access another frame in the active stack with `frame_eval(expression, file, line, function)` inside the expression. DAP `frameId` remains internal. Interactive `evaluate` and god_runner actions use identical `frame_eval` syntax, so code can be copied directly. Frames that have returned and no longer appear in `get_stack()` cannot be accessed. This supersedes the outer caller-selector syntax recorded in older entries below.

v0.9.1 fixes retained breakpoint-frame references in frequently executed god_runner actions. Both regular and `frame_eval` branches now clear their merged namespaces immediately after success or failure, releasing strong references to frame locals such as `loss`, models, gradients, and CUDA tensors instead of waiting for cyclic GC.

v0.9.0 simplifies `evaluate` guidance and fixes recipe launch selection. The Agent obtains the actual root `launch_name` through `attach_debugpy(full=true)` fields `selected_session/root_session_id/launch_name`, then reads only the matching configuration in `.vscode/launch.json` instead of guessing among configurations.

v0.8.9 only clarifies `evaluate` return semantics: one expression returns its value, while comma-separated values remain a single tuple expression. Multi-statement code executes but may return empty or `(omitted)`; query the result separately when needed. Execution, parameters, and return protocols are unchanged.

v0.8.8 fixes ordinary recipe actions that do not use `frame_eval`. Like the cross-frame branch, they now merge breakpoint `globals/locals` into one execution namespace, allowing top-level variables created by the action to be read by comprehensions, generators, `lambda`, and nested functions.

v0.8.7 adds guidance for frequently executed CUDA actions: move data to CPU early and delete large objects in `finally`, call `gc.collect()`, and use `torch.cuda.empty_cache()` when needed. Unless the user requests sampling, do not use `hitCondition` as a substitute for cleanup. The runtime never clears the training process's CUDA cache on its own.

v0.8.6 fixes the god_runner process lifecycle. Every `runInTerminal` launch now enters a recipe-specific process group; normal completion, action/child-session failure, and Ctrl+C clean up the entire torchrun/launcher/rank-worker group rather than only the direct child. The process-group identity is retained until recipe teardown even if the launcher exits first. SIGTERM is sent first, followed by SIGKILL only if processes remain after five seconds. Cleanup is limited to terminal process groups created by this recipe and neither stops the shared daemon nor affects later MCP use.

It also fixes debugpy 1.8.21 behavior where reinstalling temporary breakpoints during consecutive `run_to_line` calls immediately retriggered a user breakpoint on the current line. The first resume now temporarily suppresses the current line, then restores all user breakpoints after stopping.

v0.8.5 keeps `evaluate(expression=...)` and its `frame_eval(expression, file, line, function)` helper, but changes the implementation to exact static-selector capture. `file/line/function` must be literals copied from `get_stack()`, while `expression` may be computed dynamically. Each target frame is captured once, and recipe actions without `frame_eval` skip cross-frame capture entirely. Interactive evaluate and god_runner recipes use identical syntax. No MCP tools or public `evaluate` parameters were added. Real objects remain in the same debuggee process without MCP/RPC serialization. Only frames still visible in the active stack are accessible; returned frames and other threads/processes/ranks/sessions are not.

v0.8.2 fixes god_runner appearing stuck after only “session started” in `integratedTerminal`, torchrun/DDP, and subprocess scenarios. It preserves the launch `console`, executes debugpy `runInTerminal` requests in the terminal that started the recipe with inherited stdout/stderr, correctly establishes `startDebugging` child sessions, fails immediately with a reason when child attach fails, and prints session/process/child status every 15 seconds while waiting. Breakpoint verification, process creation, child sessions, hits, action completion, and exit all have live logs.

v0.8.1 clearly separates Agent and user responsibilities. Only after an explicit user request may the Agent turn the user's instructions and existing debugging evidence into a god_runner recipe and explain how to run it. The Agent may not start or run long train/infer jobs for the user. The user always controls actual recipe execution, batch size, and launch timing.

v0.8.0 adds **god_runner headless mode** to the same debugger program. MCP mode remains interactively controlled by the Agent; god_runner reads an ordinary Python recipe, reuses `.vscode/launch.json`, launches, sets breakpoints, executes actions on hits, and continues until exit. It is not a second debugger and does not traverse the MCP call chain; both entry points share the existing daemon, DAP proxy, and debugpy.

v0.7.1 extends the same **exact caller-frame backtracking** to `inspect_structure`:

- Regular `inspect_structure(expression=...)` still uses the latest stopped session's top frame in the current workspace;
- When a nested object exists only in a caller or caller's caller, call `get_stack()` first and pass the target line's `file + line + function` together to `inspect_structure`;
- The selector matches `evaluate`: all three fields are required together and must match exactly one frame in the complete active stack. Zero or multiple matches fail; nothing is guessed;
- `session_id / frame_id` are removed from the Agent-facing `inspect_structure` interface; DAP frame IDs remain internal;
- The inspect helper is cached and installed by the target frame's `globals()` identity, so backtracking across modules no longer fails because the helper was installed only in the original top-frame module;
- `evaluate`, DAP routing, timeouts, audit logging, and execution-control behavior are unchanged.

Typical caller inspection:

```text
get_stack()
#0 pixel.py:318 process_pix
#1 frame.py:201 process_frame

inspect_structure(
    expression="metainfo",
    file="frame.py",
    line=201,
    function="process_frame",
    profile="summary",
)
```

v0.7.0 adds **exact caller-frame backtracking** to `evaluate` while continuing to hide DAP frame IDs:

- Regular calls remain `evaluate(expression)` and automatically use the latest stopped session's top frame in the current workspace;
- When a value exists only in a caller or caller's caller, call `get_stack()` first and pass the target line's `file + line + function` to `evaluate`;
- All three selector fields are required together and must match exactly one frame in the complete active stack. Zero or multiple matches fail without guessing;
- Default `get_stack()` output replaces abstract DAP frame IDs with `#0/#1/...` stack depth plus `workspace-relative/path.py:line function`, making selectors easy to copy;
- `#N` is only stack depth and cannot be passed to `evaluate`; DAP `frame_id` remains internal;
- `timeout_seconds`, v0.6.7 late-response dropping, and DAP audit logging are unchanged.

Typical usage:

```text
get_stack()
#0 pipeline.py:318 process_pix
#1 pipeline.py:201 process_frame
#2 pipeline.py:87 process_sequence

evaluate(expression="frame_idx", file="pipeline.py", line=201, function="process_frame")
evaluate(expression="sequence_id", file="pipeline.py", line=87, function="process_sequence")
```

v0.6.8 previously narrowed the **public Agent interface for `evaluate`**:

- The Agent sees only `evaluate(expression)` and optional `timeout_seconds`;
- `full / frame_id / session_id / context` were removed to prevent requests for unrelated DAP/session metadata;
- debugger-mcp resolves session, thread, top frame, and context from the current MCP workspace and latest stopped state;
- The default timeout remains **60 seconds**; normally pass only `expression`, adding `timeout_seconds` only for work expected to exceed 60 seconds;
- Results still contain only the complete expression result, with no session/frame/type/presentationHint envelope;
- v0.6.7 late-response dropping, the 60-second default, extendable RPC timeouts, and DAP audit logging are retained.

v0.6.7 focuses on **DAP stability and traceability for long-running `evaluate` calls**:

- Fixes a DAP proxy protocol bug: after an MCP-injected DAP request times out, any late debugpy response is identified and dropped and is **never incorrectly forwarded to VSCode/Cursor**;
- Adapter responses that match neither an outstanding MCP request nor an outstanding IDE request are dropped and audited;
- Raises the default `evaluate` DAP timeout from 15 to **60 seconds**;
- `evaluate(..., timeout_seconds=N)` can extend each wait without a fixed debugger-mcp ceiling; the control RPC timeout extends with it so the outer RPC does not expire first;
- Adds an enabled-by-default **DAP audit log** covering key MCP/IDE requests, `continued/stopped/terminated/exited` events, session/adapter lifecycle, request timeouts, and dropped late/unmatched responses;
- Writes debugpy adapter stdout/stderr to the daemon log instead of `/dev/null`;
- Defaults to `/tmp/debugger-mcp-daemon.log` (or `DEBUGGER_MCP_DAEMON_LOG`); set `DEBUGGER_MCP_DAP_AUDIT=0` to disable audit lines.

v0.6.6 retains the Skill behavior and `evaluate` positioning without tightening the Skill again.

The main v0.6.6 feature is a **rewrite of runtime-aware Skill guidance for `evaluate`**:

- Defines `evaluate` as a **live Python REPL / runtime execution channel** inside the paused debuggee, not a read-only variable query;
- Removes `read-only` / `focused expression` restrictions from the Skill and MCP tool description;
- Encourages direct multi-statement computation, function calls, temporary mutation, validation experiments, artifact creation, and visualization through `evaluate`;
- When users request visualization or saving runtime values, prefers one in-process `evaluate` transform/render/save operation over the default `dump -> plot.py -> external Python` workflow;
- When no artifact path is specified, permits `<workspace>/.debugger_mcp_artifacts/` or `/tmp/debugger_mcp/`; absolute `/tmp` paths must be reported immediately;
- Retains one-time stepping authorization and explicit authorization for cross-file `run_to_line`; these execution-control permissions are independent of Python execution through `evaluate`;
- Retains v0.6.5 complete-core output and the rule that errors/tracebacks are never summarized or truncated.

The main v0.6.5 feature **corrects v0.6.4 over-compaction and establishes a “complete core data, compact representation” output contract**:

- `get_stack()` returns the **complete call stack** from `start_frame` by default; every frame is represented compactly. Only an explicit `levels` returns a bounded slice;
- `get_locals()` retains **every local name** by default and summarizes only each value's type/shape/dtype/device/length/scalar representation, removing the 120-variable cap;
- `evaluate()` returns the **complete expression result text** by default without the previous 30K-character truncation;
- `step_over / step_into / step_out` remain compact single lines but restore the actual `stop_reason` and `thread_id`, preventing breakpoint/exception stops from being mistaken for normal steps;
- `full=true` now truly means full: `get_stack/get_locals/evaluate/step_*` no longer remove DAP fields or recursively truncate long strings;
- `inspect_structure` remains an explicitly bounded-summary tool governed by profile/depth/width/nodes/chars/runtime limits, but the second hidden truncation layer outside the helper is removed;
- `get_threads()`, `attach_debugpy()`, and `run_to_line()` retain v0.6.4 compact formats because they do not omit their core data sets;
- Normal MCP tool results send only one `content.text` and do not duplicate it into `structuredContent`.

**Every error, exception, and traceback is an absolute exception: return it completely and verbatim, without summarization, character truncation, JSON wrapping, or a duplicate structuredContent copy.**

The output principle is fixed from v0.6.5 onward:

> Default results must completely cover each tool's core data set. Only representation may be compacted, and unrelated diagnostic metadata may be removed.

v0.6.3 MCP Roots / Cursor workspace support remains fully preserved:

- When the MCP client declares the `roots` capability, debugger-mcp obtains the frontend workspace through standard `roots/list`;
- One root automatically becomes `client_workspace`, with `notifications/roots/list_changed` support;
- Explicit `DEBUGGER_MCP_WORKSPACE` has highest priority; multiple roots are never resolved by guessing the first;
- Hosts without Roots continue to use cwd/explicit-env compatibility;
- Cursor can therefore use only global `~/.cursor/mcp.json` configuration.

Note: MCP Roots identifies only the **Agent/frontend workspace**. Debug-session ownership is still established explicitly by `debugServer: 4711` and `env.DEBUGGER_MCP_WORKSPACE=${workspaceFolder}` in `launch.json`.


v0.6.2 changes the error-output channel:

- Normal tool results remain compact;
- Python/debuggee/runtime exceptions return directly as MCP `text` errors with complete multiline tracebacks;
- Errors are no longer wrapped in `{"error": "...\n..."}` JSON;
- Errors bypass normal output truncation and are not duplicated into `structuredContent`.

This prevents the critical final traceback line, such as `RuntimeError` or `ValueError`, from being lost to debugger-mcp's JSON wrapping or truncation. The host UI may still visually collapse content, but the MCP response remains complete.

v0.6.1 retains simplified shared-daemon version switching:

- Reuse a running daemon when its version matches the installed version;
- Otherwise stop the old daemon and start the installed version;
- Do not check for active VSCode debug connections or require users to stop debugging or free ports first.

Existing debug connections disconnect when the daemon is replaced; press F5 again.

The main v0.6.0 features remain:

- **Lazy debug-location change notifications**: each Claude MCP frontend remembers its own last-seen `(session_id, stop_id)`. After the user or another Agent moves execution to a new stop, the next debugger tool call from that Claude receives one compact location-change notice;
- **No duplicate notice after the Agent's own execution control**: when the current Agent successfully uses `step_over`, `step_into`, `step_out`, or a completed `run_to_line`, the final stop is immediately marked seen;
- **Current-frame semantics and stale-frame protection**: `frame_id` selects a level in the current paused stack and never a historical pause;

1. **Explicit, deterministic debug-session workspace routing**: only launch/attach `env.DEBUGGER_MCP_WORKSPACE` establishes session ownership; DAP `cwd` and source paths are diagnostic only and never guessed;
2. **Workspace-isolated default targets**: each MCP frontend accesses only the latest stopped session in its own workspace by default;
3. **Concurrent multi-project debugging**: every VSCode connects to `4711`, while each independent root run receives its own debugpy adapter and torchrun/subprocess sessions return to that root adapter;
4. **Compact default output**: `get_locals()` returns compact lines and `evaluate()` returns only the complete expression result without a DAP/session envelope;
5. **`evaluate()` follows the latest stop and accesses the active stack**: it resolves the latest stopped session's top frame in the current workspace; other active frames are accessed with `frame_eval(...)` inside the expression, without Agent-facing DAP `frame_id/session_id/context`;
6. **Shorter, recoverable stale-frame errors**: when an expired `frame_id` is supplied explicitly, the response says to omit it or call `get_stack()` again;
7. **Official runtime-aware Skill**: the Agent treats the debugger as a strong source of runtime evidence when useful without invoking it when static analysis is already sufficient.
