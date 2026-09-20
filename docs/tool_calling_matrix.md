# 模型 Tool Calling 兼容性矩阵

> 基于 2026-09-20 实测（Ollama + llama-server 0.4.0-dev），逐模型验证。

## 测试方法

- **Ollama 路径**：`POST /api/chat`，传 `tools` 参数，检查 `message.tool_calls` 字段
- **llama-server 路径**：`POST /v1/chat/completions`，传 OpenAI 格式 `tools`，检查 `choices[0].message.tool_calls`
- 每模型 3 场景 × 3 轮 = 9 次调用，全部即时卸载释放显存

## 结果矩阵

### ✅ 原生支持（双运行时验证）

| 模型 | 显存 | Ollama | llama-server | 结构化 tool_calls | 验证级别 |
|---|---|---|---|---|---|
| **llama3.1:8b** | 4.9G | ✅ 9/9 | ✅ 3/3 | ✅ `finish_reason=tool_calls` | **双运行时** |
| **qwen3:8b** | 5.2G | ✅ 9/9 | ✅ 3/3 | ✅ `finish_reason=tool_calls` | **双运行时** |

### ⚠️ 文本格式工具调用（adapter 可兜底解析）

| 模型 | 显存 | Ollama | llama-server | 输出格式 | adapter 覆盖 |
|---|---|---|---|---|---|
| **qwen3.5:9b** | 6.6G | ✅ 9/9 | ⚠️ 启动失败 | Ollama: 原生 tool_calls | Ollama 路径验证 |
| **qwen2.5-coder:7b** | 4.7G | ⚠️ text JSON | ⚠️ `<tools>` XML | 两种格式 adapter 都能解析 | ✅ |

### ❌ 不支持

| 模型 | 显存 | Ollama | llama-server | 失败模式 | 根因 |
|---|---|---|---|---|---|
| **glm4:9b** | 5.5G | ❌ TEXT | ❌ TEXT | 模型忽略工具参数，回纯文本 | **权重未对齐 tool calling** |
| **gemma2:9b** | 5.4G | ❌ 400 | ❌ TEXT | Ollama: "does not support tools" | **权重未对齐 tool calling** |
| **deepseek-coder:6.7b** | 3.8G | ❌ 400 | ❌ TEXT | Ollama: "does not support tools" | **权重未对齐 tool calling** |
| **deepseek-r1:7b** | 4.7G | ❌ TEXT | — | 推理模型，专注思维链 | 推理模型设计 |
| **yi:6b-200k** | 3.5G | ❌ 400 | ❌ 乱码 | Ollama: "does not support tools" | **权重未对齐 tool calling** |

## 关键发现

### 1. Ollama 不是瓶颈

llama3.1 和 qwen3 在 llama-server 下同样触发结构化 `tool_calls`（`finish_reason=tool_calls`），证明**模型权重本身支持 tool calling**。Ollama 的 chat template 是通道，不是原因。

### 2. Ollama 400 错误的根因

Ollama 返回 `"does not support tools"` 不是 chat template 缺占位符——是**Ollama 注册表级别的模型能力声明**。基于 GGUF 元数据或官方发布说明。即使给这些模型手动指定 tool-aware template，模型权重本身也不具备 tool calling 能力。

### 3. gemma2 未测试 tool-aware template 的说明

- Gemma 2 原始发布**没有 tool calling 训练**
- 社区无已知的 tool-aware Jinja template 适配 Gemma 2
- llama-server 内置 `gemma` 模板是给原版 gemma 的，套在 gemma2 上输出混乱（生成 Python 代码）
- **当前结论：gemma2 在所有已知 template 下都不支持 tool calling**
- ⚠️ **未测**：如果社区未来为 Gemma 2 发布 tool-aware 微调版，需要重新验证

### 4. qwen3.5:9b 在 llama-server 下启动失败

- **不是 OOM**：CPU-only（`-ngl 0`）+ 512 ctx 也失败（exit code 1）
- **是 blob 兼容性问题**：Ollama blob 格式与 llama-server 0.4.0-dev 不兼容
- Ollama 下正常工作（9/9），说明模型本身支持 tool calling
- **建议**：如需 llama-server 路径，从 HuggingFace 下载官方 GGUF 替代 Ollama blob

## VRAM 使用建议（8GB 卡）

| 场景 | 推荐模型 | 显存占用 | 注意事项 |
|---|---|---|---|
| **embedding + LLM + 工具** 常驻 | qwen3:8b | 5.2G + 1.2G = 6.4G | KV 用 q8_0；8K ctx 够，16K 悬 |
| **LLM + 工具** 常驻（无 embedding） | qwen3.5:9b | 6.6G | 可单跑，**不建议加 embedding** |
| **长上下文工具调用** | llama3.1:8b | 4.9G | 显存最宽裕，适合长对话 |
| **轻量任务** | qwen2.5-coder:7b | 4.7G | 需 adapter 解析文本格式工具调用 |

Ollama 推荐配置：
```
OLLAMA_KV_CACHE_TYPE=q8_0   # KV 量化到 q8_0 省显存
OLLAMA_NUM_PARALLEL=1        # 禁止并发，防止 OOM
```

## 已知限制

1. **GLM4-9B 不支持 tool calling**——无论 Ollama 还是 llama-server，模型权重不生成 tool_calls
2. **qwen2.5-coder 的文本格式工具调用**需要 adapter 的 `parse_tool_call_tags` 解析，不走原生通道
3. **400 模型（gemma2/deepseek-coder/yi）**在 Ollama 下直接拒绝请求，不可用
4. **推理模型（deepseek-r1）**不调工具——专注思维链推理
