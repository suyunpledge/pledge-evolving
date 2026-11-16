# Forge 连续对话缓存前缀优化

本轮对照本机 DeepSeek Harness，补齐 Forge 在连续对话中丢失真实请求历史的问题。优化已进入 `EditorBridge → Agent.run → ModelRouter → HttpTransport`，只需保持原有会话并继续追问。

## 对照来源与取舍

实际参考文件位于 `C:/Users/匡溯昀/Desktop/本地工作流/dsh-desktop/node_modules/@deepseek-ai/`，包版本为 `0.1.5-rc.2`。另一个 `DeepSeek harness` 目录是工作区，并不包含实现。本轮阅读的是本机安装包内可读 JavaScript：

| 本地 DSH 文件 | 采用的设计 |
| --- | --- |
| `dsh-session/lib/index.js`，`Session.deriveMessages()` | 按真实消息表面派生历史；保留既有投影；表面被替换时重建。DSH 使用冻结对象，Forge 用私有深拷贝隔离调用方修改 |
| `dsh-agent-loop/lib/index.js`，`step()` | 提示变化可追加到历史；工具变化或表面替换会开启新的请求序列 |
| 同文件中的请求上下文比较 | 缓存连续性应以实际请求上下文判断，不能仅比较用户可见聊天文本 |

Forge 先前已拆分稳定系统头与易变运行信封，也有规则增量追加和缓存覆盖统计。本轮补齐跨 `run()` 的请求延续；没有引入 DSH 依赖，也没有复制其事件存储架构。

## 原问题与实现

旧流程把上一轮重建为普通 user/assistant 文本，丢掉任务契约投影、运行信封、原生工具调用及结果。即使系统头相同，下一轮也很快失配。

新增 `forge/conversation_prefix.py`，在成功结束且通过现有验收之后保存一份宿主请求快照。追问时，只有客户端普通聊天历史的摘要完全匹配，才恢复真实消息并追加本轮上下文与任务。客户端仍不能提交 system/tool 消息。

快照保存最终请求上下文及最终 assistant 回复，保留 OpenAI 推理字段和 Anthropic thinking/signature 块。`forge/toolwire.py` 同时修复无工具最终回复丢失 `reasoning_content` 的分支。工具历史只是上下文，恢复不会执行旧工具。

连续性保护覆盖系统头、冻结记忆、权限、上下文模式、路由模型/端点/账户/行为画像、工具声明和处理器身份。旧规则的来源与版本必须仍有效；新增规则走既有追加路径。历史被编辑、截断或切换分支，旧规则被改动/删除，失败或取消，均放弃复用并走正常组装。

每个 Agent 只保存一份不超过 2 MiB 的快照。它仅存在于当前 Agent 内存中，不能用于进程重启、磁盘会话恢复或每轮重建 Agent 的入口。压缩和容量检查继续生效，因此长对话压缩仍会有意改变前缀。没有通过填充提示、隐藏工具或固定旧规则来追求命中。

## 统计语义

`forge/protocol.py` 的轨迹跳过缺失、未知或不合法的缓存回执，记录原请求下标。未知间隔会切断断点比较，首请求未知时 `cold_start` 为 `null`。

`Agent._finish()` 的 `cache_hit_rate` 统一使用 `cache_coverage().all_observed`，按已观测输入 token 加权；完全没有回执时为 `null`。usage 事件附带已观测及未知请求数。原始 token 总量和计费账本不变。兼容字段 `avg_hit_rate` 仍是已观测请求比例的算术均值，分析总体命中率应使用 coverage。

`cache_prefix_reused` 是宿主恢复快照的诊断事件，不代表厂商已命中缓存。

## 验证

专项测试覆盖跨三轮追加、工具配对、两种协议的最终推理块、深拷贝、容量上限、失效条件和统计分母。编辑器集成测试使用实际 `HttpTransport.complete()` 生成最终 HTTP JSON，并在本地截获 I/O：同一追问中，相同消息前缀由旧流程的 **1 条增加至 5 条**，工具声明保持相同。此数字是该固定场景的消息前缀长度，不是 token 数或线上命中率。

本轮回归结果：核心 413 项（2 跳过）、客户端及运行链 206 项（1 跳过），共 **616 项通过、3 项跳过、0 失败**；专项包含在总数内。框架自检 **595/595**，代码预算 146 个文件通过，Git 空白检查通过。

复验核心与自检：`python -m unittest discover -s forge -t . -q`、`python -m forge.cli selftest`。单独复验最终请求体：`python -m unittest discover -s tests -p test_cache_continuation_entries.py -q`。

验证日志：`reports/dsh-cache-core.log`、`reports/dsh-cache-runtime.log`、`reports/dsh-cache-selftest.log`、`reports/dsh-cache-entries.log`。本轮未调用付费云模型，因此没有声称实测成本下降或厂商缓存命中率提升。

本轮独立补丁为 `reports/dsh-cache.patch`，文件清单为 `reports/dsh-cache-files.json`，修改前快照路径见 `reports/dsh-cache-backup.txt`。这些补丁以本轮开始时的工作区为基线，保留已有改动。
