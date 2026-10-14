# Coding Mode 示例图重构 · M2 欢迎页 · 验收裁决

- 对象：WorkBuddy（Coding Mode 布局域）
- 分支：`feature/coding-editor-ux`（worktree `C:/Users/匡溯昀/forge-coding-editor-ux`）
- 提交：`fa2c9ae`（单独成笔；父提交 `e4b9908` = M1-fix 验收裁决入库，基线即此）
- 改动面：`app.py`（+64/-6，行区纪律内 4 方法）、`welcome.py`（新文件 109 行）、`test_layout_regression.py`（新增 WelcomeViewTests 4 用例）；`forge/development/*` 与 Codex 域文件零改动
- 裁决：**通过 ✅，M3 放行**

## 集成方独立复验（2026-10-14 09:30–09:52，非交互后台会话）

| 套件 | 结果 | 备注 |
|---|---|---|
| run.py selftest | **574/574** ✅（退出码 0） | 基线不回退 |
| test_app | **23/23** ✅ | 140s 全绿，无 Tcl 抖动 |
| test_layout_regression | **21/21 + 17 subtests** ✅（含新增 WelcomeViewTests 4 例） | 首轮 20/21：唯一失败 `test_agent_column_does_not_overflow_at_default_and_narrow_sizes` 为 TclError `tcl_findLibrary` @ setUp `tk.Tk()` 创建阶段（早于任何应用代码），与 M1-fix 期间定性一致的 tk.tcl IO 环境抖动；solo 重跑 **1 passed + 2 subtests**（6.85s）确认非回归 |
| test_project | 3/3 ✅ | 顺带巡检 |
| 两尺寸布局回归 | ✅ | 1440×900 / 1200×760 不越界断言在 layout_regression 内全绿（含上列 solo 复跑） |

## 提交内容核对（对照 M2 派单）

1. 未打开项目时中央区显示欢迎视图（标题 + [打开项目] + Recent Projects）——`_build_welcome`/`_sync_welcome` 二态切换，复用 `editor_frame`，不触碰三栏 pane 增量挂载路径 ✅
2. Recent 数据源 `state.value('recent')`（`remember_project` 真实持久化记录，不伪造）✅
3. `_sync_welcome` pack 后 `update_idletasks()` 同步冲刷——与 M1-fix `_settle_mount` 同源机制，无 Configure 自激风险 ✅
4. 既有属性契约（`self.tabs`/`self.document_state`/`self.write_output` 等）不变，`_build_editor` 收进 `editor_body` 子帧对调用方无感 ✅
5. 单模块单笔提交，commit message 含改动方法清单与自测数据 ✅

## M3 放行纪律（不变）

- ① 新代码归位计划新文件，app.py 只动计划清单内方法
- ② 51f6f82 保护清单 + M1-fix `_settle_mount` 冲刷机制均不得绕开
- ③ 不碰 `forge/development/*` Codex 域文件
- ④ 单模块单笔提交，附自测（selftest 574 基线 + test_app/test_layout_regression）
- ⑤ M3 完成后停手等验收，通过后再继续 M4

排队超时不用重试刷屏，等外部唤醒即可。

---

* CodingMode改造巡检 cron 代理验收；证据：exec 会话记录（selftest 574/574 exit 0、test_app 23/23、layout_regression 20/21+solo 1/1、test_project 3/3）。*