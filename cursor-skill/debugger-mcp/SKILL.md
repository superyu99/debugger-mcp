---
name: debugger-mcp
description: Actively use live VSCode/Cursor debugpy runtime evidence when it can answer Python debugging questions more reliably than static inference. Inspect workspace-scoped stacks and variables, use evaluate as a live Python runtime console including its frame_eval helper for combining real objects from multiple currently active stack frames, inspect nested caller-frame objects, and control short interactive debugging with permission. When the user explicitly asks for a god_runner recipe, write it and provide run guidance, but never execute a long-running train or inference job.
---

# Debugger MCP: live runtime evidence and execution

Use this skill when Python is launched from VSCode or Cursor with:

```json
"debugServer": 4711,
"env": {
  "DEBUGGER_MCP_WORKSPACE": "${workspaceFolder}"
}
```

## Core principle

Debugger MCP exposes what the program is actually doing, not merely what the source code suggests it should do.

Actively consider debugger tools when runtime evidence can materially improve correctness, including questions about:

- actual execution paths and branch decisions;
- current variable values;
- Tensor shape, dtype, device, content, or intermediate features;
- labels, coordinates, flags, masks, inputs, outputs, and transformed data;
- unexpected differences between intended and observed behavior;
- verification of a suspected cause or a code change;
- runtime experiments, temporary computations, artifact generation, and visualization.

Do not wait for the user to recite a special trigger phrase. When the task depends on runtime facts and a paused debugger session is available, use the debugger proactively.

Do not use debugger tools merely because they are available. Static tools are usually cheaper and sufficient for locating definitions, browsing file structure, reading general implementation logic, or explaining an algorithm independent of runtime state.

Choose the information source that can most directly and reliably answer the current question.

## Tool selection

Do not mechanically call every debugger tool or force a fixed sequence. Pick the primitive that directly advances the investigation:

- `get_stack()` — establish the current location/call path and provide exact `file:line function` selectors for `frame_eval` or caller-frame `inspect_structure`;
- `get_locals()` — inventory all local variable names with compact metadata;
- `evaluate()` — execute Python directly inside the current paused runtime; this is the primary power tool for computation, experiments, transformations, validation, artifact generation, visualization, controlled mutation, and—with `frame_eval` inside its expression—real-object composition across active stack frames;
- `inspect_structure()` — obtain a bounded structural map of one large/nested object in the current frame or an exact caller frame;
- `step_over / step_into / step_out / run_to_line` — move execution when runtime progression is required;
- `full=true` — obtain raw DAP/session diagnostics only when those raw fields themselves matter.

Runtime observations have priority over assumptions inferred from source code. Clearly distinguish observed facts from subsequent inference.

## `evaluate`: the runtime power tool

`evaluate` is the most powerful debugger-mcp primitive. Treat it as a Python REPL/runtime execution channel operating inside the currently paused debuggee, with direct access to the real frame, live Python objects, imports, globals/locals, model state, Tensors, CUDA objects, filesystem, and libraries available to that process.

Do **not** artificially restrict `evaluate` to read-only expressions, scalar queries, or an "advanced print" role. Use as much Python as the debugging task requires.

Valid uses include, but are not limited to:

- reading runtime values and metadata;
- arbitrary derived calculations and statistics;
- direct multi-statement Python without wrapping it in `exec`/`eval`/`compile`;
- importing helper libraries;
- calling existing functions and methods;
- constructing temporary helper objects;
- slicing, reshaping, filtering, sorting, grouping, transforming, or comparing runtime data;
- converting Tensor / NumPy / container data for inspection;
- checking numerical properties, assertions, invariants, and hypotheses;
- testing a proposed fix or behavior directly against live runtime state;
- temporary assignment or controlled runtime mutation when it helps debugging;
- saving runtime values or derived data to disk;
- creating NumPy, pickle, JSON, text, image, point-cloud, or other debugging artifacts;
- directly rendering plots, masks, boxes, feature maps, distributions, trajectories, images, or other visualizations;
- launching helper subprocesses or using other Python facilities when that is genuinely the most direct runtime solution.

For simple questions, a one-line expression is naturally efficient:

```text
loss.item()
feature.shape
feature.dtype
feature.min().item()
torch.isnan(feature).sum().item()
```

Use one expression when a returned value matters; comma-separated values form one tuple expression. Imports, assignments, loops, and definitions are statement suites: they execute, but an empty or `(omitted)` result is normal. Verify their side effects or query the produced value separately.

Pass multi-statement Python directly. Use `exec`/`eval`/`compile` only when the task truly requires dynamic code, and keep every `frame_eval(...)` call outside the dynamic string.

Keep `evaluate` calls simple: normally call `evaluate(expression=...)` and nothing else. debugger-mcp automatically selects the current workspace's latest stopped session and top frame, and uses the appropriate DAP context internally. The complete expression result text is returned without unrelated DAP/session metadata or debugger-mcp truncation.

## `frame_eval` inside `evaluate`: access objects from the current active call stack

This is part of the existing `evaluate`, not a separate MCP tool. `evaluate` always runs in the current `#0` namespace. Access top-frame values normally; for any other frame still present in the current active call stack—even when reading only one value—first call `get_stack()`, then call `frame_eval(expression, file, line, function)` inside the expression:

```text
get_stack()
#0 losses.py:318 calculate_loss
#1 model.py:201 forward

evaluate(expression='frame_eval("raw_images", "model.py", 201, "forward")')
```

The same expression may combine current-frame values with objects from one or more other active frames:

```text
get_stack()
#0 losses.py:318 calculate_loss
#1 model.py:201 forward

evaluate(expression='''
raw = frame_eval("raw_images", "model.py", 201, "forward")
prediction = logits
render_and_save(raw, prediction, "/workspace/debug/comparison.png")
''')
```

`frame_eval` returns the actual object reference inside the same debuggee process. Tensor/custom-object identity is preserved and the objects can be compared, transformed, rendered, mutated, or saved together without crossing MCP/RPC. Only the final displayed result or artifact path reaches the Agent.

Keep `file`, `line`, and `function` as literals copied from `get_stack()`; never compute them dynamically. The `expression` argument may be a variable. Put all required `frame_eval` calls in one outer `evaluate`: debugger-mcp captures each unique target frame once and reuses it within that program.

This is access to the current active call stack, not historical execution. Every target must still appear in the current `get_stack()` output. Exactly one current frame must match; zero or duplicate matches fail rather than guess. Never claim that it can recover a function that already returned, and never use it across another thread, process, rank, session, or earlier pause.

`evaluate` exposes only `expression` and optional `timeout_seconds`. It never exposes DAP `frame_id` or outer `file/line/function` selection. DAP frame IDs remain internal. The `frame_eval(...)` spelling is identical in interactive `evaluate` and god_runner actions, so validated code can be copied directly into a recipe.

The default timeout is 60 seconds. Only pass `timeout_seconds` when the operation is expected to take longer, for example a heavy visualization or animation render.

### Runtime mutation and experiments

Runtime mutation is a legitimate debugging technique, not a forbidden capability. If modifying a value, monkey-patching an object, changing a threshold, calling a local function, or performing another controlled experiment is the shortest way to test a hypothesis, `evaluate` may do so.

Be intentional about effects that persist after execution resumes. When an experiment is intended to be temporary, preserving/restoring the original value is often useful. Do not avoid a useful experiment merely because it changes runtime state.

Execution-control permissions for `step_*` and cross-file `run_to_line` are separate rules; they do not limit Python execution performed through `evaluate`.

### Runtime visualization and artifact creation

When the user asks to visualize, plot, render, export, inspect as an image, or save a runtime value, prefer the shortest direct path:

```text
live runtime object
→ evaluate
→ transform/render/save inside the paused debuggee
→ report the artifact path
```

Do **not** default to this indirect workflow:

```text
evaluate dumps .npy/.pkl
→ create plot.py
→ launch a separate Python process
→ render the image
```

A paused debugpy process can execute Python through `evaluate`; being paused, launched by torchrun, or participating in DDP does not inherently require visualization to happen in another process. For matplotlib, a non-interactive backend such as `Agg` is appropriate when needed; save the artifact instead of waiting for a GUI window.

Use an external plotting/helper process only when there is a concrete reason direct runtime rendering is unsuitable or actually fails, such as a missing library/backend in the debuggee environment. In that fallback, export only what is needed and explain the concrete reason for leaving the debuggee process.

Artifact path rules:

1. If the user provides a path, use it.
2. Otherwise, freely choose a sensible writable path under the current workspace, for example `<workspace>/.debugger_mcp_artifacts/<descriptive-name>.png`.
3. For temporary artifacts, or when the workspace should not be written, use `/tmp/debugger_mcp/<descriptive-unique-name>`.
4. Create parent directories as needed.
5. Always tell the user the final saved path. If `/tmp` is used, explicitly provide the absolute path immediately so the user can find the artifact.

A request to visualize/save/export a runtime value already implies permission to create the artifact required to complete that request; do not ask for a redundant file-write confirmation.

## Generate god_runner recipes for the user

Write a god_runner recipe only when the user explicitly asks for one. Base it on the user's prompt and available debugger evidence. Do not proactively create a recipe merely because an operation looks reusable, and do not merely explain the format or tell the user to copy previous `evaluate()` code manually.

Use this workflow:

1. Identify the active launch precisely: call `attach_debugpy(full=true)`, follow `selected_session.root_session_id` to the matching root session when needed, and copy that root session's `launch_name`. Inspect only that named configuration in `.vscode/launch.json`; never choose among configurations. If no root `launch_name` is available, ask the user. Ground the remaining recipe in relevant source lines, the current stack, and runtime code that actually succeeded.
2. When practical, validate the final action at the intended pause through debugger-mcp before writing it. Copy `frame_eval` selectors from the current `get_stack()` output; never guess them.
3. Create a normal Python file with editable inputs near the top, normally `WORKSPACE`, `LAUNCH`, `ARTIFACTS_DIR`, and `BREAKPOINTS`, followed by one `run(...)` call.
4. Reuse the DAP `setBreakpoints` shape: each group contains `source` and `breakpoints`; each action breakpoint adds only `action` and optional `name`. Preserve useful standard fields such as `condition` or `hitCondition`.
5. Make each action self-contained: include required imports, create output directories, use runtime identifiers in filenames when repeated hits could overwrite each other, and keep the verified local-variable names unchanged.
6. Use god_runner's injected `frame_eval(expression, file, line, function)` inside the action for caller-frame values. This is exactly the same spelling and call shape as the helper inside an interactive MCP `evaluate` expression, so validated code can be copied directly into a recipe action without rewriting it. Keep `file`, `line`, and `function` as literals; the expression may be a variable and the call may appear inside conditions, loops, functions, or comprehensions. Put all cross-frame reads in one action so each unique target frame is captured once. Recipe actions expose no legacy `evaluate(...)` alias: always use this one spelling.
7. god_runner clears its merged action namespace after every hit. For repeatedly hit actions that touch CUDA tensors, also move results to CPU inside `frame_eval` as early as practical and use `finally` to `del` action-created large temporaries, run `gc.collect()`, and call `torch.cuda.empty_cache()`. Do not substitute `hitCondition` unless the user requests sampling.
8. Report the created recipe path, selected launch configuration, breakpoint locations, artifact destination, and exact user-run instructions. If any of these choices are materially ambiguous, ask the user instead of inventing them.
9. In the run guidance, tell the user that program stdout/stderr appears in the terminal where they launch the recipe, `[god] waiting` is the 15-second liveness heartbeat, TCP 4711 is the shared DAP proxy rather than the stdio MCP channel, and stopping the recipe cleans its torchrun/rank process group while leaving the shared daemon/MCP available. While the recipe is running, do not use MCP execution-control or evaluation tools against the same workspace/debug task.

The Agent's responsibility ends after writing/checking the recipe and providing run guidance. The Agent must never start, execute, wait for, or monitor a long-running train or inference job, even if the user asks it to run the generated recipe. This is a role boundary, not an action that becomes allowed after confirmation. The user always launches the recipe and controls task timing, scale, datasets, sequences, and checkpoints.

Short validation is limited to static/syntax checks and small action experiments in an already-existing paused debugger session. Never start train/infer merely to validate a recipe, and never invoke the recipe's `run()` during handoff. Do not put MCP calls, Agent dependencies, DAP frame IDs, or a second debugger inside the recipe.

Minimal generated shape:

```python
from debugger_mcp.god_runner import run

WORKSPACE = "/path/to/project"
LAUNCH = "Python: Infer"
ARTIFACTS_DIR = "god_run_artifacts"
BREAKPOINTS = [{
    "source": {"path": "${workspaceFolder}/model.py"},
    "breakpoints": [{
        "line": 1778,
        "name": "dump-runtime-value",
        "action": r'''# Python already validated at this pause''',
    }],
}]

run(workspace=WORKSPACE, launch=LAUNCH, breakpoints=BREAKPOINTS, artifacts_dir=ARTIFACTS_DIR)
```

## Error output

Debugger errors and debuggee Python/runtime exceptions are high-value evidence. The MCP returns them as complete multiline text with `isError=true`, without JSON wrapping, summary, truncation, or duplicate structured content. This rule also applies to debugger-mcp helper failures and unexpected server-side tool exceptions.

**Never summarize or truncate an error before presenting it to the model.** Read the full traceback, especially the final exception line. Do not request the same failing expression again merely to recover an error message that is already present.

## Complete-core, compact-format output contract

Default tool results are text-first and compact **without silently dropping the tool's core data set**. Compactness means fewer redundant fields, not fewer stack frames, locals, or result characters.

- `get_threads()` returns every thread as `[thread_id] name`.
- `get_stack()` returns every frame by default as `#stack_depth workspace-relative/path.py:line function`, where `#0` is the current top frame. The `#N` value is stack depth, not a DAP frame ID. Do not pass `levels` during ordinary stack inspection; pass it only when an explicitly bounded slice is desired.
- `get_locals()` returns every local-variable name, using metadata-first summaries for each value rather than container/Tensor dumps.
- `evaluate(expression=...)` returns the complete expression result text without debugger-mcp truncation and uses the latest top frame. Access every other frame in the current active call stack through `frame_eval(...)` inside the expression. Pass `timeout_seconds` only for work expected to exceed the 60-second default.
- `step_over / step_into / step_out` return the new source location plus the actual stop reason and thread, so breakpoint/exception stops are not mistaken for ordinary stepping.
- `run_to_line` returns the outcome and necessary actual/target locations.
- `inspect_structure` is intentionally different: it is an explicitly bounded structural-summary tool and may report its declared depth/width/node/character/runtime limits.
- Ordinary MCP tool results are sent once as `content.text`; debugger-mcp does not duplicate them into `structuredContent`.

Use `full=true` only when detailed diagnostics are genuinely necessary for tools that expose it. `evaluate` and `inspect_structure` intentionally do not expose session/frame selection by DAP ID. `evaluate` has no full/session/frame/context or outer frame-selector mode: its public contract is `evaluate(expression[, timeout_seconds])`. For `get_stack`, `get_locals`, and the three step tools, full mode means raw DAP data without debugger-mcp field removal or string truncation. It is also available for `attach_debugpy` and `get_threads`.

**Any error, exception, or traceback is always complete original text: never summarize it and never truncate it.**

## Workspace-scoped session model

All MCP frontends (for example Claude Code and Cursor) may share one daemon, but default session selection is isolated by workspace.

The MCP frontend workspace is resolved in this order:

1. explicit `DEBUGGER_MCP_WORKSPACE` configured for the MCP frontend;
2. exactly one filesystem root returned by the MCP client's standard `roots/list` capability (used by Cursor since v0.6.3);
3. the MCP stdio process cwd as a compatibility fallback.

If the client exposes multiple MCP roots and no explicit frontend workspace is configured, do not silently choose the first root. Automatic session routing is ambiguous; use compact `attach_debugpy()` diagnosis and ask for a single-root workspace or an explicit frontend workspace if needed.

- Each VSCode/Cursor root launch must explicitly provide `env.DEBUGGER_MCP_WORKSPACE=${workspaceFolder}`.
- This launch/attach marker is the only source of **debug-session** workspace ownership. MCP Roots identify the Agent/frontend workspace; they do not assign DAP session ownership.
- DAP `cwd`, program paths, arguments, and source paths are diagnostic evidence only; never use them to auto-bind or guess a workspace.
- The proxy consumes the marker before forwarding launch/attach to debugpy, so the debuggee does not receive it.
- Omitting `session_id` selects the latest genuinely stopped process inside the current MCP frontend workspace.
- Within that workspace, root and subprocess sessions are treated equally; the latest stopped event wins.
- Never silently select a stopped session from another workspace.
- Pass `session_id` explicitly only when another worker, process, or project is intentionally requested.

A root process and its torchrun/debugpy subprocesses inherit the same workspace ownership.

Normal debugging should not require listing or comparing sessions. Use `attach_debugpy()` for compact connection/routing diagnosis. Use `attach_debugpy(full=true)` for detailed session diagnosis or to identify the active root launch configuration from `selected_session`, `root_session_id`, and `launch_name`; never infer it from multiple `launch.json` entries. Compact mode may expose `client_workspace_source` and `mcp_root_count`; full mode may additionally expose the resolved MCP roots and a roots protocol error.

## Current frame and current active call stack

For normal operations, do not specify a frame selector:

```text
evaluate(expression="loss.item()")
get_locals()
inspect_structure(expression="data", profile="summary")
```

The daemon resolves the current workspace's latest stopped session and latest top frame on every call. This also applies after the user manually steps or runs to another breakpoint in VSCode/Cursor.

When a needed value/object lives in another frame still present in the current call stack, first call `get_stack()` on the current pause:

```text
#0 worker/pixel.py:318 process_pix
#1 worker/frame.py:201 process_frame
#2 worker/sequence.py:87 process_sequence
```

For `evaluate`, put the exact selector inside `frame_eval`:

```text
evaluate(expression='frame_eval("frame_idx", "worker/frame.py", 201, "process_frame")')
```

`inspect_structure` keeps its own outer caller selector:

```text
inspect_structure(
    expression="metainfo",
    file="worker/frame.py",
    line=201,
    function="process_frame",
    profile="summary",
)
```

Rules for both forms:

- Copy the values from the **current** `get_stack()` output. The file is the workspace-relative path shown there when possible.
- Matching is exact on file + line + function (apart from path-separator normalization). Function-name-only or fuzzy matching is intentionally not supported.
- Exactly one current frame must match. Zero matches means the stack probably moved or the selector was copied incorrectly; call `get_stack()` again. Multiple exact matches (for example recursion at the same source line) are reported as ambiguous; debugger-mcp does not guess.
- The visible `#0/#1/...` prefixes are call-stack depth only. They are not DAP frame IDs and are not accepted by `evaluate` or `inspect_structure`.
- The selector describes a frame in the current pause, not historical execution. Any continue, step, new breakpoint, restart, or process switch can change the stack; refresh it before relying on an old selector.

Other tools may still expose `frame_id` for advanced/raw DAP workflows. Do not transfer that concept into `evaluate` or `inspect_structure`.

## Lazy debug-context change notice

Each MCP frontend independently remembers the last paused debug context it observed. If the user or another Agent moves execution to a different pause, the next debugger tool result may begin with one compact notice such as:

```text
[Debug location changed] models/head.py:1213 forward -> data/transform.py:1037 __call__
```

A new pause at the same source line is still meaningful because runtime values may have changed:

```text
[Debug pause changed] new pause at train.py:88 step (same source location as the previous pause)
```

When this notice appears:

- treat previously observed locals, branch conclusions, and caller-frame selectors as potentially stale;
- use the result that follows the notice as evidence from the current pause;
- refresh `get_stack` when the new call path or a caller-frame `evaluate` / `inspect_structure` matters;
- do not ask the user to repeat that they moved the debugger.

The notice is consumed once per MCP frontend. The same stop will not repeat it. Successful execution-control calls made by this Agent mark their final stop as already seen, so the next inspection does not produce a redundant notice.

## Information granularity

### `get_locals`

Use `get_locals()` when you need an inventory of the current frame. It returns compact text such as:

```text
bboxes_corners, ndarray, shape=[3,8,3], float64
loss, Tensor, shape=[], float32, cuda:0, requires_grad
data, dict, len=17
epoch, int, 3
__name__
__doc__
```

It does not dump container contents or Tensor elements. Double-underscore bookkeeping variables are listed by name only. The local-name set itself is complete: do not expect a hidden first-N cutoff.

Use `get_locals(full=true)` only when the original complete debugpy previews and DAP metadata for all locals are genuinely required. The output may be extremely large.

### `inspect_structure`

Use `inspect_structure(expression=..., profile="summary")` when a bounded structural map of a nested object is useful. Increase to `normal` or `deep` when needed, and choose a narrower subtree when that better answers the question.

By default it inspects the current top frame. If the object exists only in another active frame, first call `get_stack()` and pass the exact current `file + line + function` triplet to `inspect_structure`. This selector is strict and all-or-none. Use it directly instead of falling back to hand-written `evaluate` structure probing merely because the object lives in another frame.

The tool is intentionally bounded by depth, child count, node count, characters, value length, and runtime. Those limits are part of this tool's declared summary semantics. When exact arbitrary computation or transformation is needed instead, use `evaluate`.

## Execution control

Debugger MCP can control execution with:

- `step_over`;
- `step_into`;
- `step_out`;
- `run_to_line`.

Execution control changes the program's current stop. Use it when moving execution materially helps the investigation, such as understanding control flow, reaching a relevant point, or verifying behavior after a change.

### Stepping permission

Before the first `step_over`, `step_into`, or `step_out` in a root debug run, ask the user once for permission to control execution with those three tools.

After permission is granted, use them as needed within the same `root_session_id`. Permission ends when the root debug run changes, the user revokes it, or synchronization becomes unreliable.

Each call performs exactly one visible step.

### `run_to_line`

Use `run_to_line` when repeated one-line stepping would waste context and a meaningful target can be selected.

For a same-file target, briefly reason about reachability and put that reasoning in `reason`. Check for unconditional returns, raises, branch boundaries, loop exits, and leaving the current function.

For a cross-file target, state the target and purpose, obtain explicit user approval, then call with `cross_file_authorized=true`.

Do not assume the requested line was reached. Inspect the actual result and current runtime location.

## Fallback behavior

Use source search or static analysis without debugger calls when:

- debugger-mcp is unavailable;
- no matching workspace session is paused;
- the question is purely static;
- runtime evidence genuinely cannot help answer the question.

Do not use another project's stopped session as a fallback.

If a particular `evaluate` strategy fails, use the complete error as evidence and try another runtime strategy when appropriate; do not prematurely abandon the live runtime merely because one expression/library/backend failed.

## Response discipline

Do not paste full tool payloads into the answer unless the user asks for them. Summarize the runtime evidence needed for the conclusion.

Mention workspace, process/rank, file, line, function, and key values only when they matter. Avoid repeating large stacks, locals dumps, or structure output unless the user explicitly requests them.

When an artifact is generated, always report its saved path. For artifacts under `/tmp`, always report the absolute path immediately.
