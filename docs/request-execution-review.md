# 请求计划执行与真实厂商验证

日期：2026-10-07。仅修改源码、测试和本报告；未生成 EXE 或打包副本。

## 本轮边界

保留既有 Provider、Transport、Completion、SmartRouter 与 Gateway 接口。
普通聊天仍同步/流式执行；Batch 是显式的异步任务，不把聊天偷偷放进 24 小时队列。
缓存状态是上游 usage 支持的温暖度线索，不能保证未来命中，也不能跨模型共享真正的厂商缓存。
本轮没有改造插件系统或 UI 风格。

## 修复矩阵

| 故障/场景 | 修复前行为 | 风险 | 本轮修复 | 验证 |
| --- | --- | --- | --- | --- |
| defer 只有建议 | 高峰建议出现在 plan，发送时间不变 | 报告与执行不同 | 显式 opt-in、最长等待限制、可取消等待；交互请求拒绝 defer；等待后重新规划 | 高峰边界、超等待上限、取消、交互拒绝 |
| Batch 只有折扣建议 | 普通请求不提交 Batch | 误算异步折扣 | 新增小型 BatchExecutor 与 `forge batch submit/status/results/cancel`；上传 JSONL、提交、查询、下载结果、取消；保存不含密钥的任务回执 | 本地模拟完整生命周期、重开执行器、网络结果不确定时不自动重复 POST |
| Flex 未写入请求 | 只在估算中使用折扣 | 未执行却声称省钱 | 支持的 OpenAI 兼容请求设置 service_tier=flex；不支持的厂商/协议提前拒绝；按返回的 service_tier 标记 confirmed/unconfirmed | 实际 payload 断言、厂商/协议边界；没有调用未授权的 OpenAI 付费模型 |
| 思考预算只是数值 | plan 的 reasoning_budget 容易被理解成已生效 | 超预算或发送非法参数 | 显式 thinkBudget：千问使用 thinking_budget；DeepSeek 使用总输出 max_tokens 上限并标记 total_output；不支持的端点标记 unsupported；显式 effort/thinking 保留并标记 override | 参数到线、payload 不被原地修改、冲突处理 |
| premium/MoA 合并不完整 | 只保留最后请求的 plan；MoA 候选 usage 也可能丢失 | 账本漏记或模型归属错误 | Completion.requests 保留每次成功调用的模型、usage、plan；premium/MoA 汇总实际 usage；循环报告携带请求回执；账本按实际模型分别记 token | 两阶段不同模型、不同 plan、MoA 三次调用统计；核心回归 |
| 把窗口估价当完整账单 | plan.est_cost 是复用窗口的输入成本 | 重复求和，遗漏输出成本 | 标注 estimate_scope、includes_output_cost=false、invoice_verified=false；保留每阶段估价，不产生“完整账单”总额 | 回执与多阶段 scope 断言 |
| CacheWarmth 跨实例/进程不同步 | 只在启动读取；实例保存可覆盖其他写入 | 丢记录、旧状态复活 | JSON 格式保持兼容；线程锁 + 有限等待的 OS 文件锁；每次读写重新加载；变更原子写入；save 不写回旧快照 | 两实例互见、撤销不复活、三个独立 Python 进程并发写 90 条记录 |
| 自动缓存无 telemetry 也被当成已确认 | 成功响应就记为热；按默认模型选择厂商 | 假命中、聚合器厂商错配 | 必须看到 cached/cache_write；按本次 plan 的实际厂商和模型记录；指纹隔离端点、账号、模型、协议、缓存模式和稳定前缀 | 无 usage 不确认、实际厂商、命名空间回归 |
| GUI 直连 Gateway 绕过规划器 | Gateway 原样转发；没有缓存 usage 接线 | GUI 与 CLI 行为不同 | 普通和协议转换路径在别名映射后使用共用规划器；GUI 传递非密钥适配设置；采集 SSE usage 并记录 ADAPT 审计 | 本地真实 HTTP 转发、协议转换、真实 DeepSeek/MiMo Gateway 流式调用 |
| 流式内容等待缓冲区填满 | response.read(4096) 可能延迟小事件 | UI 迟迟不显示 | 使用 read1 尽早转发；有界 usage observer；流开始后的错误不再二次发送 HTTP 头 | 上游尚未结束时首事件可读；分片、超大、畸形 usage 事件 |
| GUI 工具循环丢失 reasoning_content | 后续 assistant/tool 回放不带模型要求的字段 | 厂商拒绝或上下文质量下降 | 流式与非流式结果保留字段；工具回放和会话保存恢复保留；不拼入普通正文 | 客户端流式、usage、回放字段回归 |

## 如何使用已落实的执行能力

Provider 的既有 config 可增加 `thinkBudget`、`flex`、`defer`、`maxDeferSeconds`。
默认没有自动错峰或异步 Batch。defer 必须明确设置最长允许等待，GUI 交互请求拒绝错峰等待。
`reasoning_effort` 或 `thinking` 已显式选择时，预算配置不覆盖它；计划会说明 override。
MiMo 等未接入独立思考预算的端点会标记 unsupported，不伪装成硬上限。

Batch 输入为 JSONL，每行包含 `custom_id` 和完整请求 `body`（包括实际 model、messages）。
当前执行器支持 OpenAI/百炼的 OpenAI 兼容 Batch 接口，限制 100 个请求、1 MiB 提交和 8 MiB 结果。
返回的 tool_calls 只是数据，不会由 BatchExecutor 自动执行工具。

```text
python run.py batch submit --provider <已有配置行ID> --input <JSONL路径>
python run.py batch status --provider <同一配置行ID> --job <返回的任务ID>
python run.py batch results --provider <同一配置行ID> --job <返回的任务ID>
python run.py batch cancel --provider <同一配置行ID> --job <返回的任务ID>
```

任务元数据保存在指定 Forge home 下的 `batch-jobs/`，没有 API key 或输入对话正文。
若提交结果因网络失败不确定，保留上传文件回执供恢复；不会自动再提交一个可能重复计费的任务。

## 真实联网结果

授权上限：每个模型 5,000,000 tokens。本次每次完整探测采用更小的 100,000 tokens 保守预约上限。
只向现有配置中的 `deepseek-flash`、`qwen3.8-flash`、`mimo-v2.6-flash` 发送探测请求；未修改密钥。
输入是明确标记的合成测试记录，未使用仓库正文或用户对话，也未写入 Forge 会话历史。

| 模型 | 实际验证 | 这轮全部成功请求返回的 tokens | 最后一次真实 Gateway 流式请求 |
| --- | --- | ---: | --- |
| deepseek-flash | 直连冷请求、复用请求、真实 Gateway SSE 与规划审计 | 18,138 | 输入 3,022，输出 1，缓存读 2,816；8 行 SSE；ADAPT 已观察 |
| mimo-v2.6-flash | 直连冷请求、复用请求、真实 Gateway SSE 与规划审计 | 26,292 | 输入 4,430，输出 2，缓存读 4,416；10 行 SSE；ADAPT 已观察 |
| qwen3.8-flash | 阻塞：百炼 HTTP 400，错误码 Arrearage | 未返回 usage，不能记为已验证的 0 消耗 | 未完成 |

合计已观察到 44,430 tokens。两家各完成两轮三次请求，所有成功调用都纳入以上统计。
千问一次完整探测和一次小型诊断均被欠费状态阻塞，未持续重复请求。

### 探测脚本纠正

首轮脚本中标为 gateway_stream 的调用遗漏了 via_gateway=True，实际是直连重复请求。
该轮仍计入真实 token 消耗，但不能作为 Gateway SSE 证据。
已修正脚本，第二轮通过真实本地 Gateway 转发到厂商，并新增本地回归断言发送路径必须为 non-stream、non-stream、stream。
首轮原始数据保留在 `forge-gui/desktop/build/vendor-live-review/`；有效流式证据在 `forge-gui/desktop/build/vendor-gateway-live-verified/`。
这些验证目录被 Git 忽略，没有打包到产品。

### 仍未验证的边界

- 千问真实缓存命中与 5 分钟显式 TTL：需要百炼账户恢复后继续；脚本支持 --ttl-wait 315。
- DeepSeek/MiMo 自动缓存没有在本轮测得确定 TTL；本地 disk/managed 过期时间是保守元数据策略，不能冒充厂商保证。
- Batch/Flex 的真实异步折扣与正式账单：本轮只做本地协议验证；百炼欠费，OpenAI 不在付费授权范围。
- 上游请求失败/超时后是否计费，以及具体金额：没有正式账单证据，不能由 plan 推断。
- thinking_budget 到线经过离线验证；不能声称对三个厂商均完成独立思考硬上限的实测。

可复用的探测入口：`python -m forge.verify_execution --execute --output <报告目录> --models <授权模型> --max-tokens <不超过5000000> --ttl-wait 315`。
不给 --execute 时不发请求。正式测试发现流程不会自动运行它。

## 自动化验证

- 首先新增 11 项失败测试：4 个 assertion failures、7 个接口缺失 errors，确认缺口后修复。
- 最终新增 22 项执行回归 + 原有 30 项模型回归：52/52。
- 核心 selftest：569/569；适配器：56/56；厂商：77/77。
- 路由及通道 tests：12 项 unittest 及文件级断言通过。
- 插件故障隔离、运行时、能力与权限门回归：94/94，无插件系统改造。
- GUI 运行/启动/消息/导入回归组：51 项通过；导入/消息/导航/DPI 组：50 项通过，含五档缩放和五种窗口尺寸。两组有重叠，不能相加为唯一测试数。
- 一次 GUI 组合测试退出时遇到临时目录 WinError 32；隔离重跑通过，原始失败日志保留。
- Python 编译检查排除了 `forge/templates/` 的未填参数模板（模板含 `def ...(...)` 占位符）；运行源码及改动的 GUI 文件编译通过。

## 官方接口依据

- [百炼 Chat Completions 参数：thinking_budget 与 reasoning_effort 互斥](https://help.aliyun.com/zh/model-studio/qwen-api-via-openai-chat-completions)
- [百炼 Context Cache：显式 5 分钟，隐式命中不保证](https://help.aliyun.com/zh/model-studio/context-cache)
- [百炼 Batch：独立异步接口与 24h completion window](https://help.aliyun.com/en/model-studio/batch-inference)
- [DeepSeek 思考模式及工具调用历史字段](https://api-docs.deepseek.com/guides/thinking_mode/)
- [DeepSeek Anthropic 兼容接口：budget_tokens 被忽略](https://api-docs.deepseek.com/guides/anthropic_api/)
- [OpenAI Flex：service_tier=flex 与返回档位](https://developers.openai.com/api/docs/guides/flex-processing)
