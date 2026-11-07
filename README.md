# debugger-mcp v0.9.3

面向 Claude Code、Cursor、VSCode/Cursor Debug 和 debugpy 的轻量运行时调试 MCP。

v0.9.3 仅补充 `evaluate` 用法：多语句 Python 直接传入，不要为此套 `exec/eval/compile`；确需动态执行代码时，把所有 `frame_eval(...)` 调用写在动态字符串外。执行代码、参数和返回协议均未改变。

v0.9.2 统一 `evaluate` 的活动调用栈访问接口：公开参数只保留 `expression` 和可选的 `timeout_seconds`，删除外层 `file/line/function` 选帧；访问当前活动调用栈中的其他帧时，一律在表达式内部使用 `frame_eval(expression, file, line, function)`。DAP `frameId` 继续仅作为内部实现。交互式 `evaluate` 与 god_runner action 使用相同的 `frame_eval` 写法，代码可直接复制。已经返回、不再出现在当前 `get_stack()` 中的帧无法访问。此项变更取代下方旧版本记录中的外层 caller selector 用法。

v0.9.1 修复 god_runner 高频 action 的断点帧引用滞留：普通与 `frame_eval` 分支现在都会在 action 成功或异常后立即清空合并命名空间，解除其中对 `loss`、模型、梯度和 CUDA Tensor 等 frame locals 的强引用，不再等待 Python 循环 GC。

v0.9.0 精简 `evaluate` 指导，并修正 recipe 的 launch 选择：Agent 通过 `attach_debugpy(full=true)` 的 `selected_session/root_session_id/launch_name` 精确取得本次实际使用的 root launch，再读取 `.vscode/launch.json` 中同名配置；不再从多个配置中猜测。

v0.8.9 仅澄清 `evaluate` 的返回语义：单一表达式返回值，逗号分隔仍是一个元组表达式；多语句代码会执行，但可能返回空或 `(omitted)`，需要结果时再单独查询。执行代码、参数和返回协议均未改变。

v0.8.8 修复不含 `frame_eval` 的普通 recipe action：现在与跨帧分支一样，把断点 `globals/locals` 合并为同一个执行命名空间，因此 action 顶层新建的变量可被列表推导式、生成器、`lambda` 和嵌套函数正常读取。

v0.8.7 补充高频 CUDA action 指导：尽早转到 CPU，并在 `finally` 中删除大对象、执行 `gc.collect()`，必要时调用 `torch.cuda.empty_cache()`；除非用户要求抽样，否则不以 `hitCondition` 代替清理。底层不会擅自清空训练进程的 CUDA 缓存。

v0.8.6 修复 god_runner 的进程生命周期：每个 `runInTerminal` 启动现在进入 recipe 独有的进程组；正常结束、action/child-session 失败和 Ctrl+C 都会清理整组 torchrun/launcher/rank worker，而不是只终止直接子进程。即使 launcher 已先退出，进程组身份仍会保留到 recipe 收尾；先发送 SIGTERM，5 秒后仍存活才发送 SIGKILL。清理范围只包含本 recipe 创建的终端进程组，不停止共享 daemon，也不影响后续 MCP 使用。

同时修复 debugpy 1.8.21 下连续 `run_to_line` 时，临时断点重装会让当前行的用户断点立即重触发的问题：首次恢复执行时暂时抑制当前行，停下后恢复完整用户断点。

v0.8.5 保留原有 `evaluate(expression=...)` 和其中的 `frame_eval(expression, file, line, function)`，但将内部实现改为按静态 selector 精确捕获：`file/line/function` 必须是从 `get_stack()` 复制的字面量，`expression` 可以动态计算；相同目标帧只捕获一次，没有 `frame_eval` 的 recipe action 完全跳过跨帧捕获。交互式 evaluate 与 god_runner recipe 使用完全相同的写法，代码可以原样复制。没有新增 MCP 工具，也没有改变 `evaluate` 的公开参数。真实对象始终留在同一个 debuggee 进程，不经过 MCP/RPC 序列化；只能访问当前栈仍可见的帧，不能读取已经返回的历史帧，也不能跨 thread/process/rank/session。

v0.8.2 修复 god_runner 在 `integratedTerminal`、torchrun/DDP 和 subprocess 场景中“只显示 session started 后看似卡住”的问题：保留 launch 的 `console`，在当前用户终端执行 debugpy 的 `runInTerminal` 请求并继承 stdout/stderr；正确建立 `startDebugging` 子 session；子 session attach 失败会立即终止并显示原因；等待期间每 15 秒输出一次 session/process/child 状态。断点验证、进程建立、子 session、命中、action 完成和退出均有实时日志。

v0.8.1 明确划分 Agent 与用户的职责：Agent 只在用户明确要求时，根据用户提示和已经获得的调试证据写好 god_runner recipe，并提供运行指导；Agent 无权启动或代替用户执行长时间 train/infer。recipe 的实际运行、批量任务规模和启动时机始终由用户控制。

v0.8.0 在同一个 debugger 程序中增加了 **god_runner headless 模式**。MCP 模式仍由 Agent 交互控制；god_runner 模式读取普通 Python recipe，复用现有 `.vscode/launch.json`，自动完成 launch、设置断点、命中后执行 action、continue，直到程序结束。它不是第二个调试器，也不经过 MCP 调用链；两种入口共用现有 daemon、DAP proxy 和 debugpy。

## god_runner：用户怎么使用

1. 保留项目已经存在的 `.vscode/launch.json`。god_runner 按 `name` 读取其中一个 configuration；`${workspaceFolder}` 和 `${env:NAME}` 会展开。v0.8.2 起保留原有 `console`。当 debugpy 请求 `runInTerminal` 时，程序直接连接到用户启动 recipe 的当前终端，因此原程序 stdout/stderr、进度条和 torchrun worker 输出可见。
2. 用户明确要求 Agent 生成 recipe，例如：“把刚才验证过的可视化做成 god_runner recipe，保存为 `god_run.py`”。Agent 先用 `attach_debugpy(full=true)` 精确取得实际 root `launch_name`，只读取 `.vscode/launch.json` 中的同名配置，再把已验证的 `evaluate()`、绘图或 dump 代码整理成断点 action；用户不需要手工复制代码。
3. Agent 报告 recipe 路径、launch 名称、断点位置、输出路径和运行方法，到此结束，不启动 recipe。
4. 用户检查后自行运行该 Python 文件。god_runner 随后自动完成断点 action 和 continue，无需启动 MCP、无需按 F5、无需手动 continue。

运行期间应能看到 `[god] session ... process`、`breakpoint verified`、`startDebugging received`、`hit`、`action completed` 和 `exited`。若 15 秒内没有新的 DAP 消息，会输出 `[god] waiting` 心跳；这表示仍在等待，而不是静默卡死。若一直没有 `hit`，应根据日志先判断 worker 子 session 是否建立、断点是否在实际执行进程中命中。

端口职责：

| 通道 | 默认值 | 用途 |
|---|---:|---|
| MCP frontend | stdio | Agent 与 debugger-mcp 通信，不监听 TCP 端口 |
| DAP proxy | `127.0.0.1:4711` | VSCode、god_runner 和 debugpy session 的 DAP 入口 |
| daemon control | `127.0.0.1:4712` | debugger-mcp 内部控制 RPC |

所以 `4711` 不会与 MCP stdio 发生端口冲突，god_runner 也不另占第二个 `4711`。但 god_runner 与 MCP 共用同一个 daemon/session 路由；用户运行 recipe 时，不要再让 Agent 同时 continue、step 或 evaluate 同一 workspace 的相同调试任务，避免两个控制方竞争执行状态。

recipe 正常结束、action/child-session 失败或用户按 Ctrl+C 后，只清理该 recipe 建立的 root/child DAP session 和独立终端进程组。torchrun launcher 已先退出时，仍会继续清理同组 rank worker，避免残留进程占用 GPU；共享 daemon 和 MCP frontend 不会关闭。结束日志会明确显示 `shared debugger daemon remains available for MCP`；此后 MCP 和 VSCode 可以继续使用 `4711/4712`。唯一的例外发生在升级后的第一次连接：若内存中的 daemon 版本与已安装包不同，统一的版本切换机制会替换旧 daemon，并要求原有调试连接重新启动一次。

职责边界：

- Agent：只在用户明确提示后编写或修改 recipe；可以检查源码、`launch.json`，并在已经存在的暂停现场做短小 action 验证；提供清楚的运行指导。
- Agent 禁止事项：不得调用 recipe 的 `run()`，不得启动、代跑、等待或监控长时间 train/infer，也不得把“再确认一次”当成执行授权。这些操作不属于 Agent 职责。
- 用户：决定是否运行、何时运行、运行多少数据/序列/checkpoint，并亲自启动 recipe。

推荐闭环：

```text
用户 + Agent + debugger-mcp 交互探索
→ Agent 在真实暂停现场验证 evaluate/可视化/dump 代码
→ 用户要求“把刚才的操作写成 god_runner recipe”
→ Agent 查询实际 root launch，再读取同名配置和源码，生成并检查 god_run.py
→ Agent 提供运行指导并停止
→ 用户自行运行 recipe，批量采集结果
```

用户也可以手写 recipe；下面的结构同时是 Agent 生成文件时必须遵循的格式。

最小 recipe：

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

`BREAKPOINTS` 沿用 DAP `setBreakpoints` 的现有结构：每组仍是 `source + breakpoints`，断点仍支持 debugpy 已有的 `line / condition / hitCondition`。god_runner 只增加：

- `action`：命中后在真实 breakpoint frame 中执行的 Python 代码，必填；
- `name`：日志和 manifest 中的可读名称，可选。

action 能直接读取当前断点局部变量、globals、`self` 和真实 Tensor/object。内置 `frame_eval(expression, file, line, function)` 与交互式 MCP `evaluate` 表达式中的同名 helper 完全一致，所以验证代码可以原样复制进 recipe。`file/line/function` 必须是字面量，`expression` 可以是变量；调用可出现在 `if`、`for`、函数或推导式中。每个唯一目标帧在一次 action 中只捕获一次；不使用 `frame_eval` 的 action 直接执行，不遍历捕获整条栈。返回值是同一进程里的真实对象，不是 DAP 展示字符串。

每次命中会追加写入 `god_run_artifacts/god_runner_manifest.jsonl`，记录 probe、hit、session/process、耗时、状态和完整错误。action 默认失败即终止本次运行；批量采集希望记录错误后继续时，显式传 `continue_on_error=True`。

完整可运行模板见 `examples/god_run.py`。

设计目标是“人机共驾”：用户在 VSCode/Cursor 中启动调试、设置初始断点并观察执行位置；Agent 读取真实调用栈和变量，并直接在暂停现场执行 Python、做运行时实验/可视化/验证，在获得相应权限后控制单步或运行到目标行。

v0.7.1 将同一套 **精确 caller-frame 回溯**扩展到 `inspect_structure`：

- 普通 `inspect_structure(expression=...)` 仍自动使用当前 workspace 最新 stopped session 的 top frame；
- 当嵌套对象只存在于 caller/caller's caller 时，先 `get_stack()`，再把目标栈行的 `file + line + function` 三项一起传给 `inspect_structure`；
- selector 与 `evaluate` 完全一致：三项 all-or-none，在当前 pause 完整调用栈中严格匹配恰好一个 frame，0 个或多个匹配都报错，不猜；
- 删除 `inspect_structure` 对 Agent 暴露的 `session_id / frame_id`，DAP frame ID 继续只作为内部实现；
- inspect helper 改为按目标 frame 的 `globals()` 身份缓存/安装，跨 module 回溯时不会因为 helper 只装在原 top-frame module 而失败；
- `evaluate`、DAP routing、timeout、audit log 和 execution-control 行为均不改变。

典型 caller inspect：

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

v0.7.0 为 `evaluate` 增加 **精确 caller-frame 回溯**，同时继续隐藏 DAP frame ID：

- 普通调用仍是 `evaluate(expression)`，自动使用当前 workspace 最新 stopped session 的 top frame；
- 当目标变量只存在于 caller/caller's caller 时，先 `get_stack()`，再把目标栈行的 `file + line + function` 三项一起传给 `evaluate`；
- 三项 selector 必须 all-or-none，并且在**当前 pause 的完整调用栈**里严格匹配恰好一个 frame；0 个匹配或多个匹配都直接报错，绝不模糊猜测；
- 默认 `get_stack()` 不再显示抽象 DAP frame ID，而是显示 `#0/#1/...` 栈深度以及 `workspace-relative/path.py:line function`，便于 Agent 直接复制回溯 selector；
- `#N` 只表示当前调用栈深度，不是可传给 `evaluate` 的 ID；DAP `frame_id` 继续作为内部实现细节；
- `timeout_seconds`、v0.6.7 late-response 丢弃和 DAP audit log 行为保持不变。

典型用法：

```text
get_stack()
#0 pipeline.py:318 process_pix
#1 pipeline.py:201 process_frame
#2 pipeline.py:87 process_sequence

evaluate(expression="frame_idx", file="pipeline.py", line=201, function="process_frame")
evaluate(expression="sequence_id", file="pipeline.py", line=87, function="process_sequence")
```

v0.6.8 曾收敛 **`evaluate` 的 Agent 公共接口**：

- Agent 侧只暴露 `evaluate(expression)` 和可选的 `timeout_seconds`；
- 删除 `evaluate` 的 `full / frame_id / session_id / context` 参数，避免 Agent 为“更完整”而主动请求无关 DAP/session 元数据；
- session/thread/top-frame/context 全部由 debugger-mcp 根据当前 MCP workspace 和最新 stopped 状态自动解析；
- 默认 timeout 仍为 **60 秒**；通常只传 `expression`，只有预计超过 60 秒的重操作才传 `timeout_seconds`；
- 返回仍然只包含完整 expression result，不附带 session/frame/type/presentationHint 等 DAP envelope；
- v0.6.7 的 late-response 丢弃、60 秒默认 timeout、可延长 RPC timeout 和 DAP audit log 全部保留。

v0.6.7 只聚焦 **长时间 `evaluate` 的 DAP 稳定性与可追溯性**：

- 修复一个确定的 DAP proxy 协议 bug：MCP 注入的 DAP request 超时后，如果 debugpy 稍后返回迟到 response，该 response 现在会被识别并丢弃，**绝不会再错误转发给 VSCode/Cursor**；
- 所有无法匹配到 MCP outstanding request 或 IDE outstanding request 的 adapter response 都会直接丢弃，并写入审计日志；
- `evaluate` 默认 DAP timeout 从 15 秒提升为 **60 秒**；
- `evaluate(..., timeout_seconds=N)` 可由 Agent 按次延长等待时间，没有 debugger-mcp 侧固定上限；control RPC timeout 会同步自动延长，避免外层 RPC 比 evaluate 更早超时；
- 新增默认开启的 **DAP audit log**：记录 MCP/IDE 关键 DAP request、`continued/stopped/terminated/exited` 状态事件、session/adapter 生命周期、request timeout，以及 late/unmatched response 的丢弃；
- debugpy adapter 自身 stdout/stderr 不再丢到 `/dev/null`，而是写入同一个 daemon 日志；
- 默认日志路径：`/tmp/debugger-mcp-daemon.log`（或 `DEBUGGER_MCP_DAEMON_LOG` 指定路径）；可用 `DEBUGGER_MCP_DAP_AUDIT=0` 关闭 audit 行。

v0.6.6 的 Skill 行为和 `evaluate` 能力定位保持不变，本版不重新收紧 Skill。

v0.6.6 的主要 feature 是 **重写 runtime-aware Skill 对 `evaluate` 的定位**：

- `evaluate` 明确定义为当前暂停 debuggee 内的 **live Python REPL / runtime execution channel**，而不是只读变量查询器；
- 删除 Skill 和 MCP tool description 中的 `read-only` / `focused expression` 限制；
- 鼓励直接用 `evaluate` 做多语句计算、函数调用、临时 runtime mutation、验证实验、文件落盘和可视化；
- 用户要求可视化/保存 runtime 变量时，优先在当前 debuggee 中一次 `evaluate` 完成 transform/render/save，不再默认走 `dump -> plot.py -> 外部 Python`；
- 未指定产物路径时可自动使用 `<workspace>/.debugger_mcp_artifacts/` 或 `/tmp/debugger_mcp/`；使用 `/tmp` 时必须立即向用户报告绝对路径；
- 保留 stepping 一次授权和 cross-file `run_to_line` 显式授权规则，这些执行控制权限与 `evaluate` 的 Python 执行能力相互独立；
- 0.6.5 的完整-core 输出契约和“任何错误/traceback 永不摘要、永不截断”规则保持不变。

v0.6.5 的主要 feature 是 **纠正 v0.6.4 的过度精简，并建立“核心数据完整、表示形式紧凑”的输出契约**：

- `get_stack()` 默认返回从 `start_frame` 开始的**完整调用栈**，所有 frame 一个不少；每帧仅压成 `[frame_id] 相对路径:行号 函数`。只有调用者显式传 `levels` 时才返回有限切片；
- `get_locals()` 默认保留**全部 local 变量名**，只对每个变量的表示做摘要（type/shape/dtype/device/len/scalar），不再使用 120 个变量上限；
- `evaluate()` 默认返回**完整 expression result 文本**，不再对正常结果做 30K 字符截断；
- `step_over / step_into / step_out` 保持单行紧凑，但补回实际 `stop_reason` 和 `thread_id`，避免把 breakpoint/exception 停止误认为普通 step；
- `full=true` 现在真正表示 full：`get_stack/get_locals/evaluate/step_*` 不再裁 DAP 字段，也不再递归截断长字符串；
- `inspect_structure` 仍然是明确的 bounded-summary 工具，继续受 profile/深度/宽度/nodes/chars/runtime 限制，但删除 helper 之外的第二层隐藏截断；
- `get_threads()`、`attach_debugpy()`、`run_to_line()` 保留 v0.6.4 的紧凑格式，因为它们没有删掉各自的核心数据集合；
- 普通 MCP tool result 仍只发送一份 `content.text`，不复制到 `structuredContent`。

**任何错误、异常和 traceback 是绝对例外：完整原样输出，不做摘要、不做字符截断、不 JSON 包装、不生成 structuredContent 副本。**

输出原则从 v0.6.5 起固定为：

> 默认结果必须完整覆盖该工具的核心数据集合；只允许压缩表示形式和删除与该工具无关的诊断元数据。

v0.6.3 的 MCP Roots / Cursor workspace 支持继续完整保留：

- MCP 客户端声明 `roots` capability 后，debugger-mcp 会通过标准 `roots/list` 获取 frontend workspace；
- 单 root 自动成为 `client_workspace`，支持 `notifications/roots/list_changed`；
- 显式 `DEBUGGER_MCP_WORKSPACE` 优先级最高；多 root 不猜第一个；
- 不支持 Roots 的宿主继续使用 cwd/显式 env 兼容路径；
- Cursor 因此可以只配置全局 `~/.cursor/mcp.json`。

注意：MCP Roots 只识别 **Agent/frontend workspace**。debug session 的 workspace 归属仍由 `launch.json` 中的 `debugServer: 4711` 和 `env.DEBUGGER_MCP_WORKSPACE=${workspaceFolder}` 显式建立。


v0.6.2 调整错误输出通道：

- 正常工具结果继续保持紧凑输出；
- Python/debuggee/runtime 异常直接作为 MCP `text` error 返回，保留原始多行 traceback；
- 错误不再包装成 `{"error": "...\n..."}` JSON；
- 错误不走普通输出截断逻辑，也不重复写入 `structuredContent`。

这样 traceback 的关键末行（例如 `RuntimeError` / `ValueError`）不会因为 debugger-mcp 自己的 JSON 包装或截断而丢失。宿主 UI 是否视觉折叠仍由 Claude Code 决定，但 MCP 返回内容本身保持完整。

v0.6.1 简化的共享 daemon 版本切换仍然保留：

- 已运行 daemon 与当前安装版本相同：直接复用；
- 版本不同：直接结束旧 daemon 并启动当前安装版本；
- 不检查是否存在活跃 VSCode 调试连接，也不要求用户先停止调试或手动清理端口。

旧调试连接会在 daemon 被替换时断开，重新按一次 F5 即可。

v0.6.0 的主要功能仍然保留：

- **惰性调试位置变更通知**：每个 Claude MCP 前端分别记住自己最后看过的 `(session_id, stop_id)`。用户或另一个 Agent 把程序运行到新停点后，该 Claude 下一次调用任意 debugger 工具时，只收到一次紧凑的位置变化提示；
- **Agent 自身执行控制不重复提醒**：当前 Agent 成功执行 `step_over`、`step_into`、`step_out` 或已停住的 `run_to_line` 后，最终停点立即标记为已见，下一次读取不会重复提示；
- **保留当前 frame 语义和 stale-frame 保护**：`frame_id` 只选择当前暂停调用栈中的栈层，不代表历史暂停现场；

1. **显式、确定的 debug-session workspace 路由**：只有 launch/attach 的 `env.DEBUGGER_MCP_WORKSPACE` 能建立调试 session 的项目归属；DAP `cwd` 和源码路径只参与诊断，绝不用于自动猜测；
2. **按 workspace 隔离默认调试对象**：每个 MCP frontend 默认只访问自己 workspace 内最后停止的 session；
3. **支持多个项目同时调试**：所有 VSCode 仍连接 `4711`，但每个独立 root debug run 使用自己的 debugpy adapter，torchrun/子进程继续回到所属 root adapter；
4. **默认输出保持紧凑**：`get_locals()` 返回紧凑行，`evaluate()` 永远只返回完整表达式结果，不暴露 DAP/session envelope；
5. **`evaluate()` 自动跟随最新停点并访问当前活动调用栈**：默认自动解析当前 workspace 最新 stopped session 的 top frame；需要其他活动帧时，在表达式内部使用 `frame_eval(...)`，Agent 不传 DAP `frame_id/session_id/context`；
6. **过期 frame 提示更短、更可恢复**：显式传入已过期 `frame_id` 时，直接提示省略 `frame_id` 或重新调用 `get_stack()`；
7. **正式包含 runtime-aware Skill**：Agent 认识到 debugger 是强大的运行时证据工具，有需要时主动使用，同时避免静态分析已经足够时滥用。


## session 选择完全由 daemon 内部完成

普通工具调用不会返回所有调试 session，也不要求 Agent 逐个判断。未传入 `session_id` 时，daemon 直接选择当前 MCP frontend workspace 内最后停止的 session。

`attach_debugpy()` 默认只返回紧凑状态：总 session 数、当前 workspace 匹配数、停止数以及自动选中的 session。`attach_debugpy(full=true)` 返回完整 session 元数据；生成 recipe 时可由 `selected_session.root_session_id` 定位 root session，并读取其 `launch_name`，避免在多个 launch 配置中猜测。

普通工具找不到匹配 session 时只返回计数和配置提示，不再把所有活动 session 摘要塞入错误消息。`cwd` 仍只用于诊断，不参与 workspace 归属或自动路由。

## v0.6.0：惰性调试上下文变更通知

MCP 不主动推送、也不轮询 VSCode。每个 MCP stdio 前端拥有一个稳定的内部 `client_id`，共享 daemon 为每个前端分别记录最后已见的调试上下文。

第一次调用 debugger 工具只建立基准，不显示变化提示。之后，如果当前自动选中的 `(session_id, stop_id)` 与该前端最后已见上下文不同，下一次任意工具结果会在最前面附加一条短提示：

```text
[Debug location changed] models/head.py:1213 forward -> data/transform.py:1037 __call__
```

如果程序在循环中再次停在同一源码行，也会明确提示这是一次新的暂停，因为变量值可能已经变化：

```text
[Debug pause changed] new pause at train.py:88 step (same source location as the previous pause)
```

如果默认进程或项目发生切换，则使用 workspace 名称区分：

```text
[Debug context changed] workspace_a/model.py:10 forward -> workspace_b/loss.py:8 compute
```

规则：

- 提示按 Claude MCP 前端独立消费；A 看过不代表 B 看过；
- 每次上下文变化只提示一次，后续同一停点的调用恢复原有紧凑输出；
- 当前 Agent 自己执行并成功停住的 step/run-to-line 会直接更新该 Agent 的已见状态，不在下一次调用重复提醒；
- 如果用户在 Agent 调用 step 之前已经移动过程序，step 的结果仍会先提示那个尚未被 Agent 看见的外部变化；
- 通知只增加一行文本，不附带完整 session 列表，也不要求 Agent 自己选择 session。

## 架构

```text
Claude/Cursor A（workspace A）─ MCP stdio A ─┐
                                             ├─ shared daemon ─ control 4712
Claude/Cursor B（workspace B）─ MCP stdio B ─┘

VSCode A root ───────────┐
VSCode A worker ─────────┼─ DAP proxy 4711 ─ adapter group A
                        │
VSCode B root ───────────┼─ DAP proxy 4711 ─ adapter group B
VSCode B worker ─────────┘
```

外部只使用一个固定 DAP 端口 `4711`。daemon 根据 launch/attach 请求的 `env` 中的 `DEBUGGER_MCP_WORKSPACE` 和内部 root-session 标记完成路由。

为什么不是所有项目共用一个 debugpy adapter：debugpy adapter 只允许一个独立的 `launch` root；同一 adapter 后续连接通常必须是该 root 的 subprocess `attach`。因此 v0.6.0 使用：

```text
一个独立 root debug run → 一个 adapter
该 root 的所有 subprocess → 继续共享这个 adapter
```

这样既支持多个项目并行启动，也保留 debugpy 的 `subProcessId` 注册和 torchrun worker 自动发现。

## 工具

工具数量保持 10 个：

```text
attach_debugpy
get_threads
get_stack
get_locals
evaluate
inspect_structure
step_over
step_into
step_out
run_to_line
```

## v0.6.5+ 默认输出契约

“紧凑”只作用于表示方式，不允许静默删除工具本身的核心数据。路径位于当前 workspace 内时自动显示为相对路径；session/adapter 路由元数据只在诊断模式出现。

```text
get_threads()
[1] MainThread
[2] pydevd.CommandThread

get_stack()
[5] projects/vfm/head.py:1297 forward
[6] projects/vfm/model.py:842 _forward_single
[7] tools/train.py:428 train_step
...                  # 实际调用栈有多少层，就继续完整列多少层

step_over()
projects/vfm/head.py:1301 forward | reason=step thread=1

run_to_line(...)
target hit: projects/vfm/head.py:1350 forward
```

默认语义：

- `get_threads()`：所有 thread 都返回，只压缩每个 thread 的字段；
- `get_stack()`：所有 frame 都返回，只压缩每个 frame 的字段；
- `get_locals()`：所有 local 名称都返回，只压缩每个 value 的表示；
- `evaluate()`：完整 result text 返回；
- `step_*()`：位置、真实停止原因、线程都返回；
- `inspect_structure()`：本身就是显式 bounded summary，因此允许按声明的 profile/limits 采样。

只有调用者主动传 `get_stack(levels=N)` 时，stack 才允许返回 N 层切片并明确提示剩余 frame 数。

`get_threads(full=true)`、`get_stack(full=true)`、`step_*(full=true)`、`get_locals(full=true)` 与 `attach_debugpy(full=true)` 用于诊断；`evaluate` 不再暴露 full 模式，其结果始终只返回完整 expression result。

MCP tool result 只发送一次 `content.text`，不再把相同信息复制到 `structuredContent`。

任何错误或 traceback 都完整返回，**不做任何摘要和截断**。

## workspace 路由

### MCP frontend 自己属于哪个 workspace

v0.6.3+ 的 frontend workspace 解析优先级：

```text
1. 显式 DEBUGGER_MCP_WORKSPACE（MCP server env / 构造参数）
2. MCP client Roots：roots/list 恰好返回一个 file:// root
3. MCP stdio 进程 cwd（兼容旧宿主）
```

Cursor 会在支持 Roots 时通过 MCP 协议提供当前项目 root，因此即使 Cursor 从 `/root` 启动 stdio MCP，`client_workspace` 也会被更新为实际项目路径，而不是错误地保持 `/root`。

`attach_debugpy()` 额外返回：

```text
client_workspace_source   # env / explicit / mcp_roots / cwd / mcp_roots_ambiguous
mcp_root_count
```

`attach_debugpy(full=true)` 还会返回 `mcp_roots` 和 `mcp_roots_error` 方便协议诊断。

如果客户端暴露多个 roots，MCP Roots 只表达“这些目录都在当前上下文范围内”，并不表达“哪个是当前活动项目”。debugger-mcp 不会猜第一个 root；此时自动 session 路由会明确提示歧义，可改为单 root workspace，或显式设置 `DEBUGGER_MCP_WORKSPACE`。

### debug session 属于哪个 workspace

每个 VSCode 调试配置必须加入：

```json
"debugServer": 4711,
"env": {
  "DEBUGGER_MCP_WORKSPACE": "${workspaceFolder}"
}
```

每个 MCP stdio frontend 通过上面的优先级确定 `client_workspace`。每个 VSCode/Cursor root 调试会话则必须通过 `env.DEBUGGER_MCP_WORKSPACE` 显式声明归属。工具省略 `session_id` 时：

1. 只考虑显式 workspace 标记与当前 MCP frontend workspace 完全匹配的 session；
2. 在该 workspace 内选择最后一次真正进入 `stopped` 状态的进程；
3. root 和 subprocess 不设固定优先级，谁最后停住就选谁；
4. 当前 workspace 没有匹配 session 时直接报错，不会静默读取其他项目；
5. 没有显式标记的 session 保持 `workspace_root=null`，即使它的 `cwd` 位于当前项目下也不会被自动绑定；
6. 显式传入 `session_id` 时允许有意访问其他进程或项目。

例如 A、B 两个项目都暂停时：

```text
Agent A 默认读取 A 的最后停点
Agent B 默认读取 B 的最后停点
```

在 A 内单步不会改变 Agent B 的默认对象；在 B 内单步也不会让 Agent A 串到 B。

### 为什么必须显式配置

`cwd`、`program`、`args` 和栈顶源码路径可能经过子目录、符号链接、容器挂载、多根工作区或公共运行目录。v0.6.0 不使用这些信息猜测 session 归属。

DAP `cwd` 只会作为 `launch_cwd` 出现在 `attach_debugpy` 和错误诊断中，帮助发现配置写错；它不参与 session 选择。这样宁可明确提示“当前 workspace 没有匹配 session”，也不会把 A 项目的 Claude 静默连到 B 项目。

`DEBUGGER_MCP_WORKSPACE` 会在代理转发 `launch/attach` 给 debugpy 前被移除，因此不会进入训练进程。其他 `env` 项保持不变。torchrun/debugpy 子 session 通过内部请求继承 root 的显式标记。

不支持 MCP Roots、且 MCP 进程 cwd 也不是项目根目录时，仍可在 MCP 配置中显式设置：

```json
"env": {
  "DEBUGGER_MCP_WORKSPACE": "/absolute/path/to/project"
}
```

## get_locals：默认紧凑，按需完整

默认调用：

```text
get_locals()
```

直接返回紧凑文本行，不再输出大量 JSON 字段名和 session/frame 元数据。**当前 frame 有多少个 local，就列出多少个 local；不会因为数量多而只显示前 N 个：**

```text
bboxes_corners, ndarray, shape=[3,8,3], float64
loss, Tensor, shape=[], float32, cuda:0, requires_grad
data, dict, len=17
epoch, int, 3
model, VFMModel
__name__
__doc__
```

规则：

- Tensor：`name, Tensor, shape, dtype, device`，`requires_grad=true` 时额外标记；
- ndarray：`name, ndarray, shape, dtype`；
- dict/list/tuple/set/string/bytes：显示 `len`；
- int/float/bool/None：显示标量值；
- 普通对象：只显示名字和类型；
- `__name__`、`__doc__`、`__builtins__` 等双下划线变量只显示名字；
- 不递归展开容器，不读取 Tensor 元素，不生成大型对象完整 repr；
- shape 始终压成单行，例如 `shape=[3,8,3]`；
- **local 名称集合完整保留，不使用默认数量上限。**

需要旧版完整 DAP locals 时：

```text
get_locals(full=true)
```

完整模式返回完整 raw debugpy `variables` 响应和 scope 信息，不裁字段、不截长字符串，输出可能很大。

## evaluate：默认 top frame，通过 frame_eval 访问其他活动帧

绝大多数时候只需要：

```text
evaluate(expression="valid_area")
```

Agent 可见参数为：

```text
expression
timeout_seconds  # 可选；默认 60s，仅预计更久时传
```

`file / line / function / session_id / frame_id / context / full` 都不暴露给 `evaluate`。daemon 自动选择：

```text
当前 MCP frontend workspace
→ 该 workspace 内最后停止的 session
→ 当前 stopped thread
→ 最新 top frame
→ DAP context="repl"
```

如果需要当前活动调用栈中其他帧的对象，例如：

```text
process_sequence()   # sequence_id
  → process_frame()  # frame_idx
    → process_pix()  # 当前停点
```

先获取当前完整调用栈：

```text
get_stack()
#0 worker/pixel.py:318 process_pix
#1 worker/frame.py:201 process_frame
#2 worker/sequence.py:87 process_sequence
```

在 `evaluate` 表达式内部使用 `frame_eval`：

```text
evaluate(expression='frame_eval("frame_idx", "worker/frame.py", 201, "process_frame")')
evaluate(expression='frame_eval("sequence_id", "worker/sequence.py", 87, "process_sequence")')
```

匹配规则故意严格：

- `file + line + function` 是 `frame_eval` 的三个定位参数，必须直接写成字面量，不能只靠函数名或行号猜；
- `file` 应原样使用 `get_stack()` 展示的 workspace-relative path（仅路径分隔符会归一化）；
- 每次都在**当前 pause 的完整 stack**重新匹配；
- 0 个匹配：报错并要求重新 `get_stack()`；
- 多个完全相同匹配（例如递归停在同一源码行）：报歧义，不自动选择；
- `#0/#1/...` 是栈深度，不是 DAP frame ID，也不是 `evaluate` 参数。

这表示访问当前活动调用栈中的帧，不是访问历史执行。用户手动 step/continue、命中新断点或切换进程后，旧的 `file:line function` 可能失效，重新调用 `get_stack()` 即可。

单一表达式会返回结果；逗号分隔的多个值仍是一个元组表达式。import、赋值、循环、函数定义等多语句代码会正常执行，但可能返回空或 `(omitted)`；这不表示执行失败，需要结果时再单独查询生成的变量。

多语句 Python 直接传给 `evaluate`，不要为此套 `exec/eval/compile`。只有任务确实需要动态代码时才使用它们，并把所有 `frame_eval(...)` 调用写在动态字符串外。

返回只包含 debugpy 提供的**完整 expression result**，不做 debugger-mcp 字符截断，也不附带 session/frame/type/presentationHint 等 DAP envelope。

默认最多等待 **60 秒**。普通调用不要主动传 timeout：

```text
evaluate(expression="data['img_feats_4'].shape")
```

只有预计操作会超过 60 秒，例如较重 GIF/动画渲染，才按次延长：

```text
evaluate(expression="...", timeout_seconds=180)
```

`timeout_seconds` 只控制本次 `evaluate` 的 DAP 等待时间；debugger-mcp 会同步延长 frontend → daemon 的 RPC timeout。v0.6.7 的 late MCP response 丢弃规则继续生效。

## evaluate 内的 frame_eval：访问当前活动调用栈中的真实对象

这仍然是原来的 `evaluate`，不是新工具。`evaluate` 固定在当前 `#0` 顶帧执行；无论只读取另一个活动帧中的一个值，还是组合多个活动帧的真实对象，都使用表达式内部的 `frame_eval`。

先读取最新调用栈：

```text
get_stack()
#0 losses.py:318 calculate_loss
#1 model.py:201 forward
```

然后调用原来的 `evaluate`：表达式直接使用 `#0` 的 locals，并用同步函数 `frame_eval(...)` 取得其他活动帧对象：

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

`raw_images` 是同一 debuggee 进程中的原始对象引用，不是 DAP 字符串、JSON 或 `variablesReference`。对象 identity、Tensor、自定义类和容器关系保持不变；只有最终显示文本或已经保存的 artifact 路径返回 Agent。

约束：

- 每次先调用当前 pause 的完整 `get_stack()`，`file + line + function` 必须原样复制并严格匹配一个活动帧；
- `file`、`line`、`function` 必须直接写成字面量，不能由变量、函数、`*args` 或 `**kwargs` 动态生成；`expression` 可以是变量；
- `evaluate` 表达式固定运行在 `#0` namespace；顶帧变量直接使用，不要为它们调用 `frame_eval`；
- `evaluate` 不再接受外层 `file/line/function`；顶帧变量直接使用，其他活动帧调用 `frame_eval`；
- 已经返回、不再出现在 `get_stack()` 中的函数属于历史执行，无法访问；
- 不跨 thread、process、torchrun rank、debug session 或早先 pause 搬运对象；
- selector 失效或歧义时重新 `get_stack()`，debugger-mcp 不猜。

一次外层 `evaluate`/recipe action 会预先定位所有 selector，只捕获实际引用的唯一目标帧；相同 selector 重复调用不会增加 DAP 捕获次数。批量采集时把所需跨帧读取集中在同一个 action 中，并优先用 `condition`/`hitCondition` 抽样，避免每个 batch 都做绘图和 GPU→CPU 同步。

单表达式会返回其正常显示结果；多语句表达式执行完成后通常返回 `None`。可视化/导出时应在 debuggee 内直接保存文件并报告明确路径。

## DAP 审计日志

v0.6.7 默认把调试链路的关键状态写入 daemon 日志：

```text
/tmp/debugger-mcp-daemon.log
```

典型记录：

```text
[DAP AUDIT] request source=ide session_id=6 ... command=next
[DAP AUDIT] request source=mcp session_id=6 ... command=evaluate timeout=60.000s
[DAP AUDIT] event session_id=6 event=continued ...
[DAP AUDIT] event session_id=6 event=stopped stop_id=9 reason=step thread_id=1 ...
[DAP AUDIT] timeout source=mcp session_id=6 ... command=evaluate timeout=60.000s
[DAP AUDIT] response route=drop_late_mcp session_id=6 ... command=evaluate ...
[DAP AUDIT] adapter event=stopped root_session_id=5 pid=... returncode=...
```

这使得意外位置变化或 adapter 断开后可以直接区分：是 IDE 发了 `next/continue`、MCP 发了执行控制、debugpy 自己发了终止事件、还是某个长 request 超时后出现迟到 response。

debugpy adapter 的 stdout/stderr 也追加到同一文件，不再静默丢弃。可配置：

```bash
export DEBUGGER_MCP_DAEMON_LOG=/path/to/debugger-mcp-daemon.log
export DEBUGGER_MCP_DAP_AUDIT=1   # 默认开启；设为 0 可关闭 audit 行
```

## inspect_structure

`inspect_structure` 对单个表达式进行受限结构查看，不替代 `get_locals` 和 `evaluate`。

```text
inspect_structure(expression="batch", profile="summary")
inspect_structure(expression="outputs", profile="normal")
inspect_structure(expression="features[0]", profile="deep")
```

如果对象只存在于 caller frame，先 `get_stack()`，然后把目标栈行的 `file + line + function` 原样一起传入：

```text
inspect_structure(
    expression="metainfo",
    file="worker/frame.py",
    line=201,
    function="process_frame",
    profile="summary",
)
```

这三个 selector 字段 all-or-none，并且只匹配当前 pause 的完整 stack。0 个匹配说明 stack 已变化或 selector 不正确；多个精确匹配（例如递归同位）则报歧义，debugger-mcp 不猜。`inspect_structure` 不向 Agent 暴露 `session_id` 或 DAP `frame_id`。

默认档位：

| profile | 最大深度 | 每容器子项 | 总节点 | 总字符 |
|---|---:|---:|---:|---:|
| summary | 2 | 8 | 60 | 6000 |
| normal | 4 | 12 | 160 | 14000 |
| deep | 6 | 20 | 400 | 28000 |

硬上限：

```text
max_depth <= 8
max_children <= 30
max_nodes <= 500
max_chars <= 30000
max_value_chars <= 200
inspection runtime <= 3s
```

Tensor/ndarray 默认只显示元信息；容器使用有限采样；循环引用、节点数、字符数、深度和运行时间均受限制。v0.6.5 默认直接从第一条结构行开始，不再额外输出 `inspect '<expression>' profile=... nodes=...` 这类重复 header。

结构查看本身的深度/字符上限只限制**正常结构结果**；如果 helper 发生异常，完整 traceback 会转入 MCP error channel，不受这些上限影响。

## run_to_line

```text
run_to_line(
  file="/workspace/project/model.py",
  line=238,
  reason="当前位于同一 forward 函数前方，路径中没有无条件 return。"
)
```

内部流程：

```text
保存用户断点
→ 合并内部临时断点
→ continue
→ 等待真实 stopped / terminated / timeout
→ 报告实际停点
→ 清理临时断点并恢复用户断点
```

同文件目标可由 Agent 在说明可达性后使用；跨文件目标必须先得到用户明确同意，并传入：

```text
cross_file_authorized=true
```

## frame 缓存与过期保护

frame 缓存按 `session_id + stop_id` 隔离。同一次暂停中，`get_locals`、`evaluate` 和 `inspect_structure` 可复用 stack/frame 信息；手动或 Agent 单步、继续、命中新断点、终止或重启后自动失效。

`evaluate` 和 `inspect_structure` 都不接受 DAP `frame_id`。`evaluate` 默认使用最新 top frame，其他活动帧统一通过表达式内部的 `frame_eval(...)` 访问；`inspect_structure` 保留自己的外层 `file + line + function` selector。两者都在当前 pause 的完整 stack 中严格匹配，因此旧 selector 在执行移动后会安全失败。其他高级/raw DAP 工具仍可能使用自己的 `frame_id` 参数。

## 安装

```bash
cd debugger-mcp-v0.9.3-source
python -m pip install -e . --no-build-isolation
```

验证：

```bash
python -c "import debugger_mcp; print(debugger_mcp.__version__); print(debugger_mcp.__file__)"
```

预期：

```text
0.9.3
```

MCP 配置使用的 Python 必须是安装了该源码的同一个 Python。

# MCP 配置

## Cursor：推荐全局配置

v0.6.3 起支持 Cursor 的 MCP Roots，因此可以只配置一次：

```text
~/.cursor/mcp.json
```

```json
{
  "mcpServers": {
    "debugger": {
      "type": "stdio",
      "command": "/path/to/python",
      "args": ["-m", "debugger_mcp"]
    }
  }
}
```

不需要在每个项目的 `.cursor/mcp.json` 中再写 `DEBUGGER_MCP_WORKSPACE=${workspaceFolder}`。只要 Cursor 在 MCP initialize 中声明 Roots capability，debugger-mcp 会主动请求 `roots/list` 并把单一 root 作为当前 frontend workspace。

注意：这是 **MCP frontend** 的 workspace 自动识别。调试配置的 `.vscode/launch.json` 仍需：

```json
"debugServer": 4711,
"env": {
  "DEBUGGER_MCP_WORKSPACE": "${workspaceFolder}"
}
```

## Claude Code / 其他 stdio MCP 宿主

项目 `.mcp.json` 示例：

```json
{
  "mcpServers": {
    "debugger": {
      "type": "stdio",
      "command": "/path/to/python",
      "args": ["-m", "debugger_mcp"]
    }
  }
}
```

Windows 路径示例：

```json
"command": "D:\\Softwares\\anaconda3\\python.exe"
```

Linux 路径示例：

```json
"command": "/home/lin/miniconda3/bin/python"
```

# VSCode 配置

## 普通单进程

```json
{
  "name": "Python through debugger-mcp",
  "type": "debugpy",
  "request": "launch",
  "program": "${file}",
  "console": "integratedTerminal",
  "justMyCode": true,
  "debugServer": 4711,
  "env": {
    "DEBUGGER_MCP_WORKSPACE": "${workspaceFolder}"
  }
}
```

`env.DEBUGGER_MCP_WORKSPACE` 由代理消费并在转发前移除，不会进入被调试程序；其他 `env` 项保持不变并继续传给 debugpy。`cwd` 仅记录为诊断字段 `launch_cwd`。

如果原配置已经有 `env`，把标记合并进同一个对象；JSON 里不要写两个 `env` 键：

```json
"env": {
  "PYTHONWARNINGS": "ignore",
  "DEBUGGER_MCP_WORKSPACE": "${workspaceFolder}"
}
```

## torchrun / DDP

```json
{
  "name": "train debug ddp",
  "type": "debugpy",
  "request": "launch",
  "program": "/usr/local/bin/torchrun",

  "preLaunchTask": "source-env",
  "envFile": "${workspaceFolder}/.vscode/.env/.cache",
  "console": "integratedTerminal",
  "justMyCode": false,
  "python": "python",
  "cwd": "${workspaceFolder}/projects/vfm",

  "debugServer": 4711,
  "env": {
    "PYTHONWARNINGS": "ignore",
    "DEBUGGER_MCP_WORKSPACE": "${workspaceFolder}"
  },
  "subProcess": true,

  "args": [
    "--nproc_per_node", "1",
    "--node_rank", "0",
    "--nnodes", "1",
    "--master_addr", "127.0.0.1",
    "--master_port", "28537",
    "tools/train.py",
    "${workspaceFolder}/projects/vfm/configs/vfm/A5.16.17.py",
    "work_dirs/test_debug",
    "--debug"
  ]
}
```

代理会自动把以下信息注入 VSCode/debugpy 的内部 `startDebugging` 子 session，并在子 session 建立后再次消费 workspace 标记：

```text
debugServer=4711
DEBUGGER_MCP_WORKSPACE=<root workspace>
parent/root session marker
```

因此 worker 会回到所属项目的 adapter group，不会串到另一个项目。

## 多 rank

多个 worker 同时存在时调用：

```text
attach_debugpy
```

`attach_debugpy` 可根据 `session_id / process_id / subprocess_id / process_name / debug_state` 诊断多个 worker。`evaluate` 本身不暴露 `session_id`，始终作用于当前 workspace 最新 genuinely stopped 的 session；若必须检查指定 worker，可先让该 worker 成为当前停点，或使用仍支持显式 session 选择的诊断/读取工具。

确认当前被自动选中 worker 的 rank：

```text
evaluate(expression="__import__('os').environ.get('LOCAL_RANK')")
```

# Skill

源码包内的正式 Skill：

```text
claude-skill/debugger-mcp/SKILL.md
```

它包含 runtime-first 强触发规则：用户明确说程序已命中断点、已经暂停或要求查看当前运行时值时，Agent 必须先调用 debugger MCP，而不能先 Grep/Read。

安装：

Windows：

```powershell
New-Item -ItemType Directory -Force "$HOME\.claude\skills\debugger-mcp" | Out-Null
Copy-Item ".\claude-skill\debugger-mcp\SKILL.md" "$HOME\.claude\skills\debugger-mcp\SKILL.md" -Force
```

Linux：

```bash
mkdir -p ~/.claude/skills/debugger-mcp
cp claude-skill/debugger-mcp/SKILL.md ~/.claude/skills/debugger-mcp/SKILL.md
```

替换后完全退出并重启 Claude Code。

# 从旧版本升级

先安装 v0.9.3：

```bash
python -m pip install -e . --no-build-isolation
```

安装完成后无需手动处理 daemon：

```text
daemon 版本相同 → 直接复用
daemon 版本不同 → 直接替换为当前安装版本
```

不需要预先停止 VSCode 调试，也不需要手动清理 4711/4712。旧调试连接如果因替换而断开，在 VSCode 中重新按一次 F5 即可。

仍需在每个项目的 `launch.json` 中加入：

```json
"env": {
  "DEBUGGER_MCP_WORKSPACE": "${workspaceFolder}"
}
```

不要再使用非法的顶层字段 `debuggerMcpWorkspace`。不要依赖 `cwd` 自动匹配；v0.6.0 会把它当作诊断信息，而不是路由依据。

# 验证

Claude Code：

```text
/mcp
```

预期：

```text
debugger · connected · 10 tools
```

命中断点后调用：

```text
attach_debugpy
```

应看到：

```text
client_workspace
workspace_session_count
selected_session_id
sessions
```

默认 `get_locals()` 应是紧凑文本；默认 `evaluate()` 应只有结果本身。

## daemon 日志

Windows：

```powershell
Get-Content "$env:TEMP\debugger-mcp-daemon.log" -Tail 100
```

Linux：

```bash
tail -n 100 /tmp/debugger-mcp-daemon.log
```

# 开发测试

```bash
pytest -q
python tests/integration_smoke.py
python tests/integration_v040.py
python tests/integration_run_to_line_timeout.py
python tests/integration_subprocess.py
python tests/integration_sequential.py
python tests/integration_workspaces.py
python tests/integration_torchrun.py
```

覆盖范围：

- `get_stack` 默认完整 frame 集合、`get_locals` 默认完整 local 名称集合、`evaluate` 完整结果，以及真正不裁字段的 `full=true`；
- 两个 Claude 前端共享同一 daemon；
- 两个独立 workspace 同时运行 root launch，并各自默认读取自己的停点；
- 显式 `session_id` 跨项目读取；
- root + subprocess 自动路由；
- 真实 torchrun rank worker；
- 连续多次 F5；
- `inspect_structure` 限制；
- `run_to_line`、用户断点保留、超时后台清理和跨文件授权。
