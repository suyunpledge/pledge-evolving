# forge 图形界面 v3（深色三栏）

把 `forge` 命令行框架包成一个开箱即用的桌面应用。

**版式**：顶栏（品牌 + 主导航 + CPU/GPU/RAM + Gateway）/ 左栏 200px（新建对话 +
主导航 + 最近对话）/ 中间视图 / **右侧可收起工作区** / 底部状态栏。

**导航与视图**：

| 导航 | 视图 | 作用 |
|---|---|---|
| 对话 | chat | 连本机 gateway 与模型对话（消息气泡 / 计划步骤 / 工具调用卡 / 完成块 / 输入卡）|
| 任务 | task | 用 `run.py run <task> --json` 跑任务，把 steps 渲染成执行步骤与工具行 |
| 工具集 | tools | 功能开关（Provider 启用、MOA、通道开关等，草稿式保存）|
| 配置 | config | 粘贴地址+密钥 → 整理 → 预览 → 保存到 `~/.forge/forge.patch.json` |
| 文件与项目 | — | 打开右栏工作区（文件树）|
| Agents / 知识库 / 演化 | stub | 规划中的面板，写明会接入哪些 forge 能力 |

**右栏工作区**（默认收起，顶栏「▤ 工作区」或消息里的「打开工作区」展开）：
三段**同时可见、上下堆叠**——上区约 55%：文件树 | 代码（行号槽 + Python 高亮 + 滚动同步）|
minimap（彩条缩略 + 可视框 + 点击跳转）；中区约 25%：变更文件列表（chip 筛选 + git 徽章 +
`+X −Y`）与 diff 视图（`git diff` 行级着色，未跟踪文件按整文件新增渲染）左右并排；下区约 20%：
预览 / 控制台 / 终端 / 图像 / Markdown 子标签 + 「在新窗口打开」「刷新」。三区高度可拖拽调整。

**设计系统**：`gui_theme.py` 是唯一样式来源（深色背景层级 `#0B0B10 → #0E0E13 →
#111117 → #15151C → #1A1A22`，主色 Indigo `#4F46E5` / 亮态 `#6366F1`，语义色
`#22C55E`/`#F59E0B`/`#EF4444`，圆角、间距、字体与绘制原语都在这里）。
`chat_widgets.py` 负责消息渲染，`workspace.py` 负责右栏，`sysmon.py` 负责顶栏指标。
配色不再是参考 AutoClaw 浅色主题的那一套。

## 安装

零依赖——只要求 Python 3.10+（系统 Python 自带 tkinter）。
把这个目录放到 `~/.openclaw/tmp/forge-gui/` 或 `FORGE_REPO/` 同级。

```bash
# 推荐：用系统 Python（tkinter 通常随系统 Python 自带）
python3 forge_gui_v2.py        # 控制台可见
pythonw forge_gui_v2.py       # Windows 无控制台
```

> Windows 用户如果报 `No module named tkinter`：安装官方 Python 3.x（不要用嵌入版）。
> macOS：`brew install python-tk@3.12`。
> Linux：`sudo apt install python3-tk`。

## 第一次使用

### 1. 启动 GUI

```bash
python3 forge_gui_v2.py
```

### 2. 添加一个 Provider（管理标签页）

把厂商给的信息粘贴进右侧输入区（任意格式都行）：

```json
[{"id":"deepseek","config":{"wire":"openai",
  "baseURL":"https://api.deepseek.com",
  "apiKey":"sk-你的密钥",
  "model":"deepseek-flash"}}]
```

点 **整理并预览**（或在输入区按 Ctrl+Enter）——内置格式矫治器会自动：
- 补齐 `wire` 字段（按 baseURL 推断）
- 把明文 `apiKey` 包成 `{"$expr": "get('env.FORGE_KEY_xxx', '')"}`（**密钥不落盘**）
- 保留已有路由、通道和禁用状态，GUI 添加 Provider 时不会重置 `model` 路由行
- 给出警告（如缺字段、用了明文密钥）

预览对了之后点 **保存到用户层**，矫治结果会合并写入 `~/.forge/forge.patch.json`。

### 3. 导出密钥（关键）

矫治后的警告里会列出要导出的环境变量名，类似：

```
export FORGE_KEY_7E55B0='你的密钥'
```

把这个环境变量设上（建议加到 `~/.bashrc` 或「系统 → 环境变量」），
**密钥本身**永远不会被写到磁盘上——`apiKey` 字段在 patch 里是 `$expr`，
forge 启动时从环境变量读。

### 4. 启动 gateway

切到交互客户端标签页，点 **▶ 启动 gateway**。
按钮会变红表示「在线」，状态栏显示实际端口。

### 5. 开始对话

在底部输入框打消息，回车。模型选择自动从你配置的 provider 里列出。
流式输出，逐字回显。

Provider 列表与编辑区之间的分隔线可以拖动；提示与环境变量区可滚动查看。修改输入后，需要重新整理才能保存新的预览。

### 沉思模式（输入卡工具条）

输入框左下角有「◎ 沉思 · ⋯」胶囊，点击后在三档之间切换，写入用户层的 `thinking` 行：

| 档位 | 值 | 含义 |
|---|---|---|
| 关闭 | `off` | 不启用沉思（默认）|
| 智能 | `smart` | 按任务复杂度自动决定 |
| 开启 | `on` | 始终启用沉思 |

对应 forge 自己的 `thinking.mode`：设置后 `forge run` 任务即时生效，
已在运行的服务需重启才读取新配置。非「关闭」时胶囊变浅橙底表示激活。

### 功能开关

**功能开关** 是独立页面，列表可滚动，标题可折叠。可以直接选择并保存：

- 每个已配置 Provider 是否参与路由和 gateway 调用
- 多模型协作（MOA）
- 已配置消息通道（例如微信）是否启用
- 用户层中其他布尔型功能配置

开关根据当前用户层自动生成，显示开启/关闭（高级布尔项显示 true/false）以及保存状态。按钮会显示待保存的修改数量；改回原值会恢复“已保存”。选择后点 **保存功能开关** 即写入用户层，无需编辑 JSON；保存前也可点 **还原未保存修改**。

保存只更新本次改变的开关。运行中的 Forge、gateway 或通道服务需要重新启动才能读取新配置；“已保存”不代表已验证运行时生效。面板目前列出用户层已有项目，不自动枚举 bundle 中的所有可选功能。

缺少 Forge 路径时可点 **选择 Forge 目录**，选择包含 `run.py` 的目录。本次会话会使用该目录；永久路径仍可通过 `FORGE_REPO` 指定。

配置编辑区支持滚动，底部保存按钮固定可见。编辑期间条目若被其他操作修改，保存会停止并提示重新载入；损坏的用户层不会当作空文件覆盖。文件通过同目录临时文件写入后替换，降低写入中断导致原配置损坏的风险。

## 矫治器接受的输入形态

| 形态 | 示例 |
|---|---|
| 标准 patch 数组 | `[{"id":"x","config":{"wire":"openai","baseURL":"...","apiKey":"...","model":"..."}}]` |
| 带 `//` 行注释 | forge 自己的 patch 格式 |
| 语义化分组 | `{"providers":{"x":{"baseURL":"...","apiKey":"..."}}}` |
| 平铺 provider 表 | `{"deepseek":{"baseURL":"...","apiKey":"..."}}` |
| 单条 row | `{"id":"x","baseURL":"...","apiKey":"...","model":"..."}` |
| 纯文本 | `baseURL: https://...\napiKey=sk-...\nmodel: ...` |

别名也接受：`baseUrl` ↔ `baseURL`、`api_key` ↔ `apiKey`、缺 `wire` 时按 baseURL 推断。

## 文件布局

```
forge-gui/
├── forge_gui_v2.py     # 主 GUI（外壳 + 左栏 + 对话/任务/工具/配置视图）
├── gui_theme.py        # 设计系统：配色 / 字体 / 圆角 / 绘制原语 / 代码高亮
├── chat_widgets.py     # 消息渲染：气泡、角色徽章、步骤、工具卡、完成块、输入卡
├── workspace.py        # 右栏工作区：文件树 / 变更 / 代码 / diff / 预览 / 终端
├── sysmon.py           # 顶栏 CPU / RAM / GPU 采样（ctypes，零依赖）
├── config_model.py     # 配置模型 + 内置格式矫治器
├── forge_client.py     # 与 gateway 通信的 OpenAI 兼容客户端
├── test_config_model.py  # 矫治器单元测试
├── test_forge_client.py  # 客户端单元测试（mock gateway）
├── test_integration.py   # 端到端集成测试
├── test_gui_review.py    # GUI 回归（配置保护、布局可达性、异步交互）
└── readme.md
```

## 测试

```bash
python3 test_config_model.py    # 矫治器（13 正向 + 2 负向 + 合并 + 启发式）
python3 test_forge_client.py    # 客户端（5 个：health/非流式/流式/不可达/错误传播）
python3 test_integration.py     # 端到端（矫治 → 保存 → 客户端 → mock 对话）
python -m unittest test_gui_review -v  # 需桌面：18 项（配置保护、视图可达性、异步交互）
```

改版后另做了两项不靠肉眼的手工核对：控件树扫描（所有 bg 必须落在 `gui_theme.C`
的 token 上，当前 0 例外）与整窗截图核对（顶栏/左栏/对话/工作区版式、行号与语法色）。

不需要 GUI 显示、不需要真实 forge、不需要真实模型 API——mock gateway 用本地 socket 模拟。

## 修改指南

| 想改什么 | 改哪里 |
|---|---|
| 配色 / 字体 / 圆角 / 间距 | `gui_theme.py` 顶部 `C` / `FONT_*` / `R_*`（唯一样式来源）|
| 顶栏 / 左栏 / 导航项 | `forge_gui_v2.py` 的 `NAV_ITEMS` + `_build_topbar()` / `_build_sidebar()` |
| 视图切换与新面板 | `_build_views()` / `_show_view()` / `_build_stub_view()` |
| 消息气泡 / 步骤 / 工具卡 | `chat_widgets.py`（`UserMessage` / `AgentMessage` / `ToolCard` / `StepList`）|
| 输入卡（占位、工具条、模型、发送） | `chat_widgets.py` 的 `InputCard` |
| 右栏工作区（树 / 代码 / diff / 预览） | `workspace.py`（`WorkspacePanel`） |
| 顶栏指标采样 | `sysmon.py`（`SysMon(callback, interval)`）|
| 任务视图 | `_build_task_view()` / `_run_task()` / `_task_finished()` |
| 最近对话持久化 | `_sessions_path()` / `_archive_current_session()` / `_load_session()` |
| 矫治规则（接受更多形态 / 更严） | `config_model.py` 的 `normalize()` + `_normalize_provider_row()` |
| 沉思三档 | `THINKING_LABELS` / `THINKING_CHOICES` + `_set_thinking_mode()` |
| Gateway 启动参数 | `_start_gateway()`（默认 `--upstream openai`，可以加 `--model-map` 等） |
| 客户端超时 | `ForgeGatewayClient(timeout=60.0)` |

## 本次改版的取舍（对照参考稿）

| 参考稿 | 现状 | 说明 |
|---|---|---|
| 无边框圆角窗口 + 自绘 `− □ ×` | 保留系统标题栏 | 无边框会丢任务栏图标与系统贴靠/缩放，可用性代价大于视觉收益 |
| 分隔线可拖动 vs 固定高度 | 三区用 PanedWindow，可拖拽 | 参考稿未体现拖动把手，深色 sash 5px 已尽量弱化 |
| 预览面板显示「App 缩略图」 | 显示真实文件内容 | 预览面板按扩展名渲染 md / HTML 源码 / 图片缩略图 / 文本，比缩略图更有用 |
| 顶栏 `CPU/GPU/RAM` | 真实采样 | CPU 用 `GetSystemTimes`、内存用 `GlobalMemoryStatusEx`、GPU 走 `nvidia-smi`；取不到显示 `—` |
| Agents / 知识库 / 演化 面板 | 占位页 | 写明各自会接入 registry / memory+curator / iteration-ledger，尚未实现 |
| 「任务」视图的计划步骤 | 真实数据 | 来自 `run.py run --json` 的 `steps[]`（tool / decision / note），不是演示文案 |

## 已知限制

- **整理输入的密钥需要设环境变量**：矫治器把输入的明文 `apiKey` 转成 `$expr`。
  第一次用必须手动 `export`。客户端和 gateway 都能读到。
  功能开关保存会保留已有配置的其他字段，不会替换已有密钥存储方式。
- **gateway 子进程不能中途取消回复**：Python `urllib` 没暴露 abort 点；想换 `httpx`/`aiohttp` 可改 `forge_client.py`。
- **Tkinter 在 macOS 上偶发字体问题**：已用平台判断回退到 Menlo/Helvetica。
- **不读 bundle 行**：默认 bundle 里的 provider（如 base.json 的 deepseek）靠 forge 自己读；GUI 只编辑 **用户层**（叠加在 bundle 之上）。

## 设计原则

1. **矫治必须可预测**：同输入永远产同输出，不"猜测意图"。
2. **密钥永远不落盘**：矫治器一旦见到明文，立即包成 `$expr`。
3. **GUI 不假设 forge 是完美配置**：缺字段就标 disabled，警告里讲清。
4. **每个环节可单测**：`config_model` / `forge_client` / 集成测试互相独立，无 GUI 依赖。
