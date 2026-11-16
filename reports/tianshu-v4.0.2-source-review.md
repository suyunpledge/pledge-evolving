# 天枢 v4.0.2：公开源码核验与 Forge 取舍

> 后续已取得并核验实际 4.0.2 Windows 发布包，发现其中有公开仓未同步的新运行时实现，见 [发布产物研究](tianshu-v4.0.2-artifact-review.md)。本文“源码无新增”的结论仅限所核验的公开 Git 树。

核验时间：2026-11-04。对象：[huiliyi37/Tianshu-harness](https://github.com/huiliyi37/Tianshu-harness)，指定发布版 v4.0.2。对照先前调研的 v3.37.3 / `f04665c5` 和 Forge 当前工作树（HEAD `db951939`，含现有未提交适配改动）。

## 1. 结论：发布版已到 4.0.2，公开内核源码没有相应新增

GitHub 确实存在 [v4.0.2 发布版](https://github.com/huiliyi37/Tianshu-harness/releases/tag/v4.0.2)，发布于 `2026-11-04T12:27:50Z`。但本轮远端引用实测如下：

| 远端引用 | 指向提交 |
| --- | --- |
| main | `2d0b22e0b3554686e1e4131f8bdc568ef768e060` |
| v4.0.0 | 同上 |
| v4.0.1 | 同上 |
| v4.0.2 | 同上 |

该提交时间为 2026-10-31，提交说明为 `sync: from dev repo (v3.37.4 stable rev45)`；[package.json:3](https://github.com/huiliyi37/Tianshu-harness/blob/2d0b22e0b3554686e1e4131f8bdc568ef768e060/package.json#L3) 的版本仍为 **3.37.4**。npm 注册表当时的 latest 也是 3.37.4，查询 4.0.2 返回 version not found。

与上轮基准 `f04665c5c3d2df9e3edae4b3f6bab94d7a0ca25d` 做 Git 对象比较：

| 子树 | 是否逐字相同 |
| --- | --- |
| `src/` | 是，tree `bec39682fd694eece2625c1592d76e39e6596c86` |
| `scripts/` | 是 |
| `prompts/` | 是 |
| `native/` | 是 |
| `vscode-extension/` | 是 |

只有 8 个文件变化：CREDITS、3.37.4 的两份发布说明、package.json/package-lock、latest.json、release-catalog.json、update-control.json。**没有新增的公开内核代码可直接作为“4.0.2 升级”移植。** 安装包所含实现与公开源码是否一致，不能由发布标签推断。

公开仓 [AGENTS.md](https://github.com/huiliyi37/Tianshu-harness/blob/2d0b22e0b3554686e1e4131f8bdc568ef768e060/AGENTS.md) 与 [EXTERNAL-PRS.md](https://github.com/huiliyi37/Tianshu-harness/blob/2d0b22e0b3554686e1e4131f8bdc568ef768e060/EXTERNAL-PRS.md) 明确说明这里是开发仓的同步投影，`desktop/`、`src/pro/` 不在公开范围。这解释了部分源码边界，但不足以断言每项 4.0 功能都位于闭源部分；其余功能只能标为“公开实现尚未核实”。

## 2. 发布说明值得关注的变化，证据仅限发布说明

| 发布版本 | 主要变化 | 对 Forge 的价值与边界 |
| --- | --- | --- |
| [4.0.0](https://github.com/huiliyi37/Tianshu-harness/releases/tag/v4.0.0) | 长对话按实际容量触发整理、原文归档与检查点；VERA 检查引用和验证声称；TPI 与双层记忆 | 方向值得比较，但本轮未取得对应新实现，不能直接声称可移植 |
| [4.0.1](https://github.com/huiliyi37/Tianshu-harness/releases/tag/v4.0.1) | Windows 主线程栈容量修复 | 针对它的原生桌面构建，不能直接套到 Forge 的 Python/Tk 程序 |
| [4.0.2](https://github.com/huiliyi37/Tianshu-harness/releases/tag/v4.0.2) | 安装器交互、浏览器帧积压、账号刷新、生成期间选择下一轮模型、余额缓存 | 可借鉴下一轮生效和事件驱动刷新等产品原则；尚无对应公开差异供逐行移植 |

特别关注 4.0.0 的命中率展示口径：说明称会单独处理压缩后的缓存重建请求，并同时展示包含它的口径。这是**产品说明**，本轮没有验证其计算实现。Forge 后续评估必须继续保留全部实际请求的 usage 和成本；若增加稳态命中率，需显式列出排除项与分母，不能用它替代总体账单口径。当前 `forge/protocol.py:23` 已有逐请求轨迹与冷启动标记，可在此基础上增加压缩事件关联。

## 3. 对现有公开代码的针对性复核

以下机制实际存在于本次检出的源码中，但它们与上轮基准相同，**不是新发现的 4.0.2 增量**。

### A. 保留 Forge 的模型种类 ID 方案，不照搬上游能力分数

上游 [`src/model/capability.ts:74`](https://github.com/huiliyi37/Tianshu-harness/blob/2d0b22e0b3554686e1e4131f8bdc568ef768e060/src/model/capability.ts#L74) 为少数具体型号设例外，其余主要通过名称含 `pro` / `flash` 来分档。toolUseReliability、jsonStability、editSuccessRate 等数值来自硬编码先验；该文件不是自动采集模型指纹的实测系统。

上游 [`src/api/provider-profile.ts:120`](https://github.com/huiliyi37/Tianshu-harness/blob/2d0b22e0b3554686e1e4131f8bdc568ef768e060/src/api/provider-profile.ts#L120) 仍优先查供应商名称。其 MiniMax 缓存档为 none，这也不适合替换本机已经采到的 MiniMax 隐式缓存证据。

Forge 当前 `forge/model_kinds.py`、`forge/vendors.py:566`、`forge/model.py:186` 已支持种类 ID、别名、家族、厂家兜底和显式网关绑定。应保留这些实现，继续让来源与证据等级可追踪，不用名称启发式或上游先验分数覆盖自己的回执。

### B. 下一步优先补实际型号的上下文容量

上游 [`src/api/provider-profile.ts:125`](https://github.com/huiliyi37/Tianshu-harness/blob/2d0b22e0b3554686e1e4131f8bdc568ef768e060/src/api/provider-profile.ts#L125) 要求调用方明确传入当前模型的 contextWindow，避免漏传后默认为 128K；[`src/bootstrap/model-resolution.ts:177`](https://github.com/huiliyi37/Tianshu-harness/blob/2d0b22e0b3554686e1e4131f8bdc568ef768e060/src/bootstrap/model-resolution.ts#L177) 将选中模型的窗口随解析结果向下传递。

Forge 当前 `forge/context_plan.py:192` 按字符预算、厂家地板与价格阶梯调整压缩；新的模型种类卡尚未提供实际模型容量。可优先增加型号/部署可覆盖的容量元数据，并为工具定义、系统提示、输出预留统一计算请求余量。厂家价格阶梯、模型硬容量和用户压缩偏好应分别处理。

验收应覆盖同一厂家大小窗口切换、不透明部署绑定、显式覆盖，以及长工具定义占满请求。容量证据不足时标为未知，避免给同一家所有型号写死同一个数字。

### C. 监督器可以补齐“同口径趋势”，避免拿单次下降当连续下降

上游 [`src/agent/score-history.ts:41`](https://github.com/huiliyi37/Tianshu-harness/blob/2d0b22e0b3554686e1e4131f8bdc568ef768e060/src/agent/score-history.ts#L41) 把相位、有效权重和缺失信号纳入 regimeKey；`:61` 的 tailAlignedScores 只取同口径、有效的连续尾窗；`:98` 在样本不足时不判下降。

Forge `forge/contrib/supervisor.py:310` 目前仍收集裸 score，并以本次低于上次判断下降。它是有意简化的首版，并非这次升级造成的回归，但相位或信号可用性改变时缺少上游那层可比性护栏。

建议作为独立小批次升级：记录任务/相位/有效信号口径，达到完整同口径窗口后才参与 score-abort；保留无工具空转的独立硬路径。测试重点是阶段切换、缺失信号、无有效样本、已提醒与未提醒，以及真实同口径持续低分。不能仅因文字说明中的“连续下降”就认为当前首版已经实现完整窗口。

### D. 缓存审计可增加实际发送消息的变换摘要

上游 [`src/api/wire-normalization.ts:16`](https://github.com/huiliyi37/Tianshu-harness/blob/2d0b22e0b3554686e1e4131f8bdc568ef768e060/src/api/wire-normalization.ts#L16) 使用无跨请求状态的确定性转换，并记录转换版本、变化消息数、长度差、首个变化消息与偏移；摘要不保留正文。

Forge `forge/cache_state.py:79` 的前缀指纹和 `forge/model.py:55` 的思考配置分区已能识别部分缓存变化，`forge/toolwire.py:210` 也已保留原协议助手字段。补充发送侧变换摘要的价值在于区分提示变化、序列化变化与后端缓存行为；它不是给现有能力指纹另起一套身份系统。优先级低于 B、C。

## 4. 本轮取舍

1. 保留现有模型种类指纹与 MiniMax/MiMo/DeepSeek 等实测适配，未用上游旧厂家表覆盖。
2. 将“实际型号容量接线”和“监督器同口径趋势”列为下一批可独立验证的小改动。
3. 发送侧变换摘要作为缓存诊断增强候选。
4. Compress2.0、VERA、TPI、Axiom 和桌面改版保留为待取得实现或做黑盒实验的研究项；本轮不按产品名重造一套系统。
5. 本轮只做研究，Forge 内核和 GUI 代码没有修改，没有安装或启动天枢安装器，也没有模型调用。

## 5. 本地交付与复核方法

- 独立源码：[source](C:/Users/匡溯昀/Documents/ChatGPT/Codex/research/tianshu-harness-v4.0.2/source)。网络 Git pack 与 codeload 传输未完成，使用本机已有 Git 对象建立独立副本，并与新查询的远端 main/v4.0.2 SHA 核验一致；没有将旧本地副本冒充新版本实现。
- 可机读审计：[source-audit.json](C:/Users/匡溯昀/Documents/ChatGPT/Codex/research/tianshu-harness-v4.0.2/evidence/source-audit.json)。包含远端引用、基准提交、子树哈希、全部变化文件、发布身份与 npm 版本。
- 原始发布元数据：同目录 `release-v4.0.0.json`、`release-v4.0.1.json`、`release-v4.0.2.json`、`release-catalog.json`、`release-notes.json`、`remote-refs.txt`。
- 离线复核：运行 [audit_snapshot.py](C:/Users/匡溯昀/Documents/ChatGPT/Codex/research/tianshu-harness-v4.0.2/audit_snapshot.py)，检查独立副本干净、提交与远端记录一致、5 个子树相同及源码差异为空。

本次验证为 Git 对象比较与针对性静态审查，未安装上游依赖、未跑上游运行时测试，因此没有把发布说明中的性能或命中率改善写成已复现实测。
