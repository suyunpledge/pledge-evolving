# Coding Mode 编辑器与布局域 · Phase 1 审查报告

- 分支：`feature/coding-editor-ux`（worktree `C:\Users\匡溯昀\forge-coding-editor-ux`）
- 基线：`main @ 51f6f82` —— `fix(ui): Coding Mode IDE 展开收起闪烁——增量挂载 + Configure 自激环打断`
- 角色：编辑器与布局域（M1–M6）。本文件为 §5 要求的**先审查再动手**产出。
- 方法：源码通读 + 真实 Tk 几何实测（非静态推断）。所有几何数字来自
  `winfo_rooty()/winfo_height()` 实测，脚本见 §7 复现命令。

---

## 0. 结论摘要

| 项 | 结论 |
|---|---|
| 定位 | **Coding Mode = `DevelopmentWorkbench`**（`forge-gui/development_mode/app.py`）。`forge_gui_v2.py:960 _build_coding_surface` 明确"以现有 DevelopmentWorkbench 为基础（不重写 IDE）" |
| `workspace.py` 的归属 | `WorkspacePanel` 由 `forge_gui_v2.py:3474 _build_workspace()` 挂载在 **Work 空间**的 `self.split` 里，是 Coding 的姊妹面板，**不是 Coding Mode 本体**。它是**组件来源**，不是目标容器 |
| 最严重发现 | **P0-L1：Agent 栏在默认窗口尺寸下纵向溢出，5/11 子控件（整个任务组 + 模型组）渲染在窗口下边缘之外** —— 用户看不到权限、复审、恢复、子 Agent、取消、执行测试、任务角色、独立模型、模型回显 |
| 基线测试 | **1 个真实失败（我域）+ 2 个抖动 + 2 个环境依赖**。详见 §6 |
| 需仲裁 4 项 | ① §7 色板冲突 ② `state.py` 加第 4 预设 ③ `_build_agent` 越界修复归属 ④ shadow git 语义澄清 |

---

## 1. 现有布局体系盘点

### 1.1 骨架（`app.py:82-119 _build`）

```
header   FORGE / Development + 模式下拉 + [模型角色] [收起/展开 IDE]      pack fill=x
options  [打开项目][近期项目] [预设下拉] + path_label(单行提示)          pack fill=x
panes    ttk.Panedwindow(horizontal)                                    pack fill=both expand
         ├ project_frame  width=230   → _build_project()
         ├ editor_frame   width=650   → _build_editor()
         └ agent_frame    width=420   → _build_agent()   ← Codex 域内容
status   ttk.Label(textvariable=self.status)                            pack fill=x
```

`editor_frame` 内还有一条**竖向** `Panedwindow`：上 `tabs`(weight=4) / 下 `output` Notebook(weight=1)。

与任务书 §M1 骨架的差异：

| §M1 要求 | 现状 | 判定 |
|---|---|---|
| 顶部导航 | 只有 header + options 两行，无 `FORGE / Work / Coding / Project / Run / Settings` 语义栏 | 未实现 |
| 左项目区 | ✅ `project_frame` 230px | 已实现 |
| 中央编辑器 | ✅ `editor_frame` 650px | 已实现 |
| 右 Agent 工作区 | ✅ `agent_frame` 420px（容器） | 已实现（容器） |
| **底部工具面板（全宽）** | ❌ 输出区在 `editor_frame` 的竖向 pane 内，**不是全宽** | 结构不符 |
| 状态栏（多字段） | 单 `StringVar` 一行文本，无 `Git: / Agent: / Model: / Project:` 分栏 | 未实现 |

### 1.2 布局状态与持久化（`forge/development/state.py`）

```python
layout = {'visible': bool,          # IDE 展开/收起
          'preset': 'agent'|'editor'|'focus',
          'fractions': [f0, f1],    # 横向 2 条 sash 的比例
          'output': 0.72,           # 竖向 sash 比例
          'focus': 'editor'|'agent'}
```

- 单一来源 ✅：`ModeState._data['layout']`，`set_layout(**changes)` 统一写入。
- 持久化 ✅：`_save()` 用 `tempfile.mkstemp` + `fsync` + `os.replace` 原子落盘（`:75-85`）。
- 校验 ✅：`_validate_layout` 校验字段集、preset 白名单、`fractions` 长度 2 且 `.05<=f0<f1<=.95`、`output` `.15~.9`。
- 读取：`layout` property 返回 `deepcopy`，`value(key)` 同。

**与 §M6 的冲突**：任务书要 `Agent Focus / Code Focus / Review Focus` 三预设；现有
`'focus'` 的语义是"编辑器 / Agent 二选一聚焦"（`toggle_focus`），**不是 Review**。
现有 `agent`/`editor` 大致对应 Agent Focus / Code Focus，但 **Review Focus 无对应值**，
且新增第 4 个值必须改 `state.py:102` 的 preset 白名单 —— **该文件不在 §3 给我的独占清单内**，见 §8 待仲裁 ②。

### 1.3 51f6f82 保护清单现存形态（§4，必须沿用）

| 机制 | 位置 | 现状 |
|---|---|---|
| 增量挂载 | `apply_layout` `:1209-1241` | ✅ 逐 `forget` 非目标面板、逐 `insert` 缺失面板；`wanted.index` 计位，挂载后 `self.later(0, place_sashes)` |
| `_placing_sashes` 防重入 | `:1244-1264` | ✅ 进入置 True，`finally` 里 `root.after_idle` 复位 |
| `<Configure>` 回声抑制 | `_panes_resized` `:1266-1273` | ✅ 旗标为真直接 return；仅在 `event.width` 变化时才重排 |
| sashpos 2px 容差 | `:1256` / `:1260` | ✅ `abs(sashpos(i) - position) > 2` 才写 |
| 避免全量 forget+add | `:1225-1240` | ✅ 无全量路径 |

结论：**保护清单完整且有效**。M6 三预设切换必须复用 `apply_layout` 的这套增量逻辑，
只允许扩展 `wanted` 集合的构造规则，不允许新增第二条挂载路径。

---

## 2. 编辑器现状（`forge/development/editor.py` + `_build_editor`）

### 2.1 Document 已有能力（**强，不要重写**）

| 能力 | 实现 | 位置 |
|---|---|---|
| 编码识别 | BOM 判 utf-8-sig / utf-16，默认 utf-8；**strict 解码，不做 replacement 静默替换**，失败则要求用户显式指定 | `open()` `:58-72` |
| 换行保留 | 记录 `newline`（CRLF/CR/LF），保存时还原 | `:70` / `:171` |
| 外部修改检测 | `check_external()` 比对 sha256 `digest` | `:83-90` |
| 不可变观测 | `inspect_external()` 在工作线程读取，**不污染活缓冲区** | `:92-98` |
| 三路 reconcile | `reconcile()` → `unchanged` / `reloaded` / `conflict` | `:100-115` |
| 三方安全合并 | `merge_external()` 基于 `SequenceMatcher`，**重叠即拒绝**，仅内存，不写盘 | `:127-153` |
| 原子保存 | `tempfile` + `fsync` + `chmod 继承` + **二次 check_external** + `os.replace` | `_save()` `:167-193` |
| 写锁 | `FileRevisionGuard(self.boundary.root).lock` | `save()` `:155-165` |
| 权限边界 | `ProjectBoundary.path()` 拒绝越界与 `.git` 内部，并 `assert_public_path` | `:25-30` |
| 大小限制 | 2 MiB | `MAX_DOCUMENT` |

### 2.2 UI 层缺口（`_build_editor` `:156-197`、`_document_loaded` `:607-632`）

| §M4 要求 | 现状 |
|---|---|
| 多文件标签页 | ✅ `ttk.Notebook`；修改态用 tab 文案前缀 `* ` / 冲突 `! ` 表示 |
| 语法高亮 | ❌ 无（Pygments 需 Phase 1 评估，见 §8） |
| 行号 | ❌ 无 |
| 当前行高亮 | ❌ 无 |
| 搜索和替换 | ❌ 无 |
| 文件修改标记 | ⚠️ 仅 tab 文案前缀，无 gutter 标记 |
| 保存 / 另存为 | ⚠️ 有保存（Ctrl+S），**无另存为** |
| 快捷键 | ⚠️ 仅 Ctrl+S |
| 编码识别 | ✅（见 2.1） |
| 外部修改检测 | ✅（3s 轮询 + 手动比对菜单） |

### 2.3 可复用组件（`workspace.py`，我的独占文件）

`WorkspacePanel` 已有可直接搬来的编辑器零件：

- `_Minimap` `:473-554` —— 代码缩略图（含 `_line_color` 分级着色、点击跳转）
- `_sync_gutter` `:1017` —— 行号槽同步
- `_FileTab` `:309-385` —— 带关闭按钮、选中/悬停态的标签
- `_FileRow` `:387-471` —— 文件行（缩进、高亮）
- `_build_home_view` `:1225-1292` —— Home 卡（标题/路径/统计/最近文件/按钮）

⚠️ **复用前提**：`workspace.py` 全文**不引用 `ProjectBoundary` / `guard` / `assert_public_path`**
（已实测 grep 无命中）。它是 Work 空间的读导向面板。任何搬到 Coding 侧的文件操作
**必须重新接入 `ProjectBoundary`**，否则违反 §2 红线 4 与 §M3。

---

## 3. shadow git 查询接口盘点

### 3.1 `forge/checkpoint.py` 是什么

`CheckpointStore` 用 `--git-dir=<home>/checkpoints/shadow.git --work-tree=<workspace>`
维护一个**独立影子仓库**，只为写前快照 / 回滚服务（`snapshot` / `rollback` / `prune`）。
它的 `add -A` 会把工作区所有文件纳入，**不代表用户仓库状态**。

### 3.2 文件树 Git 状态的正确来源

`WorkspaceSource.status()`（`development_mode/project.py:15-25`）走的是：

```python
_run_git(root, 'rev-parse', '--show-toplevel')     # 真实仓库根
_git_status_map(root)                              # git status --porcelain=v1 -z -uall
```

→ 来自 `workspace.py:184 _git_status_map`，即**用户真实仓库**，且 `-z` 正确解析了
重命名/复制记录（`next(records)` 跳过源路径）。

### 3.3 结论（需要澄清任务书 §M3 措辞）

任务书 §M3 写"Git 状态必须来自实际仓库信息（**shadow git，参考 forge/checkpoint.py**）"。
实测判定：**括号里的指引与"与实际仓库一致"这条验收标准互相矛盾**。

- shadow git = Forge 自己的回滚仓库 → 用它显示会**错**（用户改动不出现在里面；`add -A`
  反而让未跟踪文件显示为已跟踪）。
- 现行 `project.py` 用真实 git → **符合验收项"Git 状态（文件树）与实际仓库一致"**。

**建议：保持现状（真实 git），不引入 shadow git 做文件树状态**，此项需在集成时向
主线确认，避免后续 Agent 按字面把状态源改成 shadow 而引入真缺陷。

### 3.4 可只读消费的 checkpoint 能力（供 M5 Git Diff / 文件恢复）

`runtime.checkpoint_list` / `checkpoint_preview` / `restore_checkpoint` 已接好 UI
（`checkpoints()` `:1107-1154`、`restore_reviewed_checkpoint()` `:1156-1180`），
且恢复前有"查看差异 → 确认 → 校验磁盘版本"的审查链。M5 的 Git Diff 可直接复用，
无需新增查询接口。

---

## 4. 问题列表（四档）

### A. 已实现且正常

| 项 | 位置 |
|---|---|
| 三栏布局 + 拖动 + 尺寸持久化 | `apply_layout` / `capture_layout` / `place_sashes` |
| 51f6f82 五项防闪烁机制 | §1.3 |
| IDE 展开/收起（增量挂载） | `toggle_ide` `:1192` |
| 三预设切换 + 状态持久化 | `change_preset` `:1203` / `toggle_focus` `:1197` |
| 文档编码识别 / 冲突 / 安全合并 / 原子保存 / 写锁 | §2.1 |
| 外部修改 3s 轮询 + 手动比对 | `_check_external` `:755` |
| Git 状态来自真实仓库 + `-z` 解析 | `project.py:15-25` |
| 文件树 / Git 变更双页 + 文件名搜索 | `_build_project` `:121-154` |
| 权限徽标常驻 + 高于 read-only 高亮 | `_sync_perm_badge` `:480` |
| 输出区 5 tab 且**明确不伪造结果** | `:197` 测试页声明"没有结构化结果时不推算通过数" |
| 打开/关闭项目的未保存拦截（模态） | `open_project` `:397-404` |
| 退出保护链 | `can_close` `:1287-1298` |
| 检查点预览/恢复审查链 | `:1107-1180` |

### B. 已实现但有交互问题

| ID | 问题 | 证据 |
|---|---|---|
| **P0-L1** | **Agent 栏纵向溢出，5/11 子控件在窗口外** | 实测 1200×900 与 1440×900（`launch()` 默认）下，`agent_frame` 11 个子控件中底部 5 个（2×Separator + 任务组 row2 + row3 + 模型组 row4）的 `rooty+height` > 窗口高度（903/940/977/985/1020 vs 900）。1200×900 下 `run_model.winfo_viewable()==0` | 
| **P1-L2** | 输出区不是全宽底部面板 | `output` Notebook 挂在 `editor_frame` 的竖向 pane 内，宽度 = 编辑器列宽，与 §M1 全宽不符 |
| **P1-L3** | Git Diff 两处重复 | 左栏"Git 变更"页选中即写底部 Diff tab（`:567-581`）；W5 报告"未实现"里的 **B3** 正是此项，需随 M5 一起设计 |
| P2-L4 | 文件名搜索下树形退化 | 命中时 `parent=''`（`_project_scan` `:528`），结果平铺无层级 |
| P2-L5 | Git 状态只显示 2 字符 code | `status + ' ' + path`（`:541`），用户看到的是 ` M`、`??` 而非语义化标记 |
| P2-L6 | 窗口最小尺寸无约束 | `launch()` 设 `minsize(760,540)`，但工作台内容约需 856px 高才不溢出 |

### C. 只有入口无后端

| ID | 问题 | 说明 |
|---|---|---|
| P1-B1 | "终端" tab 无真实终端会话 | 仅回显 `shell_exec` 的命令与输出（`:940-943`），不是可交互 PTY |
| P2-B2 | "问题" tab 无诊断采集 | 只收集 `tool` 失败与 `model_error` 等（`:944`、`:951`），无语法诊断 |
| P2-B3 | "近期项目" 只有 Toplevel 弹窗 | `recent_projects()` `:385-391`，数据 `state.value('recent')` 已有，未做成页内欢迎页 |

### D. 未实现

| ID | § | 项 |
|---|---|---|
| D1 | M1 | 顶部导航栏（FORGE / Work / Coding / Project / Run / Settings） |
| D2 | M1 | 全宽底部工具面板容器 |
| D3 | M1 | 多字段状态栏（Git / Agent / Model / Project） |
| D4 | M2 | 未打开项目的欢迎页（标题 + [Open Project] + Recent Projects） |
| D5 | M3 | 文件类型图标、新建/重命名/删除/复制路径/刷新、右键菜单 |
| D6 | M3 | Git 状态语义化（Modified/Added/Deleted/Untracked 图标 + 颜色 + 非颜色冗余） |
| D7 | M4 | 行号、当前行高亮、语法高亮、搜索替换、gutter 修改标记、另存为、完整快捷键 |
| D8 | M5 | Terminal / Output / Problems / Git Diff 四标签重构到全宽容器 |
| D9 | M6 | Review Focus 预设 |
| D10 | §7 | Explorer / 底部面板图标统一到 `ui_icons`（即 W5 未实现的 **C2**） |

---

## 5. M1–M6 组件边界与文件修改计划

### 5.1 新增文件（我的独占）

| 文件 | 责任 |
|---|---|
| `forge-gui/development_mode/explorer.py` | M3 Explorer：树 + Git 状态装饰器 + 文件操作（全部走 `ProjectBoundary`） |
| `forge-gui/development_mode/welcome.py` | M2 欢迎页（复用 `state.value('recent')`） |
| `forge-gui/development_mode/bottom_panel.py` | M5 全宽底部容器 + Terminal/Output/Problems/Git Diff 四标签（Agent Events 交 Codex） |
| `forge-gui/development_mode/editor_area.py` | M4 编辑器区：行号槽、当前行高亮、搜索替换、gutter 标记（复用 `workspace._Minimap`/`_FileTab`） |
| `forge-gui/development_mode/layout.py` | M1/M6 布局预设定义（三/四预设的 `wanted` 集合与权重），供 `apply_layout` 调用 |

### 5.2 `app.py` 计划改动的方法（§3 允许清单内）

| 方法 | 改动 |
|---|---|
| `_build_project` | 替换为 `explorer.py` 的 Explorer 挂载（保留 `self.tree` / `self.changes` 属性名以免破坏既有测试） |
| 编辑器区段构建 | 挂载 `editor_area.py`，补齐行号/高亮/搜索替换 |
| `apply_layout` | `wanted` 集合构造外移到 `layout.py`；**增量挂载逻辑原样保留** |
| `place_sashes` | 增加底部面板 sash（全宽容器） |
| `_panes_resized` | 不变（防重入与旗标逻辑不动） |
| `capture_layout` | 增加底部面板比例持久化字段 |
| `toggle_ide` | 不变 |
| `refresh_project` / `refresh_documents` | 改为委托 Explorer / EditorArea |
| 欢迎页入口 | 新增 `_build_welcome` + 未打开项目时切换显示 |

**不在计划内**：`_build_agent` 内部（Codex 域）、`send`/`handle_event`/`render_evidence`、
模型与权限逻辑。

### 5.3 `gui_theme.py` / `ui_icons.py`（token 层归我）

- 新增 `C['editor']`（编辑器底色）—— **纯新增，向后兼容**。
- 新增 Explorer/底部面板所需 token（文件状态色），同样只增不改。
- 图标：现有 48 个矢量图标，可直接用于 M3/M5 的有
  `file / files / folder_open / chevron_down / chevron_right / close / save / search / refresh / terminal / diff / play / stop / plus / minus / copy / edit / error / warning / info / check / code`。
  **缺**：`bug`（Problems）、`output`、`trash`、`pencil`（重命名）。
  方案：Problems 用 `warning`、删除用 `close`、输出用 `terminal`，不新增图标资源（避免重出 atlas）。

---

## 6. 基线测试结果（实测，非引用）

命令（在 worktree 根执行）：

```
python -m pytest forge-gui/development_mode/test_app.py \
                 forge-gui/development_mode/test_project.py \
                 forge-gui/test_workspace.py \
                 forge-gui/test_workspace_wiring.py -q
→ 5 failed, 39 passed in 85.62s
```

逐个定性（**不把抖动当通过，也不把环境依赖当回归**）：

| 用例 | 单跑 | 定性 |
|---|---|---|
| `test_agent_groups_separate_session_task_and_model_rows` | **仍失败** | 🔴 **真实失败**。`test_app.py:395` 断言 9 个 Agent 栏控件 `winfo_ismapped()`；实测 `run_model` 未映射。根因 = P0-L1 溢出 |
| `test_cancel_button_requests_stop_without_marking_worker_complete` | 通过 | 🟡 抖动（满载失败/单跑通过，W5 已记录同源） |
| `test_obsolete_disk_read_cannot_replace_newer_saved_baseline` | 通过 | 🟡 抖动，同上 |
| `test_workspace_summary_consistent` | — | ⚪ 环境依赖：断言要求 `panel._git_status` 非空（`test_workspace.py:290`），本 worktree 干净故无改动可测；主工作区有未提交改动时可通过 |
| `test_diff_visible_for_changed_and_unchanged` | — | ⚪ 环境依赖，同上 |

**结论：基线在我域内有 1 个真实红灯（P0-L1）。它是我要修的第一个目标，也是验证
M1 改造成效的天然回归哨兵。**

---

## 7. 复现命令

```bash
PY="C:/Users/匡溯昀/AppData/Local/Programs/Python/Python312/python.exe"

# P0-L1 溢出：多尺寸实测 Agent 栏越界控件数
cd C:/Users/匡溯昀/pledge-evolving-qa
"$PY" repro/r7_coding_baseline_probe.py

# 基线测试（worktree 根，注意 worktree 需有未提交改动才能跑通 test_workspace 两例）
cd C:/Users/匡溯昀/forge-coding-editor-ux
"$PY" -m pytest forge-gui/development_mode/test_app.py -q
```

> ⚠️ pytest 必须从 worktree 根执行，否则插件 worker 子进程 `import forge` 失败 → 假失败。

---

## 8. 风险分析与待仲裁事项

### 8.1 待仲裁（4 项，交集成负责人）

**① §7 色板与现有 `gui_theme.C` 全面冲突**

实测 10 项**全部不一致**：

| token | §7 规范 | 现有 `C` |
|---|---|---|
| bg | `#15191F` | `#11151C` |
| surface2 | `#1C222B` | `#19212D` |
| editor | `#11161C` | **键不存在** |
| border | `#343D49` | `#2C3446` |
| text | `#E5EAF0` | `#EDF1F7` |
| subtext | `#9BA7B5` | `#ABB5C5` |
| **accent** | `#D6B778`（金） | `#4664BB`（蓝） |
| ok | `#63C29A` | `#48B89A` |
| error | `#E47C83` | `#E57373` |
| warn | `#E9B96E` | `#E5B45C` |

`accent` 从蓝改金是**品牌级变更**，会波及所有域（Visual System 2.0 共享契约、
其他 Agent 正在消费的页面），超出"编辑器与布局域"边界。
**建议：沿用现有 `C` 调色板，仅新增 `C['editor']` 等缺失键**，按 §7 允许的
"调整需向后兼容并记录在 Phase 1 报告"执行。请仲裁确认。

**② Review Focus 需改 `forge/development/state.py`**

新增第 4 个 preset 必须放宽 `state.py:102` 的 `{'agent','editor','focus'}` 白名单。
该文件不在 §3 给我的独占清单（也不在 Codex 域）。
**建议**：由我提交一个仅含白名单扩展 + 校验兼容（旧值仍合法）的最小补丁，
交由主线合并；或主线直接放行后我再开工。

**③ P0-L1 的修复位置落在 `_build_agent`（Codex 域容器内部）**

越界的是 Agent 栏内部的 pack 顺序（`log_tabs` 用 `expand=True` 吃掉剩余空间后，
后面 8 个控件仍按 `side=top` 顺序堆叠，总高超出）。修法有两种：

- **方案 A（推荐，我改）**：只做容器重组——把 `context_label` 以下的固定组改为
  `side='bottom'` 先行占位，`log_tabs` 作为唯一 `expand` 区域。
  **不新增/删除/改名任何 Agent 控件，不改任何回调与语义**，纯 pack 顺序调整。
- **方案 B**：Codex 在其域内改。

若 Codex 同期也要动 `_build_agent`，需按方法级合并。请仲裁归属。

**④ shadow git 语义（§3.3）**：建议确认"文件树 Git 状态用真实 git"，避免后续
按任务书字面改成 shadow git 引入真缺陷。

### 8.2 技术风险

| 风险 | 缓解 |
|---|---|
| M6 高频切换预设重新引入闪烁 | 强制复用 `apply_layout` 增量路径；新增"连续切换 20 次"测试断言挂载集合稳定 |
| 复用 `workspace.py` 组件带入无权限路径 | 所有文件操作统一经 `ProjectBoundary`；新增越界用例 |
| 新增依赖（Pygments）破坏零依赖内核 | Phase 1 结论：**不引入**。语法高亮走 Tk `Text` tag 的自实现关键字着色（可选层），保持内核零依赖 |
| 全宽底部面板改动 `vertical` pane 结构 | `capture_layout` 的 `output` 字段语义需扩展（编辑器/输出比 → 底部面板高度），旧值做兼容读取 |
| 大仓库扫描卡顿 | 沿用 `_scan_busy/_scan_pending` 合并 + `_schedule_refresh(800ms)`；`_scan_tree(max_nodes=2000)` 上限保留 |

### 8.3 Codex 域交接契约

- 我提供：`self.agent_frame`（`ttk.Frame`，width=420）作为 Agent 工作区容器挂载点，
  已在 `_build()` `:111` 加入 `panes`。
- Codex 负责：其内部三标签（Conversation / Tasks / Agents）、对话渲染、模型配置、
  Agent Diff 反馈卡、Agent Events 内容。
- **我不读也不写** `_build_agent` 内部的控件语义与回调（③ 的方案 A 仅涉及容器 pack 顺序）。
- 底部面板的 **Agent Events 标签**：我只出标签容器与排布（§M5），内容由 Codex 填。
  交接方式：我在 `bottom_panel.py` 暴露 `agent_events_frame` 属性供其挂载。

---

## 9. Phase 2 开工建议顺序

1. **P0-L1**（先修红灯，恢复基线绿灯）—— 待 ③ 仲裁
2. **M1 骨架**：顶部导航 + 全宽底部容器 + 多字段状态栏（D1/D2/D3）
3. **M2 欢迎页**（D4，风险最低，可独立验收）
4. **M5 底部四标签**（D8，顺带解决 B3 去重）
5. **M3 Explorer**（D5/D6，权限集成放首位）
6. **M4 编辑器增强**（D7，复用 `_Minimap`/`_FileTab`）
7. **M6 Review Focus**（D9，依赖 ② 仲裁）
8. **§7 图标统一 C2**（D10，最后做，视觉回归量最大）

每步提交信息按 §3 要求列出动过的 `app.py` 方法名。
