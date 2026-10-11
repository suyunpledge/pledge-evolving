# Development Mode 编程闭环增强

分支：`feature/development-loop`。独立 worktree：`C:\Users\匡溯昀\forge-development-loop`。
初始基线为主分支 `39156ae`；开发期间检查了 `c59a66a` 的导航更新，与本轮文件没有重叠。
不修改通用 GUI、导航、连接器、插件加载、知识库、演化或 Gateway。

## 架构及授权接口

沿用 Tkinter/ttk 工作台、EditorBridge、现有 Agent.run/spawn、ToolRegistry、Policy、SecretScope、Session 和 CheckpointStore。
不增加 Agent 内核、命令执行器或第三方依赖。

经用户确认，共享 `forge/loop.py` 只增加两个可选的可信宿主接口：

```python
Agent(..., child_router_factory=None, child_observer=None)
```

- factory 接收实际 child name、已经收紧的 child Policy、继承路由，返回提供 complete() 的路由。无效配置拒绝启动，不消耗 spawn budget，不偷偷回退。
- observer 接收已经过 Secret 防护的真实 child events。启用时事件携带 agent/depth/run_id；默认 None 保持原有调用路径和事件协议。
- 原有深度、预算、dispatch、权限、独立工具激活、异常处理和父任务记账路径保留。回归覆盖了观察器异常以及深度/预算拒绝。
- 回滚仍通过原有 write_file 与版本检查；未调用可绕过 Policy 的 shadow checkout。

曾怀疑 ToolRegistry 存在 CRLF 误判，用户允许最小换行修复。进一步测试证实实际是测试复用已注册 Secret 字符串导致正确拒绝，因此 **没有修改 forge/tools.py**。CRLF/Secret 对照测试保留。回滚适配层规范化 LF/CRLF，防止 Windows 写成 CRCRLF。

## 实现

| 范围 | 行为 | 安全及真实性边界 |
| --- | --- | --- |
| 编辑器联动 | 工具真实写入后及周期检查从磁盘读取；干净缓冲自动刷新；草稿保留并标记冲突 | 工作线程只读观察，Tk 线程才合并状态；对象身份和打开版本检查拒绝关闭标签/保存之后的迟到读取 |
| 文件审查菜单 | 磁盘与草稿 Diff、保守三方合并、检查点审查 | 重叠编辑拒绝；合并只修改内存，用户保存时再次检查指纹 |
| Agent 附件 | 发送时取最新已附加编辑器缓冲，多文件可附加 | 附加后的新编辑不会丢失；仍由 EditorBridge 虚拟化 Secret |
| 任务工作流 | 展示真实计划、workflow_state、工具步骤、子 Agent 状态、实际模型用量事件 | 计划不自动勾选；没有工具/测试事件就不制造进度或通过数；详情仍保留原日志 |
| 子任务 | 用户请求通过原 spawn_subagent 分工；独立模型设置绑定真实名称 | 发出请求不等于成功启动；真实事件到达才显示运行；子任务不获得额外权限 |
| 模型角色 | 父模型显式选择 > Agent 配置 > 开发角色 > 全局路由；子模型显式任务指定 > 实际名称配置 > 宿主子路由 > 子 Agent 默认 > 父路由 | 在任务边界冻结配置，运行中修改只影响后续任务；仅允许已配置模型；General 恢复宿主 hooks |
| Git | combined / working / staged，未跟踪文件新增差异；打开仓库子目录时正确限制路径 | 复用固定参数 Git helper；不变更 index、仓库或权限；预览 redaction；文件操作来源只来自真实事件 |
| 检查点 | 选择真实检查点 → 查看单文件差异 → 写入授权 → 明确确认 → 版本验证 → 原工具写入 | 阻止未保存草稿被覆盖；恢复前沿用原 checkpoint 机制备份；只读、Secret、越界和过期预览均不放宽 |
| 长任务恢复 | 有上限地读取旧日志，恢复真实父对话及事件视图；下一任务写 development_resume 来源链接 | 不修剪旧日志；末尾不完整写入标为不确定；完整坏行拒绝；子对话不混入父上下文；未知动作不重放 |
| 集成运行目录 | 项目内的宿主布局配置继续保留；运行数据库、凭据和 checkpoint 放到项目外独立 home | 不共享 General 或其他 worktree 的运行目录；旧运行数据不自动迁移 |
| GUI 性能 | 队列每轮有时间预算；任务视图限制展示数量；大事件及恢复预览有上限 | 完整 Session 不删除；预览截断明确提示；手动文件刷新不增加重复周期定时器 |

## 使用和集成

保留原主 GUI 的 DevelopmentWorkbench 导入及构造方式，无须修改共享导航。
集成负责人可审查本分支后 cherry-pick 提交；本轮不自动合并、推送或打包。

独立启动：

```powershell
python forge-gui/run_development.py --project <项目目录> --home <独立配置目录>
```

不指定 home 时，默认使用 worktree 名称区分的项目外目录 `.forge-development-<worktree-name>`。
当集成宿主把 ModeState 放在项目内时，运行目录位于项目父目录的 `.forge-development-<配置路径哈希>`。
布局/角色仍保存原 development.json；运行日志的恢复对话框使用实际 runtime.home。
Provider/Key 继续通过现有 Forge setup 配置到该独立 home；不读取或复制 General 凭据，不自动迁移旧目录。

子 Agent 默认模型在“模型角色”配置；“独立模型”支持具体名称如 `forge/sub1`。
现有 core 使用深度生成 child name，同深度后续 child 复用名称；任务视图另外保留 run_id，独立配置按名称生效。
“请求子 Agent”仍依赖父模型实际选择原 spawn 工具；不会建立绕过工具、计费和权限的旁路。

## 当前明确限制

1. Secret-protected Agent 的原有 ToolRegistry 会直接拒绝 shell_exec；仓库中没有可复用的受保护测试执行器。本轮没有取消 isolated、开放 shell 或执行任意项目脚本。测试面板显示真实拒绝及真实命令记录，不能保证目前 IDE 能独立完成执行测试环节。
   后续需 Runtime 负责人提供经过 Policy/approval 的测试执行 capability、真实退出码/结构化测试结果、输出 artifact，以及限制文件系统/网络/凭据的执行隔离。先审查该接口，再在 IDE 订阅其事件。
2. 原有 ToolResult 事件输出有长度限制，完整进程输出需要原 Runtime 的 artifact 接口；IDE 不伪造遗漏日志。
3. 单文件检查点恢复目前支持 ≤512 KiB 的 UTF-8 LF/CRLF 文本；写入沿用原工具的系统换行。BOM、UTF-16、二进制和孤立 CR 需使用编辑器/原维护工具处理。普通编辑器仍支持原有编码与换行保存。
4. 原公开 report.usage 是父请求的用量；原 ledger 合并子任务总 tokens，不改变计账协议。异模型子任务的模型级价格归属仍受原 ledger 的 residual 估算限制，不把它描述为完整厂商账单。真实 child usage/model 留在事件中，可据此独立核实。
5. 文件指纹保护不能锁住不合作外部程序在最后检查与替换之间的极小竞态；不存在宣称 OS 沙箱的保证。
6. 没有做付费厂商联网请求。Windows 原生截图工具初始化失败（os error 3）；Tk 自动化创建真实窗口并验证交互/几何，不能替代人工视觉验收或真实 Windows 系统 DPI 切换。

## 验证入口

```powershell
$env:PYTHONPATH="$PWD;$PWD\forge-gui"
python -m unittest discover -s tests -p test_development_loop.py -q
python -m unittest discover -s tests -p test_development_mode.py -q
python -m unittest development_mode.test_project -q
python -m unittest development_mode.test_app -v
python -m unittest forge.test_secret_architecture forge.test_read_scope forge.test_execution_review forge.test_phase_models forge.test_task_planning forge.test_vscode_bridge -q
```

使用临时 project/home、离线模型 transport 和本机测试 HTTP fixture，不访问真实账户。
新增核心测试在初次实现前经历模块缺失/旧恢复不支持末尾损坏的失败；GUI 写入测试在功能接入完成前失败，之后补充并发、草稿、权限与实际执行用例。
Tk pump 有界处理 Tcl/Windows 事件，避免 update() 在持续事件流下不返回，使超时断言真正可执行；未修改全局测试设施。

本轮最终执行结果：

| 测试组 | 结果 |
| --- | --- |
| 新增核心 Development 闭环回归 | 17 通过，含 JSON 转义敏感字段的先失败后修复测试 |
| 原有 Development Mode 回归 | 17 通过、1 符号链接权限跳过 |
| 真实 Git adapter 回归 | 3 通过 |
| 实际 Tk 窗口交互/几何回归 | 14 通过，最终整组 24.405 秒 |
| 既有 Secret/读取范围/执行复审/模型阶段/规划/EditorBridge 边界回归 | 103 通过 |
| compileall、启动参数、git diff --check | 通过 |

合计相关测试 154 通过、1 跳过；这不是声称全仓库所有测试均已运行。
早期 Tk 测试使用无界 update()，在本机持续 Windows/Tcl 事件流期间长时间不返回，已中止诊断运行；最终有界事件驱动整组完成。没有把中止的测试计入成功次数。

## 修改文件

- `forge/loop.py`：两个批准的兼容接口。
- `forge/development/editor.py`：真实外部版本、冲突和三方合并。
- `forge/development/runtime.py`：模型冻结与实际子路由、恢复链接、独立运行目录、检查点适配。
- `forge/development/state.py`：向后兼容的 agent_models 与不可变任务快照。
- `forge/development/evidence.py`：真实执行事件投影。
- `forge/development/review.py`：通过既有工具权限恢复单文件。
- `forge-gui/development_mode/app.py`：上述工作台交互与队列反馈。
- `forge-gui/development_mode/project.py`：真实 Git 视图和子目录路径。
- `forge-gui/run_development.py`：避免不同 worktree 默认共享运行目录。
- `tests/test_development_loop.py`：核心新回归。
- `forge-gui/development_mode/test_app.py`：实际 Tk 行为回归。
- `forge-gui/development_mode/test_project.py`：真实临时 Git 回归。
- `docs/development-loop.md`：本交付及集成说明。
