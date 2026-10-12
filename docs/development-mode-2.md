# Development Mode 2.0：执行闭环与状态一致性

日期：2026-10-12。基线：`5a27bf8`。
分支：`feature/development-mode-2`。
独立 worktree：`C:\Users\匡溯昀\forge-development-mode-2`。
未合并、未推送；本轮没有改写原始 `pledge-evolving` 工作目录。
结束时主工作区存在其他并行开发的 GUI/插件/工具改动，未触碰、未纳入本分支。

## 源码确认与边界

已检查 `forge/development/`、`forge-gui/development_mode/`、`forge/loop.py`、
`vscode_bridge.py`、`subagent.py`、`phase_models.py`、`tools.py`、`policy.py`、
`session.py`、`checkpoint.py`，以及 GUI 的 `sub_agent.py`、`config_model.py`、
`interaction_model.py` 和既有 workspace Git helpers。

现有正常能力保留：编辑器周期观察与实际写工具触发刷新、干净缓冲自动载入、
草稿冲突保留、比较/保守三方合并/明确重新载入、编码与原子保存；
不可变任务模型快照、实际子路由 factory、只读 Git adapter、通过原工具恢复检查点。
未另建编辑器、模型内核或调度器。

Development 使用 core 的同步嵌套 `spawn_subagent`。
General GUI 的并行子 Agent/集群是独立、无工具的模型请求，不接入开发文件写入链路。
没有修改 GUI 主框架、插件市场业务、Desktop Awareness、共享 Policy 或工具注册逻辑。

共享 Runtime 仅在 `forge/loop.py` 增加兼容接口与关联字段：

```python
Agent(..., cancellation_event=None, file_guard=None)
```

取消信号只由可信宿主设置，父子树共享信号；停止点位于新请求、新工具及模型返回后。
`file_guard` 提供 `protect(name, args, ctx)`、`record(name, args, result)`、`fork()`；
实际调用仍经过原 `registry.invoke`，不会放宽 Secret、Policy、工具曝光或子权限上限。
事件增加 `agent/depth/run_id/task_id`，工具结果通过 `action_id` 关联，复审通过
`review_id/message_id` 关联。字段为附加信息；未提供 hooks 的其他宿主仍走原调用路径。

## 本轮修改

| 文件 | 结果 |
| --- | --- |
| `forge/development/guard.py` | 每个 Agent 独立观察版本；同项目同进程串行文件操作；旧版本拒绝写入；读取期间外部变化拒绝登记；子 Agent 不继承父观察 |
| `forge/development/editor.py` | 人工保存与开发 Agent 共用项目锁，保留既有指纹/冲突/原子保存机制 |
| `forge/development/runtime.py` | 任务 ID、任务证据归档、取消、实际任务路由复审、冻结复审配置、所有临时 Agent 设置在 finally 恢复；独立测试/Diff 记录与恢复日志 |
| `forge/development/evidence.py` | 工具按 ID 关联，拒绝其他任务事件混入；真实测试退出码、复审与 Diff；恢复时未知工具/测试及中断 run 状态一致 |
| `forge/development/testing.py` | 人工明确授权的宿主进程执行；取消/超时/非零退出；有界输出；含任务 ID、执行 ID、argv、workspace 的脱敏 JSON 工件 |
| `forge/development/review.py` | 检查点恢复的工具详情补齐 action ID；原权限与版本验证保留 |
| `forge/loop.py` | 上述可选 hooks、树级取消、工具结果关联与复审 ID；取消后的模型返回不能执行新工具 |
| `forge-gui/development_mode/app.py` | 取消与执行测试入口，真实状态/退出码/复审展示，Diff 快照关联；仅成功写操作归因；切模式和恢复时撤销下次任务写入选择 |
| `tests/test_development_execution.py` | 18 项新增执行/并发/恢复/模型/隔离回归 |
| `forge-gui/development_mode/test_app.py` | 4 项新增 Tk 交互回归；既有真实写文件测试补上守卫要求的先读取步骤 |

恢复会创建新的续接 journal，立即保存真实对话和来源链接；原 journal 保持不变。
旧日志公共 rollout `session_id` 沿用 Session 的既有身份豁免，GUI 预览显示为
`rollout_id`；不会把公共身份登记成凭据并污染模型校验。HTTP session 凭据仍按原规则脱敏。
恢复不会复原写权限或自动重放未确定结果的操作。

## 真实闭环验收

机器记录：[`development-mode-2-acceptance.json`](development-mode-2-acceptance.json)。
入口：`ExecutionTests.test_true_execution_loop_and_recovery`。

1. 创建真实临时 Git 项目、初始提交、独立 home、Development task。
2. 实际 `DevelopmentRuntime → EditorBridge → Agent → ToolRegistry` 执行
   `read_file` 和 `write_file`，把 `value.py` 的 `VALUE = 1` 改为 `VALUE = 2`。
3. 打开的 `Document` 观察磁盘并自动刷新；真实 Git Diff 包含 `+VALUE = 2`，
   Diff 事件绑定相同 task ID。
4. 经可信宿主批准，启动真实 Python unittest 进程；执行一项断言 `VALUE == 2` 的测试，
   输出 `Ran 1 test` / `OK`，退出码 **0**，测试记录和工件绑定同一任务。
5. 检查真实输出及工件，预览并通过原 `write_file` 权限/版本检查恢复检查点，
   文件回到 `VALUE = 1`。
6. 读取会话恢复证据并持久化项目状态；历史测试结果可见，旧写入授权不恢复。

模型 I/O 为确定性的离线 transport；Agent、工具、文件、Git、测试子进程和恢复均为真实实现。
另有真实 Tk 写入刷新验证。主/子/审查角色分别实际请求 writer/code、child/small、
reviewer/review；没有仅检查界面标签。验收夹具清理临时目录，JSON 内临时路径属于历史记录，
完整输出和结果已内嵌，不要求原临时路径继续存在。

并发验收：两个独立 Runtime/Session 同时读取并写同一文件，只有一个旧版本写入成功，
另一个返回版本冲突；不同文件的并行任务均成功。另验证父观察在子写入后失效、
草稿不被 Agent 写入替换，以及父/子/复审/宿主测试的取消与后续任务可运行。

## 验证

最终相关测试：**245 通过、1 跳过**，共 246 项；重复运行未重复计数。

| 测试组 | 结果 |
| --- | --- |
| 三组 Development 核心测试，在同一进程运行 | 52 通过、1 Windows 符号链接权限跳过 |
| Development Tk 工作台及真实 Git adapter | 21 通过 |
| Secret architecture/adversarial/virtualization、read scope、execution review、phase models、task planning、EditorBridge | 160 通过 |
| runtime/failure integrity | 12 通过 |
| compileall、git diff --check | 通过 |

PowerShell 复现（Python 不在本机 PATH，使用已确认的实际安装路径）：

```powershell
$py = 'C:\Users\匡溯昀\AppData\Local\Programs\Python\Python312\python.exe'
$env:PYTHONPATH = "$PWD;$PWD\forge-gui"
$env:FORGE_ACCEPTANCE_OUTPUT = "$PWD\docs\development-mode-2-acceptance.json"
& $py -m unittest discover -s tests -p 'test_development_*.py' -q
& $py -m unittest development_mode.test_app development_mode.test_project -q
& $py -m unittest forge.test_secret_architecture forge.test_read_scope forge.test_execution_review forge.test_phase_models forge.test_task_planning forge.test_vscode_bridge forge.test_secret_adversarial forge.test_secret_virtualization -q
& $py -m unittest discover -s tests -p '*integrity.py' -q
& $py -m compileall -q forge/development forge/loop.py forge-gui/development_mode tests/test_development_execution.py
git diff --check
```

调试中，新增取消复审测试发现恢复宿主配置后默认复审误用初始路由，发生一次返回 401 的
认证请求，未成功调用模型。已修复为任务实际路由，并在新增核心测试中禁止 HttpTransport
外部模型请求。最终运行使用离线 transport/本机 HTTP fixtures；没有成功的付费模型验收。
组合测试还先失败暴露了公共日志身份误登记问题，修复后同进程 53 项通过/跳过如上。

## 已知限制

- 受保护 Agent 的 `shell_exec`、notebook/native 边界仍然拒绝。测试由用户明确批准的
  工作台宿主执行，具有当前账户的文件/网络权限，不宣称 OS 沙箱或 Secret-safe 原生能力。
  GUI 内置当前 Python 的 unittest；其他命令须由可信宿主调用 `execute_tests(argv, trusted=True)`。
  显式 Policy deny 仍优先；不会向测试子进程传递普通提供商凭据环境变量。
- Agent/复审取消是协作式，不能强制中断正在进行的同步 HTTP 或原工具；返回后停止新动作，
  已写文件保留，可查看 Diff/检查点恢复。宿主测试可终止普通进程树；自行脱离的子进程没有
  Windows Job/OS 隔离保证。测试针对实时工作区，不是不可变构建快照。
- core spawn 仍同步嵌套；并行文件写入验证使用独立 Runtime，不新增 General/IDE 调度器。
  文件锁限同进程同项目根；不合作的外部进程仍有最终指纹检查与替换之间的竞态，不能宣称 CAS。
- Agent 必须先通过文件工具读取已有文件再修改；既有 `_expected_revisions` 验证当前文件
  上限 512 KiB 不变。编辑器既有 2 MiB/编码支持保留。
  原有禁止受保护 Agent 改写宿主 Forge 执行代码/控制存储的边界同样保留。
- 测试工件输出最多 1 MiB，超出明确标记；GUI 预览最多 16 KiB。退出码真实，
  未提供通用结构化测试计数时 passed/failed 保持 null。现有 Agent 工具输出限制不扩大。
- 复审沿用原有无工具的已完成响应/代码审查 API，不宣称完整仓库审查。
  草稿仍仅保留内存，崩溃后不自动恢复未保存文本；恢复日志仍有 16 MiB 上限。
- Tk 创建真实窗口验证交互/几何；没有人工截图或真实系统 DPI 验收，未声称全仓库测试通过。

## 提交

实现提交：`77d4323` — `feat(development): close task execution and isolate runtime state`。
本交付文档与机器验收记录由后续独立文档提交保存。
