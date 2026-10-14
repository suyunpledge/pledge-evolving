# Coding Agent UX · Phase 1

基线：main `51f6f82bd6c9b09c74b710a11121449f16a2be7c`，包含 RC1；工作树 `forge-coding-agent-ux`，分支 `feature/coding-agent-ux`。主工作树及 `forge-coding-editor-ux` 检查时无未提交改动。不推送，不合并。

## 源码审查

- `forge-gui/development_mode/app.py:199`：现有 Agent 栏为对话、任务/子 Agent、默认隐藏完整事件；任务、模型、权限控制已经可用。对话使用纯 Text，没有 Markdown 样式、文件链接、工具摘要、Diff 卡或重试入口。`send:803` 的计时动画只是存活提示，本轮改为真实状态，避免定时覆盖状态。`render_evidence:867` 仅按行数追加导致已有行状态变化不能及时更新，必须修正。
- `forge/development/runtime.py:60`：EditorBridge.observe → runtime._emit → TaskEvidence.apply → emit → app.enqueue_event → 40ms _drain → handle_event。工作线程不调用 Tk。Session 是持久证据；runtime._publish 先写日志再分发。本轮沿用此链，不新增执行器/事件总线。
- `state.py:139/163` 已提供具体子 Agent 模型与冻结 TaskSnapshot；默认/复杂/简单/复审/子 Agent 角色齐备。runtime.run:193 调用现有 child_router_factory，实际 spawn 仍由 loop.py 完成。修改设置只作用于下次任务。缺少运行时模型来源、实际权限及身份详情的 UI 展示。
- `evidence.py` 保存 actions/runs/tests/diffs/reviews，但 agents 按名称覆盖跨运行信息，workflow 更新也丢失 usage 模型字段；未投影 parent_run_id、子任务、审查 Agent、unknown outcome 与 Diff 状态。
- `guard.py` 已检查实际观察版本和读后写；RC1 ToolRegistry 提供跨进程文件锁。没有每次修改的安全文本 Diff 元数据。`runtime.inspect_diff` 已采集 Git 工作区实况，但包括其他来源，不能当成 Agent 独占修改。
- `runtime.checkpoint_preview/restore_checkpoint` 与 `app.restore_reviewed_checkpoint` 已有明确确认、dirty buffer 拒绝、权限与版本保护；复用而非另写回滚逻辑。新文件/删除/过大或非 UTF-8 文件的检查点恢复有现有限制。
- `chat_widgets.py` 已有 Markdown parse_blocks/inline_segments，可只读消费解析接口。文件链接 handler 为模块全局，不能覆盖 Work Mode handler；新视图使用实例级文件链接回调。

## M1–M4 实施边界

1. 新增 `development_mode/agent_panel.py`，包含三标签、实例级 Markdown 对话、折叠工具详情、任务投影、Agent 树/详情、Diff 卡与 Agent Events 内容组件；使用 C/FONT tokens，无第三方依赖。
2. app.py 仅修改 `_build_agent/send/handle_event/render_evidence/agent_model_settings`，新增 Agent 专用方法。保留现有控件兼容属性、日志 Text 与恢复路径，复用现有安全文件打开/回滚入口。每次提交列出方法名。
3. `evidence.py` 扩展真实事件投影、稳定 run 身份、变更卡状态；`guard.py` 提供受限、Secret 脱敏的每次实际写入前后 Diff；`runtime.py` 提供模型/权限来源事件和 Git 版本核实，全部使用已有日志链。
4. 底部 Agent Events 在 `_build_agent` 中调用可挂载的内容组件，消费已存在的 output Notebook；不改 `_build_editor`。向 WorkBuddy 提供 `mount_events(notebook)` 接口。事件源仍为 Session/TaskEvidence，不建立第二流。

## 风险与仲裁点

- app.py 为共享热点：保护 `_build/_build_project/_build_editor/apply_layout/place_sashes/_panes_resized/capture_layout/toggle_ide` 原文，最终 AST 方法比较验证。禁止修改 workspace/gui_theme/chat_widgets。
- Conversation/Tasks/Agents 替换旧两标签，旧 W5 标签文本测试需按新用户任务更新；事件改底部标签，旧开发者开关保留为底部显示开关。运行/恢复/权限等行为测试保留。
- 高频工具事件合并到一次 idle 视图更新，不能丢日志；diff/git 读取留在工作线程。计划只显示计划文本，不推断阶段完成。
- 运行时可能没有完整子 Agent 身份事件；使用真实 child workflow + parent_run_id 建树，不伪造不存在的子 Agent。独立模型与继承来源必须区分当前运行冻结配置和下次配置。
- Git 提交状态通过实际文件对象 hash 与 Git HEAD 比较，仅工作区干净不能推断已提交。历史 Diff 卡标记版本已变化时禁止直接恢复。
- 实际执行测试需明确人类批准项目代码（沿用原流程），工具拒绝不算测试通过。自动 Tk 验证不等于人工可见的无闪烁验收，最终单列真机步骤。

## 验收计划

新增纯投影/实际文件模型隔离测试、真实 Tk 三标签/工具折叠/链接/Diff 按钮/主子审查树/20 次切换测试；运行 tests 全集、forge 全集、dev test_app、workspace wiring 与完整 Forge selftest，记录数量、环境、跳过及失败。只使用离线模型夹具验证真实 Runtime/工具/Session/Git，不宣称外部模型服务调用通过。
