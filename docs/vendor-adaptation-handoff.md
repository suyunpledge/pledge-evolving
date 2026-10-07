# 厂商适配层 · 接口契约与待办交接

日期：2026-10-07（上午）。撰写：AutoClaw 主会话。
用途：**给继续完善这个模块的下一位（人或模型）**。本文件只写「不能动的契约」和「还没做的部分」，不复述已修好的 bug。

---

## 0. 先说一件事：上一轮发生过互相覆盖

2026-10-07 凌晨 01:55–01:56 有一批改动落在 `routing.py` / `cmd_setup.py`（新增 `plan=` 透传、向导改用字面量 key、`test_model_adaptation.py` 等）。
同日上午我（AutoClaw）在不知情的情况下整文件覆盖了 `model.py` 与 `cli.py`，**把 `Completion.plan` 字段冲掉**，导致 `selftest` 的 `test_smart_routing` 报
`TypeError: Completion.__init__() got an unexpected keyword argument 'plan'`。

已修复，但请约定：**后续修改请只做定点 patch（edit/apply_patch），不要整文件覆盖**；两个来源同时改同一模块时，先看 `git diff` 再动手。

---

## 1. 不可破坏的接口契约

下面这些是 `forge/test_model_adaptation.py`（30 项）与 `forge/test_adapters.py`（56 项）直接断言的对象。改签名 = 直接打破契约。

| 契约 | 位置 | 说明 |
|---|---|---|
| `Completion.plan: dict` | `model.py` | 本次请求的适配计划。链路：`HttpTransport` 放进 `extra["plan"]` → `ModelRouter.complete` / `routing._completion` 透传 → `Completion.plan`。**不要改名、不要设成可选 None**（下游用 `result.plan["vendor"]`）。 |
| `_plan_and_shape(payload, messages, provider, model) -> (payload, plan_raw)` | `model.py` | 返回**新的** payload；**不得原地修改调用方对象**（重试/回退路径会复用同一份请求体，原地注入会叠加标记）。 |
| `plan_raw` 的键 | `adapters.RequestPlan.to_raw()` + `fingerprint` | `vendor / vendor_label / actions / notes / cache_control / breakpoints / ttl / est_tokens / est_cost / baseline_cost / saving / saving_pct / cliff_action / reasoning_budget / defer / cache_engages / fingerprint`。 |
| `_remember_warmth(provider, plan_raw, usage=None)` | `model.py` | `usage` 可缺省，且**同时接受 `Usage` 与 `dict`**。显式缓存厂商只在真的报了 `cache_write_tokens` / `cached_tokens` 时才记热。 |
| `prefix_fingerprint(payload, *, model="", endpoint="", account="")` | `cache_state.py` | 必须保持**单个位置参数**可调用。`account` 只参与哈希、**不得出现在任何序列化输出里**（不能泄漏 key）。 |
| `Profile` 双入口 | `model.py` | `provider.profile()` 按**声明的默认模型**；`provider.profile_for_model(model)` 按**本次实际调用**。聚合器（一个 baseURL 挂多厂商）必须走后者，否则整条链路按默认那家计费。 |
| `Provider` 字段 | `model.py` | `vendor` / `cache_control`（auto\|off\|explicit）/ `expected_calls`（默认 2）/ `call_gap_seconds`（默认 60）/ `adapt`（默认 True）。 |
| `pricing.index_price(model) -> (in, out, known)` | `pricing.py` | 三级回落：目录价 → 账单反推混合价 → 中性单位价 1.0。**适配器的缓存决策不得因为「查不到价」而失效**——它是比例决策，尺度无关。 |
| `ContextPlan.from_config(cfg)` | `context_plan.py` | 读 `context` 行的**顶层键**（`mode` / `defaultMode` / `keepTail`），不是嵌在 `policy` 下；`disabled: true` 时退回 `ask`。 |

---

## 2. 容易被改坏的三条不变量

1. **缓存命名空间是 (厂商 × 模型 × 端点 × 凭据 × 协议)。**
   `prefix_fingerprint` 已经把这五个维度都纳入哈希；任何一次「简化」都会让温暖度表说谎，进而让路由做出错误的跳转决策。

2. **只有稳定前缀享受缓存价，对话尾部永远全价。**
   成本模型是 `前缀×(写入 + (N−1)×命中) + 尾部×N`；warm 时 `前缀×N×命中 + 尾部×N`（**当前这一次也是读**，不是免费）。
   把两者合并成一个 `est_tokens` 去算，会低估长用户消息的成本、高估缓存的收益。

3. **预算单位是字符，厂商阶梯线单位是 token。**
   `adjust_budget` 必须乘 `chars_per_token`（当前 2.5）。漏了这一步，在长会话里会把压缩顶到阶梯线以上、白白触发单价翻倍。

---

## 3. 仍未完成的部分（不要假装已完成）

| # | 缺口 | 现状 | 影响 |
|---|---|---|---|
| G1 | `defer` / `batch` / `flex` / `reasoning_budget` **只是规划提示** | 适配器会把它们写进 `RequestPlan`，但没有任何调度器/参数装配去执行 | 错峰省钱、Batch 五折、思考预算上限目前**不会自动生效**，只有报告里的一句建议 |
| G2 | `plan` 是**单次请求**的计划 | premium 两阶段只合并了 usage，没有合并 plan | 别把 premium 的 `plan` 当成两阶段的完整账单 |
| G3 | 部分厂商目录价缺失 | mimo/minimax/豆包/阶跃 走混合价或单位价回落，`notes` 里有标记 | 这些档位的**金额绝对值**不可信，比例结论仍然成立 |
| G4 | 未做真实联网验证 | 全部断言离线；没有向任何真实厂商发过付费请求 | 缓存是否真的命中、TTL 是否真的按文档生效，**未经验证** |
| G5 | GUI / gateway 流式路径 | GUI 直连 Gateway 的转发路径与 `HttpTransport` 是两条路 | GUI 流式请求可能**不经过**新的成本规划器 |
| G6 | `CacheWarmth` 是**进程内**协调 | 有锁 + 原子替换；多进程不共享事务 | 多进程部署下温暖度可能互相看不到 |
| G7 | `cacheControl=off` 只能阻止 Forge 主动打标记 | 无法关闭厂商自身的隐式缓存 | 文档口径要写清楚，别承诺「关掉缓存」 |

---

## 4. 本轮（2026-10-07 上午）实际改了什么

按「谁的测试抓出来的」分组，全部有断言覆盖：

**我（AutoClaw）修的：**
- `profile_for` 改为**主机名 + 域名边界**匹配：`api.openai.com.attacker.example` 与 `other.example/api.openai.com/v1` 都不再被误判为 OpenAI（原实现对整条 URL 做子串匹配，是真实的判定漏洞）。
- `shape_bailian` 改为标记**稳定的 system 消息**（原来标最后一条 = 每轮都变的用户输入 → 每次都写新缓存、永不命中，比不开缓存还贵）。
- `shape_anthropic` 去掉「工具数 > 32 就不打标记」的上限（真实 agent 工具很多，会整体丢掉工具层缓存）。
- `normalize_usage` 把 `inf / nan / 负数` 一律丢成 0；传输层不再回读畸形原字段（`int(inf)` 会抛 OverflowError）。
- 缓存成本模型拆成「前缀 vs 尾部」；warm 路径的当前调用计入一次读。
- `context_plan.adjust_budget` 补上字符↔token 换算；`from_config` 读顶层键并尊重 `disabled`。
- `CacheWarmth`：加锁、原子保存、容忍损坏记录（含非数字时间戳）、**拒绝未来时间戳**（否则负年龄会变成一条永不过期的假记录）。
- `_remember_warmth` 接受可选与 dict 形式的 usage。
- `pricing` 补连字符型号（`claude-sonnet-5-5` 等）并新增 `index_price` 三级回落——这是「查不到价就放弃缓存决策」的根因修复。
- `cli._primary_profile` 改为询问 `SmartRouter._order(None)`，保证上下文阶梯与**实际首发档**一致。
- MiMo 口径更新：命中 ¥0.025 / 未命中 ¥3 → **倍率 0.83%**，比 DeepSeek 的 2% 还深（依据：MiMo-V2.5-Pro 官方定价，多方报道 V2.6 沿用；见 §5）。

**采纳了 ChatGPT 的设计（其测试先写出契约，我按契约实现）：**
- `Completion.plan` 透传（我覆盖掉了，已恢复）。
- 请求不可变性、wire 约束（Anthropic 标记不得上 openai wire）、`cacheControl` 开关接入规划。
- 指纹纳入模型/端点/凭据/结构化非文本内容。
- 向导保存字面量 key（进程重启后仍可用）。

---

## 5. 联网核实的厂商数据（2026-10-07）

| 厂商 | 结论 | 依据 |
|---|---|---|
| 小米 MiMo | **有缓存定价**：命中 ¥0.025、未命中 ¥3、输出 ¥6 / 百万 token → 倍率 **0.83%**；V2.6 沿用 V2.5 定价 | MiMo 官方 Token Plan 页（官方站确认型号与低谷 0.8× 规则）；多家媒体（腾讯新闻/搜狐/cnblogs，9/22–9/27）报道 V2.6 沿用 V2.5 API 定价 |
| 火山方舟（豆包） | **有缓存命中计费项**，但 `doubao-seed-evolving` 自身倍率与最小前缀**未公开** → 仍不代填（豆包 2.1 Pro 的已公布比率为 20%） | 第三方定价监测站记录到该行存在（2026-09-22）；官方文档页需 JS，未取到 |
| MiniMax / 阶跃 | 未取得官方缓存口径 → 维持保守（不假设命中） | 多轮检索未获官方文档 |

> 口径纪律：**只有能归因到该型号**的数字才写进画像表。跨型号套用（把 2.1 Pro 的 20% 套给 seed-evolving）等于编造折扣，不做。

---

## 6. 验证命令（改完必须全绿）

```bash
python -m forge.test_model_adaptation     # 30 项 —— 接口契约
python -m forge.test_adapters             # 56 项 —— 适配器与缓存连续性
python -m forge.test_vendors              # 77 项 —— 厂商画像与计价
python -m forge.cli selftest              # 569 项 —— 全量回归
python -m forge.cli smoke --dry-run
python -m unittest discover -s tests      # 12 项 —— 含旧配置兼容入口
```

离线、无网络、无付费调用。任何一项变红都说明契约被打破。
