# M6 三布局预设 + Review Focus 中央 Git Diff · 集成方验收裁决 · 2026-10-14

交付提交 `9c54ca8`（2026-10-14 17:04 +0800，WorkBuddy）。
集成方于 17:10 巡检轮独立复验；主 worktree 当时含 M7 进行中未提交改动
（editor_area/layout/ui_icons），故沿用 M3/M5 口径以 detached worktree
`forge-m6-verify-9c54ca8` 挂 `9c54ca8` 独立复验，主仓脏区零干扰。

## 独立复验结果（集成方环境：系统 Python 3.12.10 / Tk 8.6.15）

| 验收项 | 结果 | 备注 |
|---|---|---|
| `forge.selftest` | 574/574 | run.py selftest 直跑 |
| `test_app` | 23/23（43.1s） | 未复现存量 tcl8.6 IO 抖动 |
| `test_layout_regression` | 56/56（117.7s） | 49 存量 + 7 新增 LayoutPresetTests（含 27 subtests 行进内） |
| AST 边界审计 | app.py 0 删，新增 6：`_sync_central_view / _review_active / _apply_preset_emphasis / _explorer_show_diff / _render_review_diff / _write_review_diff` | 与申报清单逐项一致 |
| 域边界 | 改动仅 app/bottom_panel/explorer/layout/test_layout_regression 五文件 | 51f6f82 保护方法（apply_layout/place_sashes/_panes_resized）全部在位；Codex 域（forge/development）diff CLEAN 零改动 |

复现口径：`sys.path` 前插仓库根 + forge-gui 后 unittest（与 M4/M5 裁决一致）。

## AST 审计明细（fd6e855 → 9c54ca8）

- `app.py`：申报 9 改 6 增 0 删；独立复验 removed=NONE，added 与申报
  六项逐一吻合（`_apply_preset_emphasis / _explorer_show_diff /
  _render_review_diff / _review_active / _sync_central_view /
  _write_review_diff`）。
- `layout.py`：新增 PRESETS 表（agent/editor/review 三预设 wanted 集合
  与默认 fractions）+ docstring 同步，无既有方法改动——与申报一致。
- `explorer.py`：增 `ExplorerPanel.select_view` 公开入口（走既有
  ActivityBar.select 回调链，无第二切换路径）。
- `bottom_panel.py`：差异着色抽为模块级 `highlight_diff_lines` 单一真源，
  `_highlight_diff` 改委派——与申报一致。
- 删除：0。

## 功能面抽查（由 56 项回归背书）

- 三预设共享同一套工作区组件（不三套 IDE），差异仅在 sash 权重与内容
  侧重——LayoutPresetTests「预设表三模式共享组件」断言。
- review 预设三栏独立权重：review_fractions 独立槽，agent/editor 与
  review 互不污染；capture_layout 在 review 下双写两键。
- 中央 Git Diff 第三态：editor_frame 内 pack/pack_forget 切换，不销毁
  控件，打开文件与未保存编辑随 editor_body 保留；`_sync_welcome` 委派
  三态真源 `_sync_central_view`。
- 差异数据走 runtime.inspect_diff 同一执行路径（不重新实现 diff），
  异步取数 + 单调请求号防过期回包（`_diff_request` 同范式）。
- emphasis 增量切换不销毁（`_emphasized_preset` 防重）；模式切换/IDE
  收起不重置用户手动切过的视图与标签页。
- review 视图滚动条先 pack、正文最后 pack（M4 打包器超订坍缩同款修法）。
- 两尺寸（1440×900 / 1200×760）结构探针含 review 模式巡检：活动栏/
  文件树/编辑器/Agent 栏/底部面板全部 mapped 且零越界（回归套件内存量
  双尺寸断言 + LayoutPresetTests）。

## 结论

M6 通过验收。WorkBuddy 编码编辑器域 M2–M6 全模块验收完成，可按序进入
合并窗口（布局域先合）。M7（状态栏字段扩展）已在主 worktree 进行中，
不阻塞合并窗口，但建议 M7 待合并基线就位后再交付，避免跨基线 rebase。
