# 预存消息队列 + ScienceClaw 移植 · 巡检轮独立复验（2026-10-15 22:42）

- **巡检触发**：cron CodingMode改造-两家进度巡检（22:42）
- **复验对象**：main HEAD `1291f35`（两笔增量：`70b7ace` 预存消息队列 / `1291f35` ScienceClaw 三能力移植，均 2026-10-15 晚主会话用户授权交付）
- **复验环境**：集成方主 worktree `pledge-evolving`，系统 Python 3.12.10 / Tk 8.6.15（与 M4-M7/R1-R4 复验同环境）；PYTHONPATH = 仓库根 + forge-gui

## 独立复验结果

| 套件 | 结果 | 用时 | 退出码 |
|---|---|---|---|
| `run.py selftest` | 574/574 | — | 0 |
| `forge.scienceclaw.test_scienceclaw` | 32/32 | — | 0 |
| `forge.graph.test_graph` | 18/18 | — | 0 |
| `development_mode.test_app` | 18/18 OK | 51.4s | 0 |
| `development_mode.test_agent_panel` | 21/21 OK | 54.5s | 0 |
| `development_mode.test_layout_regression` | 64/64 OK | 167.3s | 0 |

首轮 GUI 三套件因集成方漏配 forge-gui 的 PYTHONPATH 挂 import（复现口径笔误，非代码问题），修正后全绿。stderr 中 `invalid command name "<lambda>"/"_fire"` 为 Tk 销毁后 after 回调残余噪声，不影响结果（与既往轮次一致）。

## 交付面核查

### 70b7ace 预存消息队列（GUI）

- send() 拆 _dispatch()：busy 入队（FIFO 上限 3、满队拒绝且保留输入）、done() 后 after 300ms 自动发队首（busy/loading 双保险塞回）——代码走读与申报一致。
- ⏳ 预存 n/3 标签 pack(before=input)：layout_regression 64/64 含两尺寸不越界，几何无回归。
- **过程备注**：提交信息所称「GUI 级测试 12/12」未随提交入库（主会话内 monkeypatch 验证后未持久化测试文件）。本轮以 test_app 18/18 + layout 64/64 作集成背书，功能面不阻塞；建议后续把该批测试落盘进仓。

### 1291f35 ScienceClaw 移植（forge）

- 三能力（offload 落盘三档阈值 / PlanStep 归一化幂等 / thinking 三格式提取）自带 test_scienceclaw.py 32/32 全绿。
- 域边界：仅新增 forge/scienceclaw/ 五文件，graph 18/18 与 selftest 574 零破坏，GUI 域零触碰。

## 推送状态

GitHub API 实证 origin/main = `1291f35`（22:12 提交后主会话已推，本地 tracking 落后系 fetch 网络停滞，无真实欠账）。本报告为巡检轮补录，随本提交推送。

## 窗口侧（UIA 只读）

- WorkBuddy：空闲（.workbuddy 数据目录 22:52 后无新任务痕迹）。
- ChatGPT（Codex）：活跃任务「完善 Development Mode 执行闭环」正常推进，无排队/容量满。

## 结论

**两笔增量独立复验通过，六套件全绿，远端同步无欠账。Coding Mode 改造全线（布局域 M1-M7 + Agent 域两轮 + v0.3 + R1-R4 + 队列/图/ScienceClaw）验收闭环。**