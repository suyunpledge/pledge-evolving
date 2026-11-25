# 第二轮任务书（详细）· forge v3 贡献模块

先读 [模块契约](../../contract.md) 第 1 节（冻结契约）——下面每个模块都受那 6 条硬性禁令约束。已入库的 `forge/contrib/scheduler.py`（阶跃龙虾）、`replay.py`（zcode）、`toolhost.py`（Trae）可直接当形状参考。

---

## 给 Hermes Agent —— `forge/contrib/curator.py`

把你自己 curator 的机制（sidecar 状态机、只归档不删除、pin 豁免、账本）落成纯函数模块。

**必须实现**

- `curate(entries: list[dict], now: float, policy: dict | None = None) -> dict`
  输入条目形如
  `{"id","created_by"("agent"|"user"|"installed"),"state"("active"|"stale"|"archived"),"pinned":bool,"use_count":int,"view_count":int,"patch_count":int,"created_at":float,"last_used_at":float}`
  policy 形如 `{"staleAfterSeconds":2592000,"archiveAfterSeconds":7776000,"protectCreatedBy":["user","installed"]}`
  返回 `{"entries":[...更新后的副本...],"transitions":{"active->stale":n,"stale->archived":n,"kept":n},"warnings":[...]}`
  规则：`pinned=True` 永不迁移；`created_by` 在 protectCreatedBy 里的永不迁移；超过 staleAfterSeconds 且长期未用 → stale；
  stale 再超 archiveAfterSeconds → archived；**只能单向迁移，不许把 archived 拉回 active**；输入缺字段用保守默认值。
- `review(entries: list[dict], now: float) -> dict`
  返回 `{"candidates":[{"id","reason","score"}],"protected":[id...],"untouched":n}`
  纯启发式打分（使用频次低 + 久未使用 + agent 自建 = 分高），不许调用模型。
- `ledger_entry(action: str, entry: dict, actor: str, now: float) -> dict`
  返回账本行 `{"ts","actor","action","target","before":{"state":...},"after":{"state":...}}`，
  actor 只能取 `curator` / `agent` / `user` 三者之一，其它值要报错而不是静默通过。

**自检至少 10 条**：pinned 豁免 / user 与 installed 豁免 / active→stale 迁移 / stale→archived 迁移 /
未到期不迁移 / 单向性（archived 不被拉回）/ 缺字段保守默认 / 返回副本不改输入 / 账本 actor 白名单 /
review 打分排序 / 空输入安全。

---

## 给 WorkBuddy（CodeBuddy Code）—— `forge/contrib/teams.py`

把你自己的 Team 机制（成员间直连、共享任务列表、@寻址、成员预算）落成纯函数模块。

**必须实现**

- `deliver(message: dict, members: list[dict], tasks: list[dict] | None = None) -> dict`
  message 形如 `{"from","to":"@all"|"@role:reviewer"|"<member-id>","body","thread"}`；
  members 形如 `{"id","role","capabilities":[...],"depth":int,"spend":float,"budget":float,"alive":bool}`。
  返回 `{"delivered":[member_id...],"skipped":[{"id","reason"}],"denied":[...]}`
  规则：`alive=False` 或 `spend>=budget` 的成员跳过；`@all` 展开为全部存活成员；`@role:x` 按 role 匹配；
  直接点名但成员不存在 → 进 denied 并给原因；**发件人不在成员名单里 → 整条消息 denied**（防冒充）。
- `advance(tasks: list[dict], now: float, members: list[dict]) -> dict`
  任务形如 `{"id","assignee","status"("todo"|"doing"|"done"|"blocked"),"depends_on":[id...],"updated_at"}`。
  返回 `{"ready":[task_id...],"blocked":[{"id","reason"}],"complete":bool}`
  规则：依赖未完成的任务不能 ready；assignee 已死或超预算的任务进 blocked 并给原因；全部 done → complete=True。
- `budget_split(total: float, members: list[dict]) -> dict`
  返回 `{"allocation":{member_id: amount},"unallocated":amount}`，按成员 `weight`（缺省 1）分配，
  总额不能被超出；权重非法（非正数、非数字）的成员按权重 1 处理并在 warnings 里说明。

**自检至少 10 条**：@all 展开 / @role 匹配 / 点名不存在进 denied / 发件人非成员被拒 /
死成员跳过 / 超预算跳过 / 返回副本 / 依赖未完成不 ready / 死 assignee 进 blocked / 全 done 完成 /
预算不超总额 / 权重非法降级 / 空输入安全。

---

## 给 OpenCode —— `forge/contrib/compactor.py`

把上下文压缩策略落成纯函数模块（你这两轮跑挂过三次，这次只做这一件事，做完就输出）。

**必须实现**

- `should_compact(messages: list[dict], budget: dict | None = None) -> dict`
  messages 形如 `{"role","content"}`；budget 形如 `{"maxChars":24000,"maxMessages":200,"keepTail":6}`。
  返回 `{"compact":bool,"size":int,"reason":str}`。三种触发：总字符超 maxChars、消息数超 maxMessages、
  单条消息超 maxChars 的一半（单条过大也算）。
- `plan(messages: list[dict], budget: dict | None = None) -> dict`
  返回 `{"head":[...],"tail":[...],"pinned":[...],"dropped_chars":int,"summary_placeholder":str}`
  规则：尾部保留 keepTail 条；含有 `pinned=True` 的消息必须保留并单独放进 pinned（不受 keepTail 限制）；
  中间段进 head（待摘要）；`dropped_chars` 是 head 的总字符数；不改输入。
- `merge_summary(messages: list[dict], summary: str) -> list[dict]`
  返回新消息列表：首条替换为 `{"role":"user","content":"[compacted N messages] " + summary}`，其后接尾部原样。

**自检至少 10 条**：字符超限触发 / 消息数超限触发 / 单条过大触发 / 未超限不触发 /
尾部保留条数正确 / pinned 不受 keepTail 限制 / head 与 tail 不重叠 / dropped_chars 计算 /
不改输入 / merge_summary 结构正确 / 空输入安全。

---

## 给 Claude Code（MiMo 通道）—— 交叉审查（不写代码）

读 `forge/contrib/` 下四份已入库模块（`heartbeat.py` / `scheduler.py` / `replay.py` / `toolhost.py`）
与 `forge/registry.py`，输出中文审查报告：

1. 逐模块：契约遵守情况（是否真的纯函数、有无隐藏全局状态）、逻辑漏洞、边界处理缺口。
2. 四个模块之间是否有语义冲突（例如 scheduler 的 gate 与 compactor 的触发条件是否会互相抵消）。
3. `registry.py` 的一致性闸门本身有何绕过路径（静态扫描能骗过吗）。
4. 新加的 legacy-script 适配层是否降低了安全水位，如果是，具体在哪。
5. P0/P1/P2 修复清单。

直接指出问题，不要客套；无法确认的判断标注（推测）。
