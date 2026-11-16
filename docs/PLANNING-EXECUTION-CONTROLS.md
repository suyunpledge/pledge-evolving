# 规划选择与执行权限反馈

本轮沿用桌面端和 CLI 已有的规划系统，补齐 Web 入口，并修正规划到执行、权限裁决到工具反馈的实际链路。

## 用户操作

- 桌面端：输入区域的“事前规划”可选择不规划、简要、标准、详细；菜单中的“规划模型与复审”可选择已配置的独立模型。
- Web 客户端：新增“事前规划”区域，可选择规划级别及规划模型。默认不规划；空模型选择沿用执行模型。生成方案会显示在执行流中，并保存在任务记录里。
- CLI：`python run.py run --help` 查看现有 `--planning none|low|medium|high` 和 `--planning-model PROVIDER MODEL` 参数。

关闭规划不会发起独立规划请求，也不会改变执行权限模式。简要规划只限制规划长度，不再隐式改用 provider 的 small_model。显式选择规划模型时只用于规划阶段，执行 primary 保持原选择；Web 会保留同 provider 的模型目录。设置只作用于本次 Web 任务配置，不写入用户凭据或修改全局授权。

## 方案如何进入执行

TaskPlan.context 现在明确要求执行模型按方案实施并用工具验证验收条件。完整 tasks、requirements、acceptance、design、risks 均进入当前任务上下文，并随现有固定任务机制参与压缩保护。用户原始要求、当前权限和新发现的事实优先；生成方案不能证明工作已完成，也不能扩大权限。

独立规划模型可能使用与执行模型不同的协议。原先直接转发已有 native tool 消息，可能把执行模型的工具调用结构传到另一厂商。现在将历史消息编码为显式标记的非可信观察文本，保留当前任务和规划输出契约；历史观察超过 12000 字符时明确标注并保留尾部。规划请求没有执行工具，规划阶段返回 tool_calls 会被拒绝。此边界用于规划输入，不会修改执行模型已有的工具历史。

Web 新增 `planning` 和 `planning_model` 请求字段：前者为 none/low/medium/high，后者为当前客户端模型目录中的 label，空字符串表示沿用执行模型。不合法的选择在创建任务前返回 400。缺省字段继续使用配置值。`/api/config` 增加规划配置，任务记录返回结构化 planning，实时 task_plan 事件可显示方案。

## 权限与执行效果

保留执行前、锁内及调用前的权限复核。修复 edit_config 的二次写权限裁决：若配置读取允许、write_file 审批不可用，结果现在准确标记为 approval_unavailable，而不是误报普通 policy_denied。

权限拒绝回执包含 permission_gate、policy_mode、sandbox、raw_authorization 和 execution_state=not_started；模型侧错误文本区分明确禁止和无人审批，并提示不要原样重试。真正的 ASK 返回 approval_required。没有通过修改 allow/deny 或绕过保护来提高工具成功率。新增测试检查文件没有发生副作用。

## 验证

- 核心全量：497 项，跳过 2，其余通过。
- 新增行为测试与现有 Web runtime：32 项通过。覆盖真实 HTTP 提交、两个阶段的实际 transport 模型名称、关闭规划仅一次调用、无效选项不创建任务、方案注入、native 历史转换、权限拒绝零副作用、简要规划不切小模型。
- 规划、独立模型与 token 链路定向回归：45 项通过。
- 桌面规划客户端及独立模型协议：6 项通过，覆盖 OpenAI/Anthropic、取消与凭据保护。
- Web JavaScript 通过 Node 语法检查。
- 预算检查通过：156 个 Forge 文件、48480 行。

桌面端多 DPI 窗口测试长时间未结束，已停止该测试进程，该项不计为通过；独立桌面客户端测试结果见 `reports/planning-execution-desktop-client.log`。本轮采用隔离文件、真实本地 HTTP 和脚本化模型响应验证，没有调用付费模型 API，未将这些结果当作 MiMo 的实测质量指标。

增量补丁：`reports/planning-execution.patch`；文件哈希清单：`reports/planning-execution-files.json`；原始备份位置：`reports/planning-execution-backup.txt`。补丁以本轮开始时的工作区为基准。
