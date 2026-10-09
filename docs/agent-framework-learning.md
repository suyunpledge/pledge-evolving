# Forge：Agent 框架源码研究与集成

研究日期：2026-10-09。开源项目读取官方仓库的固定提交；Kiro IDE 按公开产品文档分析，没有声称访问其私有实现。这里的 pi 指原 `badlogic/pi-mono` 的 coding agent，官方地址现重定向到 `earendil-works/pi`。

## 1. 研究依据与适合学习的优点

| 项目 | 实际读取的版本/来源 | 适合 Forge 学习的优点 | 应保留的边界 |
|---|---|---|---|
| SWE-agent | `3ea751c087f32b16e039a2233dd6eefecef325d5`；[agents.py](https://github.com/SWE-agent/SWE-agent/blob/3ea751c087f32b16e039a2233dd6eefecef325d5/sweagent/agent/agents.py)、[history_processors.py](https://github.com/SWE-agent/SWE-agent/blob/3ea751c087f32b16e039a2233dd6eefecef325d5/sweagent/agent/history_processors.py) | 针对模型的工具交互界面；格式/环境错误有不同反馈与明确重试上限；轨迹记录支持复盘；旧观察结果可以有节制地压缩 | 格式修复不等于重试已执行的写操作。不能为了恢复工具调用而放开 shell 或 Policy |
| LangGraph | `cba111d8d600a027324eba1120a22e82b7f07432`；[types.py](https://github.com/langchain-ai/langgraph/blob/cba111d8d600a027324eba1120a22e82b7f07432/libs/langgraph/langgraph/types.py)、[checkpoint/base](https://github.com/langchain-ai/langgraph/blob/cba111d8d600a027324eba1120a22e82b7f07432/libs/checkpoint/langgraph/checkpoint/base/__init__.py) | 显式状态、检查点、可观察的中断；区分同步、异步、退出时持久化；状态与执行步骤有对应关系 | 恢复前必须检查副作用。Forge 本次做执行日志与恢复诊断，没有实现整个图调度器或 exactly-once 保证 |
| pi | `f1b2e77f5b13b2a199b1052cb79c235451afe7d7`；[compaction.ts](https://github.com/earendil-works/pi/blob/f1b2e77f5b13b2a199b1052cb79c235451afe7d7/packages/coding-agent/src/core/compaction/compaction.ts)、[utils.ts](https://github.com/earendil-works/pi/blob/f1b2e77f5b13b2a199b1052cb79c235451afe7d7/packages/coding-agent/src/core/compaction/utils.ts) | 小核心、独立可组合的部件；压缩选择合法切点，保留近期上下文和文件操作线索；区分旧 usage 与编辑/压缩后的真实上下文 | [官方说明](https://github.com/earendil-works/pi#permissions--containerization)指出原生权限跟随宿主进程；不能把其插件可扩展性当作 Forge 的安全模型 |
| Kiro IDE | [Plan mode](https://kiro.dev/docs/specs/plan/)、[Feature Specs](https://kiro.dev/docs/specs/feature-specs/)，页面标注更新于 2026-08-04 | 写之前先理解需求；计划移交给执行；正式规格串起需求、设计、任务；验收条件明确可验证 | 四档强度是用户要求的 Forge 设计，并非声称 Kiro 原生拥有这四档。Forge 第一版以已有任务/附件/历史为证据，未知代码先列为检查任务；没有复制 Kiro 的完整交互问答、代码智能或审批产品流程 |
| OpenHands | 主仓库现侧重 Agent Canvas；内核读取 `OpenHands/software-agent-sdk` 的 `cd17bd89d52ef209abbfca5870631d0474ca8f66`：[stuck_detector.py](https://github.com/OpenHands/software-agent-sdk/blob/cd17bd89d52ef209abbfca5870631d0474ca8f66/openhands-sdk/openhands/sdk/conversation/stuck_detector.py)、[state.py](https://github.com/OpenHands/software-agent-sdk/blob/cd17bd89d52ef209abbfca5870631d0474ca8f66/openhands-sdk/openhands/sdk/conversation/state.py)、[agent.py](https://github.com/OpenHands/software-agent-sdk/blob/cd17bd89d52ef209abbfca5870631d0474ca8f66/openhands-sdk/openhands/sdk/agent/agent.py) | 动作与观察分离；识别相同动作/结果、动作/错误及交替循环；结构化运行状态；SDK 与客户端职责分开 | Forge 保留已有本地 Python Agent 与客户端桥，不引入另一套工具执行器。工具循环检测是停止策略，不是 OS 沙箱 |

上述四个开源快照的 `LICENSE` 均为 MIT。本次实现为 Forge 原创代码，学习机制，没有复制上游执行内核，也没有安装/执行这些仓库的代码或引入它们的依赖。

## 2. 已集成的机制

| 来源机制 | Forge 行为 | 实现 | 回归证据 |
|---|---|---|---|
| Kiro：计划后执行、规格拆分 | 规划严格校验后才进入执行；四档；无档不增加请求；规划请求无工具；错误明确停止 | `forge/planning.py`、`loop.py`、`cli.py`、GUI `forge_client.py`、`vscode_bridge.py` | 四档真实调用次数/输出上限、无工具声明、畸形与重复字段、规划故障阻止执行、GUI 消费计划、编辑器桥消费计划 |
| SWE-agent：有界格式纠正与清楚的失败结果 | 真正的坏调用最多 3 次格式尝试；一般 JSON 回复正常结束；成功写操作没有新增重试流程 | `toolwire.py`、`loop.py` | 普通 JSON 答案、坏协议停止、未知工具/权限失败 |
| LangGraph：显式状态与持久化边界 | `planning → ready → executing → completed/failed/stopped`；每轮独立 run_id；工具开始前持久化意图；完成记录与 action_id 对应 | `loop.py`、`session.py`、`execution_guard.py` | 每轮状态不串线；日志同步失败时不执行工具；未完成动作可投影查看；会话索引 v3 |
| pi：合法上下文切点与保留工作目标 | 任务与当前计划保留；压缩不拆 OpenAI/Anthropic 工具调用和结果组；工具参数计入大小；摘要保留近期片段与请求线索 | `compaction.py`、`loop.py` | 两种协议的调用/结果匹配、计划保留；现有压缩回归 |
| OpenHands：停滞检测 | 相同失败周期连续出现 3 次，或相同成功周期出现 5 次即停止；周期长度 1/2；参数或完整结果变化不算相同；同时用于文本协议、原生工具和 GUI | `execution_guard.py`、`loop.py`、GUI `_do_send` | 重复失败、交替失败、结果变化、参数变化、原生工具循环 |

## 3. 四档事前规划

| 档位 | 必须生成的内容 | 单次规划输出上限 | 额外逻辑请求 |
|---|---|---:|---:|
| 无 `none` | 无计划，直接执行 | 0 | 0 |
| 低 `low` | 简短任务清单 | 640 tokens | 最多 1 |
| 中 `medium` | 需求、任务、验收条件 | 1,400 tokens | 最多 1 |
| 高 `high` | 需求、设计、任务、验收条件、风险 | 2,400 tokens | 最多 1 |

每个数组分别最多 4/8/12 项，每项最多 400 字符，整个计划最多 16,000 字符。未知字段、重复键、空必填项、工具调用和不完整 JSON 被拒绝。这是一份可检查的行动提纲，未要求或保存模型的隐藏思维链。

“额外逻辑请求”指调用一次 router；路由器原有 fallback、premium 两阶段、MoA 可能产生多个厂商请求。每个厂商请求的 receipts 和 usage 均合并到本次核心 Agent 账本。上限是请求中的 `max_tokens`，实际厂商是否严格遵守仍由其协议决定；返回内容另有字符校验。低档使用现有 small 路由，另外两档沿用当前主路由。GUI 经当前 Gateway 选中的模型执行，不绕过其厂商适配与用量记录。

premium 的集成阶段使用最后一条任务和草稿：规划输出契约放入当前任务交接数据；执行阶段把原始目标与计划保持在同一条任务消息。回归验证两个阶段都保留契约/目标，四次实际调用用量合并，同时不把更早的私有历史追加给集成模型。

规划与任务沉思、模型 reasoning_effort、路由策略、权限模式分别控制。默认无档以兼容原有行为；子 Agent 默认不递归增加规划请求。输入是已经保护的任务、真实历史和附件；未知事实先安排读取，计划不能伪造已经完成的测试。

### 使用入口

后续新增独立规划模型及手动复审，见[使用与安全边界](planning-model-and-code-review.md)。

- 桌面聊天输入区：点击“事前规划：…”选择高/中/低/无；任务页面也有同样选择。保存到现有用户配置，结束当前执行后可修改。
- VSCode：模型、权限旁新增事前规划选择；保存在该 workspace 的扩展状态；运行时禁止改变选择。
- CLI：`python run.py run "切换模型，调整缓存，API Key 不动" --planning medium --json`。
- 配置行：`{"id":"planning","name":"planning:level","config":{"level":"medium"}}`。

计划作为无权限的 user-role 任务数据交给现有 Agent。LLM 不能通过计划字段修改 Policy、Sandbox 或 Secret resolve 能力；已有 Secret Virtualization 在规划入口和输出继续生效。GUI 规划请求位于后台线程，停止按钮能取消 HTTP 等待；失败不会悄悄退到未规划的执行路径。规划在会话/GUI 的真实执行结果中展示，不写虚构历史。

## 4. 中断与能力限制

`python run.py sessions --rebuild` 会显示最近主任务的状态。索引中的 `workflow.pending_actions` 表示已经记录开始、尚未记录完成的操作。索引使用 `unfinished`，并不据此断言进程死亡；在确认进程退出后，`workflow_status(..., assume_interrupted=True)` 才把该状态解释为 interrupted。

这是恢复证据，不会自动重放写文件、进程或网络操作。进程可能在副作用发生后、完成日志写入前崩溃，此时结果未知，需要重新检查。没有宣称 exactly-once、完整 LangGraph 图恢复、完整 Kiro 规格工作流、语义摘要无损，或真实 OS 沙箱。核心工具开始意图使用 `flush + fsync`；GUI 普通流式工具路径沿用现有 Gateway 审计，本次不把它描述为核心会话日志的同等恢复保证。

已注册 SecretValue 不得进入规划/执行请求；计划只能持有 SecretRef。重新打开独立 session 后旧 SecretRef 不具备解析权限，涉及敏感配置需重新读取安全视图。停滞检测基于完整结果摘要哈希，输出正在变化的轮询不判为停滞；稳定轮询连续达到阈值仍会停止，因此等待型工具应自身提供有界等待或明确不同的进度结果。

## 5. 测试方法

新增测试先在缺少规划实现时失败，再实现核心功能；后续补入 GUI、IPC、日志与攻击路径回归。全部测试使用离线模型、合成 Secret 或本机 HTTP stub，没有向真实厂商发付费请求。源码检查不依赖上游项目的运行环境。

- `python -m unittest forge.test_task_planning forge.test_vscode_bridge forge.test_execution_review -q`
- `python -m unittest discover -s forge -t . -p 'test_*.py' -q`
- `python -m forge.selftest`
- 在 `forge-gui`：`python run_offline_checks.py test_task_planning test_ui_ergonomics test_navigation test_forge_client test_secret_boundary`
- 在 `vscode-extension`：`npm test`

### 本次验证结果

- 原有核心 selftest：569/569 通过。
- 核心 Python unittest 全量发现：179 项通过，包含新增的 24 项规划/执行防护测试。
- 桌面 GUI 相关回归：40 项顺序运行通过，覆盖规划、导航、交互、客户端和 Secret 边界；新增规划模块有 8 项测试。
- VSCode：TypeScript 编译和 14 项测试通过，包含真实 Python IPC 与浏览器内的界面检查。
- 翻译覆盖检查通过；规划选择器在 100%、125%、150%、175%、200% 缩放与最小窗口下检查可见范围及 sidebar parent/order 保持。
- premium 两阶段的失败回归已经修复，确认规划契约、原始任务和全部调用用量保留；本机 HTTP 测试确认规划 payload 不含合成 SecretValue，且能取消等待。

上述测试集合存在重叠，不应把它们相加当作独立测试总数。未进行真实厂商付费调用，也不把离线结果视为所有厂商协议的联网验证。

交付是源码、测试、翻译和本报告；不生成新的 exe、VSIX 或快照，不发布扩展。已有他人修改的扩展发布名称与 publisher 保持原样。
