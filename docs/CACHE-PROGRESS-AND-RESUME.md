# 缓存优化：压缩进展、固定头部及传输中断续接

2026-11-08，接续 Seed / MiMo 工具专项。目标是减少丢失进展后的重复调用，并保留可复用的实际请求前缀。使用现有密钥、模型和思考配置，未改用户全局配置；所有 API 只处理隔离夹具。

## 实现

### 有界观测跨压缩保留

旧摘要只取最近两份工具结果，再次压缩会忽略旧摘要里的进展。写入/检查之后的若干次读文件可能覆盖这些线索，模型随后重新读取或重复检查。

`compaction_notes` 从实际原生调用 ID 配对出工具名称、目标和首尾摘录，跨后续压缩保留至多 6 条观测。优先淘汰重复读取，保留近期写入、检查及其他工具最后的游标。观测数组上限 740 字符，控制字符较多的单条也会缩减；此外沿用至多 4 个真实留存句柄。没有把工具参数或调用意图当成已执行结果。

新的写入尝试会移除旧检查记录；后来的检查会替换同名旧记录。摘要始终标记 unverified、earlier results may be stale，不作为完成验收或权限授予依据。原始上下文仍先归档，保留的原生工具组及加密/推理重放字段不截断。OpenAI 和 Anthropic 两种工具格式均有反复压缩和磁盘续接回归。

### 工作区路径移出 system 固定头部

旧 system 第一句含工作区绝对路径，切换 worktree 或临时项目会很早打断共同前缀。路径现在位于运行环境消息，system 提示保持相同；会话 guard 独立绑定解析后的绝对工作区，仍不能跨目录复用私有工具历史。

`reports/cache-progress-prefix-proof.json` 用真实 Agent 投影两套工作区：序列化消息的共同前缀由 128 增至 1178 字符，完整共同前缀消息由 0 增至 1，工具声明不变；两个工作区 guard 不同。这里只证明请求结构，不把字符数当成服务端命中 token。

### 可重试模型传输错误保留快照

旧调用链在模型传输错误后清除已完成的工具历史，下一轮恢复只能从普通聊天重新开始。现在复用步数暂停的检查点机制：可重试的模型请求失败时，仅提交配对完整、结果已知的交换，并保持 error / 未完成状态。错误文案不伪装成模型答案写入原生历史；新 Agent 续接仍核对用户历史、工作区、权限、规则和提供者 guard。

不可重试请求拒绝不保存；工具结果未知、取消和协议错误继续使用原来的停止路径。没有重放宿主动作、增加生产重试次数或把一次工具成功当作任务完成。

## API 对照

原始报告保留全部失败；缺失 usage 的请求不按零 token 计费，缺失缓存字段不按零命中计算。所有输出 token 都已包含推理，不额外相加。

### 两轮账单任务

复用 `scripts/verify_tool_challenge.py`：11 项金额/订单测试，追加兼容币种过滤后共 14 项；12,000 字符压缩预算、18 步上限、low 规划均相同。A 为本轮修改前的 loop/compaction；B 为固定头部与跨压缩观测版本。B 启动后才实现传输错误检查点，因此此组不宣称覆盖该后续分支。

| MiMo | 实际验收 | 工具动作 | 压缩 | 输入 | 输出含推理 | 总 token | 已观测缓存率 |
|---|---|---:|---:|---:|---:|---:|---:|
| A `cache-progress-mimo-a2.json` | 两轮通过 | 23 | 5 | 71,295 | 4,984 | 76,279 | 77.45% |
| B `cache-progress-mimo-final-b.json` | 两轮通过 | 19 | 4 | 66,225 | 5,993 | 72,218 | 71.27% |

这个样本总 token 下降 5.32%、输入下降 7.11%，输出反而增加；缓存率下降，不能宣称整组命中率提高。A 已观测缓存分母为 65,198，缓存 50,496；B 为 62,409，缓存 44,480。两组均有两条成功响应缺少缓存统计，另各有 2 / 1 次无 usage 的传输失败。两组都通过独立测试、先复现失败及只改允许文件的检查，并在第二轮重建 Agent 后恢复私有前缀。

`cache-progress-mimo-b.json` 是观测摘要初稿，TLS 中断而未完成；不用于成功收益比较。第一次 A 探针 `cache-progress-mimo-a.json` 在采集早停诊断时遇到 None 消息列表，已修复脚本并增加规划停止回归，原记录保留。

Seed 的 `cache-progress-seed-a.json`、`cache-progress-seed-final-b.json` 都在首轮检查后遇到真实读取超时，尚未改文件。均未完成，不能用它们证明账单任务的改进或缓存节省；后续另以短任务验证中断续接链路。

### 注入断线与磁盘续接

`scripts/verify_transport_resume.py` 让模型实际读取未提供在提示中的两个数值，宿主在读取之后、下一次 HTTP 发送之前注入传输错误，再销毁并重建 Agent，要求实际调用 calculator 得到 423。只有这次暂停是注入的；前后的 API 回执和工具执行均真实。注入尝试不发送 HTTP，不算作付费请求；实际网络失败单列。

两组保持相同文件、工具、权限、模型和思考设置。MiMo 按 ABBA 顺序，Seed 按 AB 顺序；原始记录包含各轮实际读取次数、恢复前缀与首次续接 wire 的逐字段一致性、原生配对及完整 usage。

| 模型 / 组 | 成功任务数 | 每次实际读取 | 有效 usage 响应 | 输入 | 输出 | 总 token | 已观测缓存率 |
|---|---:|---:|---:|---:|---:|---:|---:|
| MiMo A | 2 | 2 | 8 | 5,266 | 442 | 5,708 | 59.64% |
| MiMo B | 2 | 1 | 6 | 4,032 | 277 | 4,309 | 80.99% |
| Seed A | 1 | 2 | 4 | 4,300 | 186 | 4,486 | 0% |
| Seed B | 1 | 1 | 3 | 3,145 | 132 | 3,277 | 0% |

MiMo ABBA 的任务均正确，B 总 token 少 24.51%、输入少 23.43%，主要可验证收益是少一次重新读取和模型往返。缓存为 A 2,432 / 4,078、B 2,816 / 3,477；A/B 各有 2 / 1 条成功响应缺少缓存统计。末尾 A 单次已观测命中率为 90.23%，高于两个 B 单次的 78.05% / 83.16%，不能把聚合提升解释为每次都提高。

Seed 两次任务也正确，B 总 token 少 26.95%，但所有已观测缓存均为零，不宣称 Seed 服务端缓存率改善。两组各有一次实际网络失败未返回 usage，消费未知。原始记录为 `cache-progress-mimo-resume-abba.json` 和 `cache-progress-seed-resume-ab.json`。

最初的前缀诊断直接把宿主快照与 HTTP 消息比较，全部误报不相等：OpenAI wire 会把空 assistant content 从空串规范化成 null。已修正诊断，对保存快照使用同一 `sanitize_messages` 投影再比较，并把改进组的严格前缀匹配加入验收。新增离线测试通过真实 HttpTransport 和两种模型配置，验证失败请求与续接请求逐字段保留相同前缀、JSON 参数空白、reasoning_content 和 opaque 加密字段。

修正后两家各新增一组 `cache-progress-*-resume-wire-check.json`，均通过：宿主检查点与中断前请求一致、规范化后的完整前缀与首次续接 HTTP 请求一致、原生配对有效、只读取一次并实际得到 423。Seed 还记录了 3 个已返回加密字段的精确回放匹配（可跨请求重复计数）。这两组的服务端缓存回执仍全部为零，因此**不能从私有前缀复用或一次 ABBA 推断稳定的服务端命中收益**；本轮没有把零命中复核排除掉。

本轮 10 组付费实验共 98 次 HTTP 尝试、83 条有效 usage：观测输入 223,139、输出 17,532，合计 240,671 token。15 次传输失败没有 usage，费用未知；另有 8 条成功响应缺少缓存统计。8 个注入断线场景共 16 次宿主故障注入，没有发送 HTTP，不计入这 98 次尝试。

`reports/cache-progress-audit.py` 将 7 个账单阶段导出的模型源码放入全新目录，重新写入原始宿主测试后独立执行，全部与各自报告的通过/失败一致，并核对允许修改范围和写入前失败复现；结果见 `reports/cache-progress-evidence.json`。

## 本地回归与复跑

最终定向和入口回归 128 项通过，包含压缩观测、两种协议续接、跨工作区隔离、不可重试拒绝、真实 HTTP 规范化、客户端和网关；核心回归 497 项通过（2 项跳过）。测试集合有交集，不相加宣称覆盖数量。预算检查通过：157 文件、48,634 行。日志见 `reports/cache-progress-final-validation.log`、`reports/cache-progress-complete-core.log`。

```powershell
$baseline = Get-Content reports/cache-progress-backup.txt
python scripts/verify_tool_challenge.py --live --vendor mimo --feedback-views --baseline-dir $baseline --output reports/cache-a-new.json
python scripts/verify_tool_challenge.py --live --vendor mimo --feedback-views --output reports/cache-b-new.json
python scripts/verify_transport_resume.py --live --vendor mimo --baseline-dir $baseline --strategies baseline improved improved baseline --output reports/resume-new.json
```

输出路径必须不存在，`--live` 必填；默认每个请求最多重试一次，受整组请求上限限制。本轮增量补丁为 `reports/cache-progress.patch`，文件及证据哈希为 `reports/cache-progress-files.json`，基线目录记录在 `reports/cache-progress-backup.txt`。
