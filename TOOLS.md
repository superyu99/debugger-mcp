**English** | [简体中文](TOOLS.zh-CN.md)

# Tool Guide

debugger-mcp provides 10 MCP tools plus god_runner automation built on the same debugging infrastructure. This guide first explains the tools and runtime model, then covers inspection, execution control, and automation.

## Contents

- [Tool Overview](#tool-overview)
- [Core Runtime Model](#core-runtime-model)
- [Inspection Tools](#inspection-tools)
- [Execution-Control Tools](#execution-control-tools)
- [god_runner Automation](#god_runner-automation)

## Tool Overview

| Category | Tool | Purpose |
|---|---|---|
| Connection and location | `attach_debugpy` | Show connection state, workspace routing, and the selected debug session |
| Connection and location | `get_threads` | List threads in the selected debug process |
| Connection and location | `get_stack` | Return the active call stack and exact locations for cross-frame access |
| Runtime inspection | `get_locals` | Show every local variable in the paused frame |
| Runtime inspection | `evaluate` | Execute Python in the latest stopped top frame |
| Runtime inspection | `inspect_structure` | Return a bounded structural view of a runtime object |
| Execution control | `step_over` | Step over the current statement |
| Execution control | `step_into` | Step into called code |
| Execution control | `step_out` | Run until the current function returns |
| Execution control | `run_to_line` | Run to a source line using a temporary breakpoint |

`frame_eval(expression, file, line, function)` is a cross-frame helper built into `evaluate` and god_runner actions, not a separate MCP tool.

## Core Runtime Model

### Workspace Routing

#### Which workspace owns an MCP frontend?

Frontend workspace resolution priority:

```text
1. Explicit `DEBUGGER_MCP_WORKSPACE` (MCP server env / constructor argument)
2. MCP client Roots: `roots/list` returns exactly one `file://` root
3. MCP stdio process cwd (legacy-host compatibility)
```

When Roots is supported, Cursor supplies the current project root through MCP. Even if Cursor starts the stdio process from `/root`, `client_workspace` is updated to the actual project path.

`attach_debugpy()` additionally returns:

```text
client_workspace_source   # env / explicit / mcp_roots / cwd / mcp_roots_ambiguous
mcp_root_count
```

`attach_debugpy(full=true)` also returns `mcp_roots` and `mcp_roots_error` for protocol diagnostics.

Multiple Roots only mean that all listed directories are in scope; they do not identify the active project. debugger-mcp never guesses the first root. Ambiguous automatic routing reports an error; use a single-root workspace or set `DEBUGGER_MCP_WORKSPACE` explicitly.

#### Which workspace owns a debug session?

Every VSCode debug configuration must include:

```json
"debugServer": 4711,
"env": {
  "DEBUGGER_MCP_WORKSPACE": "${workspaceFolder}"
}
```

Each MCP frontend resolves `client_workspace` using the priority above. Every VSCode/Cursor root debug session must explicitly declare ownership with `env.DEBUGGER_MCP_WORKSPACE`. When a tool omits `session_id`:

1. Consider only sessions whose explicit workspace marker exactly matches the current MCP frontend workspace.
2. Select the process that most recently entered `stopped` in that workspace.
3. Give no fixed priority to root or subprocess sessions; whichever stopped last wins.
4. If the workspace has no matching session, fail instead of silently reading another project.
5. An unmarked session keeps `workspace_root=null` even when its `cwd` lies inside the current project.
6. An explicit `session_id` may intentionally access another process or project.

For example, when projects A and B are both paused:

```text
Agent A reads A's latest stop by default
Agent B reads B's latest stop by default
```

Stepping in A does not change Agent B's default target, and stepping in B cannot redirect Agent A to B.

#### Why explicit configuration is required

`cwd`, `program`, `args`, and top-frame source paths may involve subdirectories, symlinks, container mounts, multi-root workspaces, or shared run directories. debugger-mcp never uses them to guess session ownership.

DAP `cwd` appears only as `launch_cwd` in `attach_debugpy` and error diagnostics. It helps identify bad configuration but never selects a session. An explicit “no matching session” error is safer than silently connecting project A's Agent to project B.

The proxy removes `DEBUGGER_MCP_WORKSPACE` before forwarding `launch/attach` to debugpy, so it never enters the training process. Other `env` entries remain unchanged. torchrun/debugpy child sessions inherit the root's explicit marker internally.

When MCP Roots is unavailable and the MCP cwd is not the project root, set the workspace explicitly in the MCP configuration:

```json
"env": {
  "DEBUGGER_MCP_WORKSPACE": "/absolute/path/to/project"
}
```

### Automatic Session Selection

Normal tool calls neither return every debug session nor require the Agent to choose one. Without `session_id`, the daemon selects the latest stopped session in the current frontend workspace.

By default, `attach_debugpy()` returns compact counts and the automatically selected session. `attach_debugpy(full=true)` returns complete session metadata; recipe generation can follow `selected_session.root_session_id` to the root and read its `launch_name` without guessing.

When no session matches, normal tools return counts and configuration guidance rather than dumping every active session. `cwd` remains diagnostic only.

### Debug-Context Change Notifications

MCP neither pushes nor polls VSCode. Each stdio frontend has a stable internal `client_id`, and the shared daemon records the last-seen debug context separately for each frontend.

The first debugger call establishes a baseline. Later, when the selected `(session_id, stop_id)` differs, the next tool result begins with one compact notice:

```text
[Debug location changed] models/head.py:1213 forward -> data/transform.py:1037 __call__
```

Stopping again on the same source line is still reported as a new pause because values may have changed:

```text
[Debug pause changed] new pause at train.py:88 step (same source location as the previous pause)
```

A switch of default process or project includes workspace names:

```text
[Debug context changed] workspace_a/model.py:10 forward -> workspace_b/loss.py:8 compute
```

Rules:

- Notices are consumed independently per Claude MCP frontend.
- Each context change is shown once; later calls at the same stop return compact output normally.
- A successful step or completed run-to-line by the current Agent marks the resulting stop as seen.
- If the user moved execution before an Agent step, the step result still reports the unseen external change first.
- A notice adds one text line only, without a full session list or Agent-side session selection.

### Frame Cache and Stale-Frame Protection

The frame cache is isolated by `session_id + stop_id`. Within one pause, `get_locals`, `evaluate`, and `inspect_structure` can reuse stack/frame information. Any step, continue, new breakpoint hit, termination, or restart invalidates it.

`evaluate` and `inspect_structure` do not accept DAP `frame_id`. `evaluate` uses the latest top frame and accesses other active frames through `frame_eval(...)`; `inspect_structure` retains its outer `file + line + function` selector. Both match strictly against the complete current stack, so stale selectors fail safely.

### Default Output Contract

“Compact” applies only to representation and never silently removes a tool's core data. Workspace-local paths become relative; session/adapter routing metadata appears only in diagnostic mode.

```text
get_threads()
[1] MainThread
[2] pydevd.CommandThread

get_stack()
[5] projects/vfm/head.py:1297 forward
[6] projects/vfm/model.py:842 _forward_single
[7] tools/train.py:428 train_step
...                  # continues for every frame in the actual stack

step_over()
projects/vfm/head.py:1301 forward | reason=step thread=1

run_to_line(...)
target hit: projects/vfm/head.py:1350 forward
```

Default semantics:

- `get_threads()`: every thread, with compact fields;
- `get_stack()`: every frame, with compact fields;
- `get_locals()`: every local name, with compact value representations;
- `evaluate()`: complete result text;
- `step_*()`: location, actual stop reason, and thread;
- `inspect_structure()`: an explicitly bounded summary using its declared profiles and limits.

Only an explicit `get_stack(levels=N)` may return a bounded stack slice, with the remaining frame count reported.

`get_threads(full=true)`, `get_stack(full=true)`, `step_*(full=true)`, `get_locals(full=true)`, and `attach_debugpy(full=true)` are diagnostic modes. `evaluate` has no full mode and always returns the complete expression result.

An MCP tool result sends one `content.text` and does not duplicate it into `structuredContent`.

Every error and traceback is returned completely, **without summarization or truncation**.

### DAP Audit Log

Key debugger-chain state is written to the daemon log by default:

```text
/tmp/debugger-mcp-daemon.log
```

Typical records:

```text
[DAP AUDIT] request source=ide session_id=6 ... command=next
[DAP AUDIT] request source=mcp session_id=6 ... command=evaluate timeout=60.000s
[DAP AUDIT] event session_id=6 event=continued ...
[DAP AUDIT] event session_id=6 event=stopped stop_id=9 reason=step thread_id=1 ...
[DAP AUDIT] timeout source=mcp session_id=6 ... command=evaluate timeout=60.000s
[DAP AUDIT] response route=drop_late_mcp session_id=6 ... command=evaluate ...
[DAP AUDIT] adapter event=stopped root_session_id=5 pid=... returncode=...
```

This distinguishes whether an unexpected location change or adapter disconnect came from an IDE `next/continue`, MCP execution control, a debugpy termination event, or a late response after a long request timed out.

debugpy adapter stdout/stderr is appended to the same file. Configuration:

```bash
export DEBUGGER_MCP_DAEMON_LOG=/path/to/debugger-mcp-daemon.log
export DEBUGGER_MCP_DAP_AUDIT=1   # enabled by default; set to 0 to disable audit lines
```

## Inspection Tools

### get_locals: Compact by Default, Full on Demand

Default call:

```text
get_locals()
```

Returns compact text instead of large JSON envelopes. **Every local name is retained; no default count limit removes variables:**

```text
bboxes_corners, ndarray, shape=[3,8,3], float64
loss, Tensor, shape=[], float32, cuda:0, requires_grad
data, dict, len=17
epoch, int, 3
model, VFMModel
__name__
__doc__
```

Rules:

- Tensor: `name, Tensor, shape, dtype, device`, plus `requires_grad` when true;
- ndarray：`name, ndarray, shape, dtype`；
- dict/list/tuple/set/string/bytes: show `len`;
- int/float/bool/None: show the scalar value;
- ordinary objects: show name and type only;
- dunder variables such as `__name__`, `__doc__`, and `__builtins__`: show the name only;
- do not recursively expand containers, read Tensor elements, or generate large reprs;
- keep shape on one line, such as `shape=[3,8,3]`;
- **retain the complete local-name set without a default count limit.**

For the complete legacy DAP locals response:

```text
get_locals(full=true)
```

Full mode returns the raw debugpy `variables` response and scope information without field removal or string truncation; output may be large.

### evaluate: Top Frame by Default, frame_eval for Other Active Frames

Most calls need only:

```text
evaluate(expression="valid_area")
```

Agent-visible parameters:

```text
expression
timeout_seconds  # optional; default 60s, pass only for longer work
```

`file / line / function / session_id / frame_id / context / full` are not exposed by `evaluate`. The daemon automatically selects:

```text
current MCP frontend workspace
→ latest stopped session in that workspace
→ currently stopped thread
→ latest top frame
→ DAP context="repl"
```

To access an object in another active frame, for example:

```text
process_sequence()   # sequence_id
  → process_frame()  # frame_idx
    → process_pix()  # current stop
```

First obtain the complete current stack:

```text
get_stack()
#0 worker/pixel.py:318 process_pix
#1 worker/frame.py:201 process_frame
#2 worker/sequence.py:87 process_sequence
```

Use `frame_eval` inside the `evaluate` expression:

```text
evaluate(expression='frame_eval("frame_idx", "worker/frame.py", 201, "process_frame")')
evaluate(expression='frame_eval("sequence_id", "worker/sequence.py", 87, "process_sequence")')
```

Matching is deliberately strict:

- `file + line + function` are the three location arguments and must be literals; a function name or line alone is never guessed;
- copy `file` exactly from the workspace-relative path shown by `get_stack()` (path separators are normalized);
- match again against the **complete current stack** on every pause;
- zero matches: fail and request a fresh `get_stack()`;
- multiple exact matches, such as recursive calls stopped on the same line: report ambiguity;
- `#0/#1/...` is stack depth, not a DAP frame ID or `evaluate` parameter.

This accesses frames in the current active stack, not execution history. After step/continue, a new breakpoint, or a process switch, an old `file:line function` may be stale; call `get_stack()` again.

A single expression returns its value; comma-separated values remain one tuple expression. Imports, assignments, loops, function definitions, and other statements execute normally but may return empty or `(omitted)`. Query a produced variable separately when a value is needed.

Pass multi-statement Python directly to `evaluate`; do not wrap it in `exec/eval/compile`. Use dynamic execution only when required and keep every `frame_eval(...)` outside the dynamic string.

The result contains only the **complete expression result** from debugpy, without debugger-mcp truncation or a session/frame/type/presentationHint envelope.

The default wait is **60 seconds**. Do not pass a timeout for normal calls:

```text
evaluate(expression="data['img_feats_4'].shape")
```

Extend it only for work expected to exceed 60 seconds, such as heavy GIF rendering:

```text
evaluate(expression="...", timeout_seconds=180)
```

`timeout_seconds` controls only this DAP evaluate wait. debugger-mcp extends the frontend-to-daemon RPC timeout accordingly, and late MCP responses continue to be dropped.

#### frame_eval: Real Objects from the Active Call Stack

This is still the original `evaluate`, not a new tool. `evaluate` executes in the current `#0` namespace; use `frame_eval(...)` inside the expression for any other active frame.

Read the latest stack first:

```text
get_stack()
#0 losses.py:318 calculate_loss
#1 model.py:201 forward
```

Then call `evaluate`: use `#0` locals directly and the synchronous `frame_eval(...)` helper for objects in another active frame:

```text
evaluate(expression='''
raw_images = frame_eval(
    "raw_images",
    "model.py",
    201,
    "forward",
)
prediction = logits

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt

fig = render_comparison(raw_images[0], prediction[0])
fig.savefig("/workspace/debug/raw-and-prediction.png")
plt.close(fig)
''')
```

`raw_images` is the original in-process object reference, not a DAP string, JSON value, or `variablesReference`. Object identity, tensors, custom classes, and container relationships are preserved; only final display text or saved artifact paths return to the Agent.

Constraints:

- First read the complete stack for the current pause and copy an exact `file + line + function` matching one active frame;
- `file`, `line`, and `function` must be literals, not variables, function results, `*args`, or `**kwargs`; `expression` may be dynamic;
- the outer `evaluate` always runs in the `#0` namespace; use top-frame variables directly;
- `evaluate` no longer accepts outer `file/line/function`; use `frame_eval` only for other active frames;
- a returned function absent from `get_stack()` is history and cannot be accessed;
- objects cannot cross threads, processes, torchrun ranks, debug sessions, or earlier pauses;
- call `get_stack()` again when a selector is stale or ambiguous; debugger-mcp never guesses.

One outer `evaluate` or recipe action resolves all selectors in advance and captures each unique referenced frame once. For batch collection, group cross-frame reads into one action and use `condition`/`hitCondition` sampling when appropriate to avoid plotting and GPU-to-CPU synchronization on every batch.

A single expression returns its normal display result; a multi-statement suite usually returns `None`. Visualization/export should save directly inside the debuggee and report an explicit path.

### inspect_structure

`inspect_structure` returns a bounded structural view of one expression; it does not replace `get_locals` or `evaluate`.

```text
inspect_structure(expression="batch", profile="summary")
inspect_structure(expression="outputs", profile="normal")
inspect_structure(expression="features[0]", profile="deep")
```

When the object exists only in a caller frame, call `get_stack()` and pass the target line's exact `file + line + function`:

```text
inspect_structure(
    expression="metainfo",
    file="worker/frame.py",
    line=201,
    function="process_frame",
    profile="summary",
)
```

The three selector fields are all-or-none and match only the complete current stack. Zero matches mean the stack changed or the selector is wrong; multiple exact matches are ambiguous. `inspect_structure` never exposes `session_id` or DAP `frame_id` to the Agent.

Default profiles:

| profile | Max depth | Children per container | Total nodes | Total characters |
|---|---:|---:|---:|---:|
| summary | 2 | 8 | 60 | 6000 |
| normal | 4 | 12 | 160 | 14000 |
| deep | 6 | 20 | 400 | 28000 |

Hard limits:

```text
max_depth <= 8
max_children <= 30
max_nodes <= 500
max_chars <= 30000
max_value_chars <= 200
inspection runtime <= 3s
```

Tensor/ndarray values show metadata only; containers are sampled within fixed depth, width, node, character, and runtime limits. Output begins directly with the first structural line instead of a redundant profile header.

Depth and character limits apply only to normal structure results. Helper failures return a complete traceback through the MCP error channel.

## Execution-Control Tools

### step_over / step_into / step_out

The three stepping tools respectively step over, step into called code, and run until the current function returns. They return the new location, actual stop reason, and thread. Use `full=true` only for raw step/session diagnostics.

```text
step_over()
step_into()
step_out()
```

### run_to_line

```text
run_to_line(
  file="/workspace/project/model.py",
  line=238,
  reason="The target is later in the same forward function with no unconditional return in between."
)
```

Internal flow:

```text
save user breakpoints
→ merge the internal temporary breakpoint
→ continue
→ wait for actual stopped / terminated / timeout
→ report the actual stop
→ remove the temporary breakpoint and restore user breakpoints
```

The Agent may use a same-file target after explaining reachability. A cross-file target requires explicit user permission and:

```text
cross_file_authorized=true
```

## god_runner Automation

### Workflow

1. Keep the project's existing `.vscode/launch.json`. god_runner selects a configuration by `name`; `${workspaceFolder}` and `${env:NAME}` are expanded. The configured `console` is preserved. When debugpy requests `runInTerminal`, the program connects to the terminal that started the recipe, so stdout/stderr, progress bars, and torchrun worker output remain visible.
2. The user explicitly asks the Agent to generate a recipe, for example: “Turn the visualization we just validated into a god_runner recipe and save it as `god_run.py`.” The Agent obtains the actual root `launch_name` with `attach_debugpy(full=true)`, reads only the matching launch configuration, and converts validated `evaluate()`, plotting, or dump code into breakpoint actions.
3. The Agent reports the recipe path, launch name, breakpoint, output path, and run command, then stops without launching it.
4. After review, the user runs the Python file. god_runner performs breakpoint actions and continues automatically without MCP, F5, or manual continue.

### Runtime Status and Ports

Logs should include `[god] session ... process`, `breakpoint verified`, `startDebugging received`, `hit`, `action completed`, and `exited`. If no DAP message arrives for 15 seconds, `[god] waiting` confirms it is still waiting. If no hit occurs, use the logs to verify child-worker sessions and breakpoint placement.

Port responsibilities:

| Channel | Default | Purpose |
|---|---:|---|
| MCP frontend | stdio | Agent-to-debugger-mcp communication; no TCP listener |
| DAP proxy | `127.0.0.1:4711` | DAP entry point for VSCode, god_runner, and debugpy sessions |
| daemon control | `127.0.0.1:4712` | Internal debugger-mcp control RPC |

Port `4711` never conflicts with MCP stdio, and god_runner does not occupy another `4711`. god_runner and MCP do share daemon/session routing; while a recipe runs, do not let an Agent simultaneously continue, step, or evaluate the same debug task in the same workspace.

### Lifecycle and Responsibility Boundaries

On normal completion, action/child-session failure, or Ctrl+C, only the root/child DAP sessions and terminal process group created by this recipe are cleaned up. Rank workers are still removed if the torchrun launcher exited first, preventing leaked GPU processes. The shared daemon and MCP frontend remain running. The only exception is the first connection after an upgrade, when version switching may replace an old in-memory daemon and require existing debug connections to restart once.

Responsibilities:

- Agent: write or modify a recipe only after an explicit user request; inspect source and `launch.json`; validate short actions in an existing paused runtime; provide clear run instructions.
- Agent must not call recipe `run()`, start or operate long train/infer jobs, wait for them, monitor them, or treat “one more check” as authorization.
- User: decide whether and when to run, how much data/sequences/checkpoints to process, and personally start the recipe.

### Recommended Workflow

```text
user + Agent + debugger-mcp explore interactively
→ Agent validates evaluate/visualization/dump code in the real paused runtime
→ user asks to turn the validated operation into a god_runner recipe
→ Agent reads the actual root launch, matching configuration, and source, then writes and checks god_run.py
→ Agent provides run instructions and stops
→ user runs the recipe and collects results in batch
```

### Recipe Format

Users may also write recipes manually. Agent-generated files must follow the same structure.

Minimal recipe:

```python
from debugger_mcp.god_runner import run

WORKSPACE = "/path/to/project"
LAUNCH = "Python: Infer StreamPETR"
BREAKPOINTS = [
    {
        "source": {"path": "${workspaceFolder}/projects/vfm/model.py"},
        "breakpoints": [
            {
                "line": 1778,
                "action": r'''
frame_number = frame_eval(
    "metainfo[0]",
    "projects/vfm/petr3d_sq.py",
    549,
    "loss",
)

with open(f"data_{frame_number}.pkl", "wb") as stream:
    pickle.dump(sometensor, stream)
''',
            }
        ],
    }
]

run(workspace=WORKSPACE, launch=LAUNCH, breakpoints=BREAKPOINTS)
```

`BREAKPOINTS` retains the DAP `setBreakpoints` structure: each group is still `source + breakpoints`, and breakpoints support debugpy `line / condition / hitCondition`. god_runner adds:

- `action`: required Python code executed in the real breakpoint frame on each hit;
- `name`: optional readable name in logs and the manifest.

An action directly accesses breakpoint locals, globals, `self`, and real tensors/objects. Its `frame_eval(expression, file, line, function)` helper is identical to interactive MCP `evaluate`, so validated code can be copied unchanged. `file/line/function` must be literals while `expression` may be dynamic. Calls may appear inside `if`, `for`, functions, or comprehensions. Each unique target frame is captured once per action; actions without `frame_eval` execute directly. Returned values are real in-process objects, not DAP display strings.

Each hit appends probe, hit, session/process, duration, status, and complete error data to `god_run_artifacts/god_runner_manifest.jsonl`. An action failure stops the run by default; pass `continue_on_error=True` to record errors and continue batch collection.

See `examples/god_run.py` for a complete runnable template.
