[English](TOOLS.md) | **简体中文**

# 工具介绍

debugger-mcp 提供 10 个 MCP 工具，并额外提供复用同一套调试基础设施的 god_runner 自动化模式。本文先说明工具和运行模型，再分别介绍读取检查、执行控制与自动化用法。

## 目录

- [工具总览](#工具总览)
- [核心运行机制](#核心运行机制)
- [读取与检查工具](#读取与检查工具)
- [执行控制工具](#执行控制工具)
- [god_runner 自动化模式](#god_runner-自动化模式)

## 工具总览

| 类别 | 工具 | 用途 |
|---|---|---|
| 连接与定位 | `attach_debugpy` | 查看连接状态、workspace 路由和当前选中的调试 session |
| 连接与定位 | `get_threads` | 列出当前调试进程中的线程 |
| 连接与定位 | `get_stack` | 获取当前活动调用栈，并为跨帧读取提供精确位置 |
| 运行时检查 | `get_locals` | 查看当前暂停帧的全部局部变量 |
| 运行时检查 | `evaluate` | 在最新暂停的顶层 frame 中执行 Python |
| 运行时检查 | `inspect_structure` | 对运行时对象进行有界的结构化检查 |
| 执行控制 | `step_over` | 单步越过当前语句 |
| 执行控制 | `step_into` | 单步进入被调用代码 |
| 执行控制 | `step_out` | 运行到当前函数返回 |
| 执行控制 | `run_to_line` | 使用临时断点运行到指定源码行 |

`frame_eval(expression, file, line, function)` 是 `evaluate` 和 god_runner action 内置的跨帧访问能力，不是独立的 MCP 工具。

## 核心运行机制

### Workspace 路由

#### MCP frontend 自己属于哪个 workspace

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

#### debug session 属于哪个 workspace

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

#### 为什么必须显式配置

`cwd`、`program`、`args` 和栈顶源码路径可能经过子目录、符号链接、容器挂载、多根工作区或公共运行目录。v0.6.0 不使用这些信息猜测 session 归属。

DAP `cwd` 只会作为 `launch_cwd` 出现在 `attach_debugpy` 和错误诊断中，帮助发现配置写错；它不参与 session 选择。这样宁可明确提示“当前 workspace 没有匹配 session”，也不会把 A 项目的 Claude 静默连到 B 项目。

`DEBUGGER_MCP_WORKSPACE` 会在代理转发 `launch/attach` 给 debugpy 前被移除，因此不会进入训练进程。其他 `env` 项保持不变。torchrun/debugpy 子 session 通过内部请求继承 root 的显式标记。

不支持 MCP Roots、且 MCP 进程 cwd 也不是项目根目录时，仍可在 MCP 配置中显式设置：

```json
"env": {
  "DEBUGGER_MCP_WORKSPACE": "/absolute/path/to/project"
}
```

### Session 自动选择

普通工具调用不会返回所有调试 session，也不要求 Agent 逐个判断。未传入 `session_id` 时，daemon 直接选择当前 MCP frontend workspace 内最后停止的 session。

`attach_debugpy()` 默认只返回紧凑状态：总 session 数、当前 workspace 匹配数、停止数以及自动选中的 session。`attach_debugpy(full=true)` 返回完整 session 元数据；生成 recipe 时可由 `selected_session.root_session_id` 定位 root session，并读取其 `launch_name`，避免在多个 launch 配置中猜测。

普通工具找不到匹配 session 时只返回计数和配置提示，不再把所有活动 session 摘要塞入错误消息。`cwd` 仍只用于诊断，不参与 workspace 归属或自动路由。

### 调试上下文变更通知

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

### Frame 缓存与过期保护

frame 缓存按 `session_id + stop_id` 隔离。同一次暂停中，`get_locals`、`evaluate` 和 `inspect_structure` 可复用 stack/frame 信息；手动或 Agent 单步、继续、命中新断点、终止或重启后自动失效。

`evaluate` 和 `inspect_structure` 都不接受 DAP `frame_id`。`evaluate` 默认使用最新 top frame，其他活动帧统一通过表达式内部的 `frame_eval(...)` 访问；`inspect_structure` 保留自己的外层 `file + line + function` selector。两者都在当前 pause 的完整 stack 中严格匹配，因此旧 selector 在执行移动后会安全失败。其他高级/raw DAP 工具仍可能使用自己的 `frame_id` 参数。

### 默认输出契约

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

### DAP 审计日志

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

## 读取与检查工具

### get_locals：默认紧凑，按需完整

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

### evaluate：默认 top frame，通过 frame_eval 访问其他活动帧

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

#### frame_eval：访问当前活动调用栈中的真实对象

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

### inspect_structure

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

## 执行控制工具

### step_over / step_into / step_out

三个单步工具分别用于越过当前语句、进入被调用代码和运行到当前函数返回。它们都会返回新的源码位置、真实停止原因和线程；只有需要原始 step/session 诊断信息时才使用 `full=true`。

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

## god_runner 自动化模式

### 使用流程

1. 保留项目已经存在的 `.vscode/launch.json`。god_runner 按 `name` 读取其中一个 configuration；`${workspaceFolder}` 和 `${env:NAME}` 会展开。v0.8.2 起保留原有 `console`。当 debugpy 请求 `runInTerminal` 时，程序直接连接到用户启动 recipe 的当前终端，因此原程序 stdout/stderr、进度条和 torchrun worker 输出可见。
2. 用户明确要求 Agent 生成 recipe，例如：“把刚才验证过的可视化做成 god_runner recipe，保存为 `god_run.py`”。Agent 先用 `attach_debugpy(full=true)` 精确取得实际 root `launch_name`，只读取 `.vscode/launch.json` 中的同名配置，再把已验证的 `evaluate()`、绘图或 dump 代码整理成断点 action；用户不需要手工复制代码。
3. Agent 报告 recipe 路径、launch 名称、断点位置、输出路径和运行方法，到此结束，不启动 recipe。
4. 用户检查后自行运行该 Python 文件。god_runner 随后自动完成断点 action 和 continue，无需启动 MCP、无需按 F5、无需手动 continue。

### 运行状态与端口

运行期间应能看到 `[god] session ... process`、`breakpoint verified`、`startDebugging received`、`hit`、`action completed` 和 `exited`。若 15 秒内没有新的 DAP 消息，会输出 `[god] waiting` 心跳；这表示仍在等待，而不是静默卡死。若一直没有 `hit`，应根据日志先判断 worker 子 session 是否建立、断点是否在实际执行进程中命中。

端口职责：

| 通道 | 默认值 | 用途 |
|---|---:|---|
| MCP frontend | stdio | Agent 与 debugger-mcp 通信，不监听 TCP 端口 |
| DAP proxy | `127.0.0.1:4711` | VSCode、god_runner 和 debugpy session 的 DAP 入口 |
| daemon control | `127.0.0.1:4712` | debugger-mcp 内部控制 RPC |

所以 `4711` 不会与 MCP stdio 发生端口冲突，god_runner 也不另占第二个 `4711`。但 god_runner 与 MCP 共用同一个 daemon/session 路由；用户运行 recipe 时，不要再让 Agent 同时 continue、step 或 evaluate 同一 workspace 的相同调试任务，避免两个控制方竞争执行状态。

### 生命周期与职责边界

recipe 正常结束、action/child-session 失败或用户按 Ctrl+C 后，只清理该 recipe 建立的 root/child DAP session 和独立终端进程组。torchrun launcher 已先退出时，仍会继续清理同组 rank worker，避免残留进程占用 GPU；共享 daemon 和 MCP frontend 不会关闭。结束日志会明确显示 `shared debugger daemon remains available for MCP`；此后 MCP 和 VSCode 可以继续使用 `4711/4712`。唯一的例外发生在升级后的第一次连接：若内存中的 daemon 版本与已安装包不同，统一的版本切换机制会替换旧 daemon，并要求原有调试连接重新启动一次。

职责边界：

- Agent：只在用户明确提示后编写或修改 recipe；可以检查源码、`launch.json`，并在已经存在的暂停现场做短小 action 验证；提供清楚的运行指导。
- Agent 禁止事项：不得调用 recipe 的 `run()`，不得启动、代跑、等待或监控长时间 train/infer，也不得把“再确认一次”当成执行授权。这些操作不属于 Agent 职责。
- 用户：决定是否运行、何时运行、运行多少数据/序列/checkpoint，并亲自启动 recipe。

### 推荐工作流

```text
用户 + Agent + debugger-mcp 交互探索
→ Agent 在真实暂停现场验证 evaluate/可视化/dump 代码
→ 用户要求“把刚才的操作写成 god_runner recipe”
→ Agent 查询实际 root launch，再读取同名配置和源码，生成并检查 god_run.py
→ Agent 提供运行指导并停止
→ 用户自行运行 recipe，批量采集结果
```

### Recipe 格式

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
