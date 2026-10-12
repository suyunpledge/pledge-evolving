# Coding Mode GUI 与 IDE 交互审查 — 2026-10-12

只读审查，未修改任何代码。范围：forge-gui 全部 GUI 模块、development_mode、
vscode-extension、client Web UI、visual_system 组件包及相关设计文档。
目标：为统一、可扩展的 Coding Workspace 提供界面结构方案、组件复用计划、
交互流程、问题清单与分阶段实施建议。

## 〇、现状基线（一句话）

"Coding Mode" 目前以 **3+1 个互不相通的实现并存**：嵌入式开发工作台
（development_mode/app.py，1160 行单类，自建一切）、右侧工作区面板
（workspace.py，文件树/代码/diff/预览/只读终端）、VSCode 扩展（侧栏对话 +
原生 SCM）、Web UI（任务交付面板，当前启动即 500）。设计层已有双工作空间
规范（docs/forge-visual-system-2.md）与 visual_system 组件包
（ModeSwitcher/WorkspaceDeck/NoticeBanner/PermissionPrompt + 契约），
**但均未接入生产主窗口**；Coding 执行闭环（取消/证据/测试授权）在未合并的
feature/development-mode-2 分支。

## 一、界面结构方案

### 1.1 目标架构（对齐 forge-visual-system-2.md §3，不改其契约）

```
┌────────────────────────── Forge 主窗口（既有 shell）──────────────────────────┐
│ 顶部应用栏：品牌 · ModeSwitcher（Work/Coding，宿主确认制） · 全局状态          │
│ ┌─ WorkspaceDeck（持久 sibling surface，切换仅 grid/grid_remove）──────────┐ │
│ │ Work Mode surface（现状保留：会话/任务/Agent/自动化导航 + 阅读列）        │ │
│ │ ──────────────────────────────────────────────────────────────────────── │ │
│ │ Coding Mode surface（本方案核心）                                        │ │
│ │ ┌活动栏┬─ 次级侧栏 ──┬──── 编辑器主区 ────┬──── AI 面板 ────┐            │ │
│ │ │files │ 文件树       │ 多标签编辑器        │ Agent 对话流    │            │ │
│ │ │search│ (可换搜索/   │ (代码/Diff/审阅)   │ (MessageArea)   │            │ │
│ │ │git   │  Git 视图)   │                    │ 任务状态条      │            │ │
│ │ │debug │              │                    │ InputCard       │            │ │
│ │ ├─────┴──────────────┴──── 底部 dock ─────┴─────────────────┤            │ │
│ │ │ 终端(回显) · 任务 · Diff · 问题                             │            │ │
│ │ └───────────────────────────────────────────────────────────┘            │ │
│ └──────────────────────────────────────────────────────────────────────────┘ │
│ 状态栏（既有）· NoticeBanner（统一通知，带来源与定位回调）                      │
└──────────────────────────────────────────────────────────────────────────────┘
```

- 活动栏按 LAYOUTS.coding 契约（contracts.py：nav=files/search/git/debug），
  **git/debug 未有真实能力前灰置并如实标注**，不伪造功能（沿用"未实现即说明页"纪律）。
- 三预设沿用契约：balanced / editor-focus / review；拖动分隔线改比例不重建
  widget；预设与比例独立持久化（dev_mode 的 ModeState+capture_layout 已是
  现成样板，app.py:1093-1103 → ~/.forge-development/development.json）。
- 密度走 DENSITIES.coding（gutter 8 / 区块 8 / 行高 28，96-DPI 单位），
  不缩小关键字体。

### 1.2 各区职责与数据源

| 区域 | 职责 | 数据/运行时来源 |
| --- | --- | --- |
| 文件树 | 浏览/过滤/打开/git 状态标记 | WorkspaceSource（development_mode/project.py）复用 workspace._scan_tree/_git_status_map |
| 编辑器 | 多标签、未保存/冲突标记、外部变更对账、保存 | forge/development/editor.py（Document/EditorBridge，含三方合并与原子保存） |
| Diff | 单文件/全部、AI 未应用预览、检查点差异 | project.py diff + change_preview.py（预览与 _expected_revisions 防 TOCTOU） |
| AI 面板 | 对话流（Markdown/流式）、工具卡、任务状态、输入 | DevelopmentRuntime（隔离会话）+ TaskEvidence 投影（evidence.py） |
| 底部 dock | 终端回显、任务步骤、Diff 聚焦、问题/审批失败 | runtime 事件流 + push_terminal 缓冲 |
| 审批 | 文件修改/能力授权/测试执行 | PermissionPrompt（visual_system）+ gateway plugin_context + Policy |

### 1.3 与 VSCode / Web 的关系（协议层统一，不做第四份 UI）

三前端共享**内核协议**而非共享 Tk 组件：会话 jsonl、build_agent/Policy/
SecretScope、secrets.json、事件 schema（dev-mode-2 的 action_id/review_id
关联字段）。Coding Workspace 的对话流/任务状态/审批事件 schema 应定义一次，
Tk 直接消费，vscode_bridge 与 Web UI 经同一 schema 序列化。

## 二、组件复用计划

原则：**不新写任何视觉组件**（gui_theme/ui_icons 是唯一来源）；运行时状态机
不复制（DevelopmentRuntime + evidence 是唯一来源）；项目数据不写第二个爬虫。

| 新 Coding Workspace 区域 | 复用的既有组件 | 出处 | 缺口（需新建/增强） |
| --- | --- | --- | --- |
| 顶层切换/空间容器 | ModeSwitcher、WorkspaceDeck | visual_system/components.py:10-99 | 接线生产主窗（规范 §4 全部约束） |
| 通知/审批 | NoticeBanner、PermissionPrompt、PermissionSpec | components.py:102-145 | ASK 挂起通道（见流程 3） |
| 文件树行/扫描 | _FileRow、_scan_tree、_git_status_map、git 徽章色 | workspace.py:387-470/132-196；gui_theme C.git_* | 右键菜单（新建/重命名/删除→审批）；搜索框（project.py scan(search=) 已支持未接）；截断提示 |
| 编辑器标签 | _FileTab（× 关闭、未保存态） | workspace.py:309-384 | 中键关闭/拖拽重排（可选） |
| 编辑器正文 | tk.Text + setup_code_tags/highlight_python/gutter_lines + _Minimap + gutter 同步 | gui_theme.py:1090-1150；workspace.py:473-550/1017-1028 | dev_mode 编辑器未接 gutter/高亮；非 Python 语言高亮缺失（可后置） |
| Diff 渲染 | diff tag（add/del/hunk/meta）+ _render_diff + show_change_preview | gui_theme.py:1105-1107；workspace.py:2127-2310 | 抽公共 DiffView 组件；去重两份逐行循环；预览 meta 误染修正 |
| AI 对话流 | MessageArea/UserMessage/AgentMessage/ToolCard/ActionRow/AgentStatusIndicator/render_blocks(chunked) | chat_widgets.py:1136-1799/784-912/577-747 | dev_mode 全部未用（最大重复）；工具 rows 需补 detail/结果 |
| 输入 | InputCard（三层/草稿/IME 锚定/快捷键） | chat_widgets.py:2069-2628 | dev_mode 未用 |
| 底部 dock 终端 | push_terminal 环形缓冲 + 只读 Text | workspace.py:1604-1617 | 追加式渲染替代全量重写；ANSI 着色（后置） |
| 任务状态 | TaskEvidence 投影 + render_evidence | forge/development/evidence.py；app.py:738-758 | 结构化为状态条+步骤卡（现状纯文本报表） |
| 模型选择 | model_picker + reasoning_slider | model_picker.py | dev_mode 自建 Combobox/对话框应替换 |
| 布局持久化 | ModeState + capture_layout/place_sashes | development state.py + app.py:1030-1103 | 扩展为整个 coding surface 的预设/比例/dock 记忆 |
| 图标 | ICONS（files/code/search/terminal/diff/play/save…48 个）+ IconButton | ui_icons.py:15-108/325-463 | 无（够用） |
| i18n | tr() + locales（10 语言 517 键） | i18n.py | dev_mode 硬编码中文需收口并纳入 check_i18n_coverage |

**明确不建**：第二套调色板（tokens.py 仅语义别名）、第二个文件爬虫、第二个
对话渲染器、第二个任务状态机、第二个权限裁决（PermissionPrompt 只回传意图，
裁决仍在 Runtime/Policy）。

## 三、交互流程

### 流程 1：进入/切换 Coding Mode
1. ModeSwitcher 发起 on_request("coding") → 宿主校验（无运行中审批/挂起请求）→ set_selected。
2. WorkspaceDeck 首建 coding surface（懒构建），此后仅可见性切换；不 destroy、不 close Agent。
3. 恢复：布局预设与比例（独立 key）、打开文件与标签序、未保存草稿（不保存不丢弃）、AI 会话与任务状态（后台任务不中断）。
4. 焦点恢复到该空间最近输入位置。

### 流程 2：编码任务闭环（AI 面板驱动）
1. InputCard 输入（可选规划 chips/角色模型）→ DevelopmentRuntime 起任务（不可变快照）。
2. 事件流 → AI 面板：工具调用渲染为 ToolCard（含参数/结果 detail，修复现状不可见问题）；文件写入事件触发文件树/diff 异步刷新并归因（_file_actions）。
3. 涉及写文件：走流程 3 审批；批准后 change_preview.check_revisions 复核 → gateway 执行 → diff 聚焦。
4. 任务完成：状态条终态（completed/failed/cancelled/interrupted）+ evidence 卡（计划/各 agent/工具步骤/测试/diff 快照/复审）+ 工作区动作（仓库变更 N/预览/复审）。
5. 测试执行：dock 任务页「执行测试」→ 显式人工授权（workspace-write 快照 + 当前权限 + askyesno）→ 有界输出进 dock + evidence（testing.py 通道，不注册为 Agent 工具）。

### 流程 3：审批（Policy ASK → 内嵌审批卡）
1. 工具裁决为 ASK 时**任务进入"等待审批"态**（现状：直接失败写问题页——本方案要改的交互）。
2. AI 面板内嵌 PermissionPrompt：摘要（操作/目标/来源任务/request id）+「允许本次/拒绝」；NoticeBanner 同步提示来源。
3. 回传 (PermissionSpec, intent) → Runtime 重校验 request 仍有效（过期/已取消则禁用并解释）→ 裁决执行或拒绝；结果入 evidence 与审计。
4. 文件修改类审批叠加 diff 预览（change_preview 只读预览 + sha256 预期版本），批准即应用、拒绝保留草稿。
> 依赖：挂起/恢复能力需 feature/development-mode-2 的取消/关联字段合入后接线；当前阶段可先做"审批卡+失败重试"降级形态。

### 流程 4：编辑与外部变更（既有能力，保持）
人工编辑 ↔ Agent 写共用项目锁与观察版本（guard.py）；3s 外部对账：干净缓冲自动重载（保光标/视口）、脏缓冲标 `!` 冲突 → 比较/三方合并/明确重载；编码显式指定；Ctrl+S 原子保存。

### 流程 5：布局与状态保持
预设切换/拖动/开关面板 → capture_layout 持久化；窄窗口先折叠 AI/文件侧栏再压编辑器；断点按有效布局宽度计算；退出时 can_close 检查未保存缓冲与运行中任务（修复现状嵌入形态缺失）。

## 四、问题清单

### P0（阻断/安全/一致性）
| # | 问题 | 位置 |
| --- | --- | --- |
| 1 | 嵌入形态退出不安全：主窗 _on_close 不调 bench.can_close/capture_layout，未保存草稿可能静默丢失 | forge_gui_v2.py:7759-7779 vs 3215-3222 |
| 2 | 无逐工具审批交互：Policy ASK → 工具直接 ok=False 失败，无批准/拒绝、无"等待审批"任务态；审批散落三套（diff 面板/模态框/300s 静默超时即 PermissionError） | forge/tools.py:226-229；desktop_features.py:476-558 |
| 3 | Web UI 启动即不可用：/api/models 与 /api/diag NameError（key_keys_status/key_status），boot() 首请求即失败；凭据读 keys.json 但内核只认 secrets.json；策略模式下拉枚举不匹配（readonly≠read-only 静默降级） | client/server.py:479-524、41、97 vs index.html:222 |
| 4 | 主对话工具结果不可见：rows 只传截断 70 字符 desc、不传 detail/结果；elapsed 传字符串致耗时汇总恒 0 | forge_gui_v2.py:7497-7502；chat_widgets.py:776-781 |
| 5 | TeamTogglePill 默认分支引用未导入的 _bg_of，省略 bg 即 NameError（潜伏崩溃） | chat_widgets.py:1826 vs 46-53 |
| 6 | visual_system 双副本（pledge-evolving 与 forge-visual-system-2 各一份等价源码），无单一来源约定，必漂移 | 两工作区 |

### P1（功能与体验）
| # | 问题 | 位置 |
| --- | --- | --- |
| 7 | dev_mode 全套自建不复用：对话裸 Text（无 Markdown/流式/工具卡）、自建模型下拉与设置窗、平行 40ms 事件队列、硬编码中文（i18n 缺位）、ttk 风格与主界面割裂 | development_mode/app.py:193-259/698-805/888-953 |
| 8 | "终端"名不副实 ×2：workspace 控制台/终端两子标签指向同一只读 Text；push_terminal 100KB 全量重写有性能隐患；dev_mode 终端同为回显 | workspace.py:2325-2352/1604-1617 |
| 9 | 编辑器缺口：dev 编辑器无行号/无高亮；workspace 只 Python 高亮且全文重扫；minimap 与 highlight 两套着色口径不一致 | app.py:511-518；gui_theme.py:1114；workspace.py:526 |
| 10 | Diff 缺口：无并排（注释画的是并排）；_render_diff 两份近逐字重复；show_change_preview 误染 meta 行、单值 _change_preview 并发任务互相覆盖 | workspace.py:10/2240-2304/2157 |
| 11 | 文件树：无右键/新建/重命名/删除；workspace 侧无搜索（project.py 已支持）；2000 节点静默截断；整树销毁重建；每次展开全量重扫+git 重算 | workspace.py:132-181/1932-1984 |
| 12 | 任务无实时进度：subprocess 一次性取全量输出，长任务只有"运行中…"；reasoning_content 采集从不渲染；历史重放丢工具卡/错误卡 | forge_gui_v2.py:4044-4096/7516、3604-3614 |
| 13 | 布局不持久化：workspace 三段比例与外层宽度仅进程内；重启即丢（dev_mode 已有持久化样板未推广） | workspace.py:579/787；forge_gui_v2.py:844/3412 |
| 14 | 入口错位：desktop_features.review_change 调 _open_workspace('diff') 不被识别，落到文件树；header"general/development"Combobox 与主导航语义混淆 | desktop_features.py:545 vs forge_gui_v2.py:3390-3401；app.py:80-83 |
| 15 | VSCode 扩展：设置过滤器错发布者 ID；会话 jsonl 只写不读（无恢复）；无编辑器内联（decoration/codelens）；停止=SIGKILL 无优雅取消（三前端三种取消语义）；set_credential RPC 与子进程方案双实现 | extension.ts:250/227；bridge.ts:98-107 |
| 16 | 打开文件失败静默（_document_loaded 对 error 无反馈）；run_tests 门槛提示短；task_label 混入英文事件名 | app.py:502-505/817-821/777 |

### P2（一致性与债）
- StepList/add_steps 生产未用；_auto_wrap 死代码；ActivityGlyph 第二套 20px 图标死代码（forge_gui_v2.py:187-229）；ALIASES '☷' 重复键；i18n 收口范围不含 visual_system/model_picker/plugin_market；行内 IME 默认关闭（FORGE_INLINE_IME=1）；空态上下文卡第 3 条消息后消失；"最近修改"实为 git status 键序。

## 五、分阶段实施建议

每阶段独立可验收、不破坏既有 Work Mode；沿用真实 Tk 几何测试 + DPI 五档 + i18n 收口 + 离线回归的既有测试基建。

**Phase 0 — 修坏与止血（小改动，先行）**
1. Web UI 三处（NameError/keys.json→secrets.json/策略枚举对齐）或明确下线 Web UI 入口。
2. TeamTogglePill _bg_of 导入修复；_on_close 接入 bench 引用与 can_close。
3. 主对话工具 rows 补 detail/结果与数值 elapsed；desktop_features 'diff' 入口对齐。
4. visual_system 声明单一来源（保留一份，另一份改为引用或删除），纳入 check_i18n_coverage。

**Phase 1 — 双工作空间接线（结构落地）**
主窗口接 ModeSwitcher + WorkspaceDeck；现 development 视图迁为 coding surface 的子挂载（DevelopmentRuntime/状态机/editor 不动，只换容器与导航）；布局持久化从 ModeState 推广到整个 surface（预设/比例/dock 记忆独立 key）；退出走 can_close。
验收：切换往返保留草稿/文件/布局/后台任务；五档 DPI；导航 parent/order 稳定。

**Phase 2 — AI 面板统一（消灭最大重复）**
dev_mode 对话流替换为 MessageArea/AgentMessage/ToolCard/InputCard（保留 DevelopmentRuntime 与事件 schema，写一层事件→消息适配器）；模型选择换 model_picker/reasoning_slider；task_label 结构化（状态 glyph + 中文文案）；evidence 渲染从纯文本报表升级为状态卡 + 步骤卡；reasoning/工具结果可见性落地；i18n 收口 dev_mode 全部文案。
验收：对话 Markdown/流式/工具卡行为与 Work Mode 一致；工具结果含 detail；IME 正常。

**Phase 3 — 审批与任务实时化**
合并 feature/development-mode-2（取消/证据/关联字段）后：Policy ASK → "等待审批"态 + AI 面板内嵌 PermissionPrompt（request id + Runtime 重校验）；NoticeBanner 统一来源通知；任务事件从"一次性取全量"改为事件流桥接（run --json 事件 → ToolCard 增量渲染）；三前端取消语义统一（协作式取消优先，SIGKILL 兜底）。

**Phase 4 — 编辑器/Diff/文件树增强**
dev 编辑器接 gutter + highlight + minimap（全部复用）；DiffView 公共组件（去重两份循环、预览 meta 修正、可选并排）；文件树右键（新建/重命名/删除 → 走审批与 guard 锁）+ 搜索框 + 截断提示；终端改增量追加渲染（可选 ANSI 16 色）。

**Phase 5 — 跨端协议统一**
以 Coding Workspace 的事件/审批/任务 schema 为准，vscode_bridge 暴露同构事件（progress 已有雏形）；扩展修设置过滤器、会话恢复（读 sessions/vscode）、优雅取消；Web UI 修活后消费同一 schema。编辑器内联能力（decoration/codelens）在协议稳定后评估。

## 六、已知限制

- 本轮只读；未运行 GUI（本机 Tcl init.tcl 损坏，无法人工视觉验收——沿用既有程序化几何测试结论）。
- feature/development-mode-2 未合并：Phase 3 依赖其取消/关联字段；合并前审批卡只能以"失败+重试"降级形态落地。
- 仓库存在并行 Agent 改写与多 worktree（forge-visual-system-2、forge-development-mode-2）；实施各阶段前需与对应负责人对齐分支基线。
- 浅色主题不存在；终端 ANSI、非 Python 高亮、并排 diff 均为缺失能力，排期在 Phase 4+。
