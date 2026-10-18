# Coding Agent 输入与任务发布链路修复

日期：2026-10-18。分支：`feature/coding-submit-feedback`。
实施基线：`68f666d`，包含上一轮独立后端提交；主工作区 HEAD 为 `14b266a`。
工作树：`C:/Users/匡溯昀/forge-backend-workflow`。

## 真实复现

用户确认问题入口为 **Coding Mode 的 Agent 输入框**。
检查实际控件绑定、EditorBridge 模型目录、ModeState 快照与 DevelopmentRuntime，
并通过真实 Tk 按钮 `.invoke()` 捕获 `report_callback_exception`，首轮 7 项均失败：

- 选择主模型后发送，回调报错 `Phase model must be [configured provider, model] or empty`。
- 主模型下拉未装入异步加载的实际模型目录。
- Agent 未就绪时只有状态栏文字，对话区没有拒绝原因。
- 正在执行时发送按钮为 disabled，现有排队逻辑无法通过按钮触发。
- 界面显示 workspace-write，执行快照实际为 read-only。
- 角色下拉更新 agent_role，没有更新 Runtime 读取的 task_role。
- 已失效的模型选择被静默回落为全局模型，输入被清空。

其中模型错误的根因：`catalog_rows[i]['id']` 是 JSON 编码的标识字符串，
原 `_dispatch` 直接将其交给 `ModeState.task_snapshot(explicit_model=...)`；后者要求
`(provider, model)` 二元组。异常发生在 worker 创建前，被 Tk 回调报告通道接住，
因此表现为“点了发送但没有创建任务”。

## 修复行为

- 用宿主 `runtime.catalog[id]` 解析真实模型二元组；目录变化时明确拒绝，不悄悄换模型。
- RuntimeContext 增加公开同步方法，异步更新真实模型选项、角色及权限显示。
- Coding 宿主仅提供已有的 read-only / workspace-write 合约；未接线的高级 Mode、
  ReadScope、SecretScope、fallback、MOA 作为宿主控制项禁用，并提供解释。
  不为使控件“看起来有效”而授予 bypass、危险沙箱或 Secret 共享。
- 提交立即显示反馈；未就绪、无效模型、超限描述、排队已满和 worker 启动失败均在
  对话页明确显示原因，并保留未发送草稿。
- 发送通知不改写当前执行任务的状态条；排队/拒绝另一份草稿不代表运行中的 Agent 出错。
- 执行中发送按钮保持可用，显示“排队发送”；仍使用现有上限 3 的 FIFO。
- 在点击发送时冻结模型、权限、角色、规划和附加文件，排队期间后续设置不反写该任务。
- 队列自动发布保留当前新草稿；队列消耗用 workbench 自己管理的计时器，不再串用裸 after。
- UI 已接收、Runtime 尚未占 lane 的间隙也算已提交，连续点击不会重复启动可变 Agent。
- `DevelopmentRuntime.run(..., cancellation=None)` 兼容增加宿主取消 token。
  取消可覆盖启动前间隙，并清除排队任务；不把“请求取消”说成任意 handler 已经退出。
- 排队或提交中不能切换项目/会话，也不能启动会争用同一 lane 的辅助执行操作。
- 合并刷新回调改用已存在的 `later()`，窗口销毁时取消自己的回调，保留宿主计时器。

## 文件与边界

| 文件 | 内容 |
|---|---|
| `forge-gui/development_mode/app.py` | 输入验证、模型解析、提交反馈、冻结队列、取消及上下文切换守卫 |
| `forge-gui/development_mode/policy_widgets.py` | 公开同步方法、真实目录选项、宿主管理控件状态 |
| `forge/development/runtime.py` | 可选外部取消 token，继续复用原 Agent/Bridge/执行器 |
| `forge-gui/development_mode/test_submission.py` | 17 项真实 Tk、模型传输、文件修改及竞争验证 |
| `forge-gui/development_mode/test_app.py` | 用实际任务完成取代“按钮可用即完成”的旧等待条件 |
| `forge-gui/test_coding_visual_integration.py` | 将历史视觉提交的执行边界检查限定到该原始提交，继续保留当前布局保护测试 |
| `tools/verify_coding_send.py` | 串行 GUI/后端/selftest 验收与日志/源码哈希 |
| `docs/coding-submission-api.md` | ZCode/宿主接线与状态归属说明 |
| `reports/coding-send-validation.json` / `coding-send-integration.patch` | 原始测试来源、最终验证、源码哈希与经检查的最小接入补丁 |

没有修改 GUI 主框架、主题 Token、面板布局、插件市场或 Desktop Awareness。
原保护清单中的八个布局方法在实施分支保持不变，既有 ownership 回归通过。

## 验收口径

最终数量与原始日志由相邻 `coding-send-validation.json` 记录。真实发送验证从 Tk 的
发送按钮开始，经 DevelopmentRuntime、EditorBridge、Agent、ActionExecutor，真实
read_file / write_file 将 `one.py` 改为 `print(2)`，同时验证三次模型请求都使用所选模型。
模型传输为离线可控 transport，没有调用付费供应商。

专项覆盖：原始七项复现、队列满、草稿保留、冻结配置、启动前取消、worker 失败、
请求超限、不支持的权限、项目/会话隔离、关闭时回调清理。已有 Agent 编辑、Diff、
恢复、测试和模式切换回归继续运行；全量后端结果与指定 GUI 范围分开记录。

执行环境：Windows 11 `10.0.26200`，Python `3.12.10`，Tk `8.6.15`，
PyYAML `6.0.3`。使用系统 Python、`-B` 和 UTF-8 日志，GUI 套件串行执行。

| 范围 | 通过 | 跳过 | 失败 / 错误 |
|---|---:|---:|---:|
| 新增发送 17 + 既有 workbench / Agent 39 | 56 | 0 | 0 / 0 |
| Coding 布局 | 64 | 0 | 0 / 0 |
| Workspace / 视觉集成 | 27 | 0 | 0 / 0 |
| `tests/` 全量发现 | 156 | 1 | 0 / 0 |
| `forge/` 全量发现 | 228 | 1 | 0 / 0 |
| Forge selftest | 574 | 0 | 0 |
| 指定 GUI 适配器回归 | 120 | 0 | 0 / 0 |
| 当前主线内核 + 并行 GUI 快照接入 | 17 | 0 | 0 / 0 |
| 上述快照的 hosted 嵌入入口 | 4 | 0 | 0 / 0 |

各行按运行范围列出，不相加宣称互不重复的用例数，也不把指定 GUI 模块称为全仓所有 GUI 测试。
两项跳过对应 Windows 账户/平台符号链接限制。日志根目录：
`C:/Users/匡溯昀/forge-coding-send-validation`；兼容预览日志在
`C:/Users/匡溯昀/forge-coding-send-integration-preview`。

测试中的失败与修正均保留记录：

- 首轮 7 个新测试先复现失败，再进行修复。
- 既有 App 测试用“发送按钮恢复 normal”判断任务完成。允许排队后该条件不再有效，
  改为等待实际 worker 和已接收任务结束，没有放宽结果断言。
- 首次完整驱动退出码为 **1**，原因是历史视觉测试将当前 `send()` 与视觉改造前版本
  比较，禁止任何后续业务修复。现在该测试比较原视觉提交 `120deae` 与其基准
  `ddace89`；当前八个布局/事件方法仍由 OwnershipTests 与布局套件保护。
  修正后重新运行全部 27 项相关测试，均通过。
- 最终移除发送通知对可选 `set_agent_status` 的调用，增加运行中状态隔离测试后，
  重跑相关 GUI 56 项、快照接入 17 项及 hosted 4 项，均通过。
  全量后端/selftest 与布局运行后的生产代码变化仅是该通知钩子移除；
  全量后端源码和嵌套 Runtime 的哈希均再次核对一致。并未将原始失败驱动改写成一次成功。

GUI 56 项日志中仍有 **2 条** Tk `invalid command name "…<lambda>"` 延迟回调警告，
无失败断言。此次修复覆盖的刷新与队列回调清理测试通过，尚不能据此声称全部 Tk
组件的销毁生命周期警告都已消除。
`forge/` 全量日志还包含既有子进程管道未关闭的 `ResourceWarning`，测试退出码为 0；
本轮没有扩展修改其他模块的进程资源回收逻辑。

可复跑入口：

```powershell
python -B tools/verify_coding_send.py --output C:/Users/匡溯昀/forge-coding-send-recheck
```

该脚本同时运行指定 GUI 套件与既有完整 RC1 验证；原始命令、时长、日志和 SHA-256
均记录在验证 JSON，预览测试采用当前主线内核，而非依赖上一轮独立后端提交。

## 当前工作区与接入

主工作区有并行未提交 GUI 修改，尤其 app.py 的 `_dispatch` 新增了状态条接线。
本轮没有覆盖这些内容，也没有合并主分支。另准备了一份只读集成预览：将本次业务
修复与该 GUI 快照合并，保留其八个布局方法及新增状态条/编辑器方法，并使用当前
主线 Python 内核验证最小热修复所需接口。预览的源码哈希、冲突处理与测试结果单独归档。

修复在磁盘独立工作树中完成，不代表已经运行的 Python GUI 进程已加载新代码。
最小修复不要求先合并上一轮 HTTP 客户端后端改动。接入时应先检查并行修改是否变化，
再应用经验证的最小补丁；有活动任务时不要为了加载修复而直接终止 GUI。

`coding-send-integration.patch` 只包含三个生产文件，对当前并行 GUI 快照做过三方
合并。在 `_dispatch` 有一处真实冲突，已合并草稿保留与原有状态条接线；其他
并行新增方法及八个保护方法均保持一致。快照哈希在测试完成后重新检查，无变化；
`git apply --check --ignore-space-change` 通过。此检查没有应用补丁。

实施提交：`8b6e4226364bbdd2f1ba9ac4bdde73dae475fec4`
（`fix(coding): make task submission visible and reliable`）。报告与证据由紧随其后的
文档提交归档，可用 `git log -2 --oneline feature/coding-submit-feedback` 查询。
未推送、未合并主分支；主工作区并行未提交改动保留。

## 限制

- 此次验证不证明实际供应商网络、密钥、额度可用；它证明所选模型和任务执行链路已贯通。
- 队列仍是当前工作台内存状态，不提供应用崩溃后的草稿/排队任务自动恢复。
- 只读权限不会写文件；需要用户明确选择工作区写入。权限选择只用于后续任务。
- 任意模型/原生调用只支持合作式取消；已产生的文件修改不会自动撤销。
- 未引入新调度器、技术栈或任意 exactly-once 保证。
