# Coding Mode · Agent 工作流域交付

独立工作树：`C:\Users\匡溯昀\forge-coding-agent-ux`；分支：`feature/coding-agent-ux`；实施基线：`51f6f82bd6c9b09c74b710a11121449f16a2be7c`。未推送、未合并主线。

2026-10-13 验收勘误：下文「502 通过、2 跳过」仅是第一轮单次运行记录，不能证明重复运行稳定通过。集成方独立复验发现真实 F-1 竞态：旧卡片拒绝恢复提示被延迟项目扫描覆盖；另有历史基线可复现的 Tcl 安装抖动，两者分别记录。F-1 已在本分支修复，重新申报口径为拒绝恢复用例独立连跑 10/10、指定四套件连续两轮各 64/64，均 0 失败、0 错误、0 跳过。修复方式、范围、完整命令与日志见 [F-1 修复验收报告](coding-agent-f1-fix.md) 及 `coding-agent-f1-validation.json`。

## 改造摘要

右侧拆分 Conversation / Tasks / Agents，底部挂载 Agent Events。Conversation 使用既有 Markdown 解析器和实例级文件链接，不改变 Work Mode 全局链接处理器；工具详情默认折叠，保留错误、结果、停止及安全重试。权限徽标监听已有权限变量，拒绝授权、模式切换与恢复时正确显示真实当前值，不改变权限裁决。Tasks 从真实执行事件更新，不模拟进度。Agents 按完整运行身份建树，展示实际模型、冻结配置来源、权限上限、暴露工具、执行历史和结果，包含真实 Review Agent。

保留 `spawn_subagent`、Development Runtime、角色/独立模型冻结机制和原 Session。新增的模型来源事件在任务边界/实际子模型工厂调用时写入同一日志。实际主/子/审查路由测试验证 writer → child → writer → reviewer 四次调用；没有外部模型服务调用。

文件版本保护新增脱敏写入前后 Diff 元数据，卡片绑定 task/run/action/checkpoint/版本。written 表示已写盘；committed 需 Git HEAD 对象与记录版本匹配；pending 只来自真实写工具审批需求，不把只读审批当修改。Revert 使用原检查点预览、明确确认、dirty buffer 拒绝和版本保护。未知结果不自动重试或恢复。

## 文件清单

| 文件 | 修改目的 |
|---|---|
| `forge/development/runtime.py` | 模型/权限来源证据，实际 Git 版本核实，恢复后更新原卡片版本状态 |
| `forge/development/evidence.py` | 保留模型与任务信息，run/parent 身份，Review、unknown/未执行/审批状态，变更卡投影 |
| `forge/development/guard.py` | 在已有版本保护中采集安全、有限大小的实际 Diff；不新增文件执行路径 |
| `forge-gui/development_mode/agent_panel.py` | 三标签、Markdown Text、工具折叠、Agent 树、Diff 卡、Agent Events 内容组件 |
| `forge-gui/development_mode/app.py` | Agent 专属挂载/事件/模型/卡片回调；保留已有安全文件与任务入口 |
| `forge-gui/development_mode/test_agent_panel.py` | 16 个真实 Tk/所有权测试 |
| `forge-gui/development_mode/test_app.py` | 按新标签和弹窗更新两项旧展示契约，异步布局断言等待真实映射完成 |
| `tests/test_coding_agent_ux.py` | 12 个真实 Runtime/Git/模型及证据边界测试 |
| `tools/verify_coding_agent_ux.py` | 串行六组回归，环境、命令、结果、日志和源码 hash |
| `docs/coding-agent-panel-api.md` | WorkBuddy 公共挂载接口、事件/版本字段、兼容与限制 |
| `reports/coding-agent-ux-phase1.md` | 实施前审查，已先提交 |
| `reports/coding-agent-ux-validation.json` | 最终测试记录、hash、GUI 修正后复验说明 |
| `reports/coding-agent-ux-closed-loop.json` | 实际执行、Diff、测试、恢复结果 |
| `reports/coding-agent-ux-delivery.md` | 本交付报告 |

## 四档功能验收

### 已实现且测试通过

- M1：真实发送/接收、Markdown/代码块/受限文件引用、折叠工具摘要、错误/结果/会话恢复、取消请求、重试保留新草稿。任务状态变化更新原行；计划不伪造阶段完成。
- M1 Agents：主/子/审查的 run 身份、parent 树、模型、任务、状态、权限/工具表面、历史/输出。多运行同名不合并；下一任务清理树不产生 Tcl 删除错误。
- M2：复用冻结 TaskSnapshot/agent_models/角色配置；主与 Review 使用角色弹窗，具体子 Agent 独立弹窗；继承/独立来源展示，失效模型反馈；真实路由与持久化证据一致，运行中设置不污染当前任务。未显示密钥正文。
- M3：实际写盘 +1/-1 文本 Diff、真实 HEAD 提交核实、三状态按钮分离；新磁盘版本拒绝旧卡片恢复，已有草稿/审批保护不变；Secret 脱敏、不可安全预览明确标记。窄卡片操作按行换行。
- M4：Agent Events 底部内容与既有事件链一致、只挂载一次、隐藏/恢复不丢历史；工作线程通过既有队列更新 Tk。
- 后台任务在 20 次 IDE 切换和 10 次 general/development 往返后保持同一 Runtime，未收到取消；另有既有 Work/Coding Surface 往返测试通过。
- 保护方法源码对比通过：`_build/_build_project/_build_editor/apply_layout/place_sashes/_panes_resized/capture_layout/toggle_ide` 与基线原文一致（Git CRLF 规范化后）。`workspace/gui_theme/chat_widgets/forge.loop` 未修改。布局增量挂载、防重入、Configure 回声抑制和 2px 容差保留。

### 已实现但未真机人工验证

- 实际窗口可见的滚动、文字密度、中文 IME 使用感、拖拽面板和连续点击时的视觉闪烁。
- 在真实外部模型账号配置下的交互体验。测试使用离线确定性传输，调用的 Agent、工具、Session、Git 和子进程是真实实现。

### 未实现

- 没有新增宿主等待审批队列或 Apply 补丁执行器。当前非交互 EditorBridge 的 ASK 会拒绝；只有真实 `requires_approval` 才显示 pending。处理入口提示使用既有权限确认并提交新任务，不能声称批准旧动作。
- 未重写子 Agent 系统、GUI 主框架或原恢复执行路径；未做 WorkBuddy 编辑器/布局域改造。

### 已知限制

- Diff 数字来自规范化、脱敏文本预览；Secret 内容变化可能被隐藏，不等于字节 Diff。仅安全 UTF-8 文本预览（512 KiB），每文件 16 Ki 字符、每动作总计 64 Ki 字符，截断明确提示。
- 新建文件/缺少检查点内容不提供直接 Revert；其他编码/大文件沿用已有检查点恢复限制。仅 Forge 协作锁不能阻止外部编辑器，恢复前仍比较磁盘版本。
- Git 提交分类是保守的字节版本证据；CRLF/clean filter 转换、大对象、删除提交等无法精确确认时保持 written，不能把清洁工作区当提交。
- 暴露工具清单不等于无条件可执行；权限、激活、宿主能力及逐动作裁决仍以 Runtime 为准。自定义写工具若没有 file_changes 元数据，仅显示真实工具结果，不编造 Diff 卡。
- 取消是协作式请求，不承诺立刻中断任意模型请求/工具。结果未知时禁止自动重放，不声称 exactly-once。
- 当前任务面板聚焦最新任务，历史通过 Conversation/Session 恢复。长会话仍受既有日志恢复大小限制。
- 本轮验证本分支 `51f6f82` 基线上的最终代码；执行期间主线新增 `81acf1b` 的 review 布局白名单（仅 state.py），本分支未修改 state.py，未合并测试该新布局预设。WorkBuddy 分支检查时仅新增 Phase 1 报告，不覆盖其工作区。

## 测试报告

环境：Windows 11 `10.0.26200`，完整 Python `3.12.10`，系统 Tk；隐藏真实 Tk 测试窗口。所有六组退出码 0。

| 测试组 | 通过 / 总数 | 跳过 |
|---|---:|---:|
| tests/ 全集 | 130 / 131 | 1（平台 symlink 条件） |
| forge/ 全集 | 228 / 229 | 1（平台 symlink 条件） |
| Development GUI（app + agent_panel + project） | 42 / 42 | 0 |
| Workspace wiring | 12 / 12 | 0 |
| 7 个共享 GUI/插件适配模块 | 90 / 90 | 0 |
| 完整 Forge selftest | 574 / 574 checks | 0 |

Unittest 合计 504：502 通过、2 跳过，无失败/错误；另外 selftest 574 checks。新增 28 项（12 Runtime/投影 + 16 GUI/所有权）。这是上述测试范围的完整结果，不宣称整个 GUI tests 目录均已运行。

复现命令：

```powershell
& 'C:\Users\匡溯昀\AppData\Local\Programs\Python\Python312\python.exe' -B tools/verify_coding_agent_ux.py --output 'C:\Users\匡溯昀\forge-coding-agent-ux-recheck'
```

最终日志目录 `C:\Users\匡溯昀\forge-coding-agent-ux-final-validation`。最后仅 retry 回调及权限徽标监听做 GUI 调整，分别复验 GUI 全组 41、42 项；JSON 保留此前记录与调整前 hash，最终源码 hash 已比对一致，Runtime/共享/core 测试的源代码未再变化。

早期失败已纠正：tests 不是可直接导入的包，初始命令导入失败，改用正确 discovery/path；Secret 断言与实际 REDACTED_SECRET 标记不符，改为检验不泄露实际凭据；旧 W5 两标签测试按用户要求改为三标签/底部事件；Tk idle 合并刷新需要等待真实条件，测试不再把一次 dooneevent 当刷新完成；窄栏空间不足通过 Agent 栏内 footer/弹窗/卡片换行处理，不修改布局框架。所有这些均有后续通过记录。

真实闭环任务 `75d3b741b9a04a798f16555d1c7ea4c0`：Agent read/write 将 VALUE=1 改为 2 → 实际 Git Diff → 实际 unittest 子进程退出 0 → 检查 TaskEvidence/artifact → 原检查点恢复 VALUE=1 → 从 Session 恢复真实历史。JSON 保存 Diff、测试输出、恢复结果和 36 条恢复事件；临时测试项目随后由测试清理，JSON 中的临时 artifact 路径不保证继续存在。

## 提交与集成

- `8f7e9c1`：Phase 1 审查先行；app.py 方法变更 none。
- `a38fb03`：Runtime/证据/版本 Diff 与 12 项测试；app.py 方法变更 none。
- `82c157c`：三标签、卡片、底部事件、GUI 测试与接线文档。
- `4fdd94e`：权限徽标监听与拒绝授权/模式重置测试；app.py 方法变更仅 `_build_agent`。

UI 提交已按任务书注明 app.py 修改方法：`_build_agent/_toggle_events_tab/send/render_evidence/handle_event/model_settings/agent_model_settings`。新增方法：`open_agent_file/view_agent_diff/revert_agent_change/resolve_agent_approval/retry_agent_task/refresh_agent_versions`。其他既有方法未改。

WorkBuddy 通过 `AgentPanel.mount_events(notebook)` 挂载内容，复用外层 output/output_views/documents/runtime。集成时按这些方法区段合并 app.py，不能整文件覆盖；保留主线 state.py 的 review 白名单。详细契约见 `docs/coding-agent-panel-api.md`。

人工验收步骤：在可信临时 Git 项目开启 Coding Mode，发送受限编辑任务；查看折叠工具详情、Tasks 与 Agents 实际模型；打开卡片 Diff，授权测试并核实退出码；编辑未保存草稿后尝试 Revert，应拒绝；关闭/保存草稿后审查并恢复；提交另一修改，核实 Git 版本后卡片显示 committed；任务运行中 Work/Coding 往返及 IDE 连点 20 次；在 100%/125%/150%/175%/200% DPI 下观察闪烁、按钮和滚动。未知动作仅检查，不点击自动重放。
