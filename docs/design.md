# DESIGN — 为什么这样集成

这份文档记录的是**取舍**，不是功能清单。七套框架在同一个问题上给了不同答案，集成的工作量不在"抄"，在"选"。

## 一、五处必须做选择的地方

### 1. 配置：深合并还是整行替换

- **DSH**：空根 + 有序补丁层，按 id 后写胜，**整行替换、禁止深合并**
- **Codex**：TOML 基线 + profile 层 + 点路径 `-c` 覆盖 + `--strict-config`
- **Claude Code / CodeBuddy**：多级 settings 文件按目录层级合并

**选择**：DSH 的行模型 + Codex 的严格模式。

理由：深合并是配置事故的第一来源 —— 用户想改一个字段，结果和基线层的同名列表拼接成了四不像。整行替换的代价是"改一个字段要写整行"，但这个代价是可见的、可推演的；深合并的代价是隐式的。`dump-config` 把合成结果摊开，代价就被抹平了。

附带保留 DSH 的 `dump-default-config`：用户层写坏时跳过它即可启动。这个"恢复通道"在七套框架里只有 DSH 显式做了，而它救的是最痛的一种故障。

### 2. 权限：一维开关还是二维矩阵

- **Codex**：三档沙箱（read-only / workspace-write / danger-full-access），与审批路径强绑定
- **CodeBuddy**：mode（default/acceptEdits/auto/dontAsk/plan/bypass）+ allow/ask/deny 规则，`deny` 恒优先
- **Hermes**：默认无沙箱，靠审批 + 检查点兜底
- **Claude Code**：permission-mode + hooks

**选择**：CodeBuddy 的二维模型做骨架，Codex 的三档沙箱做硬边界，外加命令级黑名单。

理由：一维开关无法同时表达"平时放开、这条命令永远不行"。二维模型里 mode 管基线、规则管例外，且 `deny` 恒胜，语义可推演。沙箱单独做一层是因为它拦的是**路径**，规则拦的是**工具**，两者漏一个都关不住。

**本轮新增的一层**：CodeBuddy 的 `--subagent-permission-mode` 只说"子代理可配置"，没说"不可越界"。这里补了天花板钳制 —— 父会话 `plan` 的子代理永远拿不到 `bypassPermissions`。多代理架构里，越权几乎总是从子代理进来的。

命令黑名单是 CodeBuddy 的 `forbidden_programs`，我把它扩到了 `powershell -EncodedCommand`、`curl | sh` 这类"把控制权交回操作系统"的形态 —— 路径白名单对它们无效。

### 3. 工具面：全量暴露还是延迟加载

- **CodeBuddy**：55 项工具 + `Defer(X)/NoDefer(X)` 修饰符 + `ToolSearch` / `DeferExecuteTool`
- **Codex / Hermes / Claude Code**：全量工具 + 技能目录（技能本身是懒加载的）

**选择**：CodeBuddy 的 Defer 机制，`NoDefer` 恒胜。

理由：工具描述是系统提示里增长最快的一块。55 个工具的 schema 常年占着上下文，而单次任务通常只用 3–5 个。Defer 把"暴露策略"和"行为策略"解耦：可见性由 Defer 决定，能不能执行由权限决定，两者互不污染。`NoDefer` 恒胜是为了防止一条过宽的 defer 规则把一个必需工具（比如 `tool_search` 自己）藏起来。

### 4. 模型供给：单模型、重试、还是多模型聚合

- **OpenCode**：provider 即数据，`provider[] + model + small_model`，密钥内嵌配置
- **Hermes**：主模型 → fallback 链 → MoA 聚合，且 fallback 只在限流/过载/连接错误时触发
- **CodeBuddy**：`--fallback-model`，仅 `-p` 模式生效

**选择**：三者合并。OpenCode 的 provider 数据化做接入层，Hermes 的分级供给做可靠性，MoA 做成可选聚合。

理由：**只对可重试错误降级**是关键细节。400（请求本身有问题）去换三个 provider 是纯粹的浪费和噪声；429/503/连接重置才值得换。这条判断在 `model.py` 里用异常类型表达，而不是靠调用方自觉。

**否掉的**：OpenCode 把 `apiKey` 写进配置文件的做法没有采纳，改为 `$expr` 读环境变量 + `doctor` 把内联密钥报成告警。密钥进配置文件等于进 git、进备份、进截图。

### 5. 会话：内存态、数据库、还是事件日志

- **Codex**：rollout JSONL，首行 `session_meta`，后续 ordinal 事件，`resume/fork/apply` 建立在同一回放原语上
- **CodeBuddy**：JSONL 转录 + SQLite + traces 三套并存
- **Hermes**：SQLite + FTS5
- **OpenClaw**：会话文件 + 写锁 + 串行队列

**选择**：Codex 的事件日志，加上"派生状态必须可重建"的纪律。

理由：CodeBuddy 的三套状态目录（`.workbuddy` / `.workbuddy-ai` / `.codebuddy`）是这次调研里最典型的反例 —— 同一个用户三份数据，uid 混用，靠 `.fallback.bak` 兜底，最后没人知道"哪份是真的"。结论：**一份事实 + 若干可重建缓存**。索引带版本号，版本漂移即重建，不做原地迁移。

## 二、被吸收但改了形态的设计

| 原设计 | 问题 | 这里的形态 |
| --- | --- | --- |
| DSH 的插件暂存 Temp 再 import | Temp 被清 → 依赖解析失败 → 整树崩溃（本机实证） | 能力只从产品树/工作区两个锚点解析，绝不落 Temp |
| CodeBuddy 的记忆 Markdown + `RAW_JSON` | 实测 `memoryBlock` 为空，自动记忆没真正产出 | 双写照做，但 JSON 块是**权威**，Markdown 是视图 |
| Hermes 的 curator | 需要判定模型与状态机，成本高 | 只保留"归档不删除 + 可 pin + 可 restore"的最小契约 |
| 影子 git 检查点 | Hermes 在会话目录内建影子仓库 | 移到产品 home 下，用 `--git-dir` 显式寻址，绝不与用户真实仓库混淆 |
| Claude Code 的 hooks | 事件面很大 | 收敛为事件流，消费者（CLI / JSON / 看板）自己订阅，不预设钩子名 |

## 三、本次集成过程中实测踩到的坑

这几条是这次真正动手才暴露出来的，任何一份"架构对比"都不会写到：

1. **`ANTHROPIC_BASE_URL` 给不给出 `/v1`**：Claude Code 自己会拼 `/v1/messages`，配成 `https://host/v1` 就变成 `/v1/v1/messages` → 404，而它把这个 404 **渲染成"模型不存在或无权访问"**。整个排查过程就是被这句误导信息带偏的。
2. **网关转发 SSE 必须显式关连接**：不设 `Connection: close`，客户端会一直等流结束，表现为"永久挂起"。
3. **逐字节转发 SSE 会拖死长响应**：1 字节一次读写 + flush，长回答会被拖到超时并触发重试，症状和"模型不可用"一模一样。改成块读即解。
4. **CLI 沙箱的"外部目录"拒绝是静默浪费**：opencode 读工作目录外的文件会被 auto-reject，agent 会不断重试探索。对策是把证据文件复制进工作区，用相对路径喂给它 —— 这条对任何"给 agent 喂资料"的场景都适用。
5. **`settings.json` 的 `env` 会覆盖进程环境变量**：想临时换端点，用 `--settings <file>` 或独立 `CLAUDE_CONFIG_DIR`，别指望 `$env:` 能压过配置文件。

## 四、明确没做的

- **向量记忆**：记忆仍是精确匹配 + 排序。接向量库是下一步，但没做就不写在功能里。
- **内核级沙箱**：Windows 上真要关住 shell 需要 restricted token / ACL。当前是"策略 + 黑名单 + 路径白名单"，够挡误操作，不够挡蓄意绕过。
- **插件签名校验**：`install` 只做版本化落盘与 trust 标记。
- **分布式**：单机单会话串行，没有跨机调度。Hermes 的 kanban / gateway 那一层没有实现。
