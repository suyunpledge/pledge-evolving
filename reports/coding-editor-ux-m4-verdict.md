# M4 编辑器增强 · 集成方验收裁决 · 2026-10-14

交付提交 `868bc96`（2026-10-14 13:23:30 +0800，WorkBuddy/Step-5-Preview）。
集成方于 13:32 巡检轮独立复验，主 worktree 当时干净（M4 已成笔、M5 未开工）。

## 独立复验结果（集成方环境：系统 Python 3.12 / Tk 8.6）

| 验收项 | 结果 | 备注 |
|---|---|---|
| `forge.selftest` | 574/574 | 本轮整跑一次通过 |
| `test_app`（控制台整跑） | 23/23 | 未复现存量 tcl8.6 IO 抖动 |
| `test_layout_regression` | 40/40（28 存量 + 12 新增 EditorAreaTests） | 含双尺寸（1440x900 / 1200x760）栏内零越界断言 |
| AST 边界审计 | 73->77 方法，68 逐字节一致，0 删除 | 新增恰为 `_copy_current_path / _editor_more_menu / save_as_current / split_current`，与申报一致 |
| 域边界 | `app.py`（+166）、`editor_area.py`（新 414 行）、`test_layout_regression.py`（+226）三文件外零改动 | explorer/welcome/layout/bottom_panel 及 Codex 域零触碰 |

复现口径：`sys.path` 前插仓库根 + forge-gui 后 unittest discover（与 run_development.py 的 ROOT 注入一致）。

## 功能面抽查

- 标签行「Notebook 左弹性 + actions 右固定」双尺寸 mapped（回归用例断言）。
- 搜索替换（Ctrl+F/Esc、计数徽标、上一个/下一个/替换/全部替换）、行号槽与正文同字体共享竖向滚动、当前行高亮、gutter add/mod 修改标记（>4000 行放弃属大文件保护）、分屏只读镜像同步与隐藏、另存为原子写 + 边界拒绝 + 按键迁移——均有对应用例且全绿。
- 打包器超订坍缩两处修复（actions 先 pack、滚动条先于 expand 正文）有 1200x760 实测复现记录与根因分析，同族第四/五例归档。

## 已知边界（接受）

- 分屏为同文件只读镜像（非独立编辑组），属编辑器域后续演化。
- 修改标记为行级 best-effort（拆行等场景可能显示 add 而非 mod）。
- VS Code Dark+ 精确色值与主仓 One Dark 系 token 差异列报告交集成方（本裁决记录在案，M5/M6 或合并窗口时统一决策）。

## 裁决

**M4 验收通过，M5（底部四标签计数徽标）放行。**

合并窗口状态：M2/M3/M4 已验收入库；待 M5、M6 交付验收后触发（WorkBuddy 布局域先合 -> Codex Agent 域后合 -> clash 7897 推送）。