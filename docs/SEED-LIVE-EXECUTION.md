# 火山 Seed 实测

2026-11-08，使用本机已配置的火山 Coding Plan 密钥及 `doubao-seed-evolving`，端点为 `https://ark.cn-beijing.volces.com/api/coding/v3`。只发送隔离夹具，密钥、鉴权头和加密原文不写入报告。

## 已通过的实际能力

`reports/seed-execution-auto.json` 整组通过：

测试显式采用 6000 字符上下文预算和预置历史，以强制覆盖压缩；这不是默认生产上下文长度，也不是一般任务缓存率基准。

- 简要规划三次实际输出 272 / 233 / 217 token，未截断；方案作为任务指令进入执行。
- 三轮均实际调用 read_file → write_file → read_file，独立 json_value 环境探针验证 total、version、turn。
- 每轮真实压缩一次；第 2、3 轮重建 Agent 后，从磁盘会话恢复并记录 cache_prefix_reused。
- 禁止写入时只调用一次 write_file，返回 deny 后停止；目标文件不存在，没有重复尝试或权限绕过。
- 18 次成功响应都携带 encrypted_content。后续成功请求共记录 29 个与先前响应完全相同的加密字段回放匹配；同一字段在不同请求中可多次计入，29 不代表 29 次独立请求。

`reports/seed-evidence.json` 独立核对报告哈希及三个实际写入内容的 SHA-256，与环境探针读取的文件哈希完全一致。现有 Forge 适配已支持本次实际链路，本轮没有改动生产适配或用户权限配置。

## 默认模式的 usage

三轮任务加一个拒绝写入场景，共 22 次 HTTP 尝试、18 条成功 usage；4 次 URLError 未返回 usage，未纳入 token 分母，也不推断其账单。

| 输入 token | 输出 token（含推理） | 总 token | 推理 token | 缓存命中 / 输入 |
|---:|---:|---:|---:|---:|
| 34,457 | 1,644 | 36,101 | 515 | 1,848 / 34,457 = 5.36% |

唯一一次正命中来自第二轮最后请求，为 1,848 / 2,010 输入 token（91.94%）；整体命中率包含规划、冷请求及压缩重建，不能拿这一单次值代表整组。三次压缩后的首次执行请求均报零命中。单次正命中、历史本地前缀续接和服务端实际命中是不同观测。

本轮未改缓存策略。不能仅凭前缀复用事件推断服务端一定命中，也不根据这个样本给 Seed 套用其他模型的折扣。

## 失败记录与验证范围

`reports/seed-execution-off.json` 是关闭思考的首次尝试：成功读取并写入后，连续两次 TLS EOF 使回读未完成，因此整组失败；该组的权限拒绝场景通过。未把已生成文件算作完成验收。

`reports/seed-execution-off-retry.json` 将每条请求最多重试次数显式增至两次：前两轮通过，第三轮再次在写入后因三次连接失败而停止，拒绝写入场景通过。整组仍标记失败。该组 25 次尝试、16 条成功 usage，输入 29,481、输出 974、总 token 30,455；缓存 7,126 / 29,481 = 24.17%，reasoning 为 0。该数值包含先前测试留下的热前缀，而且缺少第三轮的完整验收，不能与默认模式直接计算节省比例。

四组报告合计 62 次 HTTP 尝试，44 条成功 usage，观测输入 76,149、输出 3,145、合计 79,294 token。18 次失败均无 usage，其费用未知。本轮没有继续增加重试或修改全局网络配置。

`reports/seed-wire.json` 的关闭思考部分完成两次合法 add 调用及结果 42 回放；开启思考的首条工具调用也成功，但后续请求发生传输失败，整个协议探针未通过。该版本探针只有整批返回才保存 cases，故 cases 为空；原始逐请求记录仍在，不能据此声称开启思考协议探针全部通过。默认模式的完整任务组另行验证了带加密字段的实际回放。

为避免这类观测丢失，新脚本按思考开关逐组保留结果，后面的网络失败不会抹掉已完成子项。新增离线测试覆盖这一点及密钥与官方端点绑定，合计 53 项定向测试通过。脚本语法检查、Forge 预算检查通过（156 文件、48,515 行）。

思考及加密字段的协议背景可参阅[火山方舟深度思考文档](https://docs.volcengine.com/docs/ark/deep-thinking)。本报告的数值与通过结论来自本机实际回执，不依赖文档中的缓存或价格描述。

本轮新增脚本、测试及本文的增量补丁见 `reports/seed-live.patch`，文件哈希清单见 `reports/seed-live-files.json`。

## 复跑

```powershell
python scripts/verify_seed_execution.py --live --scenario execution --output reports/seed-new-auto.json
python scripts/verify_seed_execution.py --live --scenario execution --thinking off --retries 2 --request-limit 48 --output reports/seed-new-off.json
python scripts/verify_seed_execution.py --live --scenario wire --output reports/seed-new-wire.json
```

`--live` 必填，输出文件不能已存在；默认每条请求最多重试一次，可显式选择两次，并受总请求次数上限约束。只使用已启用且模型、官方 Coding Plan 地址均匹配的配置，不更换服务端点。脚本复用已有隔离任务和 usage 记录器。
