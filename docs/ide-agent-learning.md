# Antigravity、Cursor、Qoder CN：调研与最小接入

调研日期：2026-10-10。依据公开官方文档；没有访问这些产品的私有源码，下面是机制借鉴，不是把它们的专有 Agent 引擎装进 Forge。

## 选择矩阵

| 产品 | 值得学习的公开能力 | Forge 对照与本次决定 |
| --- | --- | --- |
| [Antigravity：Artifact review](https://www.antigravity.google/docs/artifact-review/) | Planning/Fast 分开；计划与改动可作为可审阅成果；审阅暂停策略独立配置 | 现有四档规划、独立规划模型、手动复审和执行事件已承担部分职责。未新增自动弹窗或计划暂停协议；Forge 当前没有完整的可编辑 Artifact 审批流。 |
| [Antigravity：Walkthrough](https://www.antigravity.google/docs/walkthrough/) | 用实施总结帮助用户理解交付内容 | 保留现有 RunReport、diff、审计记录。不能把模型生成的总结当作测试通过的证据，也未伪造截图或浏览器录屏。 |
| [Cursor：Rules](https://cursor.com/docs/rules) | 项目规则可按文件范围加载，支持根目录与嵌套 AGENTS.md | **新增 AGENTS.md 自动上下文加载**。复用通用 Markdown 约定，不实现完整 `.cursor/rules/*.mdc`、自动相关性判断或团队规则后台。 |
| [Cursor：Plan Mode](https://cursor.com/docs/agent/plan-mode) | 先了解代码，再形成可审阅、可修改的计划 | 保持已有规划能力。Forge 的规划阶段目前不执行工具；不能宣称已复制 Cursor 的规划阶段代码检索与编辑器。 |
| [Qoder CN：规则设置](https://docs.qoder.cn/user-guide/rules) | 项目级规则；始终、文件范围、手动、模型决定四种应用方式 | 借鉴范围与按需加载，接入同一 AGENTS.md 实现。Qoder CN 官方规则目录是 `.lingma/rules`；本次**不声称兼容该格式**。 |
| [Qoder CN：Spec 驱动](https://docs.qoder.cn/user-guide/quest/spec-driven) | 澄清需求、形成需求/设计/任务/验收标准，再由用户审阅执行 | 现有 high 规划包含这些结构化字段，但不是完整 Quest Spec 审批与持续验收循环。本次复用现有规划，不再造另一套任务状态机。 |
| [Qoder CN：Repo Wiki](https://docs.qoder.cn/user-guide/repo-wiki) | 项目知识结构化，并检测代码与文档变化 | 现有记忆与能力库不等同完整 Repo Wiki。索引、生成成本、来源定位和增量更新需单独设计，本次不新增自动全仓库生成或后台扫描。 |

[Qoder CN Agent SDK](https://docs.qoder.cn/cli/sdk/overview) 已公开提供 TypeScript/Python 应用接口，其 [权限回调](https://docs.qoder.cn/cli/sdk/permissions) 也值得学习。技术上可以另做适配，但它会引入另一套 Agent runtime；现有 Forge 已有工具可见性与调用授权的分离，当前没有证明新增运行时的收益。

外部 Agent SDK、云端 Agent、专有代码补全和整套浏览器控制没有接入。它们需要独立的账号、执行边界和数据外传审查；本次没有明确需求，不能通过原生第三方 SDK 顺便绕开 Forge Policy/Secret 边界。

## 已接入：有范围、有来源的项目规则

源码：`forge/project_rules.py`；运行入口：`forge/loop.py`；编辑器入口：`forge/vscode_bridge.py`。

- 每次任务重新加载 `<workspace>/AGENTS.md`。
- 成功读取 `read_file`、`read_range`、`file_outline`、`edit_config` 的文件后，在下一次模型请求前加载其祖先目录中的 AGENTS.md。
- VSCode 已验证的附件/选区文档路径在首次请求前激活相应祖先规则，也支持尚未保存的文件。
- 只加载当前工作区根目录及本轮文件范围；不查工作区之外的父目录，不递归扫描仓库、不抓取链接、不展开 `@file`，不运行规则中的脚本。
- 每次迭代刷新这组有限来源。变更替换旧上下文，删除或变成不安全文件会移除旧快照；不会给后续任务复用旧内容。
- 规则以普通 user 背景数据提供，保留作用目录和来源版本。模型对编码规范的遵守仍属行为指导；工具权限由现有 Policy 独立执行。
- 上下文压缩保留当前规则快照，原生工具调用及结果的相邻关系不变。规划的独立模型也收到规则，不额外发起模型调用。
- 审计事件 `project_rules` 记录来源、受保护文本的 revision、跳过原因，不记录明文 Secret。

示例（由项目维护者编写，不会自动生成示例到实际项目）：

```text
AGENTS.md                # 项目通用约定
src/AGENTS.md            # 仅 src/ 内适用
src/ui/AGENTS.md         # 仅 src/ui/ 内适用
```

没有规则的项目不添加额外模型上下文。宿主可用 `Agent(..., project_rules=False)` 关闭，或在配置树把 `project_rules.enabled` 设为 false / 禁用该行。子 Agent 继承开关，使用自身的权限和 SecretScope。

## 安全与资源限制

- 每个文件最多 8 KiB，超限整体跳过；最多 8 个规则文件、32 个目标路径、16 层路径；完整规则消息最多 16,000 字符。没有全仓库索引，没有启动时扫描线程。
- 规则内容进入上下文前经过 Secret virtualization；版本摘要在保护之后计算，不导出原 Secret 的摘要。
- 必须通过 `read_file` 的 Policy 判定；ASK/DENY 不自动读取，也不新增审批弹窗。隐藏/尚未激活的 read_file 不能自动加载规则。
- Secret Store 路径及其登记的硬链接别名受到原有保护；规则中的 symlink、Windows junction/reparse point、非普通文件被拒绝。
- 规则不能改变 allow/deny、批准写入、注册工具或 resolve/export Secret。文件路径检查不是 OS 级沙箱，不为同账号的任意原生进程提供隔离保证。

## 范围说明

本次自动规则加载适用于核心 Agent：CLI、桌面 GUI 的核心任务执行和 VSCode Agent。桌面 GUI **直接连接 Gateway 的流式聊天路径**没有同一份 Agent 循环，本次不在 Tk 主线程增加文件读取，也没有假装它已经具有完整的规则加载协议。

Premium 集成阶段仍遵循现有“最后用户输入 + 草稿”的缩减数据面；独立规划请求保留规则，但执行阶段不向 premium finisher 自动转发整份项目上下文。主执行模型看到规则。完整的跨阶段上下文授权需单独处理，不能无声扩大外传范围。

## 验证

`forge/test_project_rules.py` 覆盖目录作用域、无关目录、权限拒绝/ASK、工具隐藏、超限、UTF-8 错误、符号链接/Secret Store 别名、注入不能授权写入、Secret 不进入请求或审计、新任务刷新、运行中更新/删除、重复读取、压缩保留、原生 OpenAI/Anthropic 调用结果顺序、禁用配置与编辑器未保存文件。

`forge/test_vscode_bridge.py` 增加实际 EditorBridge 的范围规则附件测试。先运行失败断言，再接入运行循环。测试使用离线替身和本机测试服务器，没有发起真实付费请求。

- 原有 selftest：569/569 通过。
- 完整核心 unittest：运行 213 项，212 项通过，1 项因当前 Windows 不允许创建符号链接而跳过。
- 项目规则专项：运行 18 项，17 项通过，同一符号链接项目跳过。
- 并发跑测试时出现一次既有启动测试的固定 4 秒等待失败；单独验证通过，最终串行完整回归通过。没有放宽该断言或改动启动代码。
- VSCode TypeScript 编译通过；15 项宿主/桥接测试通过。浏览器界面测试在 Edge 和备用 Chrome 上均触发既有 60 秒上限，因此没有记为通过。Edge 诊断日志显示页面交互断言及截图已完成，但初始化和关闭耗时导致整项超时；本次未修改界面代码或放宽测试时间限制。
