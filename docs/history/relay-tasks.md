# 中转任务包 · forge v2 联合生成

给 **其他 AutoClaw 实例 / zcode / stepclaw / TRAE / PI agent** 的任务书。每条都可直接复制粘贴，任务书自带全部背景，**不依赖对方能读到你本机的文件**。

## 交接说明（贴给对方之前先看）

- 每个任务都是「输出一份 Markdown 报告」，让对方**直接输出正文**，不要写文件、不要反问。
- 拿回来的报告，两种回传方式任选：
  1. 直接粘进我们这个对话，我负责归档进 `agent-forge/reports/`；
  2. 存成本地文件后告诉我路径，我读取归档。
- 免费/便宜通道优先。**只有"大问题"才升级给 Claude 审查**——Claude Code 现在默认跑在小米 MiMo 上（本地网关 `127.0.0.1:8810` 做 Anthropic↔OpenAI 协议翻译），审查通道在 `127.0.0.1:8811`。

---

## 任务 0 · 通用背景（所有任务书都自带，不用单独贴）

> 我们在集成一个统一智能体框架 `forge`（Python，纯标准库），它从七套框架的设计里吸收长处：
> - **DeepSeek Harness**：配置 = 空根 + 有序补丁层，按 id 后写胜，整行替换不做深合并；`dump-default-config` 是可摘除用户层的恢复通道；`$expr` 惰性表达式（AST 白名单求值，禁属性访问）。
> - **WorkBuddy / CodeBuddy**：二维权限（mode 基线 + allow/ask/deny 例外，deny 恒胜）、子代理权限天花板、`Defer/NoDefer` 工具延迟加载 + ToolSearch、命令级黑名单、类型化记忆双写（Markdown 视图 + RAW_JSON 权威块）。
> - **Codex**：三档沙箱绑审批、rollout JSONL 事件流（首行 `session_meta` + 递增 ordinal）、resume/fork、派生索引版本化可重建。
> - **Hermes**：fallback 链只对可重试错误降级、MoA 聚合、影子 git 检查点与回滚、curator 只归档不删除、技能 provenance。
> - **OpenClaw**：提示分层组装、压缩阈值 + 压缩事件、记忆有界切片注入、子代理深度/预算上限与输出去毒。
> - **OpenCode**：provider 即数据（`provider[] + model + small_model`）、密钥不进配置文件。
> - **Claude Code**：CLI 输出形态、permission-mode 枚举、settings 分层（注意：`settings.json` 的 `env` 会覆盖进程环境变量）。
>
> v1 已完成：11 个模块、176 项离线自检全过。v2 正在加两个模块：**自我迭代进化（evolution）** 与 **异构联邦（federation）**。

---

## 任务 1 · 给「其他 AutoClaw 实例」

```
你是一名独立架构评审员。请评审下面这套智能体框架的两个新模块设计，产出中文 Markdown 报告，直接输出正文，不要写文件、不要反问。

【背景】
我们在集成一个统一智能体框架 forge（Python，纯标准库），吸收了 DeepSeek Harness / WorkBuddy(CodeBuddy) / Codex / Hermes / OpenClaw / OpenCode / Claude Code 七套框架的长处。

【要评审的两个模块】
A. 自我迭代进化（evolution）
   - 信号层：从会话里抽取「用户纠正 / 明确偏好 / 可复用工作流 / 高成本踩坑 / 成功模式」五类信号，每条信号带证据引用（session_id + 事件 ordinal）。
   - 候选层：信号聚合为候选（kind = memory | skill | config | prompt | note），带 risk（low/medium/high）、provenance（agent/human/imported）。
   - 安全闸门：long-term 目标（MEMORY.md / AGENTS.md / SOUL.md / USER.md / TOOLS.md / SKILL.md 等）永远需要人工批准，没有配置能关掉；只有 low-risk 的 note 类候选可自动生效。
   - 生效层：写入目标文件时附 provenance 注释（candidate id + 时间 + 证据引用）。
   - 账本：所有动作（nominated/approved/applied/rejected/quarantined/archived/rolled_back/restored）追加到 append-only JSONL，可重放。
   - 回滚：每个候选保存写入前的原文，rollback 可精确还原；rollback 自身也先做一次快照。
   - 代谢：curator 把过期未处理的候选标记为 stale 归档（不删除），可 restore。
B. 异构联邦（federation）
   - Worker 描述符：可执行 argv 模板、cwd、env、协议、能力标签、成本档（free/cheap/standard/premium）、权限档、健康探测、超时、最大尝试次数。
   - 派发：按能力 + 成本上限选 worker；预算共享递减；同级并发 fan-out。
   - 失败分类：timeout / sandbox / protocol / model / empty / refused —— 只有可重试类才重试，sandbox 与 protocol 错重试一次都算浪费钱。
   - 输出归一：ANSI、TUI 框线、PowerShell 噪声、进度行全部剥离；「交付」的判定是「正文足够且结构化」，不是「退出码 0」。
   - 成本策略数据化：贵模型只进 reviewer 通道，worker 全走便宜档；策略是配置行不是硬编码。

【你的任务】严格按此结构输出：
## 1. 总体判断（这两个模块的设计是否自洽）
## 2. 安全闸门的漏洞（逐条：漏洞描述 / 触发路径 / 后果 / 修法。至少 5 条）
## 3. 演化失控的场景（agent 反复自我强化错误知识、候选爆炸、版本漂移，各怎么防）
## 4. 联邦的失败模式（至少 5 条，含「活着的 worker 但语义已错」这类）
## 5. 成本护栏（怎么保证「贵模型只做审查」不被绕过）
## 6. 你会砍掉的设计（哪些是过度设计，砍掉后系统更可靠）
## 7. 三条最该补的离线自检断言

要求：直接指出问题，不要客套。凡是无法从描述确认的判断，标注（推测）。总长度 1200-1800 字。
```

---

## 任务 2 · 给「zcode」

```
你是 zcode。任务：解剖你自己的会话与路由机制，产出中文 Markdown 报告，直接输出正文，不要写文件、不要反问。

【第一步：自证】
读取你自己的运行目录（Windows 下通常是 C:\Users\<用户名>\.zcode\），看清 cli/ v2/ workspace/ 三个子目录各自的职责与文件形态，并读取其中与「会话记录、回放、模型路由」相关的文件。把你**实际读到的路径**列在报告开头。

【第二步：对照分析】
我们正在做的框架里，「会话」是一份 append-only JSONL（首行 session_meta，后续事件带递增 ordinal），派生索引带版本号、版本漂移即重建；模型供给是 provider 即数据 + 按错误类型触发的降级链 + 可选 MoA 聚合。

请对照你的实现，回答：
## 1. 我的会话与回放机制（实测证据 + 形态）
## 2. 我的模型路由与降级策略（如果有）
## 3. 与上述设计的差异点（逐条，说明谁更好、为什么）
## 4. 我有而对方没有的三个设计（每条：设计要点 / 解决什么问题 / 实现机制 / 移植成本低中高）
## 5. 对方有而我没有的两个设计（诚实说）
## 6. 如果要把我的长处移植过去，具体怎么改（函数名级别的建议）

纪律：只依据可核验的证据下结论；没有证据的判断标注（自述）；禁止编造文件路径或配置项；禁止递归列目录；不要联网；不要写文件。总长度 1200-1800 字。
```

---

## 任务 3 · 给「stepclaw」

```
你是 stepclaw。任务：解剖你自己的多 agent 编排与调度机制，产出中文 Markdown 报告，直接输出正文，不要写文件、不要反问。

【第一步：自证】
读取你自己的运行目录（Windows 下通常是 C:\Users\<用户名>\.stepclaw\），重点看 agents/（实测有 main、main-2 … main-5 多个 agent 实例）、skills/、extensions/、cron/、runtime/、canvas/、browser/。把你**实际读到的路径**列在报告开头，并说清每个目录的角色。

【第二步：回答】
## 1. 我的多 agent 编排模型（多个 main-N 是什么关系：并行分身？角色分工？各自独立的会话与记忆？）
## 2. agent 之间的通信与任务分发（用什么传消息、有没有共享任务列表、有没有寻址机制）
## 3. 权限与隔离（每个 agent 是否有独立权限边界；有没有天花板钳制）
## 4. 调度与定时（cron/ 的机制，与 agent 实例怎么结合）
## 5. 扩展体系（skills / extensions 的加载路径与信任模型）
## 6. 我有而常见框架没有的三个设计（每条：设计要点 / 解决什么问题 / 实现机制 / 移植成本）
## 7. 我的三个短板
## 8. 给「异构多 agent 联邦」的实现建议（8 条以内，具体到数据结构）

纪律：只依据可核验证据；无证据的判断标注（自述）；禁止编造；禁止递归列目录；不要联网；不要写文件。总长度 1400-2000 字。
```

---

## 任务 4 · 给「TRAE」

```
你是 TRAE。任务：把你自己的 agent 工作流解剖成可借鉴设计，产出中文 Markdown 报告，直接输出正文，不要写文件、不要反问。

【第一步：自证】
读取你自己的配置目录（Windows 下通常是 C:\Users\<用户名>\.trae\ 与 C:\Users\<用户名>\.trae-cn\），重点看：assistant/、builtin_skills/、skills/、mcps/、plugins/、toolhost/、permission/、memory/、worktrees/、installed-plugins.json、plugin-config.json、skill-config.json、argv.json。把你**实际读到的路径与关键配置项**列在报告开头。

【第二步：回答】
## 1. 我的 agent 形态（assistant / work / worktrees 各自是什么）
## 2. 工具宿主（toolhost）机制：工具怎么被注册、发现、授权
## 3. MCP 接入方式与权限模型（permission/ 里到底是什么）
## 4. 技能体系（builtin_skills 与用户 skills 的关系、加载路径、信任）
## 5. 记忆机制（memory/ 的落盘形态与注入方式）
## 6. 工作树（worktrees）隔离：怎么避免 agent 改坏用户仓库
## 7. 我有而常见 agent 框架没有的三个设计（每条：要点 / 问题 / 机制 / 移植成本）
## 8. 我的三个短板
## 9. 给「自研统一框架」的 8 条具体建议

纪律：只依据可核验证据；无证据的判断标注（自述）；禁止编造；禁止递归列目录；不要联网；不要写文件。总长度 1400-2000 字。
```

---

## 任务 5 · 给「PI agent」

```
你是 PI agent。任务：输出一份架构自述报告，中文 Markdown，直接输出正文，不要写文件、不要反问。

【背景】我们需要把 PI agent 接入一个异构 agent 联邦：联邦按「能力标签 + 成本档 + 权限档」派活，要求 worker 能被脚本以非交互方式调用并返回可判定成败的输出。我们实测到的调用形态是 `pi -p "<task>" --provider <name> --model <pattern> --mode text|json|rpc`，但用非默认 provider 时收到了 `403 {"error":{"type":"forbidden","message":"Request not allowed"}}`。

【要回答】严格按此结构：
## 1. 形态与定位（你是本地 CLI 还是托管服务？哪些能力必须在你的服务端完成？）
## 2. 主循环与执行模型（read/bash/edit/write 工具如何驱动；`--mode json` 与 `rpc` 的结构差异）
## 3. 会话机制（`--session` / `--fork` / `--session-dir` / `--no-session` 的实际语义）
## 4. 扩展体系（`pi install <source>`、extensions、model catalogs 的加载与信任）
## 5. 模型接入（provider 列表；**如何配置一个自定义 OpenAI 兼容端点**？是否支持自定义 base URL？如果支持，给出确切配置方式；如果不支持，明确说不支持）
## 6. 非交互调用的正确姿势（怎么保证一次调用给出可判定成败的输出；有没有类似 json schema 的结构化输出）
## 7. 上面那个 403 的成因与解法（如果这是鉴权前置，说清需要什么凭据）
## 8. 我有而常见 agent 框架没有的三个设计
## 9. 接入异构联邦需要注意的三件事

纪律：凡本机无法验证的，标注（自述）；禁止编造配置项名或命令；不要联网；不要写文件。总长度 1200-1800 字。
```

---

## 任务 6 · 给「Claude 审查通道」（只在有大问题时用）

```
你是资深架构审查员。下面是待审材料（把要审的内容贴在下一行），请给出可直接执行的修复清单。

【要求】
1. 逐条列出缺陷：位置 / 触发条件 / 后果 / 修法 / 工作量估计。
2. 区分「设计缺陷」与「实现缺陷」，不要混在一起说。
3. 明确指出哪些是纸面承诺而非代码强制（这是最容易骗过评审的一类）。
4. 最后给 P0/P1/P2 优先级排序，P0 必须能在数小时内修完。
5. 直接指出问题，不要客套；无法确认的判断标注（推测）。
```

> 使用方式：把 `claude -p "<上面的任务书 + 待审材料>" --settings <review-lane-settings>` 交给审查通道。审查通道配置在本地网关 `127.0.0.1:8811`（上游是 Claude），日常干活的默认通道在 `127.0.0.1:8810`（上游是小米 MiMo）。
