# M5 底部面板四标签 · 集成方验收裁决 · 2026-10-14

交付提交 `7ad54e6`（2026-10-14 15:26 +0800，WorkBuddy）。
集成方于 15:40 巡检轮独立复验；主 worktree 当时含 M6 进行中未提交改动
（app/bottom_panel/explorer/layout），故按 M3 口径以 detached worktree
`forge-coding-editor-m5-verify` 挂 `7ad54e6` 独立复验，主仓脏区零干扰。

## 独立复验结果（集成方环境：系统 Python 3.12.10 / Tk 8.6.15）

| 验收项 | 结果 | 备注 |
|---|---|---|
| `forge.selftest` | 574/574 | 直跑退出码 0 |
| `test_app` | 23/23（140.8s） | 未复现存量 tcl8.6 IO 抖动 |
| `test_layout_regression` | 49/49（188.6s） | 28 存量 + 12 M4 EditorArea + 9 新增 BottomPanelTests |
| AST 边界审计 | app.py 类方法 2 改 0 增 0 删；bottom_panel.py 3 改 + 4 新增 | 与申报逐项一致（见下） |
| 域边界 | `app.py`、`bottom_panel.py`、`test_layout_regression.py` 三文件外零改动（另有本裁决前置 m4-verdict 笔误修正） | explorer/welcome/layout/editor_area 及 Codex 域零触碰 |

复现口径：`sys.path` 前插仓库根 + forge-gui 后 unittest（与 M4 裁决一致）。

## AST 审计明细（d883e73 → 7ad54e6）

- `app.py`：`DevelopmentWorkbench._build` / `refresh_project` 改动；新增
  `_scan_done` 为 `refresh_project` 内嵌套闭包（方法级粒度不计增），与申报
  「_scan_done 内补 bottom.refresh_badges()」一致。
- `bottom_panel.py`：改 `ToolPanel.__init__ / _add_page / select`；新增
  `ToolPanel.refresh_badges / _highlight_diff` + 模块函数 `count_tests /
  count_problems`——均在申报清单内，无未申报方法。
- 删除：0。

## 功能面抽查（由 49 项回归背书）

- 四标签结构（差异/终端/测试/问题）与徽标分布断言；views 五键保留、
  「输出」→「测试」别名契约、`select(0)` 语义、`write_output` 调用点零改动。
- 徽标真实数据源端到端：真实 git 仓库 22 变更 → 差异(22)；扫描完成回调即
  刷新（不等切标签）；测试/问题计数合成（失败工具+失败测试+失败审查）；
  无数据 0 不伪造。
- 差异页 +/−/@ 行着色（新增 ok / 删除 error / hunk accent），disabled 态
  临时切 normal 后还原。
- 两尺寸（1440×900 / 1200×760）结构探针含底部标签与计数断言，全部 mapped
  零越界（BottomPanelTests + 存量双尺寸断言）。

## 已知边界（接受）

- 问题徽标无独立诊断器数据源（语法/警告类），以真实失败记录计数，不伪造。
- 「跳转对应文件」未实现不伪装。
- Agent Events 标签属 Codex 域，待其按 `docs/coding-agent-panel-api.md` 的
  M5 接线说明经 ToolPanel 公开 API 增补。

## 裁决

**M5 验收通过，M6（三预设 / 状态栏字段扩展）放行。**

合并窗口状态：M2/M3/M4/M5 已验收入库；Codex Agent 面板两轮已验收入库
（21/21 + 68/68×2 + AST 8/8）。待 M6 交付验收后触发：WorkBuddy 布局域先合
→ Codex Agent 域后合 → clash 7897 推送。
