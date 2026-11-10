# 天枢 v4.0.2 发布产物研究：长会话、缓存口径与模型能力

日期：2026-11-04。对象是用户指定的 [v4.0.2 发布页](https://github.com/huiliyi37/Tianshu-harness/releases/tag/v4.0.2)及其实际 Windows 发布包。本报告补充此前的 [公开源码核验](tianshu-v4.0.2-source-review.md)，不能用公开标签源码没有变化来推断发布包没有新实现。

## 1. 已取得什么

已从发布清单登记的官方 OSS 下载 `Tianshu_4.0.2_x64-setup.exe`，进行静态解包。没有安装或启动天枢，没有加载其运行时模块，没有读取用户凭据，也没有模型调用。

| 项目 | 核验结果 |
| --- | --- |
| 文件大小 | 869,483,443 字节 |
| SHA-256 | `5ea6c5b9cf1890be864381ae1ced351c1cb019ac61a7c7448472e644a7507719`，与发布清单一致 |
| 包内 `rivet-runtime/package.json` | `tianshu-harness` **4.0.2** |
| 包内运行时 JavaScript | 311 个文件，没有附带 source map |
| 实际抽取 | 356 个代码/元数据文件，共 21,071,535 字节，包含 mobile-web |
| 内置 integrity.json | product=`tianshu-desktop`、mode=`code`、fileCount=338 |
| 抽取文件核验 | 336 个被 integrity 清单覆盖的文件，摘要全部相符 |

运行时存在标识符混淆。研究保留原文件，并另建格式化、静态字符串索引解析的阅读副本；解析过程不执行 JavaScript。多个已知字符串锚点共同验证数组偏移，记录变换方法与原文件摘要。报告里的阅读副本行号只能定位该副本，不是上游 TypeScript 行号，也不是开发仓 commit 证明。

本轮在发布包发现 `compactLongHistory`、`validateCheckpoint`、`thinkingCapabilities`、`addRebuildCoverage` 等实现；在之前固定的公开源码树中，没有这些同名函数声明。因此现在可以研究实际交付的行为，仍不能声称拿到了完整原始源码或全部源码差异。

## 2. Compress2.0：实际请求预算、归档与有条件提交

核心位置：[阅读副本 chunk-7LDCKI7C.js](C:/Users/匡溯昀/Documents/ChatGPT/Codex/research/tianshu-harness-v4.0.2/inspection/chunk-7LDCKI7C.js:5889)，原始文件 SHA-256 为 `389fe72c03835eb491b4805ed1064603ab8e821e500a3300dd03f25fc8226c96`。

静态调用链确认：

1. `compactLongHistory` 只有 `required === true` 才进入改写；检查输入预算与被保护的用户消息。
2. 对原消息做稳定序列化和 SHA-256，产生 rewriteTransactionId。在异步归档、摘要和提交边界重新检查原消息摘要，原状态发生变化时拒绝继续。
3. 先归档原文并要求有效 archiveId，再尝试裁剪较旧工具结果；默认保留最近 3 组工具结果。
4. 工具裁剪只有同时满足最小回收量、目标大小和候选请求校验才会提交；否则再进入结构化摘要。
5. 摘要可以分块，并合并先前检查点；候选状态必须通过大小检查和实际请求构建后的 validateCandidate，最后才提交。
6. 事件记录改写事务、归档引用、前后摘要、前后 token 估计、阶段与拒绝原因。

源码定位：前置条件/归档/工具裁剪在阅读副本 5890–5957；摘要分块与检查点提交在 5972–6105；实际请求准备与预算校验在 7890–7972。模型、提示引擎、客户端、持久层、预算策略和会话消息都纳入了部分不变性检查。

**需要修正对“只有窗口放不下才压缩”的理解。** 普通容量触发之外，还存在 VERA horizon 路径：`veraHorizon` 仅在 longMode 且 VERA 激活时返回阈值；请求没有超出硬预算、但达到该阈值时，也能进入长会话整理。阅读副本 `:7592`、`:7925` 可核对条件。相关常量包含 0.60 的目标比例和 0.85 的提交比例；它们不是所有厂家的通用推荐参数。

**Forge 取舍：优先学习预算接线和改写事务。** 当前型号指纹应进一步接入实际上下文容量、输出预留、工具与协议开销；压缩应有归档、版本/摘要检查与失败不提交。不要把单个比例、保留 3 组或其他产品的开关原样当成 Forge 的统一默认值。

## 3. 检查点与 VERA：能追溯到原文，不把摘要直接当事实

阅读副本 `:5310` 起的结构包含 summary、coverage、requirements、decisions、pendingApprovals、nextSteps 和 facts；facts 另外带 observed/unverified/failed 状态。

- `validateCheckpoint` 检查字段集合、版本、非空内容、覆盖范围、引用归属与重复引用。
- `bindCheckpointSources` 将消息索引绑定到 archiveId，并可附带归档的行区间。
- `parseCheckpoint` 可以在启用回调时尝试丢弃部分格式错误条目，但仍要求完整覆盖和引用有效。
- **不是任何错误都可局部吞掉**：引用指向覆盖范围外或无效消息等情况，会让修复路径失败；不能把该机制概括成“出错条目全部丢弃即可”。
- 摘要接线会调用 groundCheckpoint、VERA 的 freshen/freshenPrior/extract 等步骤；也有对已修改文件的过期引用提示。

定位：[检查点校验](C:/Users/匡溯昀/Documents/ChatGPT/Codex/research/tianshu-harness-v4.0.2/inspection/chunk-7LDCKI7C.js:5325)、同文件 `:5350` 的局部修复、`:5392` 的来源绑定、`:6004` 的核对接线。

**Forge 取舍：采用小型、可追溯的检查点结构，并与现有任务契约/证据事件复用。** 原文归档、待批准事项、事实状态与引用有效性有直接价值。VERA 的完整终答复秤、返工效果，本轮没有运行模型复现；Axiom 双层记忆和 TPI 也没有完成独立调用链审查，保留为后续项。

## 4. 缓存命中率：确认存在多种分母，不能直接横比数字

已追到实际链路：

- 历史改写提交后设置 pendingCacheRebuild（核心阅读副本 `:6726`）。
- 后续主请求结算时读取并清除该标记，把 cacheRebuild 写进 usage（`:7577`、`:23172`）。
- 会话状态把重建请求单独累计到 coverage.rebuild；普通 coverage.input/read 不含它们。原始 totalUsage 仍累计请求 token。
- 最近窗口命中率过滤 cacheRebuild 请求。
- 一部分命中率还会从输入分母扣除结构性 cacheTail。

定位：[会话统计](C:/Users/匡溯昀/Documents/ChatGPT/Codex/research/tianshu-harness-v4.0.2/inspection/chunk-SU4ID36O.js:210)、同文件 `:528` 的总 usage 累计、`:535` 的命中率与 `:546` 的最近窗口；尾部规则见 [chunk-6B55S7QN.js](C:/Users/匡溯昀/Documents/ChatGPT/Codex/research/tianshu-harness-v4.0.2/readable/chunk-6B55S7QN.js:59)。

`deepseek-128` 规则按 `128 + input % 128` 估算结构性尾部，再限制到总输入和实际未命中数量以内。该规则只有上游 observation 声明对应 cacheTailRule 时才应用，不是所有 OpenAI 兼容接口的默认规则。

使用 Forge 已有回执做一次**同请求、不同口径**的计算示例：

| 指标 | 数值 |
| --- | --- |
| 来源 | `docs/vendor-adapt-2026-11-04/forge-wire-deepseek.json`，首个模型的第 2 次请求 |
| input / cached | 362 / 128 token |
| 全输入命中率 | `128 / 362 = 35.36%` |
| 按该结构规则扣除的尾部 | `min(362, 128 + 362 % 128, 362 - 128) = 234` |
| 可缓存前缀命中率 | `128 / (362 - 234) = 100%` |

这是已有短探针的口径对照，**没有产生新的缓存命中，也不是长会话性能结论**。排除重建请求还会进一步改变统计请求集合。

**Forge 取舍：保留原始全请求 usage 与成本，另外明确命名结构性前缀命中率、重建排除后的窗口指标，并披露排除量与未知字段。** 不用新指标覆盖已有总命中率；比较天枢与 Forge 时先统一请求集合、分母和缺失值处理。

## 5. 模型适配：身份之外再叠加路由能力

已确认新发布包存在官方模型/绑定能力协商，不只是一张厂家默认表。

在 [chunk-N5V5NWSE.js:657](C:/Users/匡溯昀/Documents/ChatGPT/Codex/research/tianshu-harness-v4.0.2/inspection/chunk-N5V5NWSE.js:657)：

- 校验模型可用状态、请求操作种类、流式支持与容量元数据。
- agent 工具调用、vision、thinking 等，需要模型和当前绑定/路由都声明支持。
- thinkingPolicy 与实际协议配对，未知组合不会直接当作支持。
- effortCap 的覆盖受已有上限限制；publicModel 保留模型 ID，同时记录 bindingId、contextWindow、输出上限与生效能力。

另一方面，`capabilityCardForModel` 依然保留 pro/flash 名称启发式和硬编码可靠性先验；厂家缓存表也仍有 MiniMax=none 等旧默认。因此，不能因为发布包是 4.0.2 就用整张表替换本机调用证据。

**Forge 取舍：保留刚完成的 model_kind_id 身份层，下一步增加按部署生效的能力约束。** 例如同一模型通过不同网关时，工具支持、thinking 方言、effort 档位、容量和输出上限可能不同。厂家/型号默认值、明确的路由声明与本机证据需要分层，不把路由限制改写成模型身份，也不把某官方通道的 max→high 映射推广到其他端点。

建议优先级：

1. 型号/部署的实际容量与最终请求预算接线。
2. 压缩事务、原文归档与来源检查点。
3. 缓存指标按分母与重建事件分别展示。
4. 在模型种类适配器之上增加有证据范围的路由能力覆盖。

本轮完成研究与可追溯证据整理，未修改 Forge 内核或 GUI 行为。

## 6. 本地材料

研究根目录：`C:/Users/匡溯昀/Documents/ChatGPT/Codex/research/tianshu-harness-v4.0.2/`。

- 下载身份：[artifact-download.json](C:/Users/匡溯昀/Documents/ChatGPT/Codex/research/tianshu-harness-v4.0.2/evidence/artifact-download.json)。
- 抽取文件摘要：[extracted-files.json](C:/Users/匡溯昀/Documents/ChatGPT/Codex/research/tianshu-harness-v4.0.2/evidence/extracted-files.json)。
- 格式化与摘要映射：[readable-map.json](C:/Users/匡溯昀/Documents/ChatGPT/Codex/research/tianshu-harness-v4.0.2/evidence/readable-map.json)。
- 静态字符串解析记录：[static-string-inspection.json](C:/Users/匡溯昀/Documents/ChatGPT/Codex/research/tianshu-harness-v4.0.2/evidence/static-string-inspection.json)。
- 原始抽取文件位于 `unpacked/`；显示副本位于 `readable/` 与 `inspection/`。

以上是发布产物静态研究。归档/摘要失败恢复、不同模式下实际触发频率、VERA 返工收益和缓存长期收益，仍需独立测试后才能作为 Forge 的实测结论。
