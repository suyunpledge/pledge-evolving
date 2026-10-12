# Forge Visual System 2.0 — 双工作空间设计规范

2026-10-12 · 修订版 · 基线 `119b827`。
独立分支：`feature/visual-system-2-dual-workspace`。
工作区：`C:\Users\匡溯昀\forge-visual-system-2`。

本规范确立两个工作空间的目标、共享语言、布局和集成契约。
本轮交付设计规范、可复用视觉组件、布局样例；不替换生产主框架，不重写 Coding Runtime。

## 1. 核心目标

Forge 是一款统一的软件，提供用途不同的两个工作空间。
**共享设计语言，不强制共享布局。**

| | Work Mode | Coding Mode |
| --- | --- | --- |
| 定位 | 现代 AI 工作台 | 专业 Agent Coding IDE |
| 中心 | 对话、任务、Agent 协作、自动化 | 代码编辑、项目、终端、Git、Diff、调试、AI 辅助开发 |
| 体验 | 简洁、舒适、直观，适合长时间交互 | 高信息密度、高操作效率、灵活面板 |
| 默认导航 | 会话、任务、Agent 协作、自动化 | 文件、搜索、Git、调试 |
| 默认密度 | comfortable，留出阅读与输入空间 | compact，压缩间距而非必要文字 |
| 默认辅助面板 | 任务细节/上下文按需打开 | 文件树、AI、底部工具面板可独立调整 |

Coding Mode 是顶层工作空间，不是 Work 聊天页中的附属 IDE 面板。
Work Mode 保留自己的工作流，不继承 IDE 的活动栏、文件标签或多重工具栏。
两者共用应用品牌与基础交互，允许导航结构、布局、密度、工具栏组织和工作流程不同。

## 2. 共享语言与单一来源

沿用 `forge-gui/gui_theme.py`、`ui_icons.py` 与现有基础控件。
`visual_system/tokens.py` 只是语义别名，颜色、字体均回查既有来源，不创建第二套调色板。
`visual_system/contracts.py` 的密度和布局参数是空间级变量，不覆盖全局主题。

| 共享项 | 统一规则 |
| --- | --- |
| 主题 | 两个空间响应同一应用主题；不允许插件或模式自行覆盖全局主题。当前实现只有既有深色主题，浅色/系统跟随须完整主题交付后接入 |
| Design Tokens | 表面、文字、边框、操作色、语义状态和代码/Diff 色由 gui_theme.C 提供；新组件用语义别名解析 |
| 字体 | UI/中文/代码沿用 Segoe UI、Microsoft YaHei UI、Cascadia Code 及已有平台 fallback；Tk 字号为 point，不再次乘 DPI |
| 图标 | 使用 ui_icons 的 24×24 几何定义与现有品牌资产；图标辅以文本、提示或可访问名称，不用 Emoji 代替关键操作 |
| 基础组件 | Button/IconButton、Text/Entry、Tabs、Treeview、菜单、对话框、状态标记共用原语；允许空间局部样式，不能修改另一空间的全局控件样式 |
| 交互状态 | normal / hover / pressed / selected / focus / disabled / loading / error 同义；disabled 不可激活；等待真实结果才改变执行状态 |
| 通知 | 相同级别与结构，必须携带来源 workspace 和 task/session/project 引用；通知不是权限裁决 |
| 权限提示 | 相同摘要结构与明确一次性意图；Runtime 重新校验 request ID、操作、目标、当前状态和权限上限 |
| DPI | 使用 ui_px 转换 96-DPI 布局单位；实际窗口 geometry 不再重复缩放；字体、图标与热区不能各用一套尺度 |

语义颜色：成功/警告/错误/信息使用既有 `ok/warn/error/info`。
普通文字对实际表面对比度至少 4.5；必要非文字焦点与控件轮廓至少 3。
状态不能只靠颜色，必须有文字、图标或标记。品牌装饰不替代执行状态。
主正文沿用 FONT_UI；代码沿用 FONT_MONO；必要审批/错误/说明不使用 FONT_MICRO。
密度差异改变间距和行高，不以缩小关键字体实现高密度。

| 空间级参数（96-DPI 单位） | Work | Coding |
| --- | --- | --- |
| 内容 gutter | 20 | 8 |
| 区块间距 | 16 | 8 |
| 列表行高 | 36 | 28 |
| 常用按钮/图标最小热区 | 32 | 32 |
| 阅读/编辑 | 阅读列有最大宽度，输入与正文对齐 | 编辑器填满剩余空间，代码保持等宽和独立字号 |

现有间距 4/8/12/16/20、圆角、无装饰阴影、无新增动画等规则保留。
减少视觉负担主要依靠层级、留白、按需显示和稳定布局。

## 3. 双空间布局方案

### Work Mode

- 顶部应用栏：品牌、统一工作空间切换、必要全局状态；不放 IDE 文件工具。
- 左侧工作导航：会话与近期记录、任务、Agent 协作、自动化；配置/扩展为次级入口。
- 主内容：当前会话或任务工作页，阅读列居中；助手为文档式正文，输入卡与正文对齐。
- 右侧细节：只在用户查看任务、审批或上下文时展开；默认不常驻文件树/终端/Git。
- 后台状态在应用级保留；完整工具与日志在任务详情中展开，避免消息和 toast 重复。

阅读区沿用现有最大宽度上限，具体长文本可使用更窄内列；宽屏不无限拉长行。
任务专注视图可切主内容，但仍使用 Work 的导航与舒适密度。

### Coding Mode

- 顶部仍使用同一个品牌和切换入口；项目、分支、任务模型放在 IDE 的上下文栏。
- 左侧活动栏负责文件/搜索/Git/调试，次级侧栏承载文件树或所选工具。
- 编辑器是主区域：多标签、未保存标识、代码编辑、Diff/审阅布局。
- 右侧 AI 面板有独立开发会话与任务上下文；可折叠，不抢占编辑器主区域。
- 底部 dock 放终端、任务、Diff/测试/问题；具体面板按真实能力接入，不宣称调试器已实现。
- 文件树、编辑器、AI、底部 dock 的分隔线可拖动；不通过重建 widget 改变布局。

| 预设 | 文件/AI | 编辑与审阅 | 底部 |
| --- | --- | --- | --- |
| 均衡 | 文件树 + AI | 编辑器最大份额 | 标准 dock |
| 编辑器专注 | 辅助侧栏折叠，可随时恢复 | 编辑器填满主内容 | 保留工具入口，按需展开 |
| 审阅 | 文件/Git 侧栏，AI 按需 | Diff 或并排代码 | Diff/测试/任务证据更突出 |

收窄窗口或提高 DPI 时，先折叠可选 AI/文件侧栏，再压缩主编辑器；保留可达的面板恢复入口。
断点必须按有效布局宽度计算，不能只按未换算的物理像素判断。
预设、手动比例、最近激活的 dock tab 由 Coding 独立保存，不覆盖 Work 布局。
离开/回来不改变当前预设；窗口 resize 与主动切换预设也不能丢弃文件草稿。

## 4. 统一切换与状态保持

入口位于应用顶层，始终明确显示 Work Mode / Coding Mode，选中状态有可见反馈。
Tab/方向键/Enter/Space 可操作；切换不等于返回导航、不等于 Agent 权限模式。

`WorkspaceDeck` 提供两个持久 sibling surface：各自第一次创建，随后仅改变可见性。
父子关系不变，退出工作空间不调用 destroy、Agent.close、cancel 或重新创建会话。
先完成目标 UI 挂载，再隐藏原空间；挂载失败保留原空间与草稿。
焦点在切换按钮抢焦之前记录，回来恢复至该空间最近输入位置。

| 必须保留的状态 | 权威所有者 | 切换规则 |
| --- | --- | --- |
| 当前会话 | 各空间现有会话控制器 | 不清空、不复制历史，不把 Work/Coding 对话混成一个 session |
| 当前项目 | Coding 项目控制器 | 隐藏不关闭项目；Work 上下文引用不自动变成项目授权 |
| 已打开文件 | Coding 编辑器/Document | 保留对象、标签顺序、选中标签与打开版本 |
| 面板布局 | 各空间 UI 状态 | 独立 key/布局缓存，不互相套用比例或导航历史 |
| 未保存内容 | 既有文本缓冲 | 切换不保存、不丢弃、不询问关闭；应用退出/关闭文件仍按既有确认机制处理 |
| Agent 任务状态 | 原 Runtime/事件日志 | 后台继续；视图可减少绘制，但不能注销执行、漏掉结果或改变取消信号 |

持久化通过各模块已有 schema/home 实现；本组件包不另建会话数据库或保存裸草稿。
草稿崩溃恢复、项目切换授权等仍归 Coding 模块，不能由视觉层绕过 Secret/文件冲突机制。
模式切换不授予权限、不迁移 provider/key、不改变运行任务的模型/权限快照。
任务取消必须是有作用域的明确操作，与隐藏面板、切换工作空间和切换布局完全分离。
输入法组合、用户正在编辑时的外部变化和隐藏空间返回刷新需在正式接线阶段回归。

旧内部命名 `general/development` 可在宿主 adapter 中映射到 `work/coding`。
不得直接把新顶层切换器接到 DevelopmentWorkbench 的旧内部“隐藏 IDE / General”控制上：
那是同一开发会话的内部可见性，不是两个独立工作空间。

## 5. 通知、审批与扩展规范

通知统一 tone、标题、正文、来源和定位操作；后台 Coding 的结果在 Work 中可提示，
点击后定位原任务，不能悄悄切换/修改原任务权限。普通工具输出进入任务/终端面板，
不是每条都弹通知。待审批、失败和冲突不能仅靠会消失的 toast 表达。

权限摘要必须清楚说明：操作、项目/目标范围、来源工作空间、任务、request 引用。
明确“允许本次/拒绝”，没有默认批准、因切换自动接受或将旧批准转移到另一个项目。
`PermissionPrompt` 只回传 `(PermissionSpec, intent)`；后端确认请求仍有效后才裁决。
过期、已取消、已处理请求应禁用控件并解释原因；UI 的 selected/visible 状态没有权限含义。

扩展 UI 接入以下声明式 surface；位置不代表可执行 capability：

- `work.detail` / `work.task-actions`：Work 任务详情与局部操作。
- `coding.sidebar` / `coding.bottom` / `coding.editor-action`：IDE 扩展面板或局部动作。
- `global.notification` / `global.permission`：统一来源、状态与审批摘要。

插件、Desktop Awareness、Computer Use 等继续使用各自既有业务与权限接口；
视觉组件只接收经过宿主整理/脱敏的展示数据和明确回调。
扩展须使用语义 token、注册图标与热区、loading/error/disabled 状态、键盘顺序、来源标识，
支持 DPI/窄窗口与销毁后的订阅清理。不得新增自有主题、伪造系统通知/审批或篡改切换入口。
本轮不修改插件市场、Desktop Awareness 或 Computer Use 的业务。

## 6. 组件与模块分工

本轮可用组件位于 `forge-gui/visual_system/`：

- `ModeSwitcher`：请求目标空间，宿主确认后更新选中状态；复用原按钮反馈和图标。
- `WorkspaceDeck`：持久空间容器、挂载失败保护、焦点记录/恢复，不持有 Runtime。
- `NoticeBanner`：统一来源的通知样式和定位回调。
- `PermissionPrompt`：共享审批摘要和显式意图回调，不授予权限。
- `tokens.py` / `contracts.py`：现有主题别名、密度、布局、扩展位置与展示协议。

| 工作 | 负责人/接口边界 |
| --- | --- |
| 当前设计/组件/样例 | Visual System：本分支，业务依赖为零 |
| 生产顶层 shell、入口和 Work 导航迁移 | GUI 宿主负责人：创建稳定 sibling mount points；不热重挂已有 widget |
| Coding Workbench 挂载 facade、项目/文件/布局/会话状态 | Coding 模块负责人：保留现有对象、模型与权限生命周期；提供 visibility/focus/UI-state adapter |
| 后台任务、事件关联、取消与恢复 | Runtime/Coding 负责人：提供现有 task/session 引用和只读事件订阅；视觉层不补建执行引擎 |
| 扩展服务业务与 capability | 对应模块负责人：提供脱敏摘要、来源及动作回调，继续使用原权限 gate |

Coding 执行闭环交付在独立 `feature/development-mode-2` 分支（`77d4323` / `1ca21f0`）。
本分支不自动合并该交付，也不擅自变更其 facade 或业务逻辑。
若生产集成需要改 `development_mode/app.py` 的父容器、内部模式入口或布局协议，
必须先由 GUI 宿主与 Coding 负责人确定创建时机和接口，独立提交并共同验收。

## 7. 当前实现事实与验收

当前主程序仍在 `forge_gui_v2._build_stub_view('development')` 中嵌入开发工作台。
**正式双工作空间主框架尚未迁移；新组件没有接入生产主窗口。**
本轮改变的是设计规范、共享视觉组件与可审阅方案，不把样例状态当作实际 Agent 执行。

样例与复现：

- `reports/forge-visual-system-2-preview.html`：可切空间、文件标签、布局预设、dock、拖动分隔线。
- `reports/forge-visual-system-2-work.png` / `forge-visual-system-2-coding.png`：HTML 布局渲染图，非生产或原生 Tk 截图。
- `forge-gui/run_visual_system_preview.py`：真实 Tk 组件/分栏样例；只用内存文本与明确标识的后台回调，无项目/Agent/终端执行。
- `scripts/export_visual_system_preview.py`：从真实主题和图标重新导出 HTML，避免手工双份 palette。
- `scripts/check_visual_system_preview.cjs`：本地 Chromium 检查与渲染；结果见 `reports/forge-visual-system-2-preview-check.json`。

真实 Tk 回归：9 项新增检查加现有视觉/图标回归，共 **14 项通过**。
覆盖草稿/对象/标签/项目/分栏保留、后台回调持续、焦点、挂载失败、键盘、禁用和审批意图；
100% / 125% / 150% / 175% / 200% 五档 Tk scaling 验证统一入口热区和样式隔离。
HTML 在 920/1100/1360/1920 宽度无页面横向溢出，运行错误为 0，DOM/草稿/标签保留检查通过。

```powershell
$py = 'C:\Users\匡溯昀\AppData\Local\Programs\Python\Python312\python.exe'
$env:PYTHONPATH = "$PWD\forge-gui;$PWD"
& $py -m unittest test_visual_system test_visual_identity test_icon_identity -q
& $py forge-gui/run_visual_system_preview.py
& $py scripts/export_visual_system_preview.py
```

正式接线验收须额外执行：运行真实 Work/Coding Agent 任务时反复切换、返回后读取实际结果、
保持文件冲突/未保存内容、待审批来源与授权隔离、IME、真实系统 DPI 和用户视觉检查。
本轮仅证明组件/方案层的保持机制，不声称完成这些业务集成验收。
