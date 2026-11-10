# MiMo API 工具调用与缓存实测

日期：2026-11-07。使用本机已经配置的 Token Plan 端点 `https://token-plan-cn.xiaomimimo.com/v1`，限定 `mimo-v2.6-flash` / `mimo-v2.6-pro`，没有切换账户或回退厂商。凭据只在 HTTP 认证边界使用，未修改用户配置。

## 测试范围

新增 `scripts/verify_mimo_live.py`，必须显式传入 `--live`。使用 Python 标准库和 Forge 的 `HttpTransport` / `ModelRouter` / `Agent`，不是另一套模拟 SDK。

- 协议探针：要求 `add(17,25)`，同题重发，然后把原生调用及工具结果回放给模型；分别测试关闭和开启思考。
- Agent 探针：真实执行 `read_file → write_file → read_file`，输入为隔离目录中的 JSON。第一轮写出总量 42 及 alpha/beta，第二轮从新文件写出总量 46 及 gamma/delta。两轮均由宿主 `workspace.json_value` 环境探针验收，并独立比较最终 JSON 和全部 id。
- 每个请求记录实际参数、消息/工具摘要、前缀消息数、推理字段数、原始 usage、归一 usage、停止原因和耗时。不会保存业务文件或凭据。

临时 Agent 使用真实的内置工具、权限与文件版本保护；工作区和 home 在临时目录内。没有执行 shell、读取用户项目内容、发送消息或调用厂商联网搜索插件。

## 实测记录

最终结果见 `reports/mimo-live-agent-final.json`。两个模型都完成两轮任务，第二轮均触发宿主 `cache_prefix_reused`，每轮实际调用读、写、读三个工具，宿主验收通过。

| 最终 Agent 复验 | flash | pro |
| --- | ---: | ---: |
| 成功 API 请求 / 失败尝试 | 8 / 0 | 8 / 0 |
| 文件任务 / 实际工具调用 | 2 / 6 | 2 / 6 |
| 全组命中 / 输入 token | 11,776 / 14,460 | 8,704 / 14,716 |
| 全组加权命中率，包含明确冷启动零命中 | 81.44% | 59.15% |
| 第二轮命中 / 输入 token | 7,424 / 8,328 | 5,760 / 8,726 |
| 第二轮加权命中率 | 89.15% | 66.01% |
| 有明确缓存回执 / 未知回执 | 8 / 0 | 8 / 0 |
| 本组 HTTP 总耗时 | 48.672 秒 | 84.548 秒 |

最终两组发送参数均为 `thinking.type=enabled`、`tool_choice=auto`、`max_tokens=1024`，不带自定义采样。工具声明摘要始终相同，连续请求的共同消息前缀为 `0,4,6,8,10,13,15,17`，其中第二轮第一请求保留前一轮已发送的全部 10 条消息。

pro 最终第 4、7 个请求分别只报 256、128 缓存 token；对应请求保持了前一次全部 8、15 条消息及相同参数/工具声明。因此客户端前缀稳定不能当作服务端命中保证。现有证据不足以确定是缓存调度、模板处理还是其他端点行为，未擅自改认证端点、丢弃历史推理或关闭思考来追求一个更高数字。

本轮各阶段累计 62 次 HTTP 尝试，60 次收到成功响应，2 次 TLS 失败；成功回执合计输入 75,625、输出 2,872 token，可能失败请求的实际服务端消费未知。这些是 usage 观测，不是 Token Plan 账单核对结果。没有因中间失败删除记录或挑选最高命中率作为最终结果。

协议结果见 `reports/mimo-live-wire-before.json`、`reports/mimo-live-wire-after.json`、`reports/mimo-live-wire-pro-final.json`。flash 的开/关思考探针均通过；pro 也完成开思考的原生工具回放，关闭思考的调用及回放在此前请求中通过。

`mimo-live-wire-after.json` 的 pro 探针，以及 `mimo-live-wire-pro-retry.json`，分别保留一次 TLS EOF 失败。它们没有收到 HTTP 响应，不能当作模型或工具协议拒绝。最终 pro 协议复验通过，失败记录保留。脚本缺省不重试；最后复验显式允许同模型最多一次重试，不回退其他模型，并受总请求上限约束。

短协议请求不保证有缓存：pro 的最后三次短请求全部明确回报 cached_tokens=0。较长的实际 Agent 请求则观察到前缀命中。没有据此猜测固定最小长度、TTL 或人为填充上下文。

## 根据实测与官方约束实施的修改

1. 在 `VendorAdapter.shape_request()` 增加请求兼容边界，MiMo 的处理在 `_plan_and_shape()` 内、缓存指纹计算之前执行。直连与 Gateway 共用，不另建私有发送路径。
2. 对已识别的 MiMo 2.6 家族，把默认与显式开启思考规范成 `thinking.type=enabled`，有工具时统一使用 `tool_choice=auto`。等价请求得到同一实际请求形状及本地缓存指纹；关闭思考仍独立分区。模型别名绑定同样生效，未核实的未来型号不套用这些约束。
3. 思考开启时移除会被后端覆盖的 temperature/top_p；关闭思考保留受支持的自定义采样，并校验 temperature 的 0–1.5 范围。测试中的 0.7 不再被误解为思考模式下的有效采样设置。
4. MiMo 官方只承诺 `tool_choice=auto`，非 auto 会被移除。Forge 现在对 none/required/指定函数在发请求前明确报不支持，避免把降级 auto 当作约束已生效。旧 MiMo wire 验证脚本改为 auto 加宿主结果检查。
5. 保留原生工具及最终回复的 reasoning_content；这一能力由上一轮跨会话前缀实现提供，本轮用真实 MiMo API 验证。没有把 reasoning 文本中的伪工具片段提升为可执行动作。
6. 补齐 `repetition_truncation` / `content_filter` 的停止处理：不解析为可执行工具，也不把模型文字当成功结束，保留 usage 和明确失败原因。这两个异常分支使用离线夹具验证，本次线上没有触发它们。
7. 更新模型行为画像，去掉“推理参数一变就必然全价重读”的泛化诊断。实测开关思考后的短请求仍可能报告命中；本地保守分区不等于服务端实际零命中。

约束依据：[MiMo Chat Completions 参数说明](https://mimo.mi.com/docs/api/text-generation/openai-api)、[多轮工具与推理回放说明](https://mimo.mi.com/docs/en-US/quick-start/faq/api-integration)。其中默认思考、固定采样和 tool_choice 支持范围来自官方文档；HTTP 成功、工具结果及命中 token 来自本次原始 API 回执。

## 统计边界

缓存命中率使用 `cached_tokens / 已明确观测缓存回执的输入 tokens`。`prompt_tokens_details=null` 保持未知；明确的 cached_tokens=0 正常计入分母。网络失败不计入成功回执的命中率。

修正前 flash Agent 的 92.32% 是 7 次已观测请求、13,449 输入 token 的结果，另有 1 次缺失回执；不能直接与包含明确冷启动零命中的后续整组结果比较。工作区路径、输出推理和服务端缓存状态也会变化。本轮证明了真实工具闭环、缓存确实工作，以及等价参数规范化；没有声称得到受控 A/B 的命中率提升、延迟改善或账单节省。

每次固定场景只有两轮文件任务，不代表复杂软件工程任务成功率。Token Plan 的目录价格估计不作为实际费用结论。

## 复验

新增专项 14 项通过，覆盖参数规范化、缓存指纹等价、Gateway 一致性、别名、模型作用域、未知缓存回执、推理回放、不支持参数的请求前拒绝，以及异常结束不执行工具。框架自检 595/595 通过，代码预算 147 个文件通过。

核心回归 428 项（2 跳过），客户端/运行链回归 206 项（1 跳过），合计 631 项通过、3 项跳过、0 失败；专项不重复计数。Git 空白检查通过。

核心与客户端回归日志为 `reports/mimo-adapter-core.log`、`reports/mimo-adapter-runtime.log`，专项日志为 `reports/mimo-adapter-new-tests.log`；自检及空白检查分别见 `reports/mimo-adapter-selftest.log`、`reports/mimo-adapter-whitespace.log`。

```powershell
python -m unittest forge.test_mimo_adapter forge.test_vendor_wire forge.test_model_kinds -q
python -m unittest discover -s forge -t . -q
python -m forge.cli selftest
python scripts/budget_check.py

# 下列命令会调用真实 API 并消耗 Token Plan 额度。
python scripts/verify_mimo_live.py --live --suite agent --model mimo-v2.6-flash --request-limit 10 --output reports/mimo-live-repeat.json
python scripts/verify_mimo_live.py --live --suite wire --model mimo-v2.6-pro --thinking on --request-limit 4 --output reports/mimo-wire-repeat.json
```

本轮独立补丁为 `reports/mimo-adapter.patch`，文件及验证清单为 `reports/mimo-adapter-files.json`，基线快照路径见 `reports/mimo-live-backup.txt`。补丁以本轮开始时的工作区为基线，不包含之前的集成改动。
