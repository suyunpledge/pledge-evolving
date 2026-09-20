# 模型 Tool Calling 兼容性矩阵

> 实测日期：2026-09-20
> 测试环境：Ollama (blobs) + llama-server 0.4.0-dev (build 10819, commit 6a1a922d2)
> 精确性声明：本表区分**已验证**、**环境受限未验证**、**未做实验**三类，推测项单独标注。

## 测试方法

- **Ollama 路径**：`POST /api/chat`，传 `tools` 参数，检查 `message.tool_calls`
- **llama-server 路径**：`POST /v1/chat/completions`，传 OpenAI 格式 `tools`，检查 `choices[0].message.tool_calls`
- 每模型 3 场景 × 3 轮 = 9 次调用，测完立即卸载释放显存
- 鲁棒性测试：15 项（畸形输入 7 + 流式截断 8）

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
| **qwen3.5:9b** | 6.6G | ✅ 9/9 | ⚠️ **环境受限** | 模型能力已证，llama.cpp 版本过旧 |

**定义说明**：本档 ≠ "没测"。qwen3.5 的**模型能力（tool calling）已由 Ollama 路径证实**；未验证的是"llama-server 路径能否加载"，属于**环境问题**而非模型能力问题。与档位 4 的区别：档位 4 是"模型在这个运行时下确实不调工具"，档位 2 是"这个运行时根本没跑起来"。

**llama-server 失败根因（已抓 stderr，非推测）**：

```
E llama_model_load: error loading model hyperparameters:
  key qwen35.rope.dimension_sections has wrong array length; expected 4, got 3
E llama_model_load_from_file_impl: failed to load model
E common_init_: failed to load model
```

**归因**：llama-server 0.4.0-dev (build 10819) 未合入 qwen3.5 架构支持。

**版本对照表**：

| 项 | 值 |
|---|---|
| 当前 llama.cpp | 0.4.0-dev, build 10819, commit `6a1a922d2` |
| 不支持的原因 | GGUF 元数据 `rope.dimension_sections` 为 3 元素，旧版代码期望 4 元素 |
| 该错误的公开记录 | 搜索结果确认此错误在旧版 llama.cpp 上已知，社区建议**从源码构建** |
| 升级路径 | ① 从源码构建最新 llama.cpp；② 或从 HF 拉取适配当前版本的 GGUF |
| 验证方法 | 升级后重跑 `.openclaw/tmp/llama_crossval.py`（把 qwen3.5 blob 路径加入 MODELS） |

> ⚠️ **未确认**：具体是哪个 llama.cpp commit 合入了 qwen3.5 架构支持。本文档未查到该 PR 编号，需在 llama.cpp 仓库 issue/PR 中确认。

### 档位 3 · 需 adapter 兜底（输出文本格式工具调用）

| 模型 | 显存 | Ollama | llama-server | 输出格式 |
|---|---|---|---|---|
| **qwen2.5-coder:7b** | 4.7G | 文本 JSON | `<tools>` XML | adapter `parse_tool_call_tags` 可解析 |

**说明**：模型知道要调工具，但不走原生通道。原始训练格式是 Hermes 系 XML/JSON-in-text，运行时决定是否翻译成结构化 tool_calls。adapter 已覆盖两种格式。

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

Ollama 返回的错误原文：

```json
{"error":"registry.ollama.ai/library/gemma2:9b does not support tools"}
```

**推断的依据链**（间接证据）：

1. Ollama 的 `capabilities` 字段来自模型 **manifest**
2. manifest 由**模型发布者或 Ollama 团队**填写
3. 对 gemma2：Ollama 团队根据 Google 官方发布说明判断
4. 对 deepseek-coder / yi：发布者自己标注

**这意味着**：如果发布者标注错误，Ollama 会误报。所以"Ollama 说不支持"是**强证据但不是绝对证据**。

**与 llama-server 的交叉验证**：这几个模型在 llama-server 默认 template 下也不调工具，与 Ollama 的判断一致——但**默认 template 未必是 tool-aware 的**，所以严格说仍未验证「正确 template 下是否可能支持」。

---

## 关于 Gemma 2 的 tool calling

**当前可说的**：

- 截至 **2026-09-20**，**未在以下范围内找到** Gemma 2 的 tool-aware Jinja template：
  - HuggingFace 模型页（搜索关键词："gemma2 tool calling"、"gemma-2-9b-it function calling"）
  - Ollama registry 中的 gemma2 变体
  - llama.cpp 讨论区/issue（搜索 "gemma2 tool template"）
- Google 官方 Gemma 2 发布说明**未提及** tool calling 训练
- llama-server 内置 `gemma` 模板是给原版 gemma 的（**不适用 gemma2**，套用会输出混乱）
- Gemma 2 在 Ollama 注册表中被标记为 `does not support tools`

**不能说的**：

- ❌ "Gemma 2 无法调工具"（否定命题未证）
- ❌ "Gemma 2 试过正确 template 后失败"（从未找到正确 template）

**社区微调版说明**：存在基于 Gemma 2 的 function calling 微调版（如 Hermes-2-Pro 系列、NuExtract 等），但**这些不是原始 Gemma 2**，不改变上述结论。如果未来有人为原始 Gemma 2 找到/发布 tool-aware template，本结论需重新验证。

---

## 流式处理的设计选择

**当前方案：adapter 保持无状态，流式交给调用方。**

**理由**：

1. **职责单一**：adapter 的输入是"一段文本"，输出是"解析出的 tool calls"。它不持有跨调用的状态，便于单测（15 项鲁棒性测试全部无副作用）。
2. **不预设流式协议**：Ollama 的 `/api/chat`、llama-server 的 SSE、Anthropic 的 event stream 格式各不相同。在 adapter 里做 buffer 管理会把这层耦合进来。
3. **调用方更清楚何时算"完整"**：`</tools>` 出现？流结束？超时？这个判断依赖具体协议的语义，adapter 无从得知。

**明确的边界**：

- 应用层（`loop.py` 或网关）负责累积 chunk
- 收到 `</tools>` 或流结束时，才把完整文本交给 `parse_tool_call_tags`
- 中途调用返回空结果（**设计行为，不是 bug**）

**未选择的方案**：提供 `StreamingToolCallParser` 类，内部维护 buffer。如果未来出现多个调用方都需要流式解析、且协议趋同，可以再抽取。当前无此需求。

---

## 鲁棒性测试

adapter 对以下场景全部不崩：

| 类别 | 场景数 | 行为 |
|---|---|---|
| **畸形输入** | 7 | 截断 JSON、未闭合标签、空参数、无效 JSON、嵌套大括号、乱码字节、部分 JSON → 返回空列表或 `_raw` 兜底 |
| **流式截断** | 8 | 中途断开、只有开标签、字节级截断、重复标签、多 chunk 累积 → 容错，不崩 |

---

## VRAM 使用建议（8GB 卡）

### 常驻三件套（embedding + LLM + 工具执行）

| 方案 | 组成 | 显存 | 余量 |
|---|---|---|---|
| **推荐** | llama3.1:8b (4.9G) + nomic-embed-text (0.3G) | 5.2G | 2.8G 给 KV + 系统 |
| 次选 | qwen3:8b (5.2G) + bge-m3 (1.2G) | 6.4G | 1.6G，8K ctx 够，16K 悬 |
| 不推荐 | qwen3.5:9b (6.6G) + embedding | >7.8G | **会 OOM，不可常驻** |

**并发场景建议**：

- LLM 侧锁 `llama3.1:8b`（4.9G），显存最宽裕
- embedding 用 `nomic-embed-text`（274M）而非 `bge-m3`（1.2G），省 0.9G
- 工具执行进程尽量在 **CPU** 上跑，不占显存
- 4.9 + 0.3 = 5.2G，留 2.8G 给 KV 和系统

### Ollama 配置

```
OLLAMA_KV_CACHE_TYPE=q8_0   # KV 量化到 q8_0，省显存
OLLAMA_NUM_PARALLEL=1        # 禁止并发，防止 OOM
```

---

## 未验证的结论（本实验无法闭环）

1. **gemma2 在正确 tool-aware template 下是否支持**——未找到该 template，无法验证
2. **qwen3.5 在 llama-server 下是否原生支持**——版本不兼容，需升级 llama.cpp 或换 GGUF
3. **Ollama manifest 的 capabilities 是否准确**——发布者可能标注错误，无法独立验证
4. **具体哪个 llama.cpp commit 合入了 qwen3.5 支持**——未查到该 PR 编号

## 未做的实验（实验设计盲区，非未验证）

1. **Gemma 2 在 Hermes-2-Pro 微调版下的 tool calling 表现**——微调版非原始 Gemma 2，但值得单独测
2. **qwen2.5-coder 的 `<tools>` XML 在真实多轮对话里的累积行为**——本次只测了单轮
3. **各模型在 32K 上下文下的 tool calling 稳定性**——本次全用 2048 ctx 省显存
4. **qwen3:14b / qwen3:30b / gpt-oss:20b**——显存不足未测

---

## 测试脚本与样本来源

| 脚本 | 测试目标 | 样本来源 |
|---|---|---|
| `tests/test_malformed.py` | adapter 解析鲁棒性（非模型行为） | 手工构造，模拟常见畸形输出 |
| `tests/test_streaming_truncation.py` | adapter 流式截断鲁棒性 | 手工构造，模拟流式中断 |
| `.openclaw/tmp/batch_tool_test.py` | Ollama 批量 tool calling | 实测 9 个本地模型 |
| `.openclaw/tmp/llama_crossval.py` | llama-server 交叉验证 | 实测 3 个模型 |
| `.openclaw/tmp/qwen35_stderr.py` | qwen3.5 失败根因诊断 | 抓取真实 stderr |
| `.openclaw/tmp/gemma2_template_test.py` | gemma2 template 对照 | 4 种 template 配置 |
