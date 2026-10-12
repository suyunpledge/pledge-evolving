# Forge Runtime Convergence RC1 交付报告

## 基线与分工

- 审查基准：`01c5e166474c960956ec69bb35713dde1075f597`。
- 实施基线：`main / 3135de74b67ce233d603f41c17bff5f51696179e`。
- W1 `71bfe8a2a1df1ff41faca8ef79433ef2f4ed117f`、W2 `3135de7` 均是基线祖先。
- 分支：`feature/runtime-convergence-rc1`。
- 工作树：`C:/Users/匡溯昀/forge-runtime-convergence-rc1`。
- 开始时主工作区及既有工作树没有未提交源码变更，Codex 没有其他活跃源码任务。
  随后主工作区出现 GUI/client 的并行未提交修改，未涉及本轮 Runtime 文件；本工作树
  没有覆盖它们。不能将本轮验收视为与那些未提交 GUI 修改的集成验收。
- 最终复核时主分支已由并行工作推进到 W4 `c6c10feeb74a9ecc83b458b3ca25db6d72125b76`
  （包含 W3 `cd1b15a`）；与实施基线的差异为 GUI/client/报告，没有本轮核心 Runtime
  文件重叠。本轮测试针对独立 W2 基线工作树，不宣称已验证 W3/W4 的组合。
- 未修改 GUI 布局、视觉系统、插件市场业务或 Desktop Awareness，未合并主分支。

W1 的审批/记忆/净化/回滚修复和 W2 的注册重名保护继续保留。R1～R3 在新基线仍存在；
W2 的尾部续号能改善顺序双实例，但扫描与追加之间没有互斥，R4 仍需跨进程锁。

## R1～R4 状态

| 项目 | RC1 结果 | 边界 |
|---|---|---|
| R1 副作用授权 | 已修复 | 宿主验证的 ToolEffect 独立保存；未知工具拒绝；只读模式/沙箱硬拒写入；正常宿主读工具保留 |
| R2 上下文差异 | 已修复目标入口 | CLI、Agent/EditorBridge、Gateway 复用宿主工厂；继承 expose、控制目录与能力，拒绝 workspace/journal/identity 不一致 |
| R3 重复动作路径 | 已收敛核心与 Gateway | Agent 两种解析适配共用 ActionExecutor；GUI 对话循环和专用宿主恢复/测试发布尚未整体迁移 |
| R4 并发状态 | 已修复 RC1 临界区 | Session 修复/续号/追加持 OS 锁；独立日志；版本比较与写入持共同文件锁；shadow Git 事务锁；无版本请求保留兼容行为 |

## 修改文件

| 文件 | 修改 |
|---|---|
| `forge/execution.py` | 六种公共结构、宿主工厂、公共动作执行器、查询/去重、未知结果与恢复约束 |
| `forge/runtime_lock.py` | Windows 字节锁/POSIX flock、线程重入、进程死亡释放；锁文件在系统临时目录 |
| `forge/policy.py` | ToolEffect 输入、写入硬边界、标准路径字段范围检查；保留别名规则兼容 |
| `forge/tools.py` | 宿主效应快照、未知工具保守拒绝、能力检查、版本与写入锁、结构化结果、坏结果的未知状态 |
| `forge/loop.py` | 复用执行器、删除重复 checkpoint 分支、公共身份/结果、父子 task/run 关联、事件先持久化 |
| `forge/session.py` | OS 锁内尾部修复/续号/追加；完整长记录续号；读快照与同步/关闭保护 |
| `forge/checkpoint.py` | 工作区与共享 shadow Git 仓库事务锁，保持 W1 回滚行为和历史布局 |
| `forge/gateway.py` | 隔离 Registry、继承宿主目录/权限、动作执行/查询/事件分页/取消、认证与 scope 绑定、证据恢复 |
| `forge/cli.py` | Gateway expose/home 接入；公开运行/动作身份与日志路径；未完成/未知结果不返回成功 |
| `forge/vscode_bridge.py` | 保留原 RPC 并增加公共结果字段；使用公共 run_id；显式导入 importlib.util 支持嵌入式解释器探测 |
| `forge/desktop_services.py` | 宿主确认连接器 effect，保留原确认与撤销检查 |
| `forge/execution_guard.py` | 恢复投影增加 unknown_actions/requires_inspection，不自动重放 |
| `forge/test_task_planning.py`、`forge/test_desktop_services.py` | 修正旧夹具中 Policy.workspace 与实际 workspace 不一致 |
| `forge/test_secret_virtualization.py` | 对齐 W1：短标点不进入全局匹配，结构化局部秘密仍被保护；没有改秘密 Runtime |
| `tests/test_runtime_convergence.py` | RC1 权限、协议、HTTP、Git、错误恢复、线程和独立进程隔离验收 |
| `tools/verify_rc1.py` | 串行全量验收、日志、数量、退出码、环境和源码/日志 SHA-256 记录 |
| `docs/runtime-convergence-rc1-api.md` | ZCode 接线定义与最小调用例子 |
| 本报告及 `runtime-convergence-rc1-validation.json` | 验收证据与兼容性说明 |

## 执行协议

公共入口为 `forge.execution`：`RunRequest`、`ExecutionContext`、`ActionRequest`、
`ActionResult`、`EventEnvelope`、`RunResult`，以及 `create_execution_context()` 和
`ActionExecutor.execute()/query()`。身份至少覆盖 run/task/parent/action，动作包含
outcome/error_code/execution_state/authorization，事件包含 event_id。

执行顺序：验证上下文 → 授权 → 同步意图 → checkpoint/版本保护 → 单次工具调用 →
同步结果 → 通知订阅者。授权可以复查，handler 不重复执行。相同已记录动作返回旧结果；
不同参数复用身份被拒绝；未配对意图或副作用未知始终需要检查，不自动重放。

Gateway 提供：

- `POST /v1/tools/call`，兼容原 ToolResult 响应，增加身份/状态元数据。
- `GET /v1/runs/{run_id}/actions/{action_id}`。
- `GET /v1/runs/{run_id}/events?after={ordinal}`，每页最多 200 条。
- `POST /v1/runs/{run_id}/cancel`，承认请求而非宣称已中断任意 handler。

新查询/控制接口必须配置 Gateway key；缺失 key 返回 503，缺失/错误认证返回 401。
原无 key 模型/工具端点保持兼容。受保护 run 不能通过省略或更换 scope header 加入。
重启恢复的 Gateway run 只供证据查询/去重；继续工作需新 run 和当前宿主授权。

## 验收方法

完整执行结果、命令、数量、耗时、解释器、操作系统与日志 SHA-256 以相邻验证 JSON 为准。
最终串行结果：

| 套件 | 数量 | 通过 | 跳过 | 失败/错误 |
|---|---:|---:|---:|---:|
| RC1 新增（包含在 tests 中） | 54 | 54 | 0 | 0 |
| 仓库 tests/ 全量 | 119 | 118 | 1 | 0 |
| forge/ 测试全量 | 229 | 228 | 1 | 0 |
| GUI/插件适配指定 10 个模块 | 120 | 120 | 0 | 0 |
| Forge 全量 selftest（check 数） | 574 | 574 | 0 | 0 |

两项跳过均为不能实际创建符号链接的平台条件；GUI 结果不是整个 GUI 测试目录的全量。
不将 selftest 的 check 数与 unittest 用例数混算。系统 Python 3.12.10，Windows
`Windows-11-10.0.26200-SP0`，PyYAML 6.0.3；所有命令最终退出码为 0。
复现命令：

```powershell
& 'C:/Users/匡溯昀/AppData/Local/Programs/Python/Python312/python.exe' -B `
  tools/verify_rc1.py --output 'C:/Users/匡溯昀/forge-rc1-final-validation'
```

使用 Windows 11、Python 3.12.10、PyYAML 6.0.3 和本机 Git；模型响应为离线可控传输，
HTTP 验证使用真实 loopback server，文件/Git/子进程/并发操作是真实执行，没有付费模型调用。

主要验收包括：

- 任意名称写工具、伪造只读/宿主文件名、native、权限 allow/deny、路径范围与能力缺失。
- CLI/Gateway 同配置权限与 expose；冻结 run 不获得新权限、宿主新限制仍生效。
- 身份、持久化先于通知、查询运行中/完成/未知结果、并发重复动作只 dispatch 一次。
- 意图 fsync 失败不写文件；结果日志失败、部分写后异常、坏返回结构均不能当作安全重试。
- checkpoint 故障不 dispatch；补丁失败保留原文件；真实 Git checkpoint 恢复。
- 4 个独立进程追加 180 条 Unicode 日志：单一 meta、连续唯一 ordinal、完整记录。
- 线程、4 个进程、Gateway 线程和嵌套 workspace 同版本竞争：一个成功，其余明确冲突。
- 两项目共享 home 的并行 shadow snapshot 不混用 Git index。
- 既有真实闭环 `ExecutionTests.test_true_execution_loop_and_recovery`：创建任务、真实 Agent
  改 `value.py`、查看 Git Diff、执行子进程 unittest、检查结果、恢复文件并续写新日志。

另外执行并保留了一次独立闭环（不是临时夹具结束后删除的证据）：

- 输出目录：`C:/Users/匡溯昀/forge-rc1-closed-loop`。
- run/task：`2c0a392e552f4e578b4249510513b09f`，两次真实工具动作。
- 文件 `VALUE = 1` → `VALUE = 2`；`diff.patch` 是真实 git diff。
- `tests.txt` 保存实际 Python 子进程 unittest 通过结果（退出码 0）。
- 使用动作 checkpoint 恢复为 `VALUE = 1`，发布并同步 checkpoint_restored 事件。
- `evidence.json` 保存公开结果、动作查询、checkpoint、日志路径和模型传输口径。

## 发现并修正的回归与环境问题

- 首轮 selftest 574 个断言通过，但退出时临时日志句柄未关闭；现已关闭宿主调用句柄。
- 委托插件写调用错误持有本地 workspace 锁，等待 Gateway 再取同一锁而死锁；实际写入
  边界与委托确认边界现已分开，原插件授权与撤销逻辑未改。
- 控制 home 是项目祖先时过宽保护导致 smoke 不写输出；已修正目录关系并增加验收。
- 旧 workspace 夹具和 `Agent.__new__` 计费夹具兼容问题已修正，没有放宽生产验证。
- 嵌入式 Python 缺 PyYAML、Tk 返回 Tcl_Obj，以及从 stdin 启动 multiprocessing 的问题
  导致初次广泛测试失败；最终验收使用完整系统 Python 和可复现脚本。
- 未修改 W2 基线在系统 Python 下也存在短标点测试失败。只更新与 W1 冲突的断言，并
  保留 JSON 完整性、全局不污染和局部秘密保护三项验证。
- 一次同时运行四组回归时，未修改的 CacheWarmth 用例出现 87/90 条目；源码对锁/I/O
  失败采用最佳努力策略，推测与并行负载下的短锁窗口有关。未把此观测当作 RC1 已修复。
  随后的完整 229 项串行回归通过，最终脚本继续串行验收；缓存遥测仍是已知限制。
- selftest 中有负向夹具打印局部 7/8，最终判定使用根汇总和命令退出码，不累计中间输出。

## 兼容性与已知限制

- 未经宿主验证的第三方 ToolSpec 将被拒绝；正常内建读工具和受控插件/连接器保留。
- workspace 不一致、跨日志身份复用被拒绝；Gateway 的 expose 现在实际限制工具面。
- 默认日志由 current.jsonl 改为独立身份；显式 session_path 保留，调用方应读取 sessionPath。
- CLI step-limit/cancelled/unknown 不再被视为成功；旧字段仍在，新字段为兼容增量。
- 锁只约束合作的 Forge 进程。外部编辑器不持这些锁；版本保护应使用 expected revisions。
  无版本的 CLI/Gateway 写请求为兼容仍允许，不能声称检测了所有外部文件竞争。
- 沿用预览版本大小/目标数限制；不支持任意工具 exactly-once，也不自动撤销未知副作用。
- checkpoint 历史布局仍兼容旧全局 home，跨项目恢复必须用明确动作 commit/workspace，
  不能用全局 rollback_last 推断任务归属；未提供整个项目与外部编辑器的严格事务快照。
- Gateway live context 上限 256，日志查询为线性扫描；临时 home 不提供跨重启持久查询。
- 取消是合作边界检查，不能保证立即中断任意原生代码或持锁的 handler。
- 本轮没有迁移整个 GUI 对话循环、视觉布局、所有宿主发布事件或缓存遥测存储。

## ZCode 接线

详见 `docs/runtime-convergence-rc1-api.md`。GUI 应分配一次任务的 run/task 身份，给动作
分配 action_id，传递认证和一致 scope，按 event_id 去重、按 action_id 更新工具项。
`tool_completed` 是动作终态，旧 tool_call 只是兼容 Step 视图。使用公开 RunReport/Bridge
字段和查询接口，后台线程事件排队到 Tk，不依赖 Agent 私有字段，不因模式切换取消任务。

本轮未将分支合并或推送；代码提交与文档提交由最终交付消息列出。
