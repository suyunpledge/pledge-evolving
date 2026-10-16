# Forge GUI 概念图视觉接入

日期：2026-10-16（Asia/Shanghai）。基线 `ddace89b4599b49cf6574df8c161a10cee4b0610`；独立分支 `feature/coding-visual-integration`；实现提交 `120deae`。

本轮修改正式 `forge-gui` 主题和组件，不是再做一个 HTML 概念页。未修改 Agent/Runtime/Policy、插件市场、Desktop Awareness 或工作区状态协议；不重构主窗口与分栏控制。未重启用户正在使用的 GUI、未合并主线、未推送。

## 从参考图提取的视觉语言

- 近黑蓝的顶部与代码区、稍亮的侧栏、独立的卡片和输入面：通过明度层次组织信息。
- 蓝色用于导航、选中和焦点；紫色区分 Agent 身份；绿色/红色保留执行结果语义。品牌暖橙火花继续复用。
- 使用现有字体、图标与语法高亮 API，Coding 的树与标签采用更紧凑的局部密度，Work 保留舒适的行高。
- 参考图的彩色虚线是分区说明，不作为产品装饰边框；没有为了接近图稿伪造在线人数、进度、测试数或任务结果。

## 解决的实际问题

原主题只覆盖部分 ttk 控件：TLabel、分组框、分栏等会回退到系统浅色。Notebook 的 clam 状态映射甚至会覆盖普通 configure 的颜色，造成选中态浅色边缘。旧版截图可以看到米白标题底、亮白边框与粗亮滚动条。

`gui_theme.apply_ttk_theme` 现在覆盖基础元素、弹窗与经典 Tk 输入/按钮/选择控件，并明确覆盖 Notebook 状态映射。新增局部 Coding Notebook/Treeview、Card 与 Primary 样式；基础色和字体仍以共享 token 为单一来源。

| 层级 | 颜色 |
|---|---|
| 主 chrome | `#0D141F` |
| 代码区 | `#0B131F` |
| 侧栏 | `#111C2B` |
| 卡片 | `#172439` |
| 边界 | `#26364D` |
| 主操作 / 文字强调 | `#2454AD` / `#6AAEFF` |
| Agent 身份 | `#B79AF9` |

Conversation 增加行距与留白、角色颜色；规划/工具/Diff 卡使用同一深色卡片样式。编辑器沿用原语法高亮，只调整共享颜色和当前行底色。结果文本补充真实 unittest `... ok` / `OK` / `... FAIL` / `... ERROR` 着色，不从这些文字推算测试数量。队列提示与 Diff hunk 使用有足够对比度的文字强调色。

## 文件与边界

| 文件 | 范围 |
|---|---|
| `forge-gui/gui_theme.py` | 共享 token、基础控件/状态覆盖、Coding 局部密度、卡片与滚动条样式 |
| `development_mode/agent_panel.py` | 对话与卡片样式、角色文字色、Notebook/Treeview 样式选择 |
| `development_mode/explorer.py` | 只选择紧凑 Notebook/Treeview 样式 |
| `development_mode/editor_area.py` | 只调整当前行背景 token |
| `development_mode/bottom_panel.py` | Notebook 样式、Diff hunk 文字颜色 |
| `development_mode/layout.py` | 品牌文字与模式选中背景颜色；未改 PRESETS 或状态栏布局 |
| `development_mode/app.py` | 仅队列提示色及 `_paint_result_lines` 展示着色 |
| `forge-gui/test_coding_visual_integration.py` | 6 项覆盖、对比度、滚动、内容/权限不变和源码边界测试 |
| `tools/preview_coding_visual.py` | 隔离真实 GUI 截图夹具，可指定另一 checkout 生成同条件 before 图 |

边界测试确认 `apply_layout/place_sashes/_panes_resized/capture_layout/toggle_ide/send/handle_event/permission_changed` 原文与基线一致。原 `_build_editor` 也保持原文，Diff hunk 的最终颜色统一在共享着色函数处理。

## 真实截图

均为 1440×960 的实际 Tk 窗口，由 Pillow 12.3 的 `window=HWND` 捕获本测试进程的窗口，包含真实控件；未绘制替代 UI、未编辑图片。before 和 after 采用同一最终夹具，只切换 source-root。截图数据为临时项目；文件读取走真实 Development Runtime 与工具，模型传输为离线夹具。底部 unittest 确实执行并退出 0，画面注明不是用户项目测试，任务测试徽标不伪造数量。

- [接入前](coding-visual-before.png)
- [接入后的 Coding Mode](coding-visual-after.png)
- [接入后的 Work Mode 主窗口](coding-visual-work.png)

两个模式的最终截图 Python callback_errors 均为空。测试项目、home、模型配置均隔离，不读取用户 API 凭据。

复现（完整 Python 3.12.10 / Pillow 12.3）：

```powershell
& 'C:\Users\匡溯昀\AppData\Local\Programs\Python\Python312\python.exe' -B tools/preview_coding_visual.py --snapshot after.png --work-snapshot work.png
```

before 可添加 `--source-root C:\Users\匡溯昀\pledge-evolving`；必须先核实该 checkout 仍是目标基线。已保存的 before 确认来自 `ddace89`。

## 验证结果

最终按模块独立进程运行，均退出 0：

| 套件 | 通过 |
|---|---:|
| 新增视觉接入 | 6/6 |
| 既有视觉身份 | 3/3 |
| 视觉系统/模式切换/DPI | 9/9 |
| Development app | 18/18 |
| Agent panel | 21/21 |
| 布局回归 | 64/64 |
| Workspace wiring | 12/12 |
| Forge 全量 selftest | 574/574 checks |

GUI 共 133 项；最终经典 Tk 控件补充覆盖后，视觉接入/视觉身份/视觉系统 18 项再次全过。JSON 保留初始源码 hash 与最终 hash，并单列补充复验，避免将不同版本的测试混写成一次运行。未声称运行整个仓库的 unittest 全集。

初次一进程组合运行 127 项有 3 个失败子断言：受保护 `_build_editor` 中一个颜色常量被修改（已恢复原文，改由公共着色函数处理）；1200/1440 两个状态栏映射等待超时。该状态栏测试单独复验通过，最终完整布局组 64 项通过，未降低断言或修改状态栏。疑似组合运行的 Tk/DPI 状态影响，但原因未证实，初次失败日志保留。

最终 app 与 agent_panel 分组日志各有 2 条已销毁 `_fire` 回调的 Tcl 告警，unittest 未失败。本轮未修改其调度/销毁代码，也未将此告警宣称已修复。原始日志位于 `C:\Users\匡溯昀\forge-coding-visual-evidence`，验证 JSON 记录每组命令、退出码及日志 SHA256。

## 限制

- computer-use Node 运行时初始化/重试/重置后均报 `failed to write kernel assets: 系统找不到指定的路径。 (os error 3)`。因此采用自有测试窗口截图与真实 Tk 自动测试；未声称完成鼠标拖拽、原生弹窗和长时间人工视觉验收。
- 未改变 Windows 系统外观，原生文件选择器/消息框由系统控制。
- 本轮为视觉接入；已有回调销毁告警及其它业务缺陷仍需单独处理。
- 已接入正式 GUI 源码，但留在独立分支供集成；现有运行进程不会热重载这些颜色。
