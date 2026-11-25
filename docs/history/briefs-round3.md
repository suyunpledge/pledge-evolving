# 第三轮任务书 · forge v3 贡献模块（外部实体）

先读 [模块契约](../../contract.md) 第 1 节（冻结契约，6 条硬性禁令必须遵守）。已入库的四份（`heartbeat.py` / `scheduler.py` / `replay.py` / `toolhost.py`）可当形状参考。

---

## 给 阶跃龙虾（StepClaw）—— `forge/contrib/hooks.py`

把你对「钩子 + 同意白名单」的经验落成纯函数模块。核心是：外部脚本钩子首次使用必须显式授权，授权时记下脚本指纹，之后再跑要校验指纹有没有变。

**必须实现**

- `authorize_hook(hook: dict, approval: dict | None, current: dict) -> dict`
  hook 形如 `{"name","event","command","script_path"}`；
  approval 形如 `{"approved_at":float,"script_mtime_at_approval":float,"script_sha256":"...","approved_by":str}`；
  current 形如 `{"script_mtime":float,"script_sha256":"..."}`。
  返回 `{"decision":"allow"|"deny"|"ask","reason":str,"drift":None|"mtime"|"hash"}`。
  规则：无 approval → ask；有 approval 但脚本路径缺失 → deny；mtime 变了 → deny + drift="mtime"；
  hash 变了 → deny + drift="hash"；mtime 没变但 hash 变了也要按 hash 判；全部一致 → allow。
- `dispatch(event: str, payload: dict, hooks: list[dict], approvals: dict) -> dict`
  返回 `{"ran":[...],"skipped":[{"name","reason"}],"blocked":bool,"block_reason":str}`
  规则：只跑 event 匹配的钩子；任一钩子返回 `{"block": True}` 则 `blocked=True` 并**停止后续钩子**；
  返回 `{"block": False}` 不解除已有的阻断；未授权的钩子进 skipped 不进 ran。
- `describe(hooks: list[dict]) -> dict`
  返回 `{"by_event":{event:[name...]},"unapproved":[name...],"duplicates":[name...]}`。
  重名钩子只保留第一个执行、其余进 duplicates。

**自检至少 10 条**：无授权 ask / mtime 漂移 deny / hash 漂移 deny / 双漂移按 hash 判 / 路径缺失 deny /
授权一致 allow / 事件过滤 / block 中断后续 / block=False 不清除既有阻断 / 未授权进 skipped /
重名进 duplicates / 空输入安全。

---

## 给 Trae —— `forge/contrib/mcp_bridge.py`

把你对 toolhost/ 与 mcps/ 的经验向前推一层：不只归一工具描述，还要建模 MCP server 的生命周期与工具发现。

**必须实现**

- `plan_servers(configs: list[dict], running: list[dict] | None = None) -> dict`
  config 形如 `{"name","command"|"url","args":[...],"env":{...},"enabled":bool,"tools_cache":[...]}`；
  running 形如 `{"name","pid","started_at","status"("starting"|"ready"|"failed")}`。
  返回 `{"start":[name...],"stop":[name...],"keep":[name...],"warnings":[...]}`
  wait: 规则：enabled 且未运行 → start；未 enabled 但正在运行 → stop；enabled 且 ready → keep；
  status="failed" 的记进 warnings 并建议 start（允许重试）；重名配置只保留第一个、其余 warn。
- `discover(server: dict, raw_tools: list[dict]) -> dict`
  返回 `{"server":name,"tools":[{"name","description","schema","read_only","deferred"}],"dropped":[...]}`
  只读判定：`readOnlyHint` 优先，缺失时按名启发；名字非法丢弃；同名工具跨 server 时加前缀 `server/tool`。
- `health(running: list[dict], now: float, stale_after: float = 300.0) -> dict`
  返回 `{"ready":[...],"stale":[...],"failed":[...],"unknown":[...]}`；started_at 超 stale_after 仍未 ready → stale。

**自检至少 10 条**：enabled 未运行要 start / 未 enabled 在跑要 stop / ready 保持 / failed 进 warnings /
重名配置 warn / 只读启发 / 非法名丢弃 / 跨 server 前缀 / stale 判定 / failed 归类 / 空输入安全。

---

## 给 zcode —— `forge/contrib/router.py`

把「任务该派给谁」做成可验证的纯函数：按能力标签、成本档、权限档、健康状态与配额选 worker，并给出降级顺序。

**必须实现**

- `match(task: dict, workers: list[dict], limits: dict | None = None) -> dict`
  task 形如 `{"id","requires":["code-review"],"maxCost":"cheap","needsWrite":bool,"estTokens":int}`；
  worker 形如 `{"name","capabilities":[...],"cost"("free"|"cheap"|"standard"|"premium"),
  "permission"("read-only"|"workspace-write"|"full-access"),"healthy":bool,"spend":float,"budget":float}`；
  limits 形如 `{"globalBudget":float,"globalSpend":float}`。
  返回 `{"order":[worker_name...],"rejected":[{"name","reason"}],"constrained_by":[...]}`
  规则：不健康 / 配额耗尽 / 成本超 maxCost / 能力不全 / needsWrite 但权限只读 → 进 rejected 并给原因；
  通过者按 (成本档升序, 名字) 排序；**全局预算耗尽 → order 为空且 constrained_by 含 "globalBudget"**。
- `estimate(task: dict, worker: dict) -> dict`
  返回 `{"tokens":int,"costTier":str,"withinBudget":bool}`；estTokens 缺失按 2000 默认；预算为 0 或负 → withinBudget=False。
- `fallback_plan(order: list[str], workers: list[dict]) -> list[dict]`
  返回 `[{"attempt":1,"worker":name,"on":"retryable"},...]`，只在可重试类失败时前进；最后一项标注 `"on":"exhausted"`。

**自检至少 10 条**：能力过滤 / 成本上限过滤 / 只读拒绝写入任务 / 配额耗尽剔除 / 不健康剔除 /
排序稳定 / 全局预算耗尽 / estimate 默认值 / fallback 顺序 / fallback 末项标注 / 空输入安全。

---

## 给 PI agent —— `forge/contrib/catalog.py`

（首轮任务，仍未交付，重发一次）维护 provider/模型能力矩阵并据此构建降级链。

**必须实现**

- `catalog(providers: list[dict], observations: list[dict] | None = None) -> dict`
  provider 形如 `{"name","baseUrl","wire"("openai"|"anthropic"),"models":[...],"defaultModel","smallModel","apiKey"|无}`。
  返回 `{"providers":{name:{"wire","models","defaultModel","smallModel","hasKey"}},
  "models":{"provider/model":{"provider","id","wire","isSmall"}},
  "observations":{"provider/model":{"attempts","failures","lastErrorClass"}},"warnings":[...]}`
  hasKey 只判「是否提供了密钥字段」，**绝不能回显密钥内容**；warnings 指出：无 models、defaultModel 不在 models、
  wire 非 openai/anthropic。
- `build_chain(providers: list[dict], primary: tuple[str, str] | None, error_class: str | None,
  exclude: list[str] | None = None) -> list[tuple[str, str]]`
  primary 排第一；error_class 属不可重试类（"bad_request"/"invalid"/"400"）→ 只返回 primary；
  属可重试类（"rate_limit"/"overloaded"/"timeout"/"connection"）→ 补齐整链；error_class 为 None → 完整链；
  exclude 跳过；同一 (provider, model) 不重复。

**自检至少 10 条**：目录构建 / hasKey 不含密钥内容 / smallModel 标记 / 多 provider 同名模型 /
缺 models 告警 / defaultModel 不在 models 告警 / 非法 wire 告警 / observations 累计失败 /
可重试补齐链 / 不可重试不补齐 / exclude 生效 / 链内无重复。
