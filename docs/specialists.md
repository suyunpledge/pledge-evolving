# 主控 + 专家模型（brain + specialists）

状态：**默认关闭（opt-in）**。实测数据见 [specialists-ab.md](specialists-ab.md)。

主控模型（brain，即主代理所用的模型）负责规划和决策，并保留权限裁决权。专家是按领域配置的子代理，
各有自己的模型和收窄后的工具面。路由的单位是**整段子任务**，从不路由单个工具调用。

## 阶段 1：检索目录 + 按需展开

- `retrieval` 专家是一个子代理，只拿到检索工具：`web_search`、`fetch_url`、`grep`、`find_files`、
  `read_file`、`read_range`、`memory_recall`、`file_stats`，外加只读的 `list_dir`、`file_outline`。
- 专家每次工具调用成功，结果都由 `EvidenceLedger`（`revon/retrieval_catalog.py`）存进现有的
  `ArtifactStore`。这个存储在工作区之外，按内容寻址并校验摘要。结果开头会打上 `[evidence eN · 来源]`
  标记，供专家引用。
- 专家最后只输出 JSON 目录：`evidence` / `title` / `source` / `lines` / `summary` / `relevance`。
  宿主把每条绑定到对应的 artifact，生成稳定 id（`c_` 加 10 位十六进制，由 artifact、来源和标题哈希得到），
  并记下全文字符数。
- 主控只看到目录（id、标题、来源、摘要、相关度、全文大小），看不到全文。它调用
  `catalog_expand(ids, offset?, limit?)` 只读需要的条目。默认从被引用的那几行开始，并带上
  `[id] 标题 — 来源: path:行 · 字符 a–b / N`，方便注明出处。
- 如果专家没按格式回答，就退回到"每条证据一条目录"。如果引用的来源没被检索过，条目标记为"无全文（仅摘要）"。
- 目录索引 `catalog-index.json` 和 artifact 放在一起，最多保留最新 512 条。GUI 每轮重建 Agent 之后，
  仍能展开上一轮的条目。

## 阶段 2：领域 → 专家路由

配置行 `specialists`（`revon/bundles/base.json` 已声明，默认 `enabled: false`）：

```json
{"id": "specialists", "name": "specialists:routing", "config": {
  "enabled": true,
  "autoRoute": false,
  "domains": {
    "retrieval": {"model": ["<provider-id>", "deepseek-flash"], "maxSteps": 12},
    "coding":    {"model": ["<provider-id>", "deepseek-flash"]},
    "computer":  {"model": null},
    "math":      {"model": null}, "model3d": {"model": null}, "video": {"model": null}
  }}}
```

- 每个领域可以设置 `model`、`tools`（覆盖默认工具面）、`maxSteps`、`mode`、`output`
  （`catalog` 或 `envelope`）、`label`、`brief`、`keywords`。也可以新增自定义领域，例如 `legal`。
- `model` 为空表示该领域**未配置**。这时 `spawn_subagent(domain=…)` 照旧派普通子代理（记一条
  `specialist_fallback` 事件），或由主控自己做，行为与今天相同。模型解析失败（provider 或模型已不在配置里）也走同样的回退。
  模型只通过 `phase_router` 解析，不会悄悄换成别的厂商。
- `delegation`：
  - `advise`（默认）：只告诉主控“可以委派”。
  - `prefer`：要求属于已配置领域的整段子任务默认委派。
  - `require`：在结构上强制委派。已配置领域拥有的工具从主控的工具面移除，只有专家持有。
    主控保留 `spawn_subagent`、`catalog_expand`、`tool_search`、`skill_list`、`datetime`、`calculator`、
    `artifact_read/grep`（`BRAIN_KEEPS`），以及不属于任何已配置领域的工具。专家从完整注册表里裁出自己的工具面，
    普通子代理继承主控收窄后的工具面。
  - `advise` 和 `prefer` 只是写在请求尾部的指引。实测中 DeepSeek 主控在这两档下都不委派，所以只有 `require` 才真正走了专家路由（见 A/B 报告）。
- `autoRoute: true` 时，如果主控没写 `domain`，就按关键词投票选一个已配置的领域；平票或零票不路由。
- 预留了 `math`、`model3d`（工具 `model3d_build`/`model3d_inspect`，见 `revon/model3d*`）和 `video`
  （暂时没有视频工具）的默认工具面。只要配上 `model` 就能启用。

### 交接信封（handoff envelope）

`spawn_subagent` 新增了几个可选参数：`domain`、`constraints`、`prior`、`acceptance`。
- 发给专家：`目标 / 约束 / 已知结论 / 验收标准`，外加按领域输出格式的要求。
- 专家返回：`{"result", "evidence", "open_questions"}`，宿主解析后渲染成"专家回执"。专家改过的文件会自动补进证据。
  解析失败时整段文本当作 result，`complied=false`。结构化回执也写在工具结果的 `meta.handoff` 里。

### 权限与预算

- 专家的策略是 `parent.child(mode)`：模式被夹到主控的上限，allow/ask/deny 原样继承，所以主控的 deny 在专家里仍然有效。
- 工具面是从主控注册表克隆后物理删减的，`tool_search` 无法再扩大。专家拿不到 `spawn_subagent`，也只有根代理会路由。
- 步数取 `min(主控, 领域 maxSteps)`。spawn 预算是整棵树共享的同一个计数器。专家的 token 计入主控这一轮
  （`_child_tokens` 和成本账本）。专家的事件折叠进主控的事件流，并照常过滤注入内容。
- 事件：`specialist_route`、`specialist_catalog`、`specialist_receipt`、`specialist_fallback`、`specialist_error`。

## 测试与实测

- `revon/test_specialists.py`：目录与展开、路由表校验、交接信封、权限夹紧（deny 生效、PLAN 模式下不能写）、步数上限、
  预算共享、未配置时回退、功能关闭时行为与今天一致。
- `scripts/verify_specialists_ab.py`：真实模型 A/B（`--self-check` 只校验题目和判分器，不调用模型）。
