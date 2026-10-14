# Coding Mode 示例图重构 · M3 Explorer · 验收裁决

- 对象：WorkBuddy（Coding Mode 布局域）
- 分支：`feature/coding-editor-ux`（worktree `C:/Users/匡溯昀/forge-coding-editor-ux`）
- 提交：`057b4b7`（单独成笔；父提交 `ddc1e63` = M2 验收裁决入库，基线即此）
- 改动面：`app.py`（+93/-56，行区纪律内）、`explorer.py`（新文件 566 行）、`test_layout_regression.py`（+186，新增 ExplorerTests 7 用例）；`forge/development/*` 与 Codex 域文件零改动
- 裁决：**通过 ✅，M4 放行**

## 集成方独立复验（2026-10-14 10:14–10:20，独立 worktree）

验证方式：`git worktree add .openclaw/tmp/m3-verify 057b4b7` 干净检出，四套件在隔离环境跑，不触碰主 worktree 中 WorkBuddy 未提交的 M4 进行中改动（`app.py` 未暂存修改 + `editor_area.py` 未跟踪，均原样保留）。

| 套件 | 结果 | 备注 |
|---|---|---|
| forge selftest | **574/574** ✅（退出码 0） | 基线不回退 |
| test_app | **23/23** ✅ | 82s 全绿，无 Tcl 抖动 |
| test_layout_regression | **28/28** ✅（21 基线 + 7 新增，60s 本轮零失败，无需 solo 复跑） | 含 subtests 全绿 |
| test_project | 3/3 ✅ | 顺带巡检 |
| 两尺寸布局回归 | ✅ | `test_agent_column_does_not_overflow_at_default_and_narrow_sizes` / `..._controls_all_mapped_at_both_sizes` 在 1440×900 与 1200×760 双循环内断言全绿（test_layout_regression.py L165/L187） |

## 提交内容核对（对照 M3 派单）

1. 48px 活动栏（Activity Bar）挂载契约 + 视图切换——`test_activity_bar_mounts_with_contracts` / `test_activity_bar_switches_views` ✅
2. 文件树 + 文件操作（创建/重命名/删除走边界）——`test_file_ops_create_rename_delete_through_boundary` ✅
3. 越界路径拒绝——`test_boundary_rejects_paths_outside_project` ✅
4. Git 状态语义化（字母徽标渲染 + 真实 Git 扫描喂数）——`test_git_status_semantics_rendered_with_letter_badges` / `test_scan_populates_semantic_status_from_real_git` ✅
5. 收起全部时清空展开态——`test_collapse_all_clears_expanded_state` ✅
6. 单模块单笔提交（app.py + explorer.py + 回归测试，无夹带），commit message 含模块说明 ✅
7. M2 放行纪律 ①–⑤ 逐项满足：新代码归位新文件 `explorer.py`，未绕开 51f6f82 保护清单与 `_settle_mount` 冲刷机制，未碰 Codex 域文件 ✅

## M4 放行纪律（不变）

- ① 新代码归位计划新文件（编辑器增强 → `editor_area.py`），app.py 只动计划清单内方法
- ② 51f6f82 保护清单 + M1-fix `_settle_mount` 冲刷机制均不得绕开
- ③ 不碰 `forge/development/*` Codex 域文件
- ④ 单模块单笔提交，附自测（selftest 574 基线 + test_app/test_layout_regression）
- ⑤ M4 完成后停手等验收，通过后再继续 M5

主 worktree 中 M4 进行中改动（未暂存）不受本裁决提交影响。

---

* CodingMode改造巡检 cron 代理验收；证据：worktree `m3-verify` exec 会话记录（selftest 574/574 exit 0、test_app 23/23、layout_regression 28/28、test_project 3/3）。*
