# forge — 统一智能体框架

把 **Codex / Hermes Agent / DeepSeek Harness / Claude Code / WorkBuddy(CodeBuddy) / OpenClaw / OpenCode** 七套框架里各自最值得抄的设计，收敛成一个可运行的最小内核。

不是概念图，是能跑的代码：`forge selftest` 离线跑全套检查（无网络、无 API Key，项数以命令实际输出为准），覆盖配置合成、权限裁决、工具延迟加载、能力信任、记忆双写、会话回放、影子快照、模型降级、协议网关与协议翻译、原生工具调用、自我迭代进化、异构联邦、贡献模块一致性闸门与跨模块集成、成本核算、子代理编排，以及静态安全线（禁网/禁子进程/禁破坏性文件 API 的表对齐不变量与四路覆盖）。

零第三方依赖（纯标准库），Python ≥ 3.10。

## v2 新增（0.2.0）

| 模块 | 学谁 | 干什么 |
| --- | --- | --- |
| `evolution.py` | **Hermes**（curator / learning / journey / checkpoints） | 信号抽取 → 候选 → 安全闸门 → 账本 → 回滚 → 代谢。long-term 目标永远要人点头 |
| `federation.py` | 本轮自建（失败模式都是从实测里长出来的） | 异构 CLI worker 的描述符 / 派发 / 失败分类 / 输出归一 / 成本护栏 |
| `wire.py` | Claude Code + MiMo 的协议错配 | Anthropic ↔ OpenAI 双向翻译，含 SSE 流式与工具调用增量 |

新命令：

```bash
python run.py evolution stats                # 候选池与账本概览（默认读 ~/.forge；看仓库自带数据用 --home .）
python run.py evolution observe "以后都用中文写报告" --nominate
python run.py evolution approve <id> && python run.py evolution apply <id>
python run.py evolution rollback <id>        # 可逆
python run.py evolution curate               # 归档过期候选（不删除）
python run.py federation roster              # worker 队列的能力/成本/权限档（从 home 的 federation.json 读取）
python run.py gateway --upstream https://api.example.com/v1 --upstream-wire openai \
    --model-map claude-sonnet-5=<upstream-model> --models claude-sonnet-5 --key $KEY
```

### 自我进化的四条硬规矩

1. **无批准不落地**：只有 low-risk 的 `note` 类候选能自动生效；其余一律进 pending。
2. **long-term 目标永远需要人点头**：`MEMORY.md` / `AGENTS.md` / `SOUL.md` / `USER.md` / `TOOLS.md` / `SKILL.md` 没有配置可以绕开。
3. **写入必带出处**：目标文件里附 candidate id + 时间 + 证据引用，事后可追溯。
4. **归档而非删除**：reject / quarantine / stale 全部可 `restore`；回滚前再快照一次。

## v3：代码由各家 agent 自己写（0.3.0）

新框架不是把现有 agent 串起来，而是**先冻结接口，再让每家 agent 各写一个模块，按契约把代码合到一起**。

- 契约：[`CONTRACT.md`](CONTRACT.md) —— 冻结的模块 API、硬性禁令、每个 agent 的即贴任务书
- 闸门：`forge/registry.py` —— 静态扫源码（禁网/禁子进程/禁破坏性文件 API/禁 `eval`）+ 动态加载 + 跑对方自带自检（≥ 8 条）
- 范本：[`forge/contrib/heartbeat.py`](forge/contrib/heartbeat.py) —— 第一份入库的贡献模块，10 条自检全过

```bash
python run.py modules validate    # 一致性截面：谁入库、谁隔离、为什么
python run.py modules selftests   # 跑所有已入库模块自带的断言
```

隔离而非半挂载：贡献模块导入抛异常、或自检有失败、或引用了禁用库，都会被记录原因后拦住，不影响框架本体。

## v4：向完整架构的四步（0.4.0）

1. **原生工具调用**（`toolwire.py`）——主循环从「只会说文本协议」改成 **原生优先、文本兜底**：工具声明按 wire 生成，调用从原生字段解出，工具结果按同一 wire 回流并**保留调用 id**。id 很重要：下一轮请求里助手消息与工具消息必须对得上，用散文重建是猜的。
2. **跨模块集成测试**（`selftest:test_contrib_integration`）——闸门只证明每份模块**单独**合格，这一条证明**接缝**：路由器挑中的 worker 必须是团队层投递到的那个人，调度器的到期集必须喂给同一次派发，压缩器必须和主循环对「多大算大」有同一个答案。
3. **钩子名漂移的规范化**（`registry.HOOK_ALIASES`）——三个作者三套命名（`due_jobs` / `on_due_check`，`replay` / `on_replay`）。不要求别人改自己代码里的名字，而是用别名表解析规范名，并把漂移记成**警告**写进索引。
4. **成本核算**（`pricing.py` + `forge cost`）——把两张真实账单反推的有效单价固化下来，让「同一件事派给谁」可以按数字决定。

```bash
python run.py cost rate --models deepseek-flash,mimo-v2.5 --tokens 1273002379
python run.py modules validate        # 现包含命名漂移警告
python run.py selftest                # 项数以实际输出为准
```

## v4.5：修复轮（2026-09-16，与对抗复验席逐轮签收）

v4 交付后进入修复轮：每一轮都由独立复验席自写探针攻击、签收后才进下一轮。四个批次全清（P0/P1/P2 + H11），g3 评审账面归零：

1. **静态安全线（H1–H10 关闭）**：贡献闸门从「扫 import」扩成双层闭合——别名感知 + 引用即拦 + dunder 属性表 + **表对齐不变量**（CALLS 与 FROM_NAMES 孪生双向钉死，扩表漏配提交即红灯）。根拼写六变体、`operator.attrgetter` 洗白、`os.*` 文件变更原语逐条闭环；`gate:*` 断言数以自测实际输出为准，独立探针全绿。
2. **工具宿主声明即语义（F4-2/F4-3）**：`readOnlyHint` 经归一透传，`authorize` 接受 `spec["read_only"]` 且**声明优先于名字启发**；`server__` 前缀剥离后启发不翻转——写工具叫 `read_*` 不再绕过判定。
3. **贡献挂载保底语义（F2-2/H11）**：`mount_contrib_extensions` 双注册表——包内 contrib 保底席位，`home/contrib` 同名模块**只有自己过闸且钩子可解析才接管**；被拒文件不再让席位静默消失，接管/保底记警告并首次 `run()` 发 `mount_warning` 事件（只发一次，不重复）。
4. **router/teams 主键冻结（F2-3）**：worker/member 以 `id` 为主键（同一键空间），`name` 只作显示与排序；集成自检改反值构造（id ≠ name），同值掩盖从此翻红。
5. **validate 警告行（F5-4）+ 正则清理（F5-5）**：`modules validate` 就地打印 `[warn]` 行；evolution 信号正则清除生成期串味残留。

## v0.7.0：三档模型智能路由

新增 `forge/routing.py`（SmartRouter，ModelRouter 子类，冻结契约零改动）。任务级选档，三种策略：

| 策略 | 路由逻辑 |
| --- | --- |
| `economy` | 按有效单价升序走档（pricing.py 账单反推价），仅可重试失败才上浮；未知价保守殿后，不被当免费档打穿 |
| `balanced` | 中端主力档首发，失败向上升级 + 冻结 chain 兑底（默认） |
| `premium` | 两阶段流水：中端出草稿 → 高端集成裁决；集成段任何失败（含 fatal）都降级回草稿——草稿已付费不弃；消息形状严格角色交替（system/user/assistant/user），集成档只收原任务+草稿，完整对话不外流 |

```bash
python run.py run "任务" --strategy economy      # CLI 显式指定（> 配置 > 默认 balanced）
```

配置在 `bundles/base.json` 的 `model.routing` 块：`tiers`（档位表，按成本序声明）、`premium`（集成裁决档）、`small`（杂务档，失败不上浮烧不到审查档）。计价表未知价=0.0 的单一权威定义见 `pricing.py` 顶部注释（routing 取保守解释、预算缩放取宽松解释，有意不同）。

## 快速开始

```bash
cd agent-forge

python run.py selftest            # 离线自检，项数以实际输出为准
python run.py dump-config         # 看合成出的配置树
python run.py doctor              # 健康/漂移/密钥检查
python run.py capabilities list   # 能力清单
python run.py run "读一下 README 并总结"
python run.py gateway --upstream https://api.deepseek.com --port 8799 --models claude-sonnet-5
```

`run.py` 是必需的入口：AutoClaw 内嵌 Python 用 `._pth` 布局，`python -m forge.cli` 会报 `No module named 'forge'`（当前目录不入 `sys.path`，`PYTHONPATH` 也失效）。外面用标准 Python 时 `python -m forge.cli` 可用。

零第三方依赖（纯标准库），Python ≥ 3.10。

## 七套框架 → 一处落点

| 借鉴对象 | 被吸收的设计 | 落点模块 | 自检锚点 |
| --- | --- | --- | --- |
| **DeepSeek Harness** | 空根 + 有序补丁层、按 id 后写胜、整行替换不做深合并、`dump-default` 恢复通道、`$expr` 惰性表达式 | `config.py` | `config:*` |
| **WorkBuddy / CodeBuddy** | 二维权限（mode 基线 + allow/ask/deny 例外，deny 恒胜）、子代理权限天花板、Defer/NoDefer 延迟加载、命令级黑名单、类型化记忆双写、trace 计量 | `policy.py` `tools.py` `memory.py` | `policy:*` `tools:*` `memory:*` |
| **Codex** | 三档沙箱绑审批、rollout JSONL 事件流（`session_meta` + ordinal）、resume/fork、派生索引版本化可重建、点路径覆盖 + 严格模式 | `policy.py` `session.py` | `session:*` |
| **Hermes Agent** | fallback 链按错误类型触发、MoA 多槽聚合、影子 git 检查点与回滚、curator 只归档不删除、技能 provenance | `model.py` `checkpoint.py` `memory.py` | `model:*` (3) `checkpoint:*` (3) |
| **OpenClaw** | 系统提示分层组装、上下文压缩阈值与压缩事件、记忆有界切片注入、子代理深度/预算/结果去毒、会话串行 | `loop.py` `memory.py` | `loop:*` |
| **OpenCode** | provider 即数据（`provider[] + model + small_model`）、统一 wire 适配层、密钥不进配置文件 | `model.py` `cli.py` | `model:*` `doctor:*` |
| **Claude Code** | CLI 输出形态（`-p` 非交互 / 结构化输出）、权限模式枚举、设置分层与覆盖 | `cli.py` `policy.py` | `cli:*` |

设计原则、被否掉的方案、以及每一条的取舍理由见 [`DESIGN.md`](DESIGN.md)。各借鉴对象的席位锚点（`config:*` 等）计数随每轮修复增长，以 `selftest` 实际输出为准，此处不逐个硬编码。

## 内核长什么样

```
任务
 └─ Agent.run()                     loop.py
      ├─ 系统提示组装  ← 能力索引 + 记忆切片 + 权限状态（都有界）
      ├─ 上下文压缩    ← 超阈值折叠中段、保留尾部，写 compaction 事件
      ├─ ModelRouter   ← 主模型 → 按错误类型降级链 →（可选）MoA 聚合
      ├─ 工具调用      ← Defer 默认隐藏 → tool_search 激活 → 权限裁决 → 执行
      │    └─ 写操作前 → CheckpointStore 影子快照
      └─ spawn_subagent ← 独立上下文 / 深度上限 / 预算 / 权限天花板 / 输出去毒
```

每一次决策都落进 `sessions/*.jsonl`：`session_meta`、`user_message`、`tool_decision`、`tool_call`、`subagent_spawn`、`compaction`、`checkpoint`、`assistant_message`。日志是事实，索引是缓存 —— `sessions --rebuild` 随时重建。

## 五个"不妥协"

1. **deny 恒胜**：无论 mode 多宽、无论子代理多大胆，显式 deny 与命令黑名单先于一切裁决。
2. **子代理不得越权**：子代理的 mode 会被父会话天花板钳制；`plan` 父会话里的子代理不可能拿到 `bypassPermissions`。
3. **用户层可整层摘除**：`dump-default-config` 跳过用户层，配置写坏永远不会锁死启动。
4. **归档而非删除**：curator 只把 agent 自建知识标记为 archived，`restore` 可逆。
5. **密钥不进配置文件**：`doctor` 会把内联 `apiKey` 直接报为告警，provider 走环境变量或环回网关。

## 目录

```
agent-forge/
├── forge/
│   ├── config.py       空根 + 补丁层合成（DSH）
│   ├── policy.py       二维权限 + 三档沙箱 + 命令黑名单（CodeBuddy / Codex）
│   ├── tools.py        工具注册 + 延迟加载 + ToolSearch（CodeBuddy）
│   ├── capability.py   skills/plugins/connectors 统一契约（Hermes / Codex）
│   ├── memory.py       类型化记忆双写 + curator（CodeBuddy / OpenClaw）
│   ├── compaction.py   上下文压缩策略（从 memory 拆分，独立可测试）
│   ├── subagent.py     子代理编排：派生/预算/输出净化（从 loop 拆分）
│   ├── thinking.py     沉思引擎：预算控制/收敛判定/三档模式（从 loop 拆分）
│   ├── session.py      追加式事件日志 + 派生索引 + fork（Codex）
│   ├── model.py        provider 抽象 + 降级链 + MoA（OpenCode / Hermes）
│   ├── routing.py      三档智能路由（economy / balanced / premium）
│   ├── checkpoint.py   影子 git 快照与回滚（Hermes）
│   ├── loop.py         agent 主循环（OpenClaw / CodeBuddy）
│   ├── gateway.py      环回协议网关（本次集成实测产物）
│   ├── wire.py         Anthropic ↔ OpenAI 协议翻译
│   ├── toolwire.py     原生工具调用协议适配
│   ├── federation.py   异构 CLI 联邦调度
│   ├── evolution.py    自我迭代进化
│   ├── registry.py     贡献模块注册表 + 一致性闸门
│   ├── pricing.py      成本核算
│   ├── guard.py        共享安全禁令表
│   ├── smoke.py        端到端冒烟测试
│   ├── selftest.py     离线验证套件
│   ├── cli.py          命令行入口
│   └── test_new_modules.py  新模块独立测试
└── bundles/
    └── base.json       基线配置层（provider / policy / loop / model）
```

## 已知边界

- `gateway` 与 `model.HttpTransport` 走标准库 HTTP，没有连接池与重试退避策略；生产化需要替换 transport。
- 权限裁决的原则是"沙箱拦截**写入**路径 + 黑名单拦截程序"，不提供内核级隔离（Windows 上要做 restricted token / ACL 才能真正关住 shell）。**读取不受沙箱约束**：`read_file` / `list_dir` / `grep` 可读工作区之外的路径（相对/绝对/`..` 均按原样解析）——读侧的收敛靠声明制（toolhost 归一时的 `read_only` 标注与 `authorize` 判定）与运行期白名单批次，不是路径沙箱；需要读隔离的场景应把读工具声明为只读并在 `deny`/`ask` 层约束。
- `allow` 规则**恒高于 mode 基线**（deny 除外）：`read-only` 模式下显式 `allow` 一个写入类工具会放行——mode 是基线不是硬下限，这是显式语义而非缺陷；依赖 read-only 做硬隔离的场景应改用沙箱档位与 `deny` 规则。
- `capability.install` 只做版本化目录落盘，没有签名校验；供应链审计需要外接 OSV 之类的源。
- 记忆检索是精确匹配 + 时间/置顶排序，没有向量召回 —— 接向量库是明确的下一步。
- `read_only` 是声明而非强制——写工具标 `read_only=True` 会绕过沙箱路径校验与 read-only 模式写门（WB-P2 authorize 声明信任缺口；当前 toolhost 未被运行时挂载，影响有限）。
- premium 两阶段会把**草稿与原始任务**发送给集成裁决档（知情使用：配置了 premium 档即同意该数据面）；完整对话历史不会外流。
- `mount_contrib_extensions` 的双注册表保底覆盖静态闸和钩子可解析性；运行期钩子异常的保底由 `_use_extension` 的异常捕获兜底（不回退到包内模块）——需要「坏钩子不抢位」的场景应改为运行期降级重试（复杂，登记非挂账）。
- `teams.deliver` 的 `to` 方向只做 id 寻址（无 name→id 解析），`from` 方向支持 name 唯一归属解析——非对称设计，安全但需注意：`to` 不可用显示名。
