# DeepSeek 实测：规划预算、工具执行与适配

本轮使用本机已配置的官方 DeepSeek Flash / V4 Pro 凭据，发送隔离测试数据。未改写用户密钥、全局模型选择或权限配置。日期按用户时区为 2026-11-08。

## 复现与修复

原先简要规划最多输出 640 token。实际 Agent 请求返回 `finish_reason=length`，640 token 全部用于 reasoning，正文为空，任务在执行前以 planning_error 停止。证据：`reports/deepseek-planning-baseline-final.json`。另一个直接规划请求也发生截断，见 `reports/deepseek-direct-planning.json`。前面的三个连接失败记录保留，未将其当成规划质量问题。

修复采用共享规划模块：总输出上限调整为 low=2048、medium=4096、high=6144；提示词分别约束最终 JSON 目标长度为 256/640/1200 token，只要求当前级别需要的字段。总上限容纳推理，正文仍保持简短。这些是上限和提示目标，并非固定消耗或精确 token 校验。关闭规划仍不产生规划请求。

Agent、桌面独立规划客户端和桌面网关客户端都会显式拒绝 length/max_tokens 截断结果，即使截断处恰好形成合法 JSON，也不能启动执行。解析、任务指令注入和权限边界保持原有契约。

DeepSeek Chat Completions 适配新增：

- 已核实的 Flash / V4 Pro 支持 provider 的 `reasoningMode: auto|on|off`；请求显式参数优先，未知模型和未经验证的协议不猜测支持。
- 默认思考与显式 enabled/high 使用统一请求形状；有工具时补齐默认 auto，但保留 none、required 和指定函数约束。
- 按模型和模式移除服务端忽略的采样参数，避免无效参数分裂本地缓存指纹。Flash 思考模式保留 top_p，Pro 非思考模式保留 top_p。
- 原始 messages、工具声明和 reasoning_content 不修改；适配不授予执行权限。

参数语义依据 [DeepSeek 思考模式文档](https://api-docs.deepseek.com/guides/thinking_mode/)，默认自动缓存和前缀匹配规则依据 [上下文缓存文档](https://api-docs.deepseek.com/guides/kv_cache/)。本轮没有把本地指纹一致当成服务端命中保证；命中率仅使用实际 usage。

## 实际调用链结果

每个 Flash 场景均运行三轮任务，每轮重建 Agent 并从磁盘恢复会话；每轮发生一次真实压缩。第 2、3 轮记录到 cache_prefix_reused。每轮都通过真实 read_file → write_file → read_file 和独立 json_value 环境探针，校验 total、version、turn。另运行一个禁止写入的任务。

| 场景 | 成功回执 / HTTP 尝试 | 输入 token | 输出 token（含推理） | 总 token | 缓存命中 / 输入 |
|---|---:|---:|---:|---:|---:|
| Flash 默认思考，三轮任务 + 拒绝写入 | 18 / 22 | 31,449 | 2,400 | 33,849 | 17,536 / 31,449 = 55.76% |
| Flash 关闭思考，同样任务类型 | 18 / 20 | 29,674 | 1,335 | 31,009 | 16,256 / 29,674 = 54.78% |
| Pro 独立规划 → Flash 关闭思考执行 | 5 / 6 | 6,687 | 1,920 | 8,607 | 3,840 / 6,687 = 57.42% |
| Pro 工具协议：思考开/关、重复请求及回放 | 6 / 8 | 2,265 | 292 | 2,557 | 1,280 / 2,265 = 56.51% |

两种 Flash 模式均通过三轮验收和权限拒绝。关闭思考组在这个简单任务样本中总 token 少 8.39%，输出 token 少 44.38%，缓存率略低。两组使用独立临时目录并同时运行，规划与回复内容不完全相同，**不是严格同前缀 ABBA 对照，也不是代码修改前后的成本对照**；不能推广成复杂任务的固定节省比例，因此没有替用户切换默认模式。

两个拒绝场景都只调用一次 write_file，返回 deny 后终止操作；目标文件不存在，也没有重复尝试或绕过授权。停止后的模型解释不被当成任务完成证明。

独立规划场景第一条成功请求确为 deepseek-v4-pro，未携带工具；后续四条成功请求均为 deepseek-flash。Pro 规划实际输出 1691 token，其中 reasoning 1526，旧的 640 总预算无法容纳这个样本。Flash 三次简要规划在默认思考模式实际输出 584/248/537 token；关闭思考时为 161/181/191 token。

Pro 协议探针使用固定 add 工具定义和宿主提供的 42 结果，验证参数、重复调用缓存、reasoning_content 回放和最终答案；它不冒充真实文件工具执行。真实文件工具验证来自前面七个成功任务。

四组正式报告共 47 条成功 usage、56 次 HTTP 尝试。9 次传输失败没有 usage，不计入命中率分母，也不推断其实际账单。调试和基线请求另有报告：例如 `deepseek-planning-after.json` 的规划已成功，但随后一次网络超时使该任务失败，未计入通过结果。

## 证据与复跑

- `reports/deepseek-execution-flash-on.json`
- `reports/deepseek-execution-flash-off.json`
- `reports/deepseek-independent-planner.json`
- `reports/deepseek-pro-wire.json`
- `reports/deepseek-execution-evidence.json`：独立复核报告哈希、七个文件写入内容与环境探针文件 SHA-256 一致、完整读取/写入/回读步骤、两个拒绝结果、独立规划模型选择。

使用已配置凭据复跑，输出路径必须不存在，每个请求最多重试一次，整体有请求次数上限：

```powershell
python scripts/verify_deepseek_execution.py --live --scenario execution --output reports/deepseek-new-on.json
python scripts/verify_deepseek_execution.py --live --scenario execution --reasoning-mode off --output reports/deepseek-new-off.json
python scripts/verify_deepseek_execution.py --live --scenario planning --planner-model deepseek-v4-pro --reasoning-mode off --output reports/deepseek-new-phases.json
```

脚本沿用既有记录器，DeepSeek 子类去掉 MiMo 探针额外注入的 1024 输出限制，实际 Agent 执行使用生产默认输出预算。报告只保存请求形状、fixture 结果、usage 和适配信息，不保存鉴权头或密钥。后续复跑额外包含运行时的压缩缓存统计；上表使用统一的全部可观测输入口径，不依赖该新增字段。

## 本地验证

- 核心：497 项，495 通过、2 跳过。
- DeepSeek、规划入口和 Web runtime：38 项通过；覆盖原始请求不变、缓存指纹、显式参数优先、模型采样差异、网关与直连一致、真实 Agent 工具回放、截断规划不执行。
- 桌面规划客户端：6 项通过。
- 定向规划 / MiMo 兼容回归：69 项通过（与上述核心测试有重叠，不重复相加）。
- 预算检查：156 个 Forge 文件，48,515 行，通过；工作区 diff 空白检查通过。

本轮增量补丁 `reports/deepseek-execution.patch` 以开始时的工作区为基准，保留此前尚未提交的修改。文件清单和哈希见 `reports/deepseek-execution-files.json`，原文件备份路径见 `reports/deepseek-execution-backup.txt`。
