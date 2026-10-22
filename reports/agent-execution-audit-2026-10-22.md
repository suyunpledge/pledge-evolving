# Agent 执行链路整体审查与稳定性修复

审查实施：2026-10-22；继续验证及交付：2026-10-23（Asia/Shanghai）。

## 基线、范围与交付位置

- 主检出：`C:\Users\匡溯昀\pledge-evolving`，`main` / `8dc2944`。
- 实施工作树：`C:\Users\匡溯昀\forge-agent-execution-audit`。
- 独立分支：`fix/agent-execution-audit`，基于上一轮工具稳定性提交 `3bcb6762cd0ff6bcaa43cb097975922c5c7114ec`。主检出尚未包含该提交，本分支继承它的结果校验、取消检查和 Windows 文件锁修复。
- 本轮审查任务提交/就绪、模型及工具协议、权限、动作持久化、父子 Agent、取消恢复、文件版本保护、测试及 Diff 关联。没有改 GUI 布局、技术栈、插件市场或 Desktop Awareness，也没有合并主分支。
- 验证使用离线模型响应，工具、文件、Git、进程和本地 HTTP 服务实际运行；未使用用户密钥测试付费模型，不将离线验证表述为线上模型效果评测。

## 已确认并修复的问题

| 优先级 | 源码证据 | 原行为及影响 | 本轮修复 |
| --- | --- | --- | --- |
| P1 | `forge/tool_adapter.py:387,459`，`forge/tools.py:365` | 截断 JSON 补全产生 `_truncated`，但执行端没有拒绝；外层标记在解包时可能丢失。显式参数解析失败还会退化成空参数，误用工具默认值。 | 保留补全标记；显式错误参数不会转成平铺/空参数；公共授权边界拒绝 `_raw` 或有效 `_truncated`，返回 `invalid_arguments/not_started`，模型可重新生成完整参数。 |
| P1 | `forge/loop.py` 两条工具循环、`_stop_unknown` | ActionExecutor 已记录 unknown，但循环继续执行批次余下动作或再次请求模型。新动作 ID 无法阻止模型再次执行同一副作用。 | 首个 unknown 立即结束当前任务；保留动作身份及回执，停止批次和后续模型请求，不自动回滚、不重放。 |
| P1 | `forge/loop.py:493` `_spawn_handler` | 子 Agent 正常返回 RunReport 时，无论 stopped 原因都包装成 `ToolResult(ok=True)`；子任务结果未知不能阻止父任务继续。 | 依据公开 RunResult 返回成功/失败/未知；事件及元数据携带子 run/task 身份。子任务异常退出且留有未完成意图时也按未知处理。 |
| P2 | `forge/toolwire.py:113,160,186`，`forge/loop.py:1084` | 非对象 function/args 可使循环崩溃；缺失或重复调用 ID 使工具结果无法与下一轮消息正确配对。 | 派发前校验原生批次，结构错误最多反馈三次；重复 ID 拒绝整个批次；缺失 ID 局部分配并同步写入 assistant/result 消息。保留 reasoning/signature，不修改原始响应对象。 |
| P2 | `forge/tool_adapter.py:459`，`forge/toolwire.py:160` | 非有限数值可进入 ActionExecutor 严格 JSON 摘要计算并造成异常；Anthropic 错误 input 可能继续污染下一轮消息格式。 | 非有限参数转成明确格式错误；反馈历史使用规范化后的参数对象。 |
| P2 | `forge/loop.py:493` 子 Agent 构造和生命周期 | 子 Agent 未继承父 Agent 的 wire_hint，默认按 OpenAI 生成工具声明；子作用域未显式关闭。 | 继承宿主协议，子任务返回或异常时释放自己的 SecretScope。模型选择及权限上限沿用原实现。 |
| P1/P2 | `forge/checkpoint.py` `init/_snapshot/_git` | Git 子进程没有超时；`add/commit` 失败仍读取旧 HEAD，可能将旧快照误报为当前保护点。 | 每条 Git 命令设 30 秒超时；初始化、staging、commit、rev-parse 检查退出码，空身份报错。ActionExecutor 将准备失败记为 `preparation_failed/not_started`，不派发写工具。 |

原基线运行首批 11 项新增用例产生 12 个子断言失败及 6 个异常，确认并非仅对新实现编写自证测试。完整日志为用户目录的 `forge-agent-audit-baseline.log`。

## 已有功能的核验结果

| 链路 | 现有实现及验证依据 | 处理 |
| --- | --- | --- |
| Coding Mode 提交/就绪 | `forge/development/models.py`、`runtime.py`、`forge/vscode_bridge.py`；`test_development_readiness.py` 覆盖缺模型/凭据、初始化失败重试、模型刷新原子性、真实两轮调用。 | 保留已有修复，没有重写初始化。 |
| 主/子/审查模型及模式隔离 | `runtime.py` 冻结任务快照并 finally 恢复宿主属性；`test_development_execution.py` 和 `test_development_loop.py` 验证角色模型、取消传播、General/Development 隔离。 | 本轮仅修正子调用协议及结果传播。 |
| 权限与工具暴露 | `forge/policy.py`、`tools.py`、`execution.py`；执行协议套件覆盖不可信只读声明、缺失能力、READ_ONLY、CLI/Gateway 一致性。 | 共用现有授权器，没有增加权限。 |
| 动作身份、去重、未知状态 | `forge/execution.py:284` 先记录意图，再调用、持久化结果；同 ID 去重，未知意图禁止重放。 | 补上循环对 unknown 的消费，不新建第二套执行器。 |
| Session 与多进程 | `forge/session.py:197` 在 OS 锁内恢复尾记录、续号、追加；`runtime_lock.py` 继承上一轮修复；执行协议套件包含跨进程追加、同版本竞争、Gateway 多线程。 | 没有将磁盘续号当作互斥；本轮并发验证结果见下文。 |
| 编辑器与文件冲突 | `forge/development/editor.py`、`guard.py`；已有测试覆盖未保存内容、同大小同 mtime 外改、原子保存、过期观察和双 Agent 写冲突。 | 保留冲突机制，未改布局或编辑器。 |
| 测试、Diff、恢复证据 | `development/testing.py`、`evidence.py`、`review.py` 及现有执行闭环测试。 | 新增真实 Agent 修改、Diff、独立 Python 测试进程、Git checkpoint 恢复测试。 |

## 验证记录

环境：Windows，CPython 3.12（`C:\Users\匡溯昀\AppData\Local\Programs\Python\Python312\python.exe`），Git for Windows；使用 `-B` 和 `PYTHONIOENCODING=utf-8`。

复现命令（将 python 替换为上述解释器）：

```text
python -B -m unittest discover -s tests -p test_agent_stability.py -v
python -B -m unittest discover -s tests -v
python -B -m unittest discover -s forge -t . -v
python -B run.py selftest
git diff --check
```

- 新增稳定性测试：19/19 通过（41.906 秒）。包含三种工具协议的 unknown 停机、子任务写入后完成回执丢失、子模型故障、协议继承、无效参数、真实编辑及恢复，以及真实 Git 失败及超时注入。
- checkpoint 修改前的核心 `forge` unittest：229 项，228 通过、1 跳过，0 失败。跳过是 Windows 无符号链接权限。
- 第一轮 `tests` 全套：188 项，187 通过、1 跳过，0 失败；这是增加子 Agent 修复前的中间结果，不用它代替最终回归。
- checkpoint 修复前的再次全套 `tests`：193 项，2 项并发用例失败、4 个随后的清理错误、1 跳过。用例是 `test_two_agents_on_different_files_keep_state_and_writes` 和 `test_two_simultaneous_agents_conflict_without_losing_a_write`；工作线程超出测试等待期限，清理与仍运行的线程冲突。线程栈显示 Git checkpoint 子进程等待及工作区写锁等待。原基线独立运行开发执行 18 项通过（192.255 秒）；不能据此把当前失败直接归因于环境。诊断运行也复现超时，保留日志。
- checkpoint 修复后的首轮串行回归：`tests` 196 项，194 通过、1 HTTP 超时错误、1 跳过；`forge` 229 项，226 通过、2 HTTP 超时错误、1 跳过。之前两项开发执行并发用例已通过；HTTP 超时分别发生于 Gateway 同版本竞争（15 秒）、流式首包转发（2 秒）及配置权限边界（2 秒）。本轮不算通过。
- 2026-10-23 保持原超时阈值复测上述三个 HTTP 用例：3/3 通过，1.919 秒。没有更改 Gateway 的超时阈值来规避错误；单项重跑不能替代全量结果。
- 2026-10-23 最终完整回归：`tests` 196 项，195 通过、1 跳过，0 失败，85.144 秒；`forge` 229 项，228 通过、1 跳过，0 失败，22.931 秒。两项跳过均为宿主不能创建符号链接。代码及测试阈值与前一次串行运行相同；本次通过不抹除早期超时记录。
- Forge 全量 selftest：573/574。唯一失败 `global:readme-no-hardcoded-counts`，中文 README 的硬编码计数触发文档守卫；上一轮已在未修改主分支复现。本轮没有修改文档守卫以消除失败，也不宣称全量全绿。
- 日志：用户目录下 `forge-agent-audit-targeted.log`、`forge-agent-audit-core-final.log`、`forge-agent-audit-tests-final.log`、`forge-agent-audit-selftest-final.log`、`forge-agent-audit-concurrency-diagnostic.log`、`forge-agent-audit-concurrency-trace.log`、`forge-agent-audit-concurrency-baseline.log`。
- checkpoint 专项最终日志为 `forge-agent-audit-targeted-checkpoint.log`；整套最终串行运行写入 `forge-agent-audit-tests-verified.log`、`forge-agent-audit-core-verified.log`、`forge-agent-audit-selftest-verified.log`。保留较早失败记录，避免只呈现成功重跑。
- 10 月 23 日复测日志为 `forge-agent-audit-http-recheck.log`、`forge-agent-audit-tests-oct23.log`、`forge-agent-audit-core-oct23.log`。

新增闭环用例 `tests/test_agent_stability.py::test_agent_edit_diff_real_test_and_checkpoint_restore`：临时项目中创建 `VALUE = 1` → Agent 读取并改成 `VALUE = 2` → 验证记录的 Diff → 子进程执行 unittest 并断言退出码为 0 → 使用该动作 checkpoint 恢复并确认内容回到 `VALUE = 1`。所有文件均在独立临时目录，未操作用户项目文件。

## 接口及兼容性

- 保持 `Agent.run`、`ActionExecutor`、ExecutionContext、Gateway 和 EditorBridge 的现有公开 API，不增加新权限或协议版本。
- `RunReport.stopped` 增加实际使用的 `unknown_outcome`；已有 `report.as_result()` 返回 `outcome=unknown`、`execution_state=unknown`。GUI 可消费 EditorBridge 的同名公开字段，显示“检查文件/Diff/动作记录”，不将它当成可自动重试的普通失败。
- `subagent_result` 增加 `outcome/error_code/execution_state`；spawn 回执增加 `child_run_id/child_task_id/child_outcome`。失败子任务现在返回错误，依赖旧的无条件成功行为的调用方必须调整。
- `_raw` 与 `_truncated` 是解析器诊断标记；顶层使用这些标记的参数不会进入工具。`None` 作为空参数、完整的单引号/尾逗号修复、平铺参数继续兼容。
- 原生调用缺少 ID 时允许分配协议关联 ID；这不替代公共 action_id，不提供跨模型重新生成动作的 exactly-once 保证。重复 ID 的批次在全部动作执行前被拒绝。
- Git checkpoint 的失败现在显式阻止后续写工具；不会静默使用旧 HEAD。超时按每条命令 30 秒计算，非整个事务 30 秒。

## 剩余限制与后续验收

1. **P2：同步网络取消延迟。** `forge/model.py` 的网络读取及部分重试等待仍是同步操作（默认超时 120 秒）；当前取消在下一执行边界检查。本轮没有宣称可即时中断已经开始的网络/工具调用。后续应将取消信号贯穿 transport 等待，并验证取消后不再回退到另一供应商。
2. **P2：等待期间的取消。** 本轮为 Git 命令设置了超时并使快照失败阻断写入，但没有实现任意 Git 后代进程树的即时取消，也没有将工作区锁等待改成可取消获取；并发运行仍可能等待前一动作结束。没有通过扩大测试期限消除失败。
3. 模型推理质量、真实供应商限流/长响应、GUI 实机交互没有在本轮离线套件中量化；不能据此保证所有模型的每次请求都成功。
4. 未知副作用不会自动撤销；应检查 action_id 对应记录及文件 Diff，再明确决定新任务或恢复 checkpoint。外部进程、远端服务和任意插件无法普遍保证 exactly-once。
5. **P2：长时间回归中的时延波动。** 本轮确实出现过并发/Gateway 超时，后续单项原阈值复测通过。尚未建立 CPU、磁盘、请求排队和 Git 进程的完整性能采样，因此不能宣称已消除超时根因或保证所有负载下都满足测试时间上限。

修改文件：`forge/loop.py`、`forge/tool_adapter.py`、`forge/toolwire.py`、`forge/tools.py`、`forge/checkpoint.py`、`tests/test_agent_stability.py`、本报告。上一轮继承的修改另见 `reports/tool-invocation-stability-2026-10-22.md`。
