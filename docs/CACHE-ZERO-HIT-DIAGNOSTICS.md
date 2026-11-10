# MiMo / Seed 零命中排查与实际续接优化

2026-11-08。修复了宿主裁剪显示历史后，重建 Agent 丢失私有原生上下文的实际缺口；接入已验证 Seed 端点的缓存路由提示；补齐真实 HTTP 前缀、推理与加密字段的审计。所有 API 使用现有账户和隔离合成夹具。

## 零命中可以确认到哪一步

### 同一字节请求确实能得到不同缓存回执

`reports/cache-diagnostics-seed-exact-wire.json` 预设发送 4 次完全相同请求，其中 2 次成功、2 次传输失败。两次成功的输入均为 1,590 token：第一次 cached_tokens=1,590，另一次为 0。

这两次的完整 HTTP 请求体 SHA-256、模型、工具设置、生成参数及 prompt_cache_key 全部相同；复核见 `reports/cache-diagnostics-evidence.json` 的 identical_wire_mixed_cache_receipts。请求不含历史 assistant 消息，所以这组差异也不能归因为推理字段回放。传输失败未返回 usage，未按零 token 或零缓存计入。

这已排除这组请求的客户端输入变化。仅凭客户端回执仍无法进一步区分缓存未写入、淘汰、调度或其他后端状态；没有把“字节相同”冒称为“服务端必定命中”。方舟也说明隐式缓存受长度、可用缓存及路由等因素影响，路由标识是尽力而为的亲和提示。[方舟缓存文档](https://docs.volcengine.com/docs/ark/context-cache?lang=en)

### 短请求和未知统计不能当作缓存损坏

此前 Seed 短任务首个输入约 950 token，本轮更短的控制输入为 58 token，原始代码夹具为 934 token。方舟文档给出的默认缓存门槛为 1,024，部分模型不同；evolving 订阅别名的底层映射没有由本次回执公开，因此门槛只能作为解释线索，不能套用成这个别名的已验证常量。

较长控制输入使用同一合成账单项目的修复前后源码及测试，为 1,590 token；已经观察到完整命中，因此该端点并非始终不能缓存。没有向生产提示加入填充文本来追求命中率。

MiMo 的短控制输入仅 36 token，部分响应的 prompt_tokens_details 为 null，这表示缓存统计未知。没有查到其公开的同等最小门槛依据，不套用 Seed 的门槛。MiMo 817-token 完全相同输入的后续响应可命中 768 token。

### 保留的推理/加密字段没有发现改写或缺失

诊断记录真实请求体摘要、逐消息/逐字段摘要、首个变化位置、工具声明及生成参数摘要；opaque 与 reasoning 原文不写入报告。字段值与此前响应比对，并对最新探针按工具调用 ID 绑定检查。审计还根据已返回的调用组，核对应有的非空字段是否缺失。

全部实验共核对 155 个出站 reasoning_content、81 个 encrypted_content 值，19 个按调用绑定的字段值，以及 186 个应有字段位置，未发现不匹配或缺失。计数包含重试及同一字段多次回放，不代表独立成功请求数量。末轮 Seed 请求在传输失败前完成出站检查，不能把它们算作成功送达；此前成功的两轮 Seed 工具任务另有回执。

MiMo 官方建议保留历史 reasoning_content，与当前实现一致。[MiMo OpenAI 接口文档](https://mimo.mi.com/docs/zh-CN/api/chat/openai-api-post)

压缩确实会改写固定头部之后的历史前缀，造成重建成本；这是预算回收的结果。保留的原生工具组、字段和配对继续完整，后续正常追加能复用新的前缀。不能把“压缩移走旧交换”和“保留交换的推理字段被损坏”混为一谈。

## 实际修复

### 显示历史裁剪后同步更新磁盘快照

编辑器在显示历史超过 200 条时调用 trim_history。旧实现只改内存中的 history_digest，磁盘仍是裁剪前摘要；新建 Agent 从磁盘加载后无法匹配裁剪后的真实用户历史，退回普通聊天布局，失去原生调用和观测。

现在裁剪和完整提交共用原子落盘路径。仍只承认宿主已保存历史的精确后缀；编辑、伪造或 guard 不匹配不会获得续接。工作区、规则、权限及凭据校验保留。

API 对照把显示窗口缩到最近一轮，触发相同裁剪路径，再重建 Agent。生产的 200 条上限没有改变。两组都需要实际读取 values.json、调用 calculator 得到 423；每轮必须重新调用 calculator。压缩预算 5,000 字符，模型及思考设置相同。

| 对照 | 任务结果 | 实际读文件 | 有效 usage 响应 | 输入 | 输出含推理 | 总 token | 已观测缓存率 |
|---|---|---:|---:|---:|---:|---:|---:|
| MiMo 旧实现 | 两轮通过 | 2 | 6 | 5,718 | 290 | 6,008 | 83.84% |
| MiMo 修复 | 两轮通过 | 1 | 5 | 4,577 | 177 | 4,754 | 86.69% |
| Seed 旧实现 | 两轮通过 | 2 | 6 | 7,292 | 390 | 7,682 | 0% |
| Seed 修复 | 两轮通过 | 1 | 5 | 6,719 | 315 | 7,034 | 0% |

MiMo 观测总 token 下降 20.87%，Seed 下降 8.44%。两家修复组都在第二轮实际恢复私有前缀，省去一次重读及一次模型往返。MiMo 缓存分母为旧组 4,351、修复组 4,577，缓存分别 3,648 / 3,968；旧组两条成功响应缺少缓存统计，修复组没有，因此不把这 2.85 个百分点当作一般收益。修复组另有 MiMo 1 次、Seed 2 次无 usage 的传输失败，其消费未知。

报告为 `cache-diagnostics-mimo-trim-final-ab.json`、`cache-diagnostics-seed-trim-ab.json`。MiMo 首次裁剪探针的基线只读文件后直接回答，未按要求再次计算，被验收拒绝；原始失败报告 `cache-diagnostics-mimo-trim-ab.json` 保留。随后将每轮新调用要求明确给两组，再进行上述有效对照。

### Seed 路由标识接入真实 Agent 请求

在 Agent → ModelRouter → HttpTransport → 适配整形链路中增加宿主 cache_scope，消费后不把内部 scope 字段发给模型。仅对已实测的官方 Ark Coding Plan `/api/coding/v3`、OpenAI wire、`doubao-seed-evolving` 自动生成 prompt_cache_key。

标识对宿主会话范围、账户、模型、实际推理参数及工具声明作哈希；不包含明文密钥或项目路径。新 Agent 和压缩续接保持稳定。它也参与本地缓存命名空间，避免不同提示标识共享错误的热度观测。已有调用方提供的标识保留。

Provider 的 cacheAffinity 支持 auto / off，默认 auto。可在现有 provider 行 config 中增加 `cacheAffinity: off`，同时保留该行其他配置。off 只关闭自动路由提示，不关闭服务端隐式缓存。其他模型、MiMo、未验证端点和禁用适配的请求不自动注入。

端点实际接受此字段；生产链路两轮探针的 5 个请求保持相同标识，HTTP 上的标识摘要与执行计划一致。但接受字段不证明后台必定采用，也不保证命中：auto/off 的三轮 ABBA 中两个 off 组完成，两个 auto 组因 TLS 中断未完成；关闭思考的补充 AB 中 off 组首请求失败、auto 组完成两轮，缓存均为零。没有据这些不完整对照宣称路由标识带来稳定收益。

### 最终多轮与压缩续接

`cache-diagnostics-mimo-final-replay.json` 三轮全部通过，只读文件一次，每轮实际调用 calculator；后两轮从磁盘恢复私有前缀，期间发生真实压缩，保留字段按调用 ID 匹配。7 次成功请求，输入 7,473、输出 215、合计 7,688；6 次有缓存统计，缓存 5,760 / 6,739 = 85.47%，另一次统计未知。这是最后一组实测值，不与不同任务提示的早期探针计算因果提升率。

`cache-diagnostics-seed-final-replay.json` 在实际读取后发生连续传输失败，整组失败；出站保留字段与原调用绑定正确，但这组未完成三轮。没有为了通过继续提高生产重试次数或全局关闭思考。Seed 成功的两轮实际任务以裁剪对照及路由标识补充组为准。

## 证据及验证

13 组 API 实验共 116 次 HTTP 尝试、90 条有效 usage：观测输入 102,180、输出 10,228，合计 112,408 token。26 次失败没有 usage，费用未知；另有 8 条成功响应缺少缓存统计。全量报告含所有失败和零命中，没有筛掉不利样本。

`reports/cache-diagnostics-audit.py` 独立核对原始缓存字段、归一化统计、请求总量、回放来源、应有字段及完全相同请求的不同缓存回执，结果为 `cache-diagnostics-evidence.json`。两家真实工具请求中观察到的读取目标均为合成 values.json。报告只保留字段摘要，实验临时目录会清理，不将归档摘要冒称为可回读的原始上下文。

核心回归 497 项通过（2 项跳过），定向及客户端/网关入口回归 157 项通过；新增回放字段核对又以 7 项小范围回归确认。集合有交集，不累加覆盖数。预算检查通过：158 文件、48,677 行。

```powershell
$baseline = Get-Content reports/cache-diagnostics-backup.txt
python scripts/verify_cache_diagnostics.py --live --vendor mimo --scenario trim-compare --turns 2 --baseline-dir $baseline --output reports/trim-new.json
python scripts/verify_cache_diagnostics.py --live --vendor seed --scenario exact-repeat --affinity --thinking off --extended-repository --output reports/exact-new.json
python scripts/verify_cache_diagnostics.py --live --vendor seed --scenario agent-compare --output reports/affinity-new.json
```

`--live` 必填，输出路径必须不存在。单组默认请求上限 30、硬上限 40；真实 Agent 每条请求最多重试一次，直接重复控制不自动重试。控制组关闭思考明确标记，不与保留现有思考设置的任务计算节省率。扩展源码来自已有合成账单修复导出，不读取业务项目。

本轮增量补丁：`reports/cache-diagnostics.patch`；源文件与证据哈希：`reports/cache-diagnostics-files.json`。以本轮开始前快照为基线，保留此前所有修改。
