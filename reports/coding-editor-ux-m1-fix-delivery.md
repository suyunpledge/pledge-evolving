# Coding Mode 改造 · M1-fix 交付报告（验收打回项修复）

- 分支：`feature/coding-editor-ux`（worktree `C:/Users/匡溯昀/forge-coding-editor-ux`）
- 提交：`dd8e44e`（单独成笔；父提交 `38175e1` = M1 交付，基线 `2903cda`）
- 改动面：**仅** `forge-gui/development_mode/app.py` 一个文件（+45/-3）；`forge/development/*` 与 Codex 域文件**零改动**
- 修复期间 M2 暂停，维持未动

---

## 1. 打回项与修复要求对照

| 要求 | 落实情况 |
|---|---|
| 排查 toggle_focus / change_preset 路径上 M1 新增代码对 pane 重排的影响，找根因 | §3 完整根因链（含复现开关与探针证据） |
| 两用例在用户环境全绿 **且** 在非交互会话下连续 5/5 通过 | §4.1：控制台 5/5 + 5/5，pythonw+捕获 5/5 + 5/5 |
| 单独成笔提交，附根因说明 | `dd8e44e`，commit message 含根因与改动方法清单 |
| 不改 Codex 域、不碰 51f6f82 保护清单 | §7 AST 审计：68 个方法逐字节一致，保护方法全部字节级未动 |
| M2 暂停 | 维持暂停，等本项验收通过 |

---

## 2. 打回项现象（验收方实测，修复前）

- M1 提交（`38175e1`/`dc0f81f`）在验收环境（非交互后台会话、DPI-unaware、tk scaling=1.332、screen 1707x1067）：
  `test_focus_toggle_contract_survives_button_removal` 单跑 **0/7**（2 次首跑 + 5 连跑），断言均为 `input.winfo_ismapped()==0`
- 基线 `2903cda` 同环境 3 跑 2 过 1 挂（~75-80%，存量 flaky）；统计差异 p≈1e-4——M1 把偶发变成必现
- 探针：toggle_focus 后 agent pane 整个 unmapped（`ismapped=0`，**h=427 不变**），`panes.panes()` 只剩 frame3
- `test_scrollbar_and_focus_presets_in_five_tk_scales` 同一断言路径 0/7，一并修复

---

## 3. 根因链（本轮修复的核心结论）

**ttk::panedwindow 的 `add`/`insert` 不同步映射 slave**——slave 的映射发生在随之排队的**延迟重排**（idle 任务）里，约需 2 次事件循环迭代；`update_idletasks()` 可令其同步落地（合成实验 + 真实用例双验证）。

失败机制：

1. `toggle_focus → apply_layout → panes.add(agent_frame)` 后，agent pane 的映射在 idle 队列里排队；
2. 测试夹具 `step()` 最多 16 次 `dooneevent(DONT_WAIT)`——后台/无控制台会话中该预算被 OS 窗口突发占满（插桩实测失败跑 `events processed: 16` 触顶），idle 队列未排空；
3. 延迟重排不执行 → agent 栏连带 `input` 保持 `winfo_ismapped()==0`，而几何值（h=427）是最后一次成功重排的**陈旧值**——与验收方探针「h=427 不变」逐项吻合。

基线与 M1 的差异：基线事件级联短、16 预算够用（75-80% 通过）；M1 的竖向 pane + 底部面板同步 + 额外 sash 写入拉长事件级联，把抖动变成后台会话必现。

复现开关（已实证）：pytest 的 fd 输出捕获 + pythonw（无控制台）。带捕获 3/7 挂，`-s` 7/7 过——解释了为何控制台开发机只表现为偶发。

---

## 4. 自测结果（全部实机）

### 4.1 申报口径：双环境 5/5（验收标准）

| 用例 | 控制台（用户环境） | 非交互会话（pythonw + pytest 默认捕获） |
|---|---|---|
| `test_focus_toggle_contract_survives_button_removal` | **5/5** | **5/5**（修复前 0/7） |
| `test_scrollbar_and_focus_presets_in_five_tk_scales` | **5/5** | **5/5** |

### 4.2 全量回归

| 套件 | 结果 | 说明 |
|---|---|---|
| `test_app.py` 敌对环境全量 | **23 passed**（76.8s） | 修复前同环境首跑 3 挂（含两打回项） |
| `test_app.py` + `test_project.py` 控制台 | **26 passed, 0 failed**（72.7s） | 与 M1 报告口径一致 |
| `test_layout_regression.py` 敌对环境 | **17 passed + 17 subtests**（39.5s） | M1 结构守护 7 用例在内 |
| `test_layout_regression.py` overflow 断言单跑 | **5/5** | 与 M1 验收口径一致 |
| `run.py selftest` | **574/574** | 基线不回退 |

### 4.3 两尺寸结构探针（复刻 M1 §6 测量项）

```
1200x760:  nav h=44  status_bar h=34  bottom h=161 w=1184 == vertical w=1184（全宽）
           agent 控件 mapped=11/11  log_tabs h=64（正确让位）
1440x900:  nav h=44  status_bar h=34  bottom h=200 w=1424 == vertical w=1424（全宽）
           agent 控件 mapped=11/11  log_tabs h=165
```

与 M1 交付报告**逐项一致**——冲刷只改变映射落地时机，不改变稳态布局。

---

## 5. 控制台全量游走 TclError 的定性（非本轮回归）

控制台全量跑（test_app / layout_regression）三轮各遇到 0-2 个 `TclError`，失败项**逐轮不同**（obsolete_disk_read / reopen_project / attachment / save_and_external_change / log_tabs / cancel_button…），且：

- 错误实锤为 `_tkinter.create()` 启动时读不到 Tcl 脚本库（`tk.tcl` → `entry.tcl` / `spinbox.tcl`，逐轮换文件）——**解释器创建阶段的文件 IO 竞争**，早于任何应用代码执行，与本修复不可能存在因果；
- 每个受影响用例单独跑均通过（含本轮全部失败项）；
- 修复前代码（stash 对照）同环境同现象；
- 与 M1 验收已认可的「TclError 抖动」定性同一类（敌对环境全量反而 23/23、17/17 全绿）。

按仲裁口径「全量挂 + 单跑过 = 环境项」判定为**存量环境抖动**。

---

## 6. 性能影响（量化）

修复前曾观测到两用例耗时 ~2x 增长，本轮归因确认那是**插桩副本 + 机器负载的测量污染**。干净口径实测：

| 口径 | 修复前 | 修复后 |
|---|---|---|
| 两用例合计（控制台 pytest） | ~4.9s | 4.43-4.75s |
| focus_toggle 单跑（敌对环境） | 1.8-2.5s | 1.46-2.07s |
| `_settle_mount` 单次开销 | — | ~28ms（2 用例共 14 次、390ms） |

冲刷成本 = `update_idletasks()` 服务当时堆积的 idle 回调（含整窗重绘），无实质回归；冲刷只在**挂载集合变化**时发生，稳态布局路径零开销。

---

## 7. 边界纪律（AST 方法级审计）

`app.py` 改动方法（2 改 / 1 新增 / 0 删除，70→71 方法、**68 个逐字节一致**）：

- 改：`apply_layout`（增 `mounted` 跟踪 + 冲刷调用；forget/insert/add 增量挂载逻辑本身零改动）/ `_sync_bottom_panel`（改为返回是否改变挂载集合，原忽略返回值）
- 增：`_settle_mount`（`update_idletasks` + `_settling` 重入卫仪）

51f6f82 保护清单核验（全部字节级未动）：

| 机制 | 状态 |
|---|---|
| 三栏增量挂载（forget/insert 计位） | 原样保留，未新增第二条挂载路径 |
| `_placing_sashes` 防重入（`place_sashes`） | 逐字节一致 |
| `<Configure>` 回声抑制（`_panes_resized`） | 逐字节一致 |
| sashpos 2px 容差（`capture_layout`） | 逐字节一致 |
| 避免全量 forget+add | 未动 |

**未触碰**：`_build` / `_build_agent` / `_build_editor` / `toggle_focus` / `change_preset` / `toggle_ide` / `_mode_changed` 逐字节一致；`forge/development/*` 与 Codex 域（`editor.py` / `runtime.py` / `state.py` / `evidence.py` / `project.py`）零改动。

---

## 8. 未包含

M2 欢迎页 / M5 底部四标签 / M3 Explorer / M4 编辑器增强 / M6 Review Focus / §7 图标统一。M1-fix 已完成并停手，等本项验收通过后继续 M2。
