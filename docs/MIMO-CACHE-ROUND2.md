# MiMo 第二轮：去重上下文与可选择的工具推理开销

本轮继续上一次真实 API 验证。目标是减少能够确认的请求浪费，并用同工作区的交叉试验判断思考开关的代价。没有修改本机凭据、默认模型或用户 provider 配置。

## 已接入的优化

### 不再重复注入不变的运行指引

`ConversationPrefix` 现在记录宿主最后应用的运行信封版本。连续对话只有在历史/权限/工具/路由等已有校验通过、指引版本相同、而且它仍实际存在于可信上下文时，才省略新一轮的重复消息。上一轮已经发送的前缀保持原样。

本轮使用正式 `DevelopmentRuntime.DEVELOPMENT_GUIDANCE` 的夹具，信封长度为 **649 字符**；第二轮不再追加这 649 字符。它是确定的上下文削减，不等于 649 token 或厂商命中率提升。

指引变化、A→B→A 切换、独立任务、没有宿主快照、旧快照缺少版本信息或压缩已经移除指引时，仍正常注入。客户端自己提交一条相同文字，不能冒充宿主去重依据。

### 正式 provider 配置支持推理默认值

新增 provider 配置字段 `reasoningMode`：

| 值 | 行为 |
| --- | --- |
| `auto`（默认） | 沿用原有行为；已识别 MiMo 2.6 的默认请求仍归一为 enabled |
| `on` | 未被当前请求覆盖时，发送 `thinking.type=enabled` |
| `off` | 未被当前请求覆盖时，发送 `thinking.type=disabled` |

当前只对已核实的 MiMo 2.6 家族及其显式模型别名绑定启用 on/off；其他模型明确报不支持。`thinking` / `enable_thinking` 等显式请求参数优先。报告里的 `execution.reasoning_default` 区分 provider 默认与请求覆盖。

链路为 `Config → ModelRouter.from_config → Provider → _plan_and_shape → HttpTransport`；Gateway 的 `provider_options` 使用同一规则。它与 Forge 的沉思扩展开关是不同概念。切换推理默认值保留可信工具历史；不同推理参数的本地温暖度仍分开记录，压缩事务也检测模式变化。

如需使用，在已有 provider 行的 `config` 中增加 `"reasoningMode": "off"`，保留该行其他字段；Forge 的配置层是整行替换。本轮没有据少量夹具结果自动改掉用户默认值。

### 冻结模型原始工具表面，补齐协议边界

- OpenAI 原生工具参数在与宿主验证值严格等价时保留原始 JSON 空白和转义；需要修复或实际值变化时仍重新编码。保留 reasoning_content，工具 id 仍按宿主配对要求修正。
- Anthropic 的工具 input 复制后回放，后续修改可变参数不会改写已投影消息。
- 厂商兼容钩子接收实际 wire，MiMo 的 OpenAI 使用字符串 `auto`，Anthropic 使用 `{"type":"auto"}`。此前不能根据厂商画像表中默认的 OpenAI wire 推断实际协议。两种协议均有直连/Gateway 离线请求体验证；本轮付费测试使用本机配置的 OpenAI Token Plan 端点。
- Anthropic 原生 auto 中明确请求的 `disable_parallel_tool_use` 布尔值保留；不会丢掉调用方的串行约束，也不把字符串 `"true"` 当作布尔值。

参数和推理字段约束参考 [MiMo 官方 Chat Completions 文档](https://mimo.mi.com/docs/api/text-generation/openai-api)、[Messages 协议说明](https://mimo.mi.com/docs/en-US/api/chat/anthropic-api) 与 [官方工具回放说明](https://mimo.mi.com/docs/en-US/quick-start/faq/api-integration)。本轮不通过丢弃推理历史或提高 token 填充量来追求命中数字。

## 同工作区 on/off/off/on 实测

新增 `verify_mimo_live.py --suite compare`。使用同一模型、端点、账户、工作区路径、工具面与输入文件，四个样本按 **on → off → off → on** 运行。每个样本完成两轮读文件、写 JSON、读回及宿主环境探针验收，推理开关经正式 Provider 配置生效，没有通过测试 transport 偷塞开关。

原始结果为 `reports/mimo-round2-pro-abba-retry.json`。四组初始消息摘要完全相同，工具摘要完全相同；除推理设置及其支持的采样设置外，初始输入相同。每个样本第一轮生成总量 42/alpha/beta，第二轮为 46/gamma/delta；**8/8 文件任务通过，24 次文件工具成功执行**。四组第二轮都确认复用前缀及不变指引。

| 样本 | 思考模式 | 任务 | 输入 token | 输出 token | 思考 token | 缓存 token / 输入 | HTTP 耗时 |
| --- | --- | --- | ---: | ---: | ---: | ---: | ---: |
| 1 | on | 2/2 | 15,910 | 588 | 332 | 11,520 / 15,910 | 47.468 秒 |
| 2 | off | 2/2 | 14,482 | 195 | 0 | 10,368 / 14,482 | 47.424 秒 |
| 3 | off | 2/2 | 14,598 | 232 | 0 | 10,112 / 14,598 | 32.798 秒 |
| 4 | on | 2/2 | 15,179 | 464 | 204 | 13,824 / 15,179 | 48.609 秒 |

| 汇总，每种模式 4 个任务 | on | off |
| --- | ---: | ---: |
| 输入 token | 31,089 | 29,080 |
| 缓存命中 token | 25,344 | 20,480 |
| 未缓存输入 token | 5,745 | 8,600 |
| 输出 token | 1,052 | 427 |
| 加权缓存命中率 | 81.52% | 70.43% |
| HTTP 耗时合计 | 96.077 秒 | 80.222 秒 |

关闭思考使这组任务输出 token 减少 **59.41%**，输入也较少，但其缓存命中较低、未缓存输入更多，不能仅凭输出下降认定总费用下降。两个 on 样本的命中率本身从 72.41% 到 91.07% 波动。本轮不把跨组差异等同于确定的模式因果，也不把当前结果与上一轮不同工作区/指引条件的 59.15% 直接相减宣称收益。

耗时包含网络失败尝试，第 4 组有一次 TLS EOF 后同模型重试，故不能将耗时比例当作稳定的端到端速度提升。只有两份简单 JSON 夹具，不代表复杂编码或推理的质量评测；保持原默认是基于本次混合结果的决定。

本轮先前一次无重试试验中途遇到 TLS EOF，完整保存为 `reports/mimo-round2-pro-abba.json`。随后允许每次模型请求最多重试一次，总上限 40 次，完成 32 次成功响应、1 次 TLS 失败。上述两次比较合计 **35 次尝试、33 次成功响应、2 次 TLS 失败**。成功回执合计输入 61,652、输出 1,506 token；失败请求实际服务端消费未知。未重放任何已经执行的工具动作。

另追加同一 Agent 的 **off→on 跨轮续接实测**，结果为 `reports/mimo-round2-mode-switch.json`：8/8 请求成功，两轮文件任务均通过，第二轮保留前一轮工具历史并省略重复指引，没有触发缺失推理字段的协议错误。本组命中 10,368 / 14,701 输入 token（70.53%）；它用于验证切换兼容性，不并入上表的模式性能比较。

## 验证与复现

专项覆盖配置穿透到 Agent 的最终 HTTP、请求覆盖默认值、模式切换保留原生工具历史、Gateway 一致性、模型别名、协议原生 auto、指引增量与修复参数回放。日志为 `reports/mimo-round2-targeted.log`、`reports/mimo-round2-core.log`、`reports/mimo-round2-runtime.log`、`reports/mimo-round2-selftest.log`。

专项 65 项通过（包含既有协议用例，不重复计数）。核心回归 441 项（2 跳过），客户端与运行链 206 项（1 跳过），合计 **644 项通过、3 项跳过、0 失败**；框架自检 **595/595**。代码预算 147 个文件、Git 空白检查通过。

```powershell
python -m unittest forge.test_mimo_adapter forge.test_conversation_prefix forge.test_vendor_wire -q
python -m unittest discover -s forge -t . -q
python -m forge.cli selftest
python scripts/budget_check.py

# 真实 API；最多 40 次尝试，消耗现有 Token Plan 额度。
python scripts/verify_mimo_live.py --live --suite compare --model mimo-v2.6-pro --retries 1 --request-limit 40 --output reports/mimo-abba-repeat.json
python scripts/verify_mimo_live.py --live --suite switch --model mimo-v2.6-pro --retries 1 --request-limit 10 --output reports/mimo-switch-repeat.json
```

本轮独立补丁：`reports/mimo-round2.patch`；文件与验证清单：`reports/mimo-round2-files.json`；开始时工作区快照：`reports/mimo-round2-backup.txt`。不包含上一轮已经存在的改动。
