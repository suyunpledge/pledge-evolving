# forge 分厂家模型层适配报告 · 2026-11-04

## 接手复核：统一调用入口与实测补证

本轮按桌面《调用路径交接-20261104.md》复核最新工作区。用户补充要求先验证
DeepSeek、MiniMax；随后用户确认凭据更新，本机配置核实 MiMo token-plan 与豆包 Ark
新凭据后继续补测。本轮未调用 GLM、七牛中转或千问失效订阅。

- 推理方言与采样清理统一在 `model._plan_and_shape`，覆盖直连、GUI 网关、Batch；
  指纹在翻译后计算，原始输入不被修改。
- MiniMax 的 enabled/enable_thinking=true 译成 adaptive；DeepSeek 保留已验证的
  reasoning_effort 参数，其他仅开关型厂家按自身方言发送。关闭请求缺少已验证方言时
  不再声称已关闭。hy4-preview 仅 high；本机 Kimi Code 配置中 K3 系列 always_thinking
  对应不可关闭。单一 Codex 配置不再被当成所有 GPT 型号的完整 effort 枚举。
- 4096 token 用于默认输出预算；显式 max_tokens 与 think_budget 优先，避免擅自增加
  用户预算。输出 length/max_tokens 保留为 Completion.finish_reason，运行返回
  output_limit 失败状态并保留 usage；截断工具参数不修补执行。
- usage 的精确 prompt 缓存字段优先级不再受 JSON 键顺序影响；output 侧
  cached_tokens 不计为输入缓存。

实测脚本：`scripts/verify_vendor_wire.py`。读取本机凭据文件，固定允许端点、无自动重试、
无跨厂商回退，每请求显式上限 256 token；模型调用之外不执行外部工具。

| 模型 | 工具调用/回放/思考开关 | effort 补测 |
| --- | --- | --- |
| deepseek-flash | 3 请求通过；关闭时 reasoning=0 | high 通过，推理 45 token |
| deepseek-v4-pro | 3 请求通过；关闭时 reasoning=0 | high 通过，推理 121 token |
| minimax-m3 | 3 请求通过；adaptive 接受，关闭时 reasoning=0 | low/medium/high 均接受；medium 单例比较题答错 |
| minimax-m2.7-highspeed | 3 请求通过；adaptive 接受，关闭时 reasoning=0 | low/medium/high 均接受且该题回答正确 |

总计 20 次请求，全部返回完整响应且无接口错误；12 次工具/回放/开关检查通过。
M3 medium 的负面结果原样保留，不据单题推断档位质量或修改默认档位。
短提示词命中只证明缓存字段可观察，不作为长会话命中率或费用节省结论。

脱敏记录：`docs/vendor-adapt-2026-11-04/forge-wire-deepseek.json`、
`forge-wire-minimax.json`、`forge-effort-deepseek.json`、`forge-effort-minimax.json`。
新增离线回归：`forge/test_vendor_wire.py`（18 个测试，含实际发包形态、网关/Batch
同源、输出预算、截断动作拒绝、usage 优先级与回放字段保护）。
验证：本机 Python 3.12（已具备可选 PyYAML）内核 unittest 270 项，269 通过、1 跳过；
框架 selftest 595/595；厂商推理断言 72/72，adapter 68/68，vendor 82/82；
源码预算 122 文件、43067 行通过。Bundled Python 缺少可选 PyYAML 的环境问题未修改产品逻辑绕过。

### 凭据更新后的 MiMo / 豆包补测

凭据读取 `~/.forge/secrets.json`，MiMo 按当前配置使用
`https://token-plan-cn.xiaomimimo.com/v1`；未使用旧标准端点或回退通道。

| 型号 | 结果 | 观测 |
| --- | --- | --- |
| mimo-v2.6-flash | 3/3 请求通过 | 工具调用、原样回放、开思考；关闭时 reasoning=0，开启时 reasoning=2 |
| mimo-v2.6-pro | 3/3 请求通过 | 工具调用、原样回放、开思考；关闭时 reasoning=0，开启时 reasoning=2 |
| mimo-v2.6-pro-ultraspeed | token-plan 不支持 | 首次请求及一次单型号诊断均为 400，原文 `Not supported model mimo-v2.6-pro-ultraspeed`；未转其他端点 |
| doubao-seed-evolving | 3/3 请求通过 | 工具调用、原样回放、开思考；关闭时 reasoning=0，开启时 reasoning=296 |
| doubao-seed-2.1-turbo | 3/3 请求通过 | 工具调用、原样回放、开思考；关闭时 reasoning=0，开启时 reasoning=48 |

共 12 次成功请求、2 次型号不可用拒绝。失败记录保留在 `forge-wire-mimo.json`
和 `forge-wire-mimo-ultraspeed.json`，豆包记录为 `forge-wire-volcengine.json`。
`scripts/verify_vendor_wire.py` 新增 `--model` 单型号诊断及脱敏错误信息，避免为了诊断一个
失败型号重跑全部成功型号。型号必须属于该厂商允许列表，不能任意切换端点。

本轮短提示词 cached_tokens 均为 0，不据此判断长会话缓存命中率。
Seed-Evolving 在 max_tokens=256 时报告 completion_tokens=297 / reasoning_tokens=296，
所以记录中的参数仅证明按请求发送，不能据此保证所有厂商都把它作为含思考计费 token 的硬上限。

### WorkBuddy 免费混元 hy4 验证

用户取消千问测试后，仅测试混元。本机正在运行的 WorkBuddy 产品目录中：
`hy4free` 的 credits 为 `x0.00`，supportedEfforts 仅 high，canDisableThinking=false。
实际返回模型名为 `hy4-free`。这些是 WorkBuddy 通道证据，不外推为直连混元 API 的协议或免费政策。

调用通过 WorkBuddy 的本机 WBIPC `wb.request/http.fetch`：使用本机发现文件中的短期
ticket 完成双向 HMAC 校验，后端账号鉴权由 WorkBuddy 附加；ticket、证明与账号 access token
均不写入测试产物。探针固定模型 hy4free，调用前检查 x0.00，响应再次检查 credit=0 与型号。

已完成：

- 简单问答返回 42，HTTP 200 / stop，prompt=34、completion=30、reasoning=27、credit=0。
- Forge `_plan_and_shape` 将 hy4free 的显式 low 调整为 high，按该结果生成工具请求。
- add(17,25) 原生工具调用：HTTP 200 / tool_calls，prompt=252、completion=217、reasoning=198、credit=0。
- 原样保留 assistant 的工具调用及 reasoning 字段回放，工具结果为 42；第二轮输出 42，
  HTTP 200 / stop，prompt=283、completion=362、reasoning=359、credit=0。

通道差异也已实际验证：

- WorkBuddy `/v2/chat/completions` 拒绝非流式请求（11101 / Non-stream chat request is currently not supported）。
- `tool_choice` 必须为字符串；指定函数对象收到 400 / cannot unmarshal object into ... tool_choice of type string。
  使用字符串 auto 后完成闭环。不据此修改其他厂家或直连混元的 tool_choice 规则。
- usage 同时含 prompt/completion 两侧 reasoning_tokens，输入侧为 0，输出侧为 198/359；
  Forge 的归一结果以精确 completion 路径为准。短提示词缓存读为 0，不作稳态命中率结论。

脚本：`scripts/verify_workbuddy_hy4.cjs`（默认只读取接口目录，--live 才调用模型）。
原始请求与脱敏结果：`workbuddy-hy4-request.json`、`workbuddy-hy4-tools.json`；
失败请求与服务端拒绝记录保留在 `workbuddy-hy4-request-rejected.json`、`workbuddy-hy4-tools-rejected.json`。
`HunyuanAdapter` 的说明现在随实际型号显示可用档位，并区分 WorkBuddy 配置与官方直连证据。

> 一句话：用本机九家素材（四家直连探针 + AutoClaw 万次调用台账 + WorkBuddy/Kimi Code/Codex 本地配置与轨迹）
> 把 forge 的模型层做成「按厂家说方言」——思考参数翻译、usage 精确归一、缓存画像升级、四个专属 adapter，
> 全部证据可指认，验证全绿（单测 82/82+43/43、selftest 595/595、四家真 API 端到端冒烟通过）。

## 一、证据源（全部本机本轮采集，可复核）

| 证据 | 位置 | 覆盖 |
| --- | --- | --- |
| 直连探针（用户授权 key） | `docs/vendor-adapt-2026-11-04/vendor-probe-results.json` | 豆包(ark coding)/GLM(coding)/MiniMax/DeepSeek：usage 全量、温度、thinking 矩阵、工具单/并行、缓存 A/B |
| mimo token-plan 探针 | `vendor-probe-mimo.json`（同目录） | mimo v2.6 flash/pro 全套（tp- key） |
| 回放决定性实验 | `vendor-replay-results.json`（同目录） | 5 家 × 三变体（原样/剥 reasoning/全剥）回放，全部 200 |
| AutoClaw 调用台账 | `AppData/Roaming/AutoClaw-official/logs/model-token-usage.log`（8905 条） | GLM coding 4611 次命中 95.8%/96.8%、deepseek 98.6%、step 93-97%、ling 56.8% |
| WorkBuddy(CodeBuddy) 产品配置 | `.workbuddy/storage/.../wb-product-config.json`（60 模型） | hy3/hy4/GLM/Kimi/DeepSeek/Step/MiniMax/MiMo 的 effort 档位、canDisableThinking、采样参数 |
| WorkBuddy 会话轨迹 | `.workbuddy/projects/*.jsonl` | hy3/hy4-preview/step-5/deepseek-v4.1-flash 真实 usage（含 cached_tokens） |
| Kimi Code 配置 | `.kimi-code/config.toml` | coding 端点 api.kimi.com/coding/v1、k3/k3.1/k2.8 efforts=[low,high,max]、always_thinking |
| Codex 配置 | `.codex/config.toml` | `model_reasoning_effort = "high"`（GPT 方言=reseoning_effort） |

## 二、厂家矩阵（本轮定性）

| 厂家 | 思考方言 | 档位 | 可关 | usage 命中字段 | 隐式缓存实证 | 其他要点 |
| --- | --- | --- | --- | --- | --- | --- |
| 豆包(ark coding) | thinking.type on/off | 无分档 | ✅(0 rt) | prompt_tokens_details.cached_tokens | ✅ 2378/2378 全命中、换尾题 1848/2379 | 返回 encrypted_content；回放原样 200；temperature 0.7 OK |
| GLM(coding) | thinking.type on/off | low/high/max* | ✅(0 rt) | 同上 | ✅ 探针 2304/2357 + 台账 95.8%（4611 次） | cacheWrite 恒 0；无 encrypted_content |
| DeepSeek | thinking.type（effort 亦收） | low/high/max* | ✅ | **双字段**：details.cached_tokens + prompt_cache_hit/miss | ✅ 2048/2300（台账 98.6%） | 命中块粒度 64token |
| MiMo(token-plan) | thinking.type on/off（enable_thinking 亦收） | 无分档 | ✅ | prompt_tokens_details.cached_tokens | ✅ 1856/1862、换尾题 1024 部分 | 无缓存时 prompt_tokens_details=null；tp-key 与标准端点不通用 |
| MiniMax | **reasoning_effort 分档** | low/medium/high | ✅(thinking.type 仅 adaptive/disabled，其余 400) | 同上 | ⚠️ 部分/非单调命中（首调亦有存量） | 思考 token 58/100/175 分档实证；usage 多 total_characters |
| 千问(token-plan) | enable_thinking（DashScope 风格，文档级） | 未核实 | 文档级 | 未实证（key 403 Unpurchased） | 未实证 | qianwenaiapi.com 域名已挂到 bailian 画像 |
| 腾讯混元 hy3/hy4 | reasoning_effort | low/high | ❌ onlyReasoning | details 风格（轨迹 7872 命中） | ✅ 轨迹实证 | hy4 仅 high、960K ctx；采样 0.9/1 |
| Kimi | reasoning_effort | low/high/max | ✅ | 未直连实证（扁平 hit 字段已全局兼容） | 文档级 | coding 端点 api.kimi.com；always_thinking |
| GPT | reasoning_effort | low/medium/high | ✅ | details.cached_tokens（叶名兜底可解） | 文档级 | Codex 实证 effort=high 在用 |

\* 档位来自 workbuddy 注册表旁证；直连探针仅验证 on/off 两态（强制 enabled 会把每轮思考从 0 抬到数百 token）。

## 三、代码改动（未提交，叠加在 Codex 在途 6 文件之上、与其零冲突）

| 文件 | 改动 |
| --- | --- |
| `forge/vendors.py` | ① VendorProfile 新增 reasoning_style / reasoning_efforts / can_disable_thinking 三字段（带 to_raw）② 九家画像填实证值；volcengine unknown→implicit（探针坐实）；新增 hunyuan 画像+域名+模型提示；kimi.com/qianwenaiapi.com 域名 ③ `_BASE_USAGE_ALIASES` 增加 prompt_tokens_details.cached_tokens 等精确路径 + prompt_cache_hit/miss 全局别名 ④ normalize_usage pass-2 跳过 completion_tokens_details.cached_tokens ⑤ 新增 `shape_reasoning()`：effort/thinking_type/enable_thinking 三方言翻译+不支持键摘除+档位钳制 |
| `forge/model.py` | HttpTransport 白名单透传后接 `shape_reasoning`（翻译在指纹计算前，signature 对实际发送形态求值）；翻译动作进 plan.actions（reasoning_shaped 标记） |
| `forge/adapters.py` | 新增 MiMo/MiniMax/Volcengine/Hunyuan 四个专属 adapter（实证 notes）并注册 |
| `forge/test_adapters.py` | 2 条断言随实证更新（volcengine 不再 unknown），新增 2 条 |
| `forge/test_vendor_reasoning.py` | 新增 43 断言：识别/画像字段/usage 归一（含防误读回归）/翻译矩阵/adapter 注册 |

## 四、修掉的真 bug

**usage 命中被 completion 侧恒 0 字段吃掉**：混元/DeepSeek v4.1 轨迹里 `completion_tokens_details.cached_tokens`
（规格解码缓存，恒 0）排在 `prompt_tokens_details.cached_tokens` 之前，旧归一层叶名兜底"先到先得"，
命中会被记成 0。现在精确路径优先 + completion 侧显式跳过（test_vendor_reasoning 有回归用例）。

## 五、验证

- 基线（改动前）：内核单测 18/19+1 既有环境性失败（test_execution_review 子进程 import，与本轮无关）；selftest 595/595
- 改动后：test_adapters 82/82、test_vendor_reasoning 43/43、其余全绿、同一既有环境性失败保持不变；selftest 595/595
- 真机冒烟（forge 自身 HttpTransport 全链路）：豆包(effort→auto 翻译+encrypted_content 回放)、GLM(enable_thinking→disabled，
  reasoning=0，二轮 tools 块 cached=128 实测命中)、MiniMax(effort 保留+关思考翻译)、MiMo(关思考 reasoning=0)——四家两轮全通，
  plan.actions 带翻译说明，Usage.cached/reasoning 正确解析

## 六、遗留与建议

1. **千问 token-plan key 403 Unpurchased**：forge 里旧 key 订阅失效；画像的 enable_thinking 方言为文档级（无本机直连实证），
   档位表留空不钳制。拿到有效 key 后跑一次 `probe_vendors.py` 即可补齐。
2. **火山折扣率未公布**：缓存命中已实证但按原价保守计（不套用 2.1 Pro 的 20%）。
3. **Kimi usage 字段**：config 级实证（efforts/端点），usage 形态未直连验证；扁平 hit 字段已全局兼容，如 kimi 另有形状需一次实测确认。
4. **hy3 现场调用未做**：WorkBuddy 轨迹+产品配置已够画像；GUI 驱动对"我们发出的参数形态"验证价值有限，故未做。
5. **三个轨迹子代理被中断击杀未返**（GPT/Kimi、Claude/MiniMax、Qwen/hy/GLM-zcode），关键事实已用更直接的本地证据覆盖。
6. 采样层（sampling.py）无需改动：五家直连全部接受 temperature=0.7；Claude/GPT-6/Kimi-K 系既有限制保持。
7. 改动未提交；主仓另有 Codex 在途 6 文件（loop/tools/smoke/selftest/budgets/contrib）原样未动。


---

# 第二轮 · forge 真实任务实测与修复（同日）

> 有 key 的四家（豆包 ark coding / MiMo token-plan / MiniMax / DeepSeek）在隔离试验床
> （`forge-e2e/`：scratch FORGE_HOME + 独立任务工作区）用 `run.py run` 无头跑真实多轮工具任务
> （列目录/读文件/统计/写 report.md），实测修复问题；GLM coding key 与 qnaigc（七牛云）按指示未动。

## 一、四家真实任务结果

| 厂家 | 结果 | 步数 | 跨循环缓存命中 | 备注 |
| --- | --- | --- | --- | --- |
| DeepSeek-flash | ✅（干净工作区首跑） | 12 | 25600/30119=85.0% | find/read/calculator/write 全通；复跑两次撞 max_steps=12 属模型策略方差（连用 edit_file），非回归 |
| 豆包 seed-evolving | ✅ | 13 | 29320/38663=75.8% | 含 apply_patch；encrypted_content 原样回放全通 |
| MiMo v2.6-flash (token-plan) | ✅ | 14 | 36352/43220=84.1% | tool_search/skill_list/grep 都用到 |
| MiniMax M3 | ✅（修复后） | 24 步预算 74 条记录 | 105952/151314=70%（另一次 88%） | 修复前撞出两个真 bug（见下） |

## 二、实测修复的两个真 bug

### Bug 1：compaction 钉住区空串匹配 → 工具结果孤儿化（严重，全厂商）
**现象**：MiniMax 400 `tool result's tool id(...) not found (2013)`。
**根因**（抓包实证）：`ContextBudget.compact` 的 `pinned_contents` 元组里混有空串（无 plan 时
`plan.context()` 返回 `''`），grep 零命中的 tool 消息 `content==''` 精确匹配 `''` 被当作钉住材料
**整体提升到 system 之后**——既孤儿化（严格校验的 MiniMax 直接 400）又乱序（其余厂商静默吞下损坏历史）。
离线合成复现不出（正常路径配对完好），靠包装 HttpTransport 抓到 400 现场的 messages 才定位。
**修复**（compaction.py）：两道防御——①空串不进 pinned 集合；②tool 消息与带 tool_calls 的 assistant
永不可被钉住提升。回归测试 `test_compaction_empty_pinned_never_hoists_tool_results`。

### Bug 2：MiniMax M3 思考泄漏 + 输出截断
**现象**：最终正文以 `<think>…</think>` 开头且答案缺失（</think> 恰在截断点）。
**根因**：M3 该部署形态思考走 content；主循环 max_tokens 默认 2048 被思考整个吃掉
（末请求 completion 2048 / reasoning 2047，finish=length），答案从未生成。
**修复**：①`toolwire.strip_think_blocks`——只剥开头位置的 think 块（闭合/未闭合），中间不动，
原始 assistant 消息保持字节稳定回放；②`VendorProfile.min_output_tokens`（M3=4096 实测安全线），
HttpTransport 按画像抬输出下限。7 条新断言。

## 三、直连实测（无 raw key 的四路）

| 工具 | 结果 | forge 收获 |
| --- | --- | --- |
| Antigravity (Gemini 3.8 Flash) | ✅ 无头打通：IDE 内置 `agentapi`（LS 动态端口 + `/csrf` 页取 token），`new-conversation --model=flash` 真实建会话 | 内部模型 id **gemini-3.8-flash-tiered**（分级计费档）入画像 notes；usage 藏 proto 二进制，未解析 |
| ZCode (GLM) | ✅ 轨迹实证：ZCode 经 **open.bigmodel.cn/api/anthropic**（Anthropic 兼容线）调 GLM，cache_control ephemeral + thinking{type:enabled} 直发可用 | zhipu 画像补注：anthropic wire 接入路径（本轮 GLM key 未烧） |
| Qoder 国际版 | ⚠️ 无本地 CLI（.qoder 入口 shim 在但 qodercli.exe 未装，IDE 只带 agent-sdk）；推理走 api1/api2.qoder.sh 专有 SSE 协议+账号鉴权 | qwen3.8-flash 仅确认存在于国际版模型列表；enable_thinking/usage 维持文档级 |
| WorkBuddy (hy3) | 未驱动 GUI：workbuddy 通道发的是它自家协议，验证不了 forge 发出的参数形态；hy3 画像维持产品配置+轨迹级 | — |

## 四、本轮变更与验证

- `forge/compaction.py`：pinned 空串修复（Bug 1）
- `forge/toolwire.py`：`strip_think_blocks`（Bug 2①）
- `forge/vendors.py`：`min_output_tokens` 字段 + M3=4096（Bug 2②）；zhipu/gemini notes 补证
- `forge/model.py`：输出下限接线 + 文本清洗接线
- `forge/test_new_modules.py`：+1 回归（Bug 1）；`forge/test_vendor_reasoning.py`：+7（Bug 2）
- 验证：内核单测全绿（82/82+50/50+68/68+31/31…，唯一失败=既有环境性 test_execution_review 子进程 import）；
  selftest 595/595；MiniMax 修复后完整成功一轮（74 条步骤记录、report.md 正确、正文无泄漏）
- 试验床与证据：`forge-e2e/`（out-*.json / capture.jsonl / sessions）；全部未提交


---

# 第三轮 · 按厂家缓存/操作习惯/轨迹贴合 + 选模型自动适配（同日晚）

> 逐请求缓存轨迹分析定位断点 → 按厂家抬压缩地板与输出下限 → 循环预算随选中模型自动适配
> （用户显式配置优先，base 默认不算显式）→ run 报告内置缓存轨迹投影。

## 一、逐请求轨迹分析（第二轮 e2e 数据，cache_trajectory.py）

| 厂家 | 均命中 | 断点 | 定性 |
| --- | --- | --- | --- |
| DeepSeek | 95.4% | 0 | 前缀纪律已最优 |
| 豆包 | 72.1% | 0 | **缓存写入滞后一拍**（八连请求 cached 恒=上上轮 prompt，两次追平）——厂家特性，非纪律问题，已入画像 notes |
| MiMo | 80.8% | 0 | 首请求冷启动后 85-96% |
| MiniMax | 71.2% | **5** | 五个断点全部是 **compaction**（每次 prompt 骤缩+缓存清零）：M3 啰嗦反复越过 24k 字符预算 |

## 二、改动与量化

### 1. 按厂家压缩地板 `VendorProfile.context_floor_chars`（缓存命中）
24k 字符默认压缩阈值是小窗口时代遗产；1M 窗口+实证高命中的厂商在 24k 压缩=整段前缀打回全价。
六家 slope 厂商（deepseek/volcengine/mimo/minimax/zhipu/hunyuan）地板 96,000 字符，cliff 厂商（gemini）不受影响。
**MiniMax 实测 71.2% → 92.78%，断点 5 → 0**；deepseek 稳定 95.2%。

### 2. 思考型厂商输出下限扩展（修第三个真 bug：空文本终答）
DeepSeek e2e 出现 succeeded+空文本：末请求 **completion=2048 / reasoning=2048**——输出预算全被
reasoning_content 吃掉、content 未起笔，finish=length，空内容被当终答。四家均有逼近上限实证
（豆包 2670、mimo 恰 2048）→ `min_output_tokens=4096` 扩展到 deepseek/volcengine/mimo/hunyuan
（minimax 第二轮已设）。修复后 deepseek 完整成功（report.md 写入+回读验证）。
已知边界：若思考仍吃光 4096，空终答需要 loop 层重问——loop.py 为 Codex 在途文件本轮不动，列为建议。

### 3. 循环步数随选中模型自动适配 `VendorProfile.steps_hint`
M3 十二步撞顶两回（验证型走法）→ steps_hint=24。接线时发现 base.json 写死的 maxSteps=12
会被误判为「用户显式」→ 给 `Config.Row` 加 **source 溯源**（apply_patch 记录最后写入层），
仅用户层（forge.patch.json）声明才算显式；显式配置永远赢。实测 `[loop] MiniMax 步数提示 24 生效`。

### 4. run 报告缓存轨迹投影（运行轨迹贴合）
`protocol.run_payload` 新增 `usage.cache_trajectory`：逐请求命中率、均值、断点定位（<40% 且骤降
>30pt）、冷启动标记——把 AutoClaw 台账（model-token-usage.log 的 prefix/命中纪律）投影进每次 run
报告，回归 AutoClaw 实测 95-98% 命中的轨迹形态。

### 5. 自动适配面（选模型即生效，无需配置）
推理参数翻译（第一轮）+ usage 归一（第一轮）+ 输出下限 + 压缩地板 + 步数提示 + 温度策略（既有）
——全部按 `profile_for(实际调用模型)` 在请求/循环预算两层自动选择对应适配器。

## 三、验证
内核单测全绿（新增第三轮断言：地板×6/步数/adjust_budget 三态/轨迹四查；2 条旧断言随新语义更新）；
selftest 595/595；e2e 复测：MiniMax 92.78%/零断点（地板+步数提示 stderr 双生效）、DeepSeek
95.2%/完整成功（空终答修复）。改动文件：vendors/context_plan/cli/protocol/config +
三个测试文件；loop.py/tools.py 等 Codex 在途文件未动；全部未提交。


---

# 第四轮 · 分级检索（HR）命中优化 + 模型适配层（同日夜）

> 后续按模型种类 ID 的接线与证据分级见 [MODEL-FINGERPRINTS.md](MODEL-FINGERPRINTS.md)。
> 本轮表中的 MiniMax 精度档与 24 步提示现已收窄到 M3，M2.7 与未知型号使用普通默认值。

> 用前三轮的厂家特性给 HR（长期记忆分级检索）配模型适配：查询增富修跨话题歧义、
> 画像化预算/门槛/检索宽度（选模型即生效），进程内隔离评测拿命中数字。

## 一、架构落点

- forge 侧集成：`forge-gui/memory_retrieval.py`（桥）+ `forge_gui_v2.py` 发送点（本轮传 model）。
- 服务侧（未改动）：本机分级检索服务（127.0.0.1:8000，嵌入 bge-m3@8101），
  话题域提示+全局候选合并、THRESH_KEY=0.60 过滤。
- 评测走**进程内引擎 + 临时存储**（HierarchicalRetrieval 直连，零污染真实库）。

## 二、命中优化（查询增富——主抓手）

**痛点**：短句/指代查询（"预算上限记得吗"）嵌入质量差，且关键词跨话题多义
（14 话题语料里"预算"出现 4 次）——裸查要么空手、更糟的是返回**错话题噪声块**直接误导模型。

**修法**：`build_query()` 确定性增富——查询 <40 字符或含指代标记词时，拼上
上一轮检出话题名 + 用户发言头 48 字符（总锚 ≤80 字）。纯函数、顺序确定，
不破坏字节级缓存确定性（评测 D 项两次逐字节一致 ✓）。

**三轮隔离评测数字**（临时存储 + 真实 bge-m3 嵌入）：

| 评测 | 直查 | 指代/短句（裸→增富） | 无关问题空块率 |
| --- | --- | --- | --- |
| 8 话题净语料 | 8/8（三种预算全对） | 5/6 → **6/6** | 4/4 |
| 14 话题长回复 | 14/14 | 8/8 → 8/8 | 5/5 |
| **8 题跨话题同词竞争**（主战场） | — | 6/8（含 2 个**错话题噪声块**）→ **8/8 零噪声** | — |

## 三、模型适配层（选模型即生效）

`VendorProfile` 新增三旋钮（默认=改前行为，全部厂商零变化起步）：

| 档位 | 厂商 | 依据（前三轮实证） |
| --- | --- | --- |
| 精度优先 | MiniMax 1000 字/门槛 0.30；Gemini 1000/0.25 | M3 验证型啰嗦（噪声敏感）；Gemini 200K cliff 尾部经济学 |
| 宽档 | DeepSeek/豆包/MiMo/GLM/混元 2000 字 + top_k 8 | 96K 上下文地板 + 76-95% 实测命中 → 尾成本可忽略，宽检索高召回 |
| 默认 | 其余 1500/0.0/0 | 行为与改前一致 |

接线：`MemoryBridge.context_for(query, model=...)` → `retrieval_profile(model)` 经
`forge.vendors.profile_for` 单表识别（与请求层适配同源）→ 预算/门槛/top_k 全自动；
GUI 发送点把本轮实际模型传入。桥内维护话题锚（上一轮 detected_domain + 用户头），
同对话序列恒同锚 → 查询字节稳定。

## 四、验证

- 新测试 `forge-gui/test_memory_adaptation.py` 19/19（增富触发/确定性/门槛/预算/画像映射）
- 旧 `test_memory_retrieval.py` 15 例：11 过 + 4 个 tkinter 环境性错误（既有限制，非回归）
- 内核单测全绿（唯一失败=既有环境性）+ selftest 595/595
- 服务侧零改动；真实库零写入（评测全在临时存储）
- 改动：forge/vendors.py（三字段+七家档位）、forge-gui/memory_retrieval.py（增富/门槛/画像化桥）、
  forge_gui_v2.py（一行传 model）；全部未提交
