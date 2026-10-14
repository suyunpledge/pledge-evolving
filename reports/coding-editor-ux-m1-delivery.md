# Coding Mode 改造 · M1 骨架交付报告（编辑器与布局域）

- 分支：`feature/coding-editor-ux`（worktree `C:/Users/匡溯昀/forge-coding-editor-ux`）
- 提交：`dc0f81f`（M1 单独成笔；前序三笔 `cab5e38`/`cce6d89`/`7dc6790`/`2903cda` 已获验收）
- 基线：`main @ 81acf1b` 之后本分支累计；`forge/development/*` 与 Codex 域文件**零改动**

---

## 1. 交付内容（任务书 §M1 三项）

| 项 | 落地 | 载体 |
|---|---|---|
| 顶部导航栏 | FORGE / Work / Coding / Project / Run / Settings | 新文件 `layout.py` → `NavigationBar` |
| 全宽底部工具面板容器 | 输出 Notebook 上移到外层竖向 pane，与三栏并列 → 跨三栏全宽 | 新文件 `bottom_panel.py` → `ToolPanel` |
| 多字段状态栏 | Git / Agent / Model / Project 四字段 + 右侧瞬态消息 | 新文件 `layout.py` → `StatusBar` |

### 1.1 顶部导航栏

- 模式项（Work/Coding）激活态经 **StringVar trace** 与 header 模式下拉共享同一真源
  （`_build` 内 `nav.bind_mode(self.mode)`），**未改** `_mode_changed` / `switch_mode`；
  迟到回调有 `TclError` 卫仪。
- 动作项经新增 `_nav_navigate` 分发：Project→`choose_project`、Run→`run_tests`、
  Settings→`model_settings`；模式项复刻 `switch_mode` 的 `capture_layout` 语义。
- 品牌字从 header 上移至导航栏（header 标签 `FORGE / Development` → `Development`），
  header 只留模式下拉与「模型角色 / 收起展开 IDE」按钮。

### 1.2 全宽底部工具面板

- 原输出 Notebook（Diff/终端/测试/问题/输出五页）此前在 `editor_frame` 内的竖向
  Panedwindow 上，只占中央一栏；M1 上移到**外层竖向 Panedwindow**（`self.vertical`），
  与三栏 `self.panes` 并列，因此天然全宽。
- `self.output` / `self.output_views` 两个别名保留既有调用点契约，
  `write_output` / `output.select(0)` 等**零改动**；五页名称与 Text 配置逐项一致，
  **不增不减页**（M5 再重整四标签 + Agent Events）。
- `state.layout['output']` 语义变为「上部区域占比」，旧持久化值（默认 .72）直接兼容。

### 1.3 多字段状态栏

- 四字段由 `_project_scan` → 新增 `_refresh_status_fields(scan)` **单一推送点**供电
  （扫描是打开项目与刷新的共同完成点，一处接线覆盖两种入口）。
- Agent/Model 取运行时与状态层真实值，**不读 Agent 栏控件**（Codex 域）：
  Model 取 `state.value('roles')` 的 default 角色在 catalog 里的标签，与「模型角色」窗口同源。
- 瞬态消息沿用 `self.status` StringVar（既有测试契约
  `assertIn('等待', app.status.get())` 保留），从独占一行变为右侧一条，文案行为零变化。

---

## 2. 51f6f82 保护清单沿用情况

| 机制 | 状态 |
|---|---|
| 三栏增量挂载（forget/insert 计位） | **原样保留**；底部面板只按 IDE 展开/收起做在装/不在装二态切换（新增 `_sync_bottom_panel`），**未新增第二条挂载路径** |
| `_placing_sashes` 防重入 | 未动 |
| `<Configure>` 回声抑制 | 未动 |
| sashpos 2px 容差 | 未动 |
| 避免全量 forget+add | 未动 |

新增卫仪：`place_sashes` / `capture_layout` 的竖向 sash 读写增加
`len(self.vertical.panes()) == 2` 判断——底部面板被收起时外层 pane 只剩一个 slave，
此时 `sashpos(0)` 越界（`test_bottom_panel_hides_with_collapsed_ide_and_returns`
守护此路径）。

---

## 3. M1 引入后又实测修复的两个 Packing 次序问题

（这是本轮最值得验收方复核的部分：都是真实回归，均有探针复现与修复验证）

1. **`_build_agent`：`log_tabs` 改为最后 pack。** Tk 在空间不足时按 Packing 序列
   **从末尾开始牺牲** Slave。9 个固定组原本垫在序列末尾，1200×760 + 底部面板后
   `context_label` 实测被取消映射（`winfo_ismapped()==False`）。改为固定组先 pack、
   `log_tabs` 垫后，让位的是可伸缩 log 区——9 控件在任何窗口高度保持可见
   （760px 下 log_tabs 压到 64px，控件全 mapped）。
2. **`_build`：状态栏以 `side='bottom'` 提前入 Packing 序列。** 总自然高度超限时
   Tk 牺牲序列末尾 Slave，状态栏垫后会被压成 **1px** 挤出视线（实测）。
   提前入队则让位的是 expand 的竖向 pane（其内部再由 log_tabs 次级让位），chrome 永不被挤掉。
3. `bottom_panel`：输出 Text `height` 8→6（原值为窄栏版调优；全宽后 8 行自然高度
   在 760px 级窗口挤压 chrome。sash 可拖，6 行只是自然尺寸下限）。

---

## 4. 自测结果（全部实机，非引用）

| 套件 | 结果 | 说明 |
|---|---|---|
| `test_layout_regression.py`（15 用例） | 全量 13 过 / 2 遇 TclError；**两例各自连续单跑 5/5 通过** | 按仲裁口径「全量挂+单跑过=环境项」判定为 Tk 初始化抖动 |
| `test_app.py` + `test_project.py` | **26 passed, 0 failed**（65.8s） | 无回归 |
| `test_workspace.py` + `test_workspace_wiring.py` | 16 passed, 2 failed | 见 §5，均非本轮回归 |
| `run.py selftest` | **574/574** | 基线不回退 |
| 结构探针（1200×760 / 1440×900） | 全过 | 见 §6 |

新增 M1 守护测试 7 个（`test_layout_regression.py` 第 4 节）：导航栏五项与激活态
跟随、动作项经回调分发、底部面板全宽（== 外层 pane 宽且宽于左右两栏之和）、
IDE 收起/展开底部显隐与次序、五输出页与别名契约、状态栏占位/更新/接线、
竖向 sash 持久化与 clamp。

---

## 5. 两个非本轮失败的定性（证据附后）

1. `test_workspace.py::test_workspace_summary_consistent` —— **测试缺陷**：断言
   `s["repo"].endswith("pledge-evolving")` 硬编码主仓库目录名，**任何 worktree 必然
   失败**，与仓库脏净无关（订正 Phase 1 报告中「依赖脏状态」的定性）。
2. `test_workspace_wiring.py::WorkspaceWiringTests::test_on_close_honours_bench_can_close`
   —— 全量跑遇 `TclError`（Tk 初始化失败），**连续单跑 5/5 通过**，按仲裁口径判环境项。

---

## 6. 实测验证记录（数值探针 + 真实截图）

结构探针（打开真实项目、布局稳定后测量）：

```
1200x760:  nav h=44  status_bar h=34  底部 h=161 w=1184 == 竖向 pane w=1184（全宽）
           9 控件 mapped=True  log_tabs h=64（正确让位）
1440x900:  nav h=44  status_bar h=34  底部 h=200 w=1424 == 竖向 pane w=1424（全宽）
           9 控件 mapped=True  log_tabs h=165
```

导航栏探针：8 个子控件全部 mapped；FORGE 品牌 x=12 w=69；Work/Coding 单元 +
1px 分隔线 + Project(66)/Run(45)/Settings(73) + 底部发丝线；`active_mode=development`。

状态栏字段（真实打开 pledge-evolving 后）：`Git main·干净 | Agent forge |
Model 继承 | Project pledge-evolving`，右侧瞬态行
`main · pledge-evolving · 0 个 Git 变更 · read-only`。

真实渲染截图：`pledge-evolving-qa/design/shots/workbench-m1.png`（1440×900，
顶部导航栏 / 三栏 / 全宽底部五标签 / 底部四字段状态栏均可见；左上角卡通挂件为
桌面宠物遮挡，非产品缺陷，FORGE 品牌由探针确认 mapped）。

---

## 7. 边界纪律（AST 方法级比对已验）

`app.py` 改动方法（7 改 / 3 新增 / 0 删除，60/67 个方法逐字节一致）：

- 改：`_build` / `_build_agent`（仅 pack 次序，容器层）/ `_build_editor` /
  `_project_scan`（一行接线）/ `apply_layout`（+`_sync_bottom_panel` 调用）/
  `capture_layout`（len==2 卫仪）/ `place_sashes`（len==2 卫仪）
- 增：`_nav_navigate` / `_sync_bottom_panel` / `_refresh_status_fields`
- 新增文件：`layout.py`、`bottom_panel.py`、`test_layout_regression.py`（第 4 节）

**未触碰**：`forge/development/*` 全部文件；`forge-gui/development_mode/` 下
`editor.py` / `runtime.py` / `state.py` / `evidence.py` / `project.py`（Codex 域）；
`gui_theme.py` / `ui_icons.py`（token 层本轮无增补——M1 未引入新色，裁决 1 的
「只增不改」暂无用例）。

---

## 8. 未包含（按 §9 顺序后续交付）

M2 欢迎页 / M5 底部四标签 / M3 Explorer / M4 编辑器增强 / M6 Review Focus /
§7 图标统一。M1 已完成并停手，等验收通过后继续 M2。
