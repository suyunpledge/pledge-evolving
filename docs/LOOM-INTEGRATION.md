# Loom → Forge：结构对照与内核集成

后续代码分层和统一执行路径重构见 [结构集成说明](LOOM-STRUCTURE-INTEGRATION.md)。下方验证数字属于首轮功能集成，后续结果以结构说明为准。

日期：2026-11-06。基于本机当前源码，而非 README 中的历史测试数字。

## 项目关系与结构

- Forge 主项目：`C:/Users/匡溯昀/pledge-evolving`，HEAD `db95193`，包含既有未提交的模型适配、缓存与上下文工作。
- Loom：`C:/Users/匡溯昀/Documents/ChatGPT/Codex/loom`，HEAD `2c8f755`，也有既有未提交改动；本轮只读。
- `C:/Users/匡溯昀/forge` 是早期多智能体轮次编排工具，不是这次的集成目标。

| 层次 | Forge / pledge-evolving | Loom |
| --- | --- | --- |
| 产品入口 | CLI、Tk 桌面端、VS Code、Android、IM | CLI、HTTP/SSE 工作台、内置浏览器 |
| 主执行 | `forge/loop.py`、`execution.py`：模型循环、统一动作执行、权限、审计、取消 | `packages/kernel`：事件驱动循环、效果调度、权限、独立验收 |
| 状态 | `session.py` JSONL；线性会话与 fork；`graph/` 提供图执行；`checkpoint.py` 影子 Git | SQLite 事件 DAG、分支 head、状态与上下文投影、环境快照 |
| 模型 | `model.py`、`routing.py`、`adapters.py`、`vendors.py`，多厂商与路由、成本、网关 | `packages/models`，Scripted、OpenAI、Anthropic 流式适配 |
| 上下文 | `projector.py`、`context_transaction.py`、`cache_metrics.py`，已有前缀稳定和压缩事务 | 纯投影、artifact 句柄、内容来源标签 |
| 工具 | `tools.py`、贡献模块、MCP、Windows 电脑操作，统一宿主授权 | `packages/env-workspace`、`env-shell`、`env-browser`、`env-mcp`，按环境挂载 |
| 任务质量 | 原有 `task_contract.py` 是目标和状态描述；开发模式已有版本防护 | 可执行契约 + `verifier.ts`；全文件版本和修改冲突检查 |
| 扩展治理 | 标准库核心、贡献 AST 闸门、自我演化、长期记忆、子代理权限上限 | TypeScript monorepo，SQLite / Playwright / MCP / Zod 等依赖 |

判断：Forge 的模型适配和产品外围更丰富；Loom 的环境边界、可执行验收和事件建模更集中。应移植契约和执行不变式，保留 Forge 的标准库、已有权限链和请求缓存策略。

参考并核验了 Loom 的 `docs/FORGE-COMPLEMENT.md`，但没有照单实现。其中提到的纯投影，Forge 当前工作区已经有了；文件版本防护也并非完全缺失，而是原来只在开发模式接入。

## 已集成 1：宿主定义的可执行验收

来源：Loom `packages/kernel/src/verifier.ts`。用 Python 重新实现，没有引入 Node 运行时或新增第三方依赖。

新增 `forge/acceptance.py`：

- `file` 检查文件存在/不存在、完整文件中的字符串。上限 8 MiB，不使用被截断的工具预览做判断；返回内容摘要与字节数，不在证据中复制文件正文。
- `command` 检查命令的结构化退出码，经过现有 `_invoke_recorded` → `ActionExecutor` → `ToolRegistry`。不从 stdout 的“exit:0”猜成功。
- 所有命令检查先执行，再检查最终文件状态，避免后面的命令改坏已经验收过的文件。
- 契约仅由宿主 API 或用户指定的 CLI 文件安装。模型不能新增、删除或放宽验收项；自然语言抽取出的目标不会被冒充为可执行测试。
- 必须有 1–16 个检查项且至少一项必选；ID 唯一；类型与字段严格校验；最多 3 次验收，每个命令最多 60 秒；仍受原模型循环步数上限限制。
- 模型准备交付时进入 `verifying`；必选检查全部通过后进入 `ready_to_deliver`。失败详情追加到消息尾部，让模型修复；达到上限返回 `acceptance_failed`，CLI 非零退出。
- 命令结果未知时立即停止，不自动重放；取消不能转成通过。未通过的候选回答不作为最终 `assistant_message` 发布。
- `acceptance_defined`、`acceptance_checked` 与任务状态进入现有日志；`run_payload` / CLI `--json` 增加最后一次验收结果。没有契约时不新增该返回字段、不注入验收提示。

使用：

```powershell
python run.py run "创建 output.txt，内容为 ready" --acceptance templates/acceptance.example.json --json
```

路径相对于 `--workspace`，未指定时为当前工作区。JSON 示例：

```json
{
  "max_attempts": 3,
  "criteria": [
    {"id": "deliverable", "kind": "file", "path": "output.txt", "contains": "ready"}
  ]
}
```

Python 嵌入入口：

```python
report = agent.run("完成任务", acceptance={
    "criteria": [{"id": "result", "kind": "file", "path": "result.json"}]
})
```

契约只对当前 `run` 生效；宿主续跑时需重新提供，历史日志中的契约不会自动获得执行授权。

命令项格式是 `{"id":"tests","kind":"command","command":"python -m unittest","expect_exit":0,"timeout":60}`。
**默认受保护 Agent 禁止原生 shell，命令验收也会被拒绝**；本次没有放宽秘密隔离或权限策略。只有宿主已有的、获准的执行环境能执行命令验收。普通 CLI 可直接使用文件验收。

本轮没有添加 LLM judge、环境 probe、GUI 契约编辑器，也没有自动猜测或执行项目中的测试命令。通过检查只证明所列条件在验收时成立，不代表整体功能或自然语言需求已经被完整证明。

## 已集成 2：共享文件版本防护

来源：Loom `packages/env-workspace/src/index.ts` 的全文件版本观察与冲突拒绝；实现复用 Forge 已有的开发模式 guard。

- 原 `forge/development/guard.py` 的实现移到 `forge/file_revisions.py`，原路径保留兼容导出，编辑器与执行器共用同一实现。
- 普通 `Agent` 默认接入：成功读取或写入后记住整个文件 SHA-256；后续 `write_file`、`edit_file`、`apply_patch`、`delete_file`、结构化配置修改都按既有执行链核对版本。
- 普通任务仍兼容未读取就写文件的既有行为；一旦已经观察过文件，其他进程/用户修改或删除文件后，旧版本写入会被拒绝。
- 开发模式保持“已有文件必须先读”的严格要求。普通模式的可读范围仍由 Policy 决定，不会被开发模式的工作区限制意外收窄。
- 子代理获得独立观察表，不继承父代理已经过时的读取记录。读取过程中发生变化也会拒绝本次观察。
- 多文件 patch 在原有锁和整体授权检查中核对全部目标；任一目标过期，不会先写入其他文件。
- 将 `ToolRegistry` 的版本检查上限从 512 KiB 对齐到读取的 8 MiB，避免“能读不能改”的错误。

这是乐观并发防护：Forge 进程使用原有工作区/文件锁；不合作的外部编辑器仍可能在最后一次检查与写入之间修改文件，不能宣称提供 OS 级文件事务。未经读取的直接覆盖也不是严格防丢失模式。严格宿主可传入 `FileRevisionGuard(workspace)`。

## 为什么没有直接移植其余部分

- DAG 会话与任意步骤恢复：会改变 JSONL、fork、恢复和多个客户端契约；不能把日志分叉直接等同于恢复工作区，更不能顺手回滚用户未提交文件。
- 动态资源并行调度：Forge 当前动作串行、写入已有锁；需要先统一 shell/MCP/电脑操作资源语义，才适合增加并行，不能只复制调度函数。
- 信息流标签：需要连同数据来源、权限卡片和对外发送路径一起落地，孤立增加一个 `untrusted` 字段不构成完整防护。
- Artifact 存储：Forge 已有 `scienceclaw/offload.py` 的大结果外置；可后续补内容寻址和来源标签，本次不重复引入第二套存储。
- 纯投影与缓存：保留当前未提交的 `projector.py` 和上下文事务工作；新增验收反馈只追加在尾部。

## 验证与变更边界

新增 `forge/test_loom_integration.py` 的 22 项离线测试，覆盖验收失败后真实工具修复、限制次数、权限拒绝、可选项、未知结果、取消、命令结构化回执、缓存前缀、外部编辑、多文件 patch、大文件版本检查，以及 CLI JSON / 退出码 / 持久化日志。Windows 不支持创建符号链接时，该单项明确跳过。

最终验证：

| 检查 | 结果 | 证据 |
| --- | --- | --- |
| `python -m unittest discover -s forge -t . -p 'test_*.py' -q` | 347 项，345 通过、2 跳过、0 失败 | `reports/loom-core-tests.log` |
| Agent 稳定性、开发模式、编辑 UX、运行时一致性、客户端等 9 组回归 | 188 项，187 通过、1 跳过、0 失败 | `reports/loom-runtime-tests.log` |
| `python run.py selftest` | 595/595 | `reports/loom-selftest.log` |
| `python scripts/budget_check.py` | 133 个文件通过 | `forge-budgets.json` |
| 空白检查（Git `cr-at-eol`，接受 Windows CRLF） | 通过 | 工作区 diff |
| 隔离临时 home 的 `doctor --no-user-layer` | 退出码 0，7 项正常；默认模型凭据/本地网关未配置警告 | `reports/loom-doctor.log` |

没有测试真实云模型或重新做 GUI 视觉验收。代码行数清单按当前源码显式更新，也登记了开始时已存在但尚未列入清单的 `projector.py` / `test_projector.py`，以及其他既有未提交文件的实际行数；这不表示那些旧改动由本轮实现。

原有未提交改动保留。修改前快照位于 `C:/Users/匡溯昀/forge-loom-backup-20261106-202704`。`reports/loom-integration.patch` 仅记录相对该快照的本轮变更，便于与既有工作区差异区分。本轮没有修改 Loom、调用付费模型、修改真实凭据、提交 Git 或发布。
