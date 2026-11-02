# 贡献契约与分工 · forge v3（新框架的代码由各家 agent 自己写）

## 0. 这份文件解决什么

目标是**一个新框架**，不是把现有 agent 串起来跑。做法：先冻结接口，再让各家 agent 各自写一个模块，最后由集成者按契约把代码合到一起。

为什么要先冻结：异构 agent 各写各的，如果接口不固定，合的时候只能靠人工重写，"组合"就变成"重做"。所以这里把**拼接缝**先钉死 —— 谁的代码都只能从这条缝里伸出来。

**合并不靠人肉改写，靠机器校验**：`forge/registry.py` 会静态扫描 + 动态加载 + 跑对方自带的自检，不合规的模块直接隔离（quarantine），不会半挂载进框架。

---

## 1. 冻结契约（所有贡献模块必须遵守）

### 1.1 文件与入口

- 路径：`agent-forge/forge/contrib/<模块名>.py`（模块名 == 文件名，小写下划线）
- 顶部必须：`from __future__ import annotations` 和 `MODULE_API_VERSION = 1`
- 只暴露两个公开函数：

```python
def register(api) -> dict:
    """加载时调用一次。返回清单，声明这个模块提供什么。"""
    return {
        "name": "scheduler",            # 必须等于文件名
        "version": "1.0.0",
        "capabilities": ["schedule.periodic", "schedule.lease"],
        "hooks": {"due_jobs": due_jobs},  # 可选，但框架就靠它调用你的逻辑
        "author": "Step",
        "summary": "一句话说明",
    }

def selftest() -> list[tuple[str, bool, str]]:
    """离线自检，至少 8 条。返回 (断言名, 是否通过, 失败时的人话说明)。"""
    return [("case-name", True, "")]
```

### 1.2 硬性禁令（`registry.py` 会静态扫描源码，命中即隔离）

| 禁止 | 原因 |
| --- | --- |
| `import socket / http / urllib / requests / ftplib / smtplib` | 贡献模块在框架进程内运行，不允许联网 |
| `import subprocess / multiprocessing / ctypes` | 不允许自己起进程 |
| `import shutil`、任何 `os.unlink / os.remove / os.rmdir / os.system / os.popen` | 不允许破坏性文件操作 |
| `eval() / exec() / compile()` | 不允许动态执行 |
| `import` 其它 contrib 模块 | 模块之间不许直接耦合，只能通过 `api` |

### 1.3 必须**纯函数化**的数据接口

**模块不许自己读写文件、不许自己跨进程通信**。数据由框架以普通 `dict / list` 传进来，你返回普通 `dict / list`。理由：这样你的模块既能被框架调用，也能被你自己的 `selftest()` 用固定输入验证 —— 你写的时候就能自测，不用等集成。

### 1.4 你能用的 `api` 表面（只有这些）

```python
api.home            # Path，产品数据根
api.workspace       # Path，工作区根
api.config_get("scheduler.tickSeconds", 60)   # 点路径读配置，缺省安全返回
api.policy          # 只读的权限策略对象（frozen，改不动，别试）
api.memory          # 记忆存储（recall / remember）
api.session         # 当前会话（append 事件）
api.checkpoints     # 影子快照（snapshot）
api.fire(type="...", **fields)                # 往会话事件流写一条事件
api.say("...")                                # 写日志
```

### 1.5 交付与验收

- **交付形态**：直接输出一个完整 `.py` 文件的代码块（**不要写成文件**，不要分段给）。
- **验收**：把文件放进 `forge/contrib/`，然后
  ```bash
  python run.py modules validate     # 一致性检查：不合规会列出原因
  python run.py modules selftests    # 跑你自带的断言
  python run.py selftest             # 176+ 项框架自检，不能退化
  ```
- 三个都过 = 入库；任一个不过 = 退回重写，集成者不做人工修补。

---

### 1.6 工具名 grammar（冻结于 2026-09-14，外部审计发现后补）

跨模块的工具名必须满足同一套语法，否则一边生成、一边拒绝：

- 合法字符：`^[A-Za-z0-9_.\-]+$`（**不含 `/`**）
- 跨 server 去重时用双下划线分隔，例如 `fs__read_file`，不用 `fs/read_file`
- 同一个模块的**产出**与**校验**必须用同一套语法（这正是审计抓到的那处自相矛盾）

已登记缺陷：`mcp.tool-name-grammar` —— `mcp_bridge` 产出 `server/tool`，而 `register_tools` 拒绝 `/`，导致带前缀的 MCP 工具在真实链路上全部被丢弃。已回传作者，在本次修复前由 `selftest` 的接缝探针以「已声明缺陷」形式看守。

## 2. 分工表（第一轮：写代码）

| Agent | 模块 | 文件 | 一句话职责 | 必须提供的 hook |
| --- | --- | --- | --- | --- |
| **Step** | 调度器 | `forge/contrib/scheduler.py` | 周期任务判定 + 实例租约 + 全局暂停闸门 | `due_jobs` / `acquire_lease` / `gate` |
| **Z Code** | 会话回放 | `forge/contrib/replay.py` | 从事件流重建时间线、分叉、两次运行 diff | `replay` / `fork_plan` / `diff_runs` |
| **Trae Work** | 工具宿主 | `forge/contrib/toolhost.py` | 外部工具/MCP 描述 → 统一工具规格 + 权限挂钩 | `register_tools` / `authorize` |
| **PI agent** | 模型目录 | `forge/contrib/catalog.py` | provider 能力矩阵 + 健康状态建模 + 降级链构建 | `catalog` / `build_chain` |
| **Forge runtime** | 收敛监督 | `forge/contrib/supervisor.py` | 工具轨迹的阶段化评分 + 有界 advisory/abort 分流 | `supervise` |

## 3. 第二轮（第一轮入库后开工）

| Agent | 模块 | 文件 | 职责 | hook |
| --- | --- | --- | --- | --- |
| Hermes（本地） | 知识代谢 | `forge/contrib/curator.py` | sidecar 状态机 active/stale/archived + pin + 账本 | `curate` |
| WorkBuddy（本地） | 团队协作 | `forge/contrib/teams.py` | 成员消息投递 + 共享任务列表 + @寻址 | `deliver` |
| Claude Code（MiMo 通道） | 交叉审查 | 只交报告，不写代码 | 逐个审已入库模块的一致性、逻辑漏洞、纸面承诺 | — |
| OpenCode（DeepSeek Flash） | 上下文压缩 | `forge/contrib/compactor.py` | 压缩阈值判定 + 保留区选择（它这两轮不稳定，降为备份位） | `should_compact` / `plan` |

---

## 4. 即贴任务书

### 4.1 给 Step —— 调度器模块

```
你是 Step。任务：为统一智能体框架 forge 写一个贡献模块 scheduler.py。直接输出完整代码块，不要写文件、不要反问。

【为什么是你】你的 cron/、runtime/ 与 main-N 多 agent 实例经验，正是这个模块需要的。

【交付】一个 Python 文件全文，模块名 scheduler，路径将是 forge/contrib/scheduler.py

【冻结契约】必须逐条满足：
1. 顶部：`from __future__ import annotations` 与 `MODULE_API_VERSION = 1`
2. 只暴露 register(api) 与 selftest() 两个公开函数
3. register 返回 {"name": "scheduler", "version": "...", "capabilities": [...], "hooks": {...}, "author": "Step", "summary": "..."}
4. selftest() 返回至少 8 条 (断言名, True/False, 说明)，且必须全部通过
5. 纯标准库；禁止 import socket/http/urllib/requests/subprocess/multiprocessing/shutil/ctypes；禁止 os.unlink/os.remove/os.system/eval/exec；禁止 import 其它 contrib 模块
6. 不许自己读写文件、不许起线程、不许 sleep —— 数据和返回值都是普通 dict/list

【必须提供的 hooks】
- due_jobs(jobs: list[dict], now: float) -> list[dict]
  jobs 每项形如 {"id","everySeconds","lastRunAt","paused","leaseOwner","leaseExpiresAt"}
  返回「现在该跑」的任务副本（不要原地改输入）。规则：未跑过算 due；到间隔算 due；间隔 <= 0 跳过；全局闸门或任务 paused 时不返回。
- acquire_lease(job: dict, owner: str, now: float, ttl_seconds: float) -> tuple[bool, dict]
  返回 (是否拿到, 更新后的 job)。租约未过期且 owner 不是自己 → 拿不到；已过期 → 允许抢占。
- gate(global_state: dict, now: float) -> dict
  全局暂停闸门：{"paused": bool, "pausedUntil": float|null} → 返回 {"blocked": bool, "reason": str}。暂停到期自动解除。

【质量要求】
- 所有时间计算基于传入的 now，不读系统时钟（否则无法自测）。
- 输入缺字段、类型不对、None 都要安全返回，不许抛异常。
- selftest 至少覆盖：未跑过 due / 到间隔 due / 未到间隔不 due / interval<=0 跳过 / 租约互斥 / 租约过期可抢占 / 暂停阻断 / 暂停到期解除 / 返回是副本不是同一对象 / 空输入安全。这 10 条正好够。

【输出格式】一个 ```python 代码块，包含完整文件；代码块之前不要写任何解释。
```

### 4.2 给 Z Code —— 会话回放模块

```
你是 Z Code。任务：为统一智能体框架 forge 写一个贡献模块 replay.py，做「会话即数据」的回放与比对。直接输出完整代码块，不要写文件、不要反问。

【为什么是你】你自己的 rollout 会话目录正是这套能力的实战来源。

【交付】一个 Python 文件全文，模块名 replay，路径将是 forge/contrib/replay.py

【背景：框架的事件流格式】
会话是一份 append-only JSONL，事件形如：
{"ordinal": 3, "type": "tool_call", "ts": 1757..., "tool": "read_file", "args": {...}, "ok": true, "result": "..."}
首行是 {"ordinal": 0, "type": "session_meta", ...}。ordinal 严格递增。事件 type 常见：
user_message / assistant_message / tool_call / tool_decision / subagent_spawn / subagent_result /
compaction / checkpoint / loop_stop / model_error

【冻结契约】必须逐条满足：
1. 顶部：`from __future__ import annotations` 与 `MODULE_API_VERSION = 1`
2. 只暴露 register(api) 与 selftest()
3. register 返回 {"name": "replay", "version": "...", "capabilities": [...], "hooks": {...}, "author": "Z Code", "summary": "..."}
4. selftest() 至少 8 条断言且全过
5. 纯标准库；禁止 import socket/http/urllib/requests/subprocess/multiprocessing/shutil/ctypes；禁止 os.unlink/os.remove/os.system/eval/exec；禁止 import 其它 contrib
6. 不许自己读文件（事件列表由参数传进来）、不许起线程、不许 sleep

【必须提供的 hooks，全部为纯函数】
- replay(events: list[dict], until_ordinal: int | None = None) -> dict
  返回 {"timeline": [...], "turns": int, "tool_calls": int, "tools_used": [...], "usage": {...},
        "stops": [...], "truncated_at": int|null}
  timeline 每项 {"ordinal","type","label","detail"}，label 要是人话摘要（例如 "调用 read_file" 而不是 "tool_call"）。
  until_ordinal 给定时只回放到该 ordinal（含）。
- fork_plan(events: list[dict], keep_last: int) -> dict
  返回 {"source_ordinals": [...], "dropped": int, "reason": "..."}，说明新分叉会话会保留哪些事件。
  边界：keep_last <= 0 返回空计划；keep_last 大于总数返回全部；必须始终丢掉 session_meta（它不是历史）。
- diff_runs(a: list[dict], b: list[dict]) -> dict
  比较两次运行的事件流，返回 {"only_in_a": [...], "only_in_b": [...], "first_divergence": int|null,
                                "tool_delta": {...}}
  判定「同一事件」用 (type, tool if any) 序列对齐即可，不必精确匹配 payload。

【质量要求】
- 任何畸形事件（缺 type、缺 ordinal、ordinal 乱序、非 dict 元素）都要安全跳过而不是抛异常，并在结果里计入 "malformed" 计数。
- selftest 至少覆盖：时间线重建顺序正确 / label 是人话 / until_ordinal 截断 / fork 丢 meta / fork keep_last<=0 / fork keep_last 超限 / diff 找出首个分歧点 / diff 工具差异 / 畸形事件被安全跳过并计数 / 空输入安全。这 10 条正好够。

【输出格式】一个 ```python 代码块，包含完整文件；代码块之前不要写任何解释。
```

### 4.3 给 Trae Work —— 工具宿主模块

```
你是 Trae Work。任务：为统一智能体框架 forge 写一个贡献模块 toolhost.py，把外部工具描述（含 MCP 风格）归一成框架内部工具规格，并挂上权限判定。直接输出完整代码块，不要写文件、不要反问。

【为什么是你】你的 toolhost/ 与 mcps/ 正是「工具托管」这一层的实战来源。

【交付】一个 Python 文件全文，模块名 toolhost，路径将是 forge/contrib/toolhost.py

【背景：框架的工具规格（目标形态）】
{"name": str, "description": str, "schema": dict, "read_only": bool, "deferred": bool}
框架的权限是二维的：mode 定基线（read-only/default/acceptEdits/auto/dontAsk/plan/bypassPermissions），
另有 allow/ask/deny 三类规则，且 deny 恒优先。工具分只读与写入两类，写入类要做沙箱路径校验。

【冻结契约】必须逐条满足：
1. 顶部：`from __future__ import annotations` 与 `MODULE_API_VERSION = 1`
2. 只暴露 register(api) 与 selftest()
3. register 返回 {"name": "toolhost", "version": "...", "capabilities": [...], "hooks": {...}, "author": "Trae Work", "summary": "..."}
4. selftest() 至少 8 条断言且全过
5. 纯标准库；禁止 import socket/http/urllib/requests/subprocess/multiprocessing/shutil/ctypes；禁止 os.unlink/os.remove/os.system/eval/exec；禁止 import 其它 contrib
6. 不许联网、不许真的调用工具、不许起进程

【必须提供的 hooks，全部为纯函数、只做转换与判定】
- register_tools(specs: list[dict]) -> list[dict]
  输入是外部（含 MCP 风格）工具描述，形如
  {"name","description","inputSchema":{"type":"object","properties":{...}},"readOnlyHint":bool,"annotations":{...},"source":"mcp"}
  或 {"name","description","parameters":{...},"source":"native"}
  输出统一规格列表，字段固定为 name/description/schema/read_only/deferred/source。
  规则：schema 取 inputSchema 或 parameters，都缺则给 {"type":"object","properties":{}}；
  readOnlyHint 缺失时按名字启发（read*/list*/search*/get*/describe* 视为只读，其余视为写入）；
  名字不合法（空、非字符串、含空格或非法字符）的条目丢弃并计入 dropped；
  重名保留第一个，后续计入 deduped；deferred 默认为 False，但 annotations.deferred == True 时置真。
  返回值的每一项都要有稳定的字段集合（不许有时有、有时没）。
- authorize(tool: str, mode: str, rules: dict, target_path: str | None, workspace: str) -> dict
  输入：工具名、当前 mode、rules={"allow":[...],"ask":[...],"deny":[...]}、目标路径（可空）、工作区根路径。
  返回 {"decision": "allow"|"ask"|"deny", "reason": str}。
  规则（必须严格按此顺序）：deny 命中即 deny；写入类工具的目标路径不在 workspace 之下即 deny；
  ask 命中即 ask；mode 为 bypassPermissions 时 allow；mode 为 read-only 且是写入类 → deny；
  其余按 mode：default/auto/acceptEdits 下写入类 → ask、只读 → allow；dontAsk 下写入类 → deny、只读 → allow；
  plan 下只读与 tool_search 允许、其余 deny。
  路径判定必须处理相对路径（相对 workspace 解析）、Windows 反斜杠、以及 .. 越界。

【质量要求】
- selftest 至少覆盖：MCP 描述归一 / native 描述归一 / 缺 schema 的兜底 / 只读启发识别 / 非法名丢弃 / 重名去重 / deferred 标注透传 / deny 恒优先 / 越界路径被拒 / 相对路径正确解析。这 10 条正好够。

【输出格式】一个 ```python 代码块，包含完整文件；代码块之前不要写任何解释。
```

### 4.4 给 PI agent —— 模型目录模块

```
你是 PI agent。任务：为统一智能体框架 forge 写一个贡献模块 catalog.py，维护 provider/模型能力矩阵并据此构建降级链。直接输出完整代码块，不要写文件、不要反问。

【为什么是你】你的 model catalogs 概念正是这一层。

【交付】一个 Python 文件全文，模块名 catalog，路径将是 forge/contrib/catalog.py

【背景：框架的模型供给】
provider 即数据：{"name","baseUrl","wire"(openai|anthropic),"models":[...],"defaultModel","smallModel"}。
降级链只在**可重试错误**时前进：429 / 5xx / 连接错误可重试；400 类请求错误不可重试（换 provider 是浪费）。

【冻结契约】必须逐条满足：
1. 顶部：`from __future__ import annotations` 与 `MODULE_API_VERSION = 1`
2. 只暴露 register(api) 与 selftest()
3. register 返回 {"name": "catalog", "version": "...", "capabilities": [...], "hooks": {...}, "author": "PI", "summary": "..."}
4. selftest() 至少 8 条断言且全过
5. 纯标准库；禁止 import socket/http/urllib/requests/subprocess/multiprocessing/shutil/ctypes；禁止 os.unlink/os.remove/os.system/eval/exec；禁止 import 其它 contrib
6. 绝对不许真的发网络请求（本模块只建模，不发请求）

【必须提供的 hooks，全部为纯函数】
- catalog(providers: list[dict], observations: list[dict] | None = None) -> dict
  输出 {"providers": {name: {...}}, "models": {model_id: {...}}, "capabilities": {...}, "warnings": [...]}。
  每条 provider 记录 {"wire","models","defaultModel","smallModel","hasKey"(bool，按是否提供 apiKey 字段判断，不要回显密钥)}；
  每条 model 记录 {"provider","id","wire","isSmall"(是否为该 provider 的 smallModel)}；
  observations 形如 [{"provider","model","ok","error_class","latencyMs"}]，用来累计 {"attempts","failures","lastErrorClass"}；
  warnings 要指出：无 models 的 provider、defaultModel 不在 models 里的 provider、wire 非 openai/anthropic 的 provider。
  同 id 模型出现在多个 provider 时都要保留（键用 "provider/model"）。
- build_chain(providers: list[dict], primary: tuple[str, str] | None,
              error_class: str | None, exclude: list[str] | None = None) -> list[tuple[str, str]]
  构建有序降级链。规则：primary 排第一；error_class 属于不可重试类（"bad_request"/"invalid"/"400"）时
  返回只含 primary 的链；属于可重试类（"rate_limit"/"overloaded"/"timeout"/"connection"）时按 provider 顺序补齐；
  error_class 为 None 时返回完整链；exclude 里的 provider 跳过；同一 (provider, model) 不重复。
  没有 primary 时返回按 provider 名排序的完整链。

【质量要求】
- selftest 至少覆盖：目录构建 / hasKey 判定不含密钥内容 / smallModel 标记 / 多 provider 同名模型 / 缺 models 告警 / defaultModel 不在 models 告警 / 非法 wire 告警 / observations 累计失败 / 可重试错误补齐链 / 不可重试错误不补齐 / exclude 生效 / 链内无重复。至少 8 条以上（覆盖不了全部也要覆盖主要 10 条）。

【输出格式】一个 ```python 代码块，包含完整文件；代码块之前不要写任何解释。
```

---

## 5. 回传与合并

1. 你把某个 agent 的代码块复制回来（或直接存成文件告诉我路径）。
2. 我把它落到 `forge/contrib/<模块名>.py`，跑三条命令：`modules validate` / `modules selftests` / `selftest`。
3. 通过 → 入库，并在 `forge/contrib/INDEX.md` 记录作者、能力、断言数；不通过 → 我把**机器给的具体拒绝原因**发回，你转给它重写。
4. 全部入库后我做一次**跨模块集成自检**：确认各家 hook 在统一调用链上互相不冲突（例如 scheduler 的 due_jobs 与 compactor 的 should_compact 不会互相抢占预算），并把结果写进集成报告。

**一条纪律**：我不替任何 agent 修它的模块代码。代码是谁写的就署谁的名，改也是它的下一版。集成者的工作只有两件 —— 定契约、验契约。

---

## 6. 后记：契约的现在时（2026-09-16 修订）

上面的任务书是**历史件**——第一轮开单时的原文。入库与运行以闸门与运行时的**现状**为准；与正文有出入时按本节为准：

- **hooks 只声明运行时可达的规范名**：`selftest` 是测试入口不是运行时钩子——registry 经 `_collect_assertions` 直接调用它收集断言，不走 `HOOK_ALIASES`；把 `selftest` 写进 hooks manifest 会被记为不可达死钩子警告（F2-4，toolhost/mcp_bridge 已按此修）。
- **readOnlyHint 归一与 authorize 语义（F4-2/F4-3）**：`register_tools` 归一时先剥一层 `server__` 前缀再做只读启发；归一产物的 `read_only` 字段在重新入管时被直接采信（与 `readOnlyHint` 同级）。`authorize` 接受可选 `spec`，`spec["read_only"]` **优先于名字启发**——写工具叫 `read_*` 不能靠名字绕过判定。
- **§4.3 的 authorize 规则序与 mode 基线是 2026-09-14 的版本**；现行实现叠加了：声明优先、前缀剥离、以及「`allow` 恒高于 mode 基线（deny 除外）」的显式语义（见 readme「已知边界」）。冲突时以实现 + 自测为准。
- **router/teams 主键（F2-3）**：worker/member 以 `id` 为主键（与 `teams.deliver` 成员寻址同一键空间），`name` 只作显示与排序；`id` 缺失回落 `name`（legacy 名册兼容），两键全缺判非法。任务书写作时未冻结此点，现补记。
- **贡献模块挂载（F2-2/H11）**：`mount_contrib_extensions` 双注册表——包内 contrib 保底席位，`home/contrib` 同名模块只有自己过闸且钩子可解析才接管；被拒文件不再让席位静默消失，接管/保底记警告并首次 `run()` 发 `mount_warning` 事件。§4.3 的「落进 `forge/contrib/` 即成为参考实现」仍然成立，但不再是唯一来源。
