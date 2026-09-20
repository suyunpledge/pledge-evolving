# 模型 Tool Calling 兼容性矩阵

> 实测日期：2026-09-20
> 测试环境：Ollama (blobs) + llama-server 0.4.0-dev (build 10819, commit `6a1a922d2`, 2026-09-05)
> 精确性声明：本表区分**已验证**、**环境受限未验证**、**未做实验**三类。

## 环境（复现基线）

| 项 | 值 |
|---|---|
| llama.cpp | 0.4.0-dev, build 10819, commit `6a1a922d2`（2026-09-05） |
| Ollama 模型存储 | `D:\ollama\blobs\`（GGUF 格式，SHA256 命名） |
| GPU | 8GB（单卡） |
| 测试后显存状态 | 干净；常驻进程仅 HR 服务的 `bge-m3`（1.2G） |
| 测试后残留进程 | 无（每个模型测完立即卸载） |

**复现方法**：`nvidia-smi` 应看到 ≈1.2G 占用（bge-m3）；若看到额外进程说明有模型未卸载。

---

## 结论表（四档分级）

### 档位 1 · 已验证（双运行时结构化 tool_calls）

| 模型 | 显存 | Ollama | llama-server | 证据 |
|---|---|---|---|---|
| **llama3.1:8b** | 4.9G | ✅ 9/9 | ✅ 3/3 | `finish_reason=tool_calls` |
| **qwen3:8b** | 5.2G | ✅ 9/9 | ✅ 3/3 | `finish_reason=tool_calls` |

**含义**：模型权重本身支持 tool calling，不依赖 Ollama 的 chat template。

### 档位 2 · 模型能力已证，另一运行时因环境问题未验证

| 模型 | 显存 | Ollama | llama-server | 说明 |
|---|---|---|---|---|
| **qwen3.5:9b** | 6.6G | ✅ 9/9 | ⚠️ **GGUF 版本不匹配** | 见下 |

**定义说明**：本档 ≠ "没测"。模型能力（tool calling）**已由 Ollama 路径证实**；未验证的是"llama-server 路径能否加载"，属**环境问题**。

**根因（已抓 stderr + 读取 GGUF 元数据，非推测）**：

llama-server 报错：
```
E llama_model_load: error loading model hyperparameters:
  key qwen35.rope.dimension_sections has wrong array length; expected 4, got 3
```

GGUF 元数据（直接读取 blob 得到）：
```
general.architecture           = qwen35
qwen35.mrope_sections          = [11, 11, 10]   ← 3 元素
qwen35.rope.dimension_sections = [11, 11, 10]   ← 3 元素
qwen35.rope.dimension_count    = 64
qwen35.context_length          = 262144
qwen35.vision.block_count      = 27             ← 含视觉分量
```

**归因**：该 GGUF 使用 **3 元素** mrope sections，而 build 10819 的 qwen35 代码期望 **4 元素**。是 **GGUF 元数据版本与 llama.cpp 版本不配对**——既不是 OOM（CPU-only 同样失败），也不是文件损坏（Ollama 正常加载）。

**如何关闭这个缺口（可操作）**：

| 路径 | 操作 | 验证 |
|---|---|---|
| **升级** | 构建/下载支持 3 元素 mrope 的 llama.cpp 版本 | 重跑 `.openclaw/tmp/llama_crossval.py`，期望 `finish_reason=tool_calls` |
| **降级/换文件** | 从 HuggingFace 拉取与 build 10819 配对的 qwen3.5 GGUF（4 元素版本） | 同上 |

> ⚠️ **未确认**：具体哪个 llama.cpp commit 支持 3 元素 mrope。已查到 qwen3.5 架构**最初**合入于 PR #19468（2026-02-10 关闭），但 3 元素 mrope 的变更点未定位。文档作者截至 2026-09-20 未在公开渠道查到该变更记录。

### 档位 3 · 需 adapter 兜底（输出文本格式工具调用）

| 模型 | 显存 | Ollama | llama-server | 输出格式 |
|---|---|---|---|---|
| **qwen2.5-coder:7b** | 4.7G | 文本 JSON | `<tools>` XML | adapter `parse_tool_call_tags` 可解析 |

**说明**：模型知道要调工具，但不走原生通道。原始训练格式是 Hermes 系 XML/JSON-in-text，运行时决定是否翻译成结构化 tool_calls。

### 档位 4 · 当前配置下不可用

| 模型 | 显存 | Ollama | llama-server | 性质 |
|---|---|---|---|---|
| **glm4:9b** | 5.5G | TEXT | TEXT | 权重未对齐 tool calling |
| **gemma2:9b** | 5.4G | 400 | TEXT | 见下方说明 |
| **deepseek-coder:6.7b** | 3.8G | 400 | TEXT | 权重未对齐 |
| **deepseek-r1:7b** | 4.7G | TEXT | — | 推理模型，设计上不做 tool call |
| **yi:6b-200k** | 3.5G | 400 | 乱码 | 权重未对齐 |

---

## Ollama 400 错误的归因

```json
{"error":"registry.ollama.ai/library/gemma2:9b does not support tools"}
```

**依据链**：Ollama 的 `capabilities` 来自模型 **manifest**，manifest 由**发布者或 Ollama 团队**填写。对 gemma2 是 Ollama 团队按 Google 官方说明判断；对 deepseek-coder/yi 是发布者自标。

**这意味着**：发布者若标错，Ollama 会误报。"Ollama 说不支持"是**强证据但不是绝对证据**。

---

## 关于 Gemma 2 的 tool calling

**可说的**：

- 截至 **2026-09-20**，**未在以下范围找到** tool-aware Jinja template：
  - HuggingFace（关键词："gemma2 tool calling"、"gemma-2-9b-it function calling"）
  - Ollama registry 的 gemma2 变体
  - llama.cpp issue/讨论区（"gemma2 tool template"）
- Google 官方 Gemma 2 说明**未提及** tool calling 训练
- llama-server 内置 `gemma` 模板是给**原版 gemma** 的，套在 gemma2 上会输出混乱（生成 Python 代码）
- Ollama 注册表标记 gemma2 为 `does not support tools`

**不能说的**：
- ❌ "Gemma 2 无法调工具"（否定命题未证）
- ❌ "Gemma 2 试过正确 template 后失败"（从未找到正确 template）

**社区微调版**：Hermes-2-Pro、NuExtract 等基于 Gemma 2 做了 function calling 微调，但**非原始 Gemma 2**。

---

## 流式处理的设计选择

**当前方案：adapter 保持无状态，流式交给调用方。**

**理由**：
1. **职责单一**——adapter 输入"一段文本"、输出"tool calls"，不持有跨调用状态，便于单测（15 项测试无副作用）
2. **不预设流式协议**——Ollama `/api/chat`、llama-server SSE、Anthropic event stream 格式各异
3. **调用方更清楚何时算"完整"**——`</tools>` 出现？流结束？超时？依赖具体协议语义

**边界**：应用层累积 chunk；收到 `</tools>` 或流结束时才调 `parse_tool_call_tags`；中途调用返回空是**设计行为**。

**切换到有状态方案的可判定触发条件**（满足任一）：
- 有 **≥2 个**不同调用方报告"应用层累积 chunk"是显著负担
- 需支持的**服务端流式协议超过 3 种**
- 出现**必须**在 adapter 内做流式解析、无法上移到调用方的硬约束

---

## 鲁棒性测试

| 类别 | 场景数 | 行为 | 测试文件 |
|---|---|---|---|
| 畸形输入 | 7 | 截断 JSON / 未闭合标签 / 空参数 / 无效 JSON / 嵌套大括号 / 乱码字节 / 部分 JSON → 空列表或 `_raw` 兜底 | `tests/test_malformed.py` |
| 流式截断 | 8 | 中途断开 / 只有开标签 / 字节级截断 / 重复标签 / 多 chunk 累积 → 容错不崩 | `tests/test_streaming_truncation.py` |

---

## VRAM 使用建议（8GB 卡）

### 常驻三件套（embedding + LLM + 工具执行）

| 方案 | 组成 | 显存 | 余量 |
|---|---|---|---|
| **推荐** | llama3.1:8b (4.9G) + nomic-embed-text (0.3G) | 5.2G | 2.8G 给 KV + 系统 |
| 次选 | qwen3:8b (5.2G) + bge-m3 (1.2G) | 6.4G | 1.6G，8K ctx 够，16K 悬 |
| 不推荐 | qwen3.5:9b (6.6G) + embedding | >7.8G | **会 OOM，不可常驻** |

**并发场景**：
- LLM 侧锁 `llama3.1:8b`（4.9G）
- embedding 用 `nomic-embed-text`（274M）而非 `bge-m3`（1.2G），省 0.9G
- 工具执行进程跑 **CPU**，不占显存
- 4.9 + 0.3 = 5.2G，留 2.8G

**Ollama 配置**：
```
OLLAMA_KV_CACHE_TYPE=q8_0
OLLAMA_NUM_PARALLEL=1
```

---

## 未验证的结论（本实验无法闭环）

1. gemma2 在正确 tool-aware template 下是否支持——未找到该 template
2. qwen3.5 在 llama-server 下是否原生支持——GGUF 版本不匹配
3. Ollama manifest 的 capabilities 是否准确——发布者可能标错
4. 具体哪个 llama.cpp commit 支持 3 元素 mrope——截至 2026-09-20 未查到公开记录

## 未做的实验（按对 adapter 设计的影响排序）

| 优先级 | 实验 | 影响 |
|---|---|---|
| **P1** | Gemma 2 微调版（Hermes-2-Pro 等）的 tool calling 表现 | 可能改变档位 4 的结论 |
| **P2** | qwen2.5-coder `<tools>` XML 在真实**多轮**对话里的累积行为 | 影响 adapter 兜底逻辑的健壮性 |
| **P3** | 各模型在 **32K 上下文**下的 tool calling 稳定性 | 影响 VRAM 建议的上下文档位 |
| **P4** | qwen3:14b / qwen3:30b / gpt-oss:20b | 显存不足未测 |

---

## 测试脚本与样本来源

| 脚本 | 测试目标 | 样本来源 |
|---|---|---|
| `tests/test_malformed.py` | adapter 解析鲁棒性（非模型行为） | 手工构造，模拟实测畸形输出 |
| `tests/test_streaming_truncation.py` | adapter 流式截断鲁棒性 | 手工构造，模拟流式中断 |
| `.openclaw/tmp/batch_tool_test.py` | Ollama 批量 tool calling | 实测 9 个本地模型 |
| `.openclaw/tmp/llama_crossval.py` | llama-server 交叉验证 | 实测 3 个模型 |
| `.openclaw/tmp/qwen35_stderr.py` | qwen3.5 失败根因 | 抓取真实 stderr |
| `.openclaw/tmp/gemma2_template_test.py` | gemma2 template 对照 | 4 种 template 配置 |
| `.openclaw/tmp/read_gguf_meta.py` | GGUF 元数据读取 | 直接解析 blob 二进制 |
