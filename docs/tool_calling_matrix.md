# 模型 Tool Calling 兼容性矩阵

> 实测日期：2026-09-20
> 测试环境：Ollama (blobs) + llama-server 0.4.0-dev (build 10819, commit 6a1a922d2)
> 精确性声明：本表区分**已验证**与**未验证**，推测项单独标注。

## 测试方法

- **Ollama 路径**：`POST /api/chat`，传 `tools` 参数，检查 `message.tool_calls`
- **llama-server 路径**：`POST /v1/chat/completions`，传 OpenAI 格式 `tools`，检查 `choices[0].message.tool_calls`
- 每模型 3 场景 × 3 轮 = 9 次调用，测完立即卸载释放显存
- 流式/畸形输入另设 15 项鲁棒性测试

---

## 结论表（三档分级）

### 档位 1 · 已验证（双运行时结构化 tool_calls）

| 模型 | 显存 | Ollama | llama-server | 证据 |
|---|---|---|---|---|
| **llama3.1:8b** | 4.9G | ✅ 9/9 | ✅ 3/3 | `finish_reason=tool_calls` |
| **qwen3:8b** | 5.2G | ✅ 9/9 | ✅ 3/3 | `finish_reason=tool_calls` |

**含义**：模型权重本身支持 tool calling，不依赖 Ollama 的 chat template。

### 档位 2 · 单运行时验证（Ollama 通过，llama-server 未验证）

| 模型 | 显存 | Ollama | llama-server | 说明 |
|---|---|---|---|---|
| **qwen3.5:9b** | 6.6G | ✅ 9/9 | ❌ **版本不兼容** | 见下方归因 |

**llama-server 失败根因（已抓 stderr，非推测）**：

```
E llama_model_load: error loading model hyperparameters:
  key qwen35.rope.dimension_sections has wrong array length; expected 4, got 3
E llama_model_load_from_file_impl: failed to load model
```

**归因**：llama-server 0.4.0-dev (build 10819) 未合入 qwen3.5 架构支持。GGUF 元数据中 `rope.dimension_sections` 为 3 元素，旧版期望 4 元素。**不是 OOM**（CPU-only 同样失败），**不是 blob 损坏**（Ollama 正常加载），是 **llama.cpp 版本落后于模型架构**。

**修复路径**：升级 llama.cpp 到支持 qwen3.5 的版本，或从 HF 拉取对应版本的 GGUF。

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

**推断的依据链**（需注意这是间接证据）：

1. Ollama 的 `capabilities` 字段来自模型 **manifest**
2. manifest 由**模型发布者或 Ollama 团队**填写
3. 对 gemma2：Ollama 团队根据 Google 官方发布说明判断
4. 对 deepseek-coder / yi：发布者自己标注

**这意味着**：如果发布者标注错误，Ollama 会误报。所以"Ollama 说不支持"是**强证据但不是绝对证据**。

**与 llama-server 的交叉验证**：这几个模型在 llama-server 默认 template 下也不调工具，与 Ollama 的判断一致——但**默认 template 未必是 tool-aware 的**，所以严格说仍未验证「正确 template 下是否可能支持」。

---

## 关于 Gemma 2 的 tool calling（表述精度修正）

**当前可说的**：

- 截至 2026-09-20，**未在公开渠道找到** Gemma 2 的 tool-aware Jinja template
- Google 官方 Gemma 2 发布说明**未提及** tool calling 训练
- llama-server 内置 `gemma` 模板是给原版 gemma 的（**不适用 gemma2**，套用会输出混乱，如生成 Python 代码而非工具调用）
- Gemma 2 在 Ollama 注册表中被标记为 `does not support tools`

**不能说的**：

- ❌ "Gemma 2 无法调工具"（否定命题未证）
- ❌ "Gemma 2 试过正确 template 后失败"（从未找到正确 template）

**社区存在基于 Gemma 2 的 function calling 微调版**（如 Hermes-2-Pro 系列、NuExtract 等），但**这些不是原始 Gemma 2**，不改变上述结论。如果未来有人为原始 Gemma 2 找到/发布 tool-aware template，本结论需重新验证。

---

## 流式与畸形输入的鲁棒性

adapter 对以下场景全部不崩（15 项测试）：

| 类别 | 场景 | 行为 |
|---|---|---|
| **畸形输入** | 截断 JSON、未闭合标签、空参数、无效 JSON、嵌套大括号、乱码字节 | 返回空列表或 `_raw` 兜底 |
| **流式截断** | `<tools>{"name": "x", "arguments": {"ci`（中途断开） | 返回 0 tags，不崩 |
| | 只有开标签 `<tools>` | 返回空列表 |
| | 字节级截断（UTF-8 多字节中途） | 容错解码，不崩 |
| | 重复开标签（流式重传） | 正常提取 |
| | 多 chunk 累积后完整 | 正常解析 |

**流式场景的注意事项**：adapter 是**无状态**的——每次调用处理当前收到的完整文本。流式实现需要在应用层累积 chunk，在收到 `</tools>` 或流结束时才调用解析。中途调用会返回空结果（这是设计行为，不是 bug）。

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
- 这样 4.9 + 0.3 = 5.2G，留 2.8G 给 KV 和系统，比 qwen3:8b 方案宽裕

### Ollama 配置

```
OLLAMA_KV_CACHE_TYPE=q8_0   # KV 量化到 q8_0，省显存
OLLAMA_NUM_PARALLEL=1        # 禁止并发，防止 OOM
```

---

## 未能闭环的项（诚实清单）

1. **gemma2 在正确 tool-aware template 下是否支持**——未找到该 template，无法验证
2. **qwen3.5 在 llama-server 下是否原生支持**——版本不兼容，需升级 llama.cpp 或换 GGUF
3. **Ollama manifest 的 capabilities 是否准确**——发布者可能标注错误，无法独立验证
4. **其他未测模型**（qwen3:14b / qwen3:30b / gpt-oss:20b）——显存不足，未测

## 测试脚本

- `tests/test_malformed.py` — 7 项畸形输入测试
- `tests/test_streaming_truncation.py` — 8 项流式截断测试
- `.openclaw/tmp/batch_tool_test.py` — Ollama 批量测试
- `.openclaw/tmp/llama_crossval.py` — llama-server 交叉验证
- `.openclaw/tmp/qwen35_stderr.py` — qwen3.5 失败根因诊断
