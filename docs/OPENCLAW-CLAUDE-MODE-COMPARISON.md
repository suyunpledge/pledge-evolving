# OpenClaw / Claude Code：按 Work 与 Code 模式对照

本轮对应 Forge 的 Work / General（`WorkMode.GENERAL`）和 Code / Development（`WorkMode.DEVELOPMENT`）。两个入口共享 Agent、工具执行、权限和验收内核，模式本身不授予权限。

## 源码范围

- OpenClaw：本机 `C:/Users/匡溯昀/openclaw-cn`，HEAD `3c460ba96`。读取了 `src/agents/tool-loop-detection.ts`、`src/auto-reply/reply/memory-flush.ts`、`src/agents/pi-extensions/context-pruning.ts` 等实现。
- Claude Code：本机 npm 安装包声明版本 `2.1.312`，核心入口为 `bin/claude.exe`。当前能直接核验的是 `sdk-tools.d.ts` 和官方插件目录，**没有把 SDK 声明或插件流程冒充为核心完整源码**，也没有推断未看到的核心算法。
- 官方插件材料：`~/.claude/plugins/marketplaces/claude-plugins-official/plugins/claude-md-management/skills/claude-md-improver/SKILL.md`、`plugins/feature-dev/commands/feature-dev.md`。本轮把这些当作参考源码/协议材料，没有执行插件工作流、修改个人配置或调用 Claude CLI。

## Work 模式：主要对照 OpenClaw

| 主题 | OpenClaw 实现 | Forge 对照与本轮决定 |
| --- | --- | --- |
| 工具循环 | 有界调用历史、稳定参数/结果摘要，以及 generic repeat / known polling / ping-pong / global breaker 的诊断信息；该本地源码的检测配置缺省关闭，不能误称所有运行都启用 | Forge 已默认做精确动作/结果循环检测，但只覆盖 1–2 步周期。本轮扩展完整周期并补结构化停止原因，保留既有阈值 |
| 压缩前记忆 | `memory-flush.ts` 在接近容量时安排独立 flush 阶段，按 compactionCount 防止重复，提示对日记追加 | Forge 已有压缩前完整源归档、事务校验、任务锚点与受审批约束的长期记忆。未直接加入额外模型调用或让模型自动覆盖长期记忆文件 |
| 请求上下文剪枝 | `context-pruning.ts` 明确仅影响当前请求的内存上下文，不重写磁盘历史 | Forge 的请求投影、artifact 留存和压缩事务已分层；还需遵守 lossless 模式及多厂商缓存约束，未把上游剪枝策略作为全局默认 |

### 已实现：完整多工具周期检测

`ExecutionGuard` 从 12 条扩到最多 30 条哈希观察，检查 1–6 步周期：

- 全成功的周期仍需完整重复 5 次；包含失败的周期需重复 3 次。原来单步与两步规则兼容。
- 比较仍包含工具名、完整参数、成功状态和返回文本的稳定摘要。参数或结果变化不会被归为相同循环。
- 在三工具失败循环中，第 9 次观察就停止；不继续调用下一工具，也不自动重放已有动作。
- 停止事件记录 `detector`、`period`、`repetitions`、`observations` 和 `contains_error`，不复制参数/原始结果或秘密。
- 诊断经 `TurnResult` 传给 Agent 的 `loop_stop`，客户端日志同样收到。原生与文本协议共用这条路径；unknown_outcome 仍优先停止。

这是受 OpenClaw 的有界历史和可解释诊断启发的 Forge 实现，没有照搬其阈值或默认开关。默认 max_steps 仍可能早于较长周期阈值停止；时间戳等真实输出变化也不会被猜测为“无进展”。

## Code 模式：主要对照 Claude Code 的可读材料

| 主题 | 已核验的本地材料 | Forge 对照与本轮决定 |
| --- | --- | --- |
| 项目指引 | 官方 claude-md-management 描述根目录与模块级 CLAUDE.md；SDK `ReadFileOutput` 的 seeded 分支明确提到启动时的 CLAUDE.md / nested memory | Forge 只读 AGENTS.md，复用 Claude 项目的指引存在真实缺口。本轮沿用既有受控加载器，加入 CLAUDE.md |
| 编程工作流 | feature-dev 插件分成发现、代码阅读、设计、实现、审查，并使用待办状态跟踪 | Forge 已有 TaskPlan、开发任务快照、实际动作/差异/验收证据、独立 review；插件文本不等于核心调度实现，本轮没有以提示词替代执行器 |
| 动态任务列表 | SDK 声明 TaskCreate / TaskUpdate / TaskList，以及 pending / in_progress / completed 与 blockedBy 字段 | 接口说明可以启发后续任务状态层，但不能仅凭类型声明声称已了解其依赖调度算法；模型标记 completed 也不能替代 Forge 的独立验收 |
| Read 去重 | SDK 返回类型有 file_unchanged 与 seeded 来源 | Forge 的文件版本保护和压缩会影响旧读取是否仍在模型上下文中；本轮没有仅因文件未变就省略内容，避免压缩后出现“内容没给模型但声称已读”的问题 |

### 已实现：目录作用域内的 CLAUDE.md

`ProjectRules` 现在把 `AGENTS.md` 与 `CLAUDE.md` 放在同一个候选、校验和预算流程中：

- 只自动检查工作区根目录与实际上下文目标的祖先目录。编辑器附带的未保存文档也能激活对应模块指引，不遍历全仓库。
- 两类文件共享最多 8 个文件、每文件 8 KiB、上下文 16000 字符等原有边界；沿用拒绝符号链接/重解析路径、公共文件检查、read_file 授权和秘密保护。
- 内容仍是带 scope / revision 的仓库数据；不会变成 system 权限，也不会让文档中的命令、密钥请求或 @ 引用自动执行。
- 不读取工作区之外的全局或个人 Claude 配置，不展开引用文件，不自动移植 `.claude.local.md`、用户规则或插件配置。
- 根目录先于深层目录；同一深度按既有路径顺序稳定排列。若两种文件同层互相冲突，没有新增“CLAUDE 永远覆盖 AGENTS”的隐藏规则。
- 复用既有投影合并：新作用域接在已发前缀之后，内容变化会使旧来源失效，移除文件不会继续使用旧指引。

因此 Code 的编辑器、CLI 编码任务以及共用内核的文件任务都能识别 Claude 项目指引；没有创建另一套解释权限的实现。

## 实际验证

新增 `forge/test_modes_reference.py` 的 11 项行为测试，覆盖长周期循环、旧阈值、真实进展、上下文作用域、双原生协议、前缀稳定、权限拒绝、秘密保护、文件变更与共享预算。

新增 `tests/test_mode_reference_entries.py` 的 2 项入口测试：

- Work：真实本地 HTTP 任务执行三工具失败循环，第 9 步停止，客户端记录包含原因。
- Code：真实 DevelopmentRuntime → EditorBridge → Agent 路径中，未保存文档只激活目标模块的 CLAUDE.md，且不注入 system 层。

最终结果：

| 检查 | 结果 |
| --- | --- |
| 新增模式行为测试 | 11/11 通过，计入内核回归 |
| 新增 Work HTTP / Code Editor 入口测试 | 2/2 通过，计入运行时回归 |
| 内核完整回归 | 395 项，393 通过、2 跳过 |
| 运行时与客户端 13 组回归 | 205 项，204 通过、1 跳过 |
| 框架自检 | 595/595 |
| 代码行数预算 / Git 空白检查 | 144 个文件通过；接受 Windows CRLF 的空白检查通过 |

合计 **597 项回归通过、3 项跳过、0 失败**；专项不重复计数。模型使用离线 fixture，没有进行真实云模型评测或 GUI 视觉验收。相关日志为 `reports/modes-reference-targeted.log`、`reports/modes-reference-entries.log`、`reports/modes-reference-core.log`、`reports/modes-reference-runtime.log`、`reports/modes-reference-selftest.log`。

本轮没有修改 OpenClaw / Claude 安装内容、调用真实云模型、修改用户凭据、创建定时任务或提交 Git。快照路径见 `reports/openclaw-claude-modes-backup.txt`；独立补丁为 `reports/openclaw-claude-modes.patch`。
