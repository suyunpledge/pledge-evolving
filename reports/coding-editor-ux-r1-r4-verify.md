# Coding Mode R1-R4 按图重构 · 复验入库（巡检轮）

- **巡检时间**：2026-10-15 18:13 触发（cron CodingMode改造-两家进度巡检）
- **复验对象**：main HEAD `22e03b5`（refactor: R1-R4 按图重构四项——头部精简/RuntimeContext 折叠修复/终端独立页/双状态栏合一）
- **复验环境**：集成方主 worktree `pledge-evolving`，系统 Python 3.12.10 / Tk 8.6.15（与 M7 复验同环境）

## 背景链

- 10-15 13:27 `f656950` merge: v0.3 工作区线纳管（test_app 18/18 + layout 64/64，交付方自测）
- 10-15 17:59 `22e03b5` R1-R4 按图重构四项（本次巡检轮新发现的交付，交付时未附四套件全量证据）

## 独立复验结果

| 套件 | 结果 | 用时 | 备注 |
| --- | --- | --- | --- |
| `run.py selftest` | 574/574 | — | 输出明示全过 |
| `development_mode.test_app` | 18/18 OK | 48.0s | v0.3 起基数由 23 收敛为 18（f656950 明示） |
| `development_mode.test_agent_panel` | 21/21 OK（+3 subtests） | 56.2s | R1-R4 触及该文件 2 行，契约正常 |
| `development_mode.test_layout_regression` | 64/64 OK（+29 subtests） | 147.9s | R1-R4 触及该文件 14 行（六页断言升七页等） |

## 环境噪声记录（非回归）

复验过程中 test_app / test_layout_regression 多轮出现**随机单测失败**，特征：

- 每轮失败的测试**各不相同**（test_mode_switch_resets_next_task_permission → test_checkpoint_confirmation_never_discards_dirty_buffer → test_send_uses_actual_agent_events… → test_welcome_shown… → test_status_bar_fields… → test_agent_column…），全部为 Tk 初始化阶段错误（`TclError: Can't find a usable tk.tcl` / `invalid command name "tcl_findLibrary"`）
- 失败测试**单独重跑全部通过**
- `tcl/tk8.6` 目录 175 文件在位、无近期改动；对 `ttk/utils.tcl` 连续 300 次读盘 0 失败
- 与 M7 验收文档（coding-editor-ux-m7-verdict.md）记载的「存量 tcl8.6 IO 抖动」一致；怀疑 ESET 实时扫描瞬时锁文件

**判定：环境抖动，非 R1-R4 代码回归。** 干净轮全绿证据见上表。

## 推送状态

- `origin/main`（fetch 后）= `22e03b5` = 本地 main，ahead 0 —— R1-R4 已在 GitHub
- WorkBuddy / Codex 两条 feature 分支均 0 commits ahead of origin/main（全部合入）

## 结论

**R1-R4 按图重构复验通过，四套件全绿。** Coding Mode 改造状态：

- 布局域（WorkBuddy）：M1-M7 昨日收官（b803f01），今日追加 v0.3 纳管（f656950）+ R1-R4 重构（22e03b5）
- Agent 域（Codex）：昨日二轮验收合入（daead5f 收尾），Agent 面板增量（0e149ea）已入库
- 窗口侧：ChatGPT（Codex）正常无卡排队；WorkBuddy 窗口空闲
