# Coding Mode 示例图重构 · M3 Explorer · 验收裁决

- 对象：WorkBuddy（Coding Mode 布局域）
- 分支：`feature/coding-editor-ux`（worktree `C:/Users/匡溯昀/forge-coding-editor-ux`）
- 提交：`057b4b7`（单独成笔；父提交 `ddc1e63` = M2 验收裁决入库，基线即此）
- 改动面：`app.py`（+93 行区纪律内 2 方法 + 导入行）、`explorer.py`（新文件 566 行）、`test_layout_regression.py`（+186，新增 ExplorerViewTests 7 用例）；`forge/development/*` 与 Codex 域文件零改动
- 裁决：**通过 ✅，M4 放行**

## 集成方独立复验（2026-10-14 10:2x，独立 worktree `forge-editor-m3-verify` @ 057b4b7 干净提交）

> 复验方法说明：主 worktree 在 M3 成笔后立即进入 M4 开发（`split_current` 中间态导致 app 构造失败），为避免干扰交付方进行中工作，验收在独立 git worktree 检出 057b4b7 干净提交执行。首复验在主 worktree 跑出的 46 errors 全部归因于 M4 WIP（`_build_editor` 引用尚不存在的 `split_current`），与 M3 提交质量无关。

| 套件 | 结果 | 备注 |
|---|---|---|
| run.py selftest --coding | **574/574** ✅（退出码 0） | 基线不动 |
| test_app | **23/23** ✅（78.9s） | 全绿，无 Tcl 抖动 |
| test_layout_regression | **28/28** ✅（75.0s，含 ExplorerViewTests 7 新用例 + 17 subtests） | 比交付方申报的"控制台 27/28（1 项存量 tcl IO 抖动）"更干净，本轮未遇抖动 |
| test_project | 3/3 ✅（3.2s） | 顺带巡检 |
| 两尺寸布局回归 | ✅ | 1440×900 / 1200×760 不越界断言在 layout_regression 28/28 内全绿 |

## 提交内容核对（对照 M3 派单与 commit message）

1. 48px 活动栏（文件/搜索/Git/运行/扩展/设置，ui_icons 统一矢量图标 + 激活/悬停重绘）✅
2. 文件面板：标题「文件」+ 新建/折叠/刷新 + 文件树（FILE_TYPE_ICONS 类型图标）✅
3. Git 变更语义化：normalize_status（porcelain 双字符码 → M/A/D/U/R/C/X）+ 字母徽标/主题色/中文标签三重冗余 ✅
4. 文件操作全部经 ProjectBoundary 校验（项目外与 .git 内拒绝、删除前确认）✅
5. expanded_getter 注入解决 open_project 重绑定失效隐患（WelcomeView provider 范式）✅
6. 搜索框行为不变（Enter → refresh_project），活动栏「搜索」聚焦 ✅
7. 占位视图明示未接入，不伪造内容 ✅
8. 已知边界 docstring 明示（重命名/删除不联动已打开标签页；模型跟随归 M4）✅
9. 单模块单笔提交，commit message 含改动方法清单与自测数据 ✅

## 域边界审计

- git show --stat 确认仅 3 文件（app.py / explorer.py / test_layout_regression.py），Codex 域与 `forge/development/*` 零触碰 ✅
- 交付方申报 AST 方法级审计：73→73 方法、71 逐字节一致，改动恰为 `_build_project`/`_project_scan`（M3 计划清单内）✅
- ⚠️ 合并窗口注意：WorkBuddy 改了 `_build_project`（Codex 八保护方法清单成员，但其基线 998d3fc 的 AST 承诺是"交付时未动"）；按既定顺序 WorkBuddy 先合、Codex 后合时 app.py 不同方法 hunk 应可自动合并，届时需复跑双域全套件

## M4 放行纪律（不变）

- ① 新代码归位计划新文件（editor_area.py 已见 WIP），app.py 只动计划清单内方法
- ② 51f6f82 保护清单 + M1-fix `_settle_mount` 冲刷机制均不得绕开
- ③ 不碰 `forge/development/*` Codex 域文件
- ④ 单模块单笔提交，附自测（selftest 574 基线 + test_app/test_layout_regression + 新增用例）
- ⑤ M4 完成后停手等验收，通过后再继续 M5

---

* CodingMode改造巡检 cron 代理验收；证据：独立 worktree exec 会话记录（selftest 574/574 exit 0、test_app 23/23、layout_regression 28/28、test_project 3/3，均退出码 0）。*