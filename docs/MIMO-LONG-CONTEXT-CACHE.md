# MiMo 长任务、多轮续接和压缩缓存

本轮沿 Agent、DevelopmentRuntime / EditorBridge、ModelRouter / HttpTransport 检查实际调用链。没有修改用户的 provider 配置、推理模式或账户；付费请求只发送隔离的仓库审计合成数据。

## 修复的实际问题

1. **重复截断工具结果。** ActionExecutor 已负责持久化，tool_turn 已负责限长，但 toolwire 仍将内容再次压到约 1,200 字符。4,638 字符的有效 JSON 会在中间丢失字段，且没有新的可读取 artifact 引用。移除协议层的第二套 preview；两种原生协议均原样传输执行边界已经处理的内容。不是无限增大输出，原有落盘、分页、限长和失败提示继续生效。
2. **预算漏算重放推理内容。** ContextBudget.size 现在计入 reasoning_content 及其他不透明重放字段。压缩不能仅靠可见答案的大小判断，也不能删除签名推理块或拆散调用/结果配对来省空间。
3. **压缩移动普通消息组成的固定前缀。** Agent 显式记录 system、冻结记忆、已投影根规则等宿主前缀，事务在提交前检查原样保留。摘要放在固定前缀之后；剩余任务、规则和完整交换按原顺序保留，避免把最新任务提前到旧历史之前，或让摘要成为最后一条用户任务。
4. **固定尾部回收不够，导致连续改写摘要。** Agent 以有效软预算的 65% 为回收目标，按完整交换移动切点；必须保留的任务/规则/最近交换可以使目标无法达到，不强行截断。硬路由窗口仍独立验收，容纳不下会停止。旧式独立 ContextBudget 调用仍支持原接口。
5. **每轮清空缓存观测与压缩谱系。** 同一 Agent 的可信续接快照现在携带最近前台观测、压缩 revision、transaction id 和路由已见 revision。只在历史/权限/规则 guard 验证通过、路由命名空间一致且观测年龄不超过 60 秒时继承。端点、账户、模型、推理选项、适配配置或工具面变化、回退路由、未知回执及 MoA/评审混合用量不会被当作当前主路由热度。软预算延期另有两倍增长上限，硬溢出不延期。
6. **编辑器裁剪历史后丢失可信快照。** EditorBridge 将聊天裁到 64 KiB，却留下完整历史的旧校验摘要。新增宿主后缀裁剪确认：完整源摘要和精确后缀都匹配才更新摘要；客户端编辑或伪造后缀仍使快照失效。同时修复 200 条消息满额后追加两条导致下一轮报错的问题。工作区权限、执行日志、工具原生历史和快照 2 MiB 上限均继续执行。

没有把摘要当作已验证事实，也没有添加模型可见的 transaction/revision 诊断来污染请求前缀。压缩前先存源档案、取消/策略/源变更检查、完整工具配对校验继续经过同一个事务边界。

## 实测方法和口径

`scripts/verify_mimo_long_context.py` 通过现有配置行 id 查找 MiMo Token Plan 凭据，不输出凭据。每个样本均是同一隔离工作区的新 Agent，包含冻结长期记忆、AGENTS.md 和 Code 模式开发指导。

第一轮实际执行 3 批，第二轮读 checkpoint 恢复游标，再执行 2 批。每批返回 48 条真实夹具记录及下一个游标，工具结果 4,638 字符；每次实际写入 checkpoint，最终必须达到 5 批、963 units。宿主环境探针和独立状态校验都检查结果，同时核对每步调用、全部原生调用/结果配对和源档案。改进组还直接断言每个实际 HTTP 请求的前四条固定消息相等。

A 是本轮开始前保存的 loop / compaction，B 是修复后的实现；**两组共用修正后的无二次截断工具投影**，这样不会把丢数据造成的小输入冒充缓存收益。A/B/B/A 降低固定执行顺序的影响，但服务端缓存状态和推理长度依旧有波动。各组首个 HTTP JSON 摘要应在同一实验内完全相同。

默认将本地软门槛设为 16,000 字符、路由门槛设为 16,000 token、输出额度 4,096 token，以触发真实压缩；另用 65,536 token 的本地路由门槛检查低压力下的跨轮延期。这些是测试宿主约束，未声称测到模型的标称最大窗口。两组使用相同输出额度和既有推理设置；没有重复填充提示词或模拟 cached_tokens。

最终 JSON 允许外层一个完整 json 代码围栏，并在报告单独标为 fenced_json；数据、工具动作和环境探针仍全部严格校验。未知缓存回执单列，不计作零；缓存比例只用已观测输入 token 作分母。未把这些局部调用和估计成本当作已核对账单。

## 低压力跨轮对照：Pro

原始记录 `reports/mimo-long-pro-warm-ab.json`，两组最初请求 JSON 完全相同，均完成两轮任务；路由门槛为 65,536 token。

| 策略 | 已观测缓存 / 已观测输入 token | 加权命中率 | 压缩次数 | 第二轮压缩 | 第二轮延期 |
| --- | ---: | ---: | ---: | ---: | ---: |
| A 修改前 | 17,024 / 42,675 | 39.89% | 3 | 3 | 0 |
| B 修复后 | 33,664 / 48,225 | 69.81% | 1 | 0 | 3 |

B 第二轮继承了真实观测并连续追加，没有重写压缩摘要；最后一次实际请求命中 8,320 / 8,633 token（96.37%）。宿主断言覆盖压缩前、压缩后及第二轮每次实际 HTTP 请求，四条固定前缀保持相同。对照中的已观测未缓存输入由 25,651 降为 14,561 token（约 43.23%），同时总输入增加 13.01%。

这是一个 A/B 小样本。B 的成功请求数为 10、A 为 9；输出由 642 增为 1,667 token，HTTP 尝试耗时约 81 秒对 45 秒，B 还出现一次 TLS 失败后重试。**本次改善的是缓存复用和未缓存输入，并未改善延迟，也不证明总体费用一定下降。** 两组最终结果及全部真实动作均通过校验；两份 B 的答案带外层 json 围栏，报告保留这一格式事实。

## 频繁压缩对照：Flash A/B/B/A

原始记录 `reports/mimo-long-flash-abba.json`。四组初始 HTTP JSON 摘要完全相同，4/4 两轮任务通过，20 次批次动作和全部状态核验成功，最终均为普通 JSON。所有 B 请求在压缩和跨轮前后都保留相同的四条固定消息。

| 策略 | 完成样本 | 已观测缓存 / 已观测输入 token | 加权命中率 | 缺失回执 | 压缩 | 成功模型请求 |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| A 修改前 | 2/2 | 47,936 / 84,399 | 56.80% | 3 | 8 | 23 |
| B 修复后 | 2/2 | 50,816 / 75,848 | 67.00% | 0 | 4 | 20 |

包括缺失缓存回执的请求在内，A/B 成功响应输入分别为 94,201 / 75,848 token，输入减少约 19.48%；输出分别为 2,158 / 1,322 token。A 有两次额外文件读取，B 共少三次模型往返，收益不应全部归因于服务端缓存。三次未知回执使缓存分母不完全对称，不能将 10.20 个百分点当作普遍固定提升。B 有一次 TLS 失败后同模型重试，原始记录保留了失败尝试。

只看未标为压缩重建的已观测请求，命中率为 A 72.63%、B 78.15%；重建请求分别为 21.54%、15.23%。这也说明保留固定前缀不能让压缩后的首个请求必然命中，减少不必要的重建比宣称每次重建都更高效更符合记录。

## 诊断过程中保留的失败

- `reports/mimo-long-pro-pilot.json`：最初输出额度仅 1,024，第二轮触及输出上限。此后等额提高两组测试额度到 4,096，没有修改生产配置。
- `reports/mimo-long-pro-ab.json`：即使输出额度为 4,096，旧调用链的一次续接仍出现输出上限停止；工具边界正确停止。未把它当作完成任务的性能数据。
- `reports/mimo-long-pro-abba.json`：A/B/B/A 中两个 A 和一个 B 完成两轮；另一个 B 已正确处理首轮 3 批，但答案带 json 围栏，被当时的严格解析器标为失败，因而未继续第二轮。此记录保持原状。不能把这份不等量样本直接合并成完整双样本的缓存收益；后续的 Flash 和 Pro 低压力实验单独记录完整结果及答案格式。

包括上述诊断，本轮合计 116 次 HTTP 尝试、112 次成功响应、4 次 URLError/TLS 失败。成功回执输入共 448,557、输出 16,413 token。失败尝试的服务端消费未知，未计入已核对账单。

## 回归和可复验入口

核心回归 462 项（2 跳过）、客户端/运行链 208 项（1 跳过），合计 **667 项通过、3 项跳过、0 失败**。框架自检 **595/595**。代码预算 150 个文件通过；采用 Windows CRLF 规则的 Git 空白检查通过。

新增回归覆盖两种原生协议的完整中部工具证据、推理预算、稳定普通消息前缀、最新任务顺序、完整批量工具交换、过期/换路由/回退/未知/MoA 观测拒绝、跨轮 revision 和热观测恢复，以及真实 Work/Code 入口在 64 KiB / 200 条裁剪后的 HTTP 前缀延续。这里的入口 HTTP 用本地确定性响应替身，不冒充厂商实测。

```powershell
python -m unittest forge.test_long_context_cache forge.test_backend_context forge.test_prefix_cache -q
python -m unittest discover -s tests -p test_cache_continuation_entries.py -v
python -m unittest discover -s forge -t . -q
python -m forge.cli selftest
python scripts/budget_check.py
git -c core.whitespace=cr-at-eol diff --check

# 真实 API，会消耗现有 MiMo 额度。每个命令有 HTTP 尝试上限。
python scripts/verify_mimo_long_context.py --live --model mimo-v2.6-flash --request-limit 48 --output reports/long-flash-repeat.json
python scripts/verify_mimo_long_context.py --live --model mimo-v2.6-pro --strategies baseline improved --context-window 65536 --request-limit 26 --output reports/long-pro-repeat.json
```

对照脚本的 A 来自本轮开始时的本地源代码备份，位置见 `reports/mimo-long-backup.txt`，需要保留该目录才能复跑旧实现。提交的压缩源档案另导出到各结果文件对应的 `*-archives` 目录，可按报告中的 export_path 和 archive_digest 复核，临时工作区删除不会丢失这些材料。

完整回归日志为 `reports/mimo-long-core-final.log`、`reports/mimo-long-runtime-final.log`、`reports/mimo-long-selftest-final.log`。本轮独立补丁为 `reports/mimo-long.patch`，文件清单和前后摘要为 `reports/mimo-long-files.json`；不包含此前工作区的其他改动。
