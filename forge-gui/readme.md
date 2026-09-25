# forge 图形界面 v2

把 `forge` 命令行框架包成一个开箱即用的桌面应用。

**三个标签页**：

| 标签 | 作用 |
|---|---|
| 功能开关 | 查看和修改用户层中的启用状态，单独保存开关草稿 |
| 配置编辑 | 粘贴地址+密钥 → 自动整理 → 保存到 `~/.forge/forge.patch.json` |
| 交互客户端 | 启停 gateway → 与模型对话（流式输出）|

界面按 AutoClaw 的设计语言重做：浅色底（`#f5f5f5` / 白卡）、品牌橙 `#fc5d1e`、
圆角输入卡（22px pill）、细分隔线 `#e5e5e5`、圆形发送按钮、输入卡下方一行小字说明。
配色 / 圆角 / 阴影 token 直接取自 AutoClaw `app.asar` 的 `--theme-*` 变量，两边视觉同源。

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
├── forge_gui_v2.py     # 主 GUI（管理面 + 客户端）
├── config_model.py     # 配置模型 + 内置格式矫治器
├── forge_client.py     # 与 gateway 通信的 OpenAI 兼容客户端
├── test_config_model.py  # 矫治器单元测试
├── test_forge_client.py  # 客户端单元测试（mock gateway）
├── test_integration.py   # 端到端集成测试
└── README.md
```

## 测试

```bash
python3 test_config_model.py    # 矫治器（13 正向 + 2 负向 + 合并 + 启发式）
python3 test_forge_client.py    # 客户端（5 个：health/非流式/流式/不可达/错误传播）
python3 test_integration.py     # 端到端（矫治 → 保存 → 客户端 → mock 对话）
python -m unittest test_gui_review -v  # 需桌面：配置保护、布局、开关及异步交互回归
```

不需要 GUI 显示、不需要真实 forge、不需要真实模型 API——mock gateway 用本地 socket 模拟。

## 修改指南

| 想改什么 | 改哪里 |
|---|---|
| 矫治规则（接受更多形态 / 更严） | `config_model.py` 的 `normalize()` + `_normalize_provider_row()` |
| GUI 配色 / 字体 / 字号 | `forge_gui_v2.py` 顶部 `C` / `FONT_*` 常量（注释标了 AutoClaw 来源）|
| 圆角半径 | 同文件 `R_PANEL / R_CARD / R_PILL / R_MD / R_SM` |
| 输入卡内部布局 | `_layout_input_card()`（Canvas 内手排：输入行 / 提示 / 工具条 / 圆形按钮）|
| 沉思三档 | `THINKING_LABELS` / `THINKING_CHOICES` + `_set_thinking_mode()` |
| 标签页 / 布局 | `_build_ui()` + `_build_manage_tab()` / `_build_client_tab()` |
| Gateway 启动参数 | `_start_gateway()`（默认 `--upstream openai`，可以加 `--model-map` 等） |
| 客户端超时 | `ForgeGatewayClient(timeout=60.0)` |

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
