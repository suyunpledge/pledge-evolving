# Agent 工作流域接线契约

基线 `51f6f82`。组件不执行 Agent、不注册工具、不改变布局状态。

## WorkBuddy 的底部容器

当前 `_build_agent` 在 `_build_editor` 完成后挂载 Agent Events，不修改编辑器/布局方法：

```python
content = bench.agent_panel.mount_events(bench.output)
bench.events = content.text  # 原恢复/事件文本适配器兼容
```

`mount_events(notebook)` 同一组件仅挂载一次，返回 `AgentEvents(ttk.Frame)`。容器首次创建时调用即可，不要因面板隐藏、模式切换或 sash 变化重建。若 WorkBuddy 替换底部 Notebook，应在双方集成时先确定唯一挂载点：内容 `AgentEvents(new_notebook)`，统一由 `handle_event → append_event` 驱动，不能同时追加两次。

右侧 `AgentPanel` 内部只含「对话」「任务」「子 Agent」三个标签；「变更事件」仍是底部可挂载内容。保留任务清单与运行树的独立视图，避免窄栏的复合标签挤压内容。任务面板 `tasks_toolbar` 可放复审、恢复、测试的已有回调。它不调用 `apply_layout`，不监听工作台 `<Configure>`。Conversation 的 `<Configure>` 仅在宽度变化时调整卡片换行；无 after 循环。

## 公共证据与界面

`AgentPanel.render(TaskEvidence)` 是只读投影。主/子 Agent 行以 `(agent, run_id)` 唯一标识，以 `parent_run_id` 建树。Review 行来自真正的 `review_started/result/error/cancelled`，不制造常驻伪 Agent。工具详情默认折叠；计划显示计划文本，不能按计时或工具名称推断计划阶段完成。

`Conversation.message(role, text, timestamp=None, historical=False)` 消费现有 `chat_widgets.parse_blocks/inline_segments`；Text 标签呈现标题、粗体、列表、引用、代码块。灰色 hh:mm 使用真实事件时间（传入 epoch 秒）或实时消息显示时间；历史恢复通过 `history_source` 读取已获准恢复的 Session 原始 `ts`，不会用恢复时刻冒充历史时间。缺时间的旧记录显示「时间未记录」。读取上限与原恢复一致为 16 MiB，按原主对话顺序匹配最近 100 条消息，排除子 Agent；只补显示元数据，不发事件、不重放工具。文件链接调用实例的 `open_file` 回调，不改 Work Mode 全局 handler；工作台适配器仍通过 ProjectBoundary。旧恢复 `append(chat, '\nassistant\n...\n')` 自动走 Markdown。

第二轮新增可选回调 `new_conversation/history/settings/view_changes/history_source`：标题栏「AI Agent」与新对话/历史/设置图标复用共享图标和 tooltip。新对话只在 Runtime 及后台操作空闲时创建新的 Session，保留旧日志、Runtime、磁盘文件和输入草稿，清空对话上下文并重置为只读。

`PlanningCard` 显示当前任务规划级别、真实冻结权限与逐动作审批/拒绝状态；恢复卡片明确为历史授权、当前只读。原计划文本没有逐项执行事件，因此保持待办；工具步骤的 ☐/◐/☑ 分别源于 not_started/awaiting_approval、running、completed，失败及未知另行显示。任务完成不会把计划列表自动勾满。

规划卡片「查看变更」绑定该卡片的 TaskEvidence，并通过工作台已有 `view_agent_diff → output_views['Diff']` 打开安全预览；无变更证据时禁用。旧任务卡不会误显示新任务的 Diff。WorkBuddy M5 替换底部容器时保留此接口及 `output_views['Diff']` 适配器即可，Agent 域不创建或重建底部容器。

输入区 `TaskInput` 用独立 Label 显示占位，不写入正文；沿用 Enter/Shift+Enter/IME 处理。`agent` 角色映射既有 `default`，complex/simple 原样传入既有 TaskSnapshot；「创建子 Agent」沿用 `send(delegate=True)`，只有真实运行事件才出现子 Agent 行。模型徽标使用配置来源或实际 usage.model，不固定填入示例模型名。

所有控件读取 `gui_theme.C/FONT_*`。原 `_enter_key`、IME 守卫和安全打开/保存/恢复入口保留。主框架、workspace、gui_theme、chat_widgets 未修改。

## 事件与 Diff 数据

沿用 EditorBridge.observe → DevelopmentRuntime._emit → TaskEvidence.apply → GUI queue → handle_event。高频视图刷新合并为一次 idle；日志事件不合并、不丢弃。

- `development_agent_config`：`task_id, agent, selected_model, model_source, permission, tool_surface, role`。在任务边界或真实 child_router_factory 调用时冻结；model_source 为 inherited / independent task / independent role / independent / role / explicit / host configured。selected_model 为配置来源，usage.model 为实际调用证据。暴露工具不是无条件授权清单，每次调用仍经 Policy。
- `tool_completed.meta.file_changes[]`：实际写入版本的 `path, before_revision, revision, state='written', diff, added, removed, preview_only, diff_truncated`。外层带 task/run/action ID、checkpoint。安全 UTF-8 Diff 预览每文件最多 16 Ki 字符、每动作总计 64 Ki 字符；Secret 不进入预览。
- `development_change_state`：`task_id, action_id, path, state, commit, revision_changed`。`refresh_change_states()` 只读比较 Git HEAD 文件对象与记录版本；不执行 add/commit/checkout。缺少 Git、不同版本、对象大于 512 KiB 或 Git 转换导致不能证实时保守显示 written。
- Pending 卡只投影真正的写工具 `requires_approval` 结果；只读工具审批不产生修改卡。该状态未写入，不提供 Apply。用户通过现有权限确认后，重试产生新的任务与动作；未知结果必须先检查，不自动重放。

Diff Card 的 View Diff 查看该次写入安全快照；Open File 经过工作区边界；Revert 只对有现有文件检查点的 written 版本提供，通过 `checkpoint_preview → restore_reviewed_checkpoint → restore_checkpoint`。dirty buffer、权限不足、磁盘版本变化均拒绝恢复。committed 卡无直接 Revert，避免误导成 Git revert。

## 集成注意

保留外层 `output`、`output_views`、`documents`、`runtime` 与旧 Text 属性，以免恢复/编辑器域重接。不能从 GUI 回调直接调用 Agent.run、ToolRegistry.invoke，或新建第二个运行循环。

模式切换只改变可见性与后续任务权限；不销毁 Runtime、不取消后台任务。人工视觉验收仍需检查真实窗口、DPI 与 20 次连点；隐藏 Tk 自动测试不能证明没有可见闪烁。
