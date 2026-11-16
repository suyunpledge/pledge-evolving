# 模型种类指纹与适配

模型身份由 `manufacturer_id → model_family_id → model_kind_id` 表达。供应商配置行的 ID、网关名称和域名不再充当模型种类。适配器按模型种类 ID 分派，同族复用实现，型号卡决定差异参数。

- 运行表：`forge/model_kinds.py`；配置解析：`forge/vendors.py`。
- 适配器：`forge/adapters.py`；请求接线：`forge/model.py`、`forge/gateway.py`。
- 离线指纹清单：`docs/vendor-adapt-2026-11-04/model-fingerprints.json`。
- 可重复导出：`python scripts/model_fingerprints.py --output docs/vendor-adapt-2026-11-04/model-fingerprints.json`。

## 识别优先级

显式 `modelKindId` / `modelKinds` 绑定 → 模型 ID 与已登记别名 → 已知家族前缀 → 厂家前缀默认值 → 旧 `vendor` 字段 → 端点域名 → 保守通用适配。`ollama` / `llamacpp` / `mnn` 服务始终保留本地适配与计费策略。

模型匹配忽略大小写，并识别 `厂家/型号` 命名空间。匹配过程不修改上游请求的 `model` 字段。未知新版本不冒充已实测型号；只能确定厂家时，`model_kind_id` 留空，适配器使用 `<manufacturer>.default`。家族前缀匹配标记 `family_inherited:*`。

## 当前覆盖与证据

登记 12 家厂商、29 个种类，关联 76 条已有调用回执。下表描述证据范围；没有凭据或未获调用授权的厂家只保留配置级默认值。

| 厂家 | 已登记种类 | 差异与证据范围 |
| --- | --- | --- |
| DeepSeek | V4 Flash / V4 Pro / V4.1 Flash | V4 两款工具回放、thinking 开关与 high 请求已实测；V4.1 来自产品轨迹 |
| MiniMax | M3 / M2.7 / M2.7 Highspeed | M3 与 Highspeed 的思考方言、effort 请求和工具回放已实测；普通 M2.7 为注册信息 |
| MiMo | V2.6 Flash / Pro / Pro Ultraspeed | Flash、Pro 已实测；Ultraspeed 在 token-plan 端点被拒，保留失败且不全局封禁 |
| 豆包 | Seed Evolving / Seed 2.1 Turbo | 两款工具回放和 thinking 请求已实测；Evolving 额外助手字段按原协议保留 |
| 混元 | HY3 / HY4 / HY4 Free / HY4 Standard | HY4 Free 经 WorkBuddy 实测：high、流式工具回放、credit=0；其余是注册信息/轨迹 |
| 智谱 | GLM 5.3 / 5.3 Flash | 5.3 使用已有探针，Flash 为配置默认；本轮没有调用 GLM |
| 千问 | Qwen 3.8 Max / Flash | 标记订阅不可用，未进行新调用 |
| Kimi | K3 / K3.1 Pro / K2.8 / K2.6 | 本机产品注册信息或配置，不记为新实测 |
| OpenAI | GPT 6 Sol / GPT 6.1 Sol | 现有配置与采样策略，未做新实测 |
| Anthropic | Claude Sonnet 5 / Opus 5.5 | 现有配置与协议策略，未做新实测 |
| Google | Gemini 3.8 Flash | 本机客户端信息，未做新实测 |
| 阶跃 | Step 5 Preview | 产品轨迹，未做新实测 |

指纹记录可操作特征：思考参数方言、effort 档位、采样约束、输出预算、压缩地板、循环提示、长期记忆预算、缓存模式和工具回放证据。不根据一次回答推断“人格”或质量排名。

MiniMax M3 的 `steps_hint=24`、记忆预算 `1000`、最低分 `0.30` 只限定到 `minimax.m3`。M2.7 和未知 MiniMax 型号保留默认 `steps_hint=0`、预算 `1500`、最低分 `0`，避免把 M3 的走法套给所有型号。显式用户预算继续优先。

WorkBuddy 的流式要求、tool_choice 限制与零积分观察带通道标记，不推广为所有混元原生端点的承诺。MiniMax medium 探针的错误答案、MiMo Ultraspeed 拒绝和 WorkBuddy 被拒请求均保留在清单；单条成功或失败不自动改写运行规则。

## 网关不透明模型名

正常的模型 ID 自动识别。仅当网关使用 `deployment-blue` 这种不透明名字时，在 provider 的 `config` 中绑定：

```json
{
  "model": "deployment-blue",
  "modelKindId": "minimax.m3",
  "modelKinds": {
    "deployment-green": "deepseek.v4.pro",
    "deployment-fast": "minimax.m2.7.highspeed"
  }
}
```

`modelKindId` 只作用于该行默认模型；`modelKinds` 按本次实际模型逐一匹配，并优先于默认绑定。切换到其他型号时不会沿用默认模型的种类。网关先完成 `model_map`，再按实际上游 ID 查绑定。多模型网关推荐使用 `modelKinds`。

HTTP 请求、网关与 CLI 首发模型的预算解析共用同一配置。计划日志包含 `adapter_id`、`manufacturer_id`、`model_family_id`、`model_kind_id`、`model_match_source`、`model_evidence` 和 `behavior_fingerprint`；关闭整形后仍报告身份。`forge vendors --detect` 显示默认模型的种类、家族和指纹。

## 指纹含义

`behavior_fingerprint` 是生效能力配置的稳定 SHA-256 摘要，排除供应商行名称、账号凭据和回答措辞。别名切换或供应商改名不会改变同一能力配置的指纹；型号或有效能力改变会改变指纹。它用于追踪适配配置，不能证明远程模型权重或厂商真实性。

能力指纹与缓存前缀的 `fingerprint` 分开：后者继续按实际模型 ID、端点、账号、协议及思考参数隔离。相同能力指纹不会导致不同端点/账号共享缓存温暖度。短探针有无命中也不等于长会话稳态命中率。

离线导出器只读已有回执的白名单字段，保留通道、参数、usage、完成原因和场景成败；不读凭据，不导出消息正文、思考正文和认证头，不发新请求，也不自动学习或覆盖规则。

## 验证

- 新增模型身份回归 20 项：别名/网关绑定、型号切换、采样约束、缓存隔离、本地引擎、失败证据保留。
- 内核 unittest 292 项通过（含 1 项跳过）；selftest 595/595。
- adapters 68/68、vendors 82/82、reasoning 73/73；GUI 记忆相关 unittest 15 项通过。
- 指纹导出内容与再次离线生成逐项相同；预算清单与差异空白检查通过。

本轮使用既有回执，没有新增模型 API 调用。12 家登记覆盖中，6 家有现存调用证据，其余明确保留注册/轨迹/配置等级。
