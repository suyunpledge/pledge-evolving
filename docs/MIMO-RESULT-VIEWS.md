# MiMo 工具结果视图与 token 优化

本轮把结构化工具结果的“完整留存、按需展示”接入共享 ActionExecutor，Agent 与 Gateway 使用同一条调用链。对于宿主明确声明的 JSON 结果字段，模型先看到继续任务所需的字段和原文引用，完整工具输出仍保存在 ArtifactStore，需要明细时通过 artifact_read 分页读取。普通工具默认不启用，也没有修改用户的 provider 配置、推理模式或输出上限。

## 实现与使用

ToolSpec 新增 `result_fields`，选择完整 JSON 对象的顶层字段。字段清单由可信宿主注册时验证并冻结，既不进入模型工具 schema，也不由模型或工具返回内容决定。例如，拥有完整批次明细的工具可以声明：

```python
registry.register(
    ToolSpec('process_batch', 'Process one batch', handler,
             result_fields=('batch_result', 'next_cursor')),
    trusted=True, effect=ToolEffect.READ,
)
```

工具的 effect 必须与实际行为相符；上例假定 handler 只读。调用方还需正常暴露 artifact_read 并拥有其读取授权。宿主需保证所选字段足够支持常规工作，不能将此机制当作自动摘要或通用截断器。本轮没有批量改造内置工具的返回契约。

调用顺序为工具授权、执行、原始文件修订记录、结果视图与原文留存、动作完成记录、模型工具消息。模型收到类似：

```json
{"batch_result":{"batch":1},"next_cursor":"next","_retained":{"id":"a_<sha256>","trust":"untrusted"}}
```

实际保留选中字段的值，包括整数、浮点数的原始 JSON 数字写法，避免经过浮点数转换造成精度损失。完整原文先经过现有 secret scope 保护，再按内容哈希持久化；读取内容继续标记为 untrusted。

约束与回退：

- 只有 `trusted is True` 的注册可以声明字段；最多 16 个唯一字段，每个 1–128 字符，禁止 `_retained`。克隆和注销同步维护注册快照。
- 当前 registry 必须暴露 artifact_read，且其实际 effect 为 READ、当前策略直接 ALLOW；不会触发 ASK、自动打开隐藏工具或扩大授权。
- 仅处理成功、无 error 诊断、执行状态 completed 的结果；失败或状态未知沿用现有保留路径。
- 非对象 JSON、重复键、缺失字段、非 JSON 数值、无法编码的代理字符、过大源数据、视图不更短或超出 inline_limit 均回退到已有路径。
- 原文保存失败、可选字段选择器失败，不改变已执行动作的成功结果，不重试副作用。artifact_read / artifact_grep 不递归留存自身输出。

## MiMo 实际 API 对照

正式 Pro 和 Flash 各做一组 baseline → improved。两臂运行相同的当前 Agent、ContextBudget、相同工具 schema、任务、原始工具返回字节、固定前缀以及输出上限；唯一开关是宿主 result_fields。报告确认两臂首个完整 HTTP payload 的摘要一致。本地路由窗口显式设为 16000、输出上限 4096；测试 fixture 的 inline_limit 为 8000，源结果 4638 字符，视图 215 字符。生产默认 inline_limit=4000 的视图和完整分页读取另有离线测试覆盖。

每臂执行两个连续用户轮次，处理五个批次、持久化精确批次头及 963 个单位，并实际 read_file 验证。每臂都是五次 process_batch、三次 read_file 和十次成功模型请求。正式四个样本共执行 20 个批次、32 次工具动作，全部任务验收通过、无工具错误，最终答案均为严格 plain JSON。

| 模型 / 策略 | 输入 token | 输出 token | 总 token | 已知缓存 token / 已知输入 | 命中率 | 已提交压缩 |
| --- | ---: | ---: | ---: | --- | ---: | ---: |
| MiMo Pro / baseline | 35007 | 1062 | 36069 | 12800 / 35007 | 36.56% | 3 |
| MiMo Pro / improved | 21077 | 451 | 21528 | 12032 / 21077 | 57.09% | 0 |
| MiMo Flash / baseline | 35030 | 896 | 35926 | 21568 / 33736 | 63.93% | 3 |
| MiMo Flash / improved | 22934 | 600 | 23534 | 20544 / 22934 | 89.58% | 0 |

本次 Pro 总 token 降低 **40.31%**，Flash 降低 **34.49%**。减少不必要的结果重放后，两臂中的 improved 都避免了本 fixture 原本触发的三次压缩，从而同时减少输入并保留较长的可复用前缀。缓存 token 的绝对数量也有所下降，命中率提升应结合总输入减少一起解读。

Flash baseline 有一次缓存回执未知，涉及 1294 个输入 token：计入总输入，但从已知命中率分母排除，没有算成零命中。这是小样本顺序 A/B，存在缓存状态和模型生成波动；不代表所有 Forge 任务都能达到同样收益，也没有以发票或延迟数据证明费用、速度改善。

正式证据：

- [Pro 对照](../reports/mimo-view-pro-final-ab.json)
- [Flash 对照](../reports/mimo-view-flash-ab.json)
- [遗漏明细取回](../reports/mimo-view-retained-lookup.json)

单独的实际 Flash 调用要求找出视图中不存在的 B1-R048 数量。实际链路为 load_batch → artifact_read × 3 → 最终 JSON，第三页观察到原始记录，正确返回 quantity=6。共 6 次 HTTP 尝试、5 次成功回执、1 次 TLS/URLError，输入 8634、输出 480 token。不是仅检查引用存在，也没有只根据批次头猜答案。

连同早期 pilot，共导出并验证 15 份完整原文（pilot 5 份、正式 10 份），内容哈希和 fixture 原始字节一致；7 份压缩归档校验了摘要与工具调用配对。导出文件位于对应报告旁的 `*-artifacts` 和 `*-archives` 目录。

早期 [Pro pilot](../reports/mimo-view-pro-ab.json) 保留原始失败记录：baseline 首轮实际任务正确，但最终 JSON 前有文字，旧严格解析器导致未进入第二轮，不能作为完整性能对照。脚本现在分别记录任务值、格式、strict_format；仅接受完整 plain JSON 或单个最终 JSON 代码块，前有说明文字时 strict_format=false，存在尾随文本或多个 JSON 代码块不接受。本次正式两组均为 strict_format=true、plain_json，没有依赖放宽格式得到成功结论。

本轮所有付费测试合计 **66 次 HTTP 尝试，61 次成功回执，5 次 TLS/URLError**；成功回执输入 165112、输出 4752 token。失败请求的服务端消耗未知，不推定为零，也不将成功回执 token 当作发票金额。

## 验证与复现

新增 13 个测试方法，覆盖真实 ActionExecutor、Agent 的 OpenAI/Anthropic 工具消息、原文分页取回、数值精度、secret 保护、工具授权门禁、注册快照、失败回退与动作去重。核心 488 项（跳过 2），runtime 208 项（跳过 1），合计 **693 通过、3 跳过**；selftest **595/595**；budget_check **154 文件、48179 行**。

本轮开始前已有的 ConversationPrefix 磁盘持久化、loop 修改及 test_conversation_prefix_disk.py 保持原样；预算清单更新也登记了这些既有文件和行数，不将它们算作本轮源代码实现。

本机已有 MiMo 配置可运行以下命令（会产生 API 消耗；输出文件请另选名称以保留原始证据）。result-views 对照不依赖历史源码备份：

```powershell
python scripts/verify_mimo_long_context.py --live --model mimo-v2.6-pro --result-views --strategies baseline improved --context-window 16000 --request-limit 26 --output reports/new-view-pro.json
python scripts/verify_mimo_long_context.py --live --model mimo-v2.6-flash --result-views --strategies baseline improved --context-window 16000 --request-limit 26 --output reports/new-view-flash.json
python scripts/verify_mimo_retained_lookup.py --live --model mimo-v2.6-flash --request-limit 10 --output reports/new-view-lookup.json
```

运行时仅从本地 Forge 配置读取凭据，不将密钥写入报告。改动范围与归一化哈希见 `reports/mimo-view-files.json`，独立增量见 `reports/mimo-view.patch`；原始备份位置记录于 `reports/mimo-view-backup.txt`。
