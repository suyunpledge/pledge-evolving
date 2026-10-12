# Forge 工作台 GUI/UX 规范与架构审查

> 2.0 核心目标修订见 [双工作空间设计规范](../docs/forge-visual-system-2.md)。
> 本文记录既有实现；“深色 IDE 工作台”不再作为整个产品的布局定位。
> Work Mode 与 Coding Mode 共享设计语言，各自采用适合用途的导航、密度和布局。

## 实际架构（2026-10-11）

桌面端为 Python Tkinter/ttk，不是 Web 前端。`forge_gui_v2.py` 负责主窗口、稳定的 sidebar、PanedWindow、页面导航、配置表单及事件适配；`chat_widgets.py` 负责 Canvas 滚动列表、Unicode/Emoji 文本、Markdown、工具记录和输入卡；`workspace.py` 负责文件树、代码、diff、预览和可拖动分隔线。`gui_theme.py` 与 `ui_icons.py` 是既有共享组件系统，继续复用。

主分栏 parent/order 不能随页面切换改变。右侧工作区由 show/hide 和 PanedWindow add/forget 控制，已经有关闭入口。聊天流通过 UI 队列更新，Markdown/历史分批绘制，市场后台读取后分批渲染。DesktopFeatures 用后台线程访问本地任务数据库、连接器和授权服务，独立窗口包含定时任务、连接器、代码预览。

GUI 只适配业务：模型选择/PhaseClient、Gateway、team.load_config/save_config、Marketplace、DesktopStore/ConnectorService。此次不改变这些协议、执行策略或 Secret 边界。知识库/演化部分是现有说明页；不把尚未实现的页面伪装成可用功能。

现有测试包括真实 Tk 布局、五档 Tk DPI、窗口缩放、消息/Emoji、模型弹层焦点、IME、导航 parent/order、异步市场、配置保存、插件攻击、权限、Secret 和连接器。测试使用临时配置与离线服务；保留原测试基础设施。

## 统一设计规范

- 深色 IDE：背景 #11151C、侧栏 #171C25、卡片 #1D2430、输入 #151B25、悬停 #293344。正文 #EDF1F7，辅助说明 #ABB5C5。
- 交互强调色使用有白字对比度的蓝色；浅蓝用于链接/选中描述。低饱和绿/金/红仅表示成功、警告、失败。品牌主图标不变。
- 聊天/输入最大宽度 1040 个 96-DPI 布局单位；设置表单 800；市场 1040。实际宽度为 viewport 减左右 gutter，DPI 用同一 ui_px 换算；宽屏居中，窄屏收缩。组件不重新挂载。
- 助手为文档式正文，背景与对话一致，无大面积气泡边框；用户为低饱和轻量气泡。正文、头像、时间、工具和操作分层。
- 标准间距 4/8/12/16/20，卡片圆角 10，输入 12，用户消息 16。边框仅用于编辑边界、焦点、可操作卡片。
- 动画时长为 0，阴影为无。反馈由已有 Button/IconButton 的 Hover/Pressed/Focus/Disabled 与文本状态完成。
- Entry/Text 使用输入底色、可见 caret 和 focus 边框；Select、Checkbox、Radio、Tabs、Scrollbar、Treeview 用同一 ttk/Tk 主题。复用 pill_button、glyph_button、RoundedCard、badge、attach_tooltip、Dialog 和状态栏，不引入第二套组件。
- 异步操作显示实际状态，不伪造进度；错误单个状态卡，细节按需展开；保留日志、草稿和审批。错误重试恢复原草稿，仍由用户发送。
- 输入三层：正文编辑 → 文件/上下文/命令/Agent/规划 → Provider/Model/模式/发送。快捷键不变。
- Agent：模型方案、子 Agent 卡、通信、上下文；系统提示词多行编辑；不新增不存在的 Agent 名称/权限/备用路由字段。保存继续使用原格式，状态明确。

## 实施与验收记录

1. 架构检查及共享 tokens/宽度/表单规范。
2. 对话文档排版、统一内容列、错误去重、输入层级和分栏恢复。
3. Agent 配置多行编辑与分组、API 表单宽度。
4. 市场、自动化/连接器及现有页面共享样式和状态。
5. 真实 Tk 回归、DPI/窗口检查、全量离线回归；最终结果另附。

视觉验收需区分真实窗口布局测试、样式样本和实际截图。程序化 Tk 几何测试不能代替人眼检查，不宣称完成未经观察的视觉验收。
