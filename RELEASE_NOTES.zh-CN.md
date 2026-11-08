[English](RELEASE_NOTES.md) | **简体中文**

# Release Notes

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
