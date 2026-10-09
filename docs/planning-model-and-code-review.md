# 独立规划模型与手动复审

实现日期：2026-10-09。复审依据是用户本轮提供的 always / AI platform 四个文件节选：`api/code-review/route.ts`、`chat-page-shell.tsx`、`settings-dialog.tsx`、`chat-store.ts`，以及 `cloudChat`。本机找到的旧 AI platform 快照没有该模块，因此没有把旧代码误当成当前实现。

## 使用

### 桌面 GUI

1. 在聊天输入区点击“事前规划：…” → “规划模型与复审”；任务页面也有同名入口。
2. 规划模型可以选择“沿用执行模型”，也可以选择已启用 Provider 中的一个具体模型。执行模型仍由原有模型选择器/任务路由控制。
3. 选中“启用手动复审”，选择复审模型；留空沿用当前执行模型/路由。
4. 回复或任务完成后，点击该结果下面的“复审”。不会自动请求，不会自动改代码。

独立 Provider 使用原有密钥设置中的凭据，可与执行 Provider 不同；不在这份阶段设置里保存 Key，不向模型传递 API Key。新增对话或切换消息后，旧消息按钮不能审核新的回复。结果保留在对应消息上，并单独记录到 `~/.forge/sessions/reviews/`，不加入 Agent 对话历史。

设置窗口提供保存、取消和 Escape 退出；运行/复审期间不能修改阶段设置。按钮和弹窗沿用现有主题；不改变 sidebar 的 parent、order 或 dock。

### VSCode

输入区展开“规划模型与复审”，分别选择规划模型、复审模型并开启手动复审。回复生成后点击“复审”。三个阶段选择保存到该 workspace 的扩展状态，不存储凭据。Restricted Mode 不能发起请求。

审核请求只提交最新已完成结果的 `messageId`；Python 主机从自己的结果读取正文，拒绝旧 ID 和伪造的目标。停止/新建对话后迟到的审核结果不能附到新对话；审核失败保留原回复。

### CLI

```powershell
python run.py run "实现任务" --planning high --planning-model planner model-id --json
python run.py review "需要审核的代码或完整回复" --review-model reviewer model-id --json
```

`planner` / `reviewer` / `model-id` 是示例，需替换成实际已配置的 Provider ID 与模型 ID。`run` 不会自动审核；`review` 是明确的手动操作。独立模型必须已经配置；模型被删除/禁用或请求失败时明确报错，不偷偷换到另一厂商。

## 与 always 的对应关系

| always 机制 | Forge 实现 |
|---|---|
| 手动触发、默认关闭 | GUI 与 VSCode 开关；完成回复后按钮；CLI 独立 `review` 命令 |
| 独立 API 地址、Key、模型；未指定沿用对话配置 | 选择现有 Provider / Model 对，复用其既有凭据与 Secret 边界；不复制另一套明文 Key 设置 |
| 非流式 `1200` tokens、温度 `0.3` | 核心与 Gateway 请求一致；采样参数仍经过现有厂商适配器 |
| 审核代码最多 `20000` 字符 | 先检查完整文本并虚拟化 Secret，再截取前 `20000` 字符；超长明确标为局部审核，GUI 与 CLI 使用同一个阈值 |
| 结果绑定消息，切换后失效 | GUI 消息对象/会话/请求标识校验；编辑器主机验证 `messageId`，前端检查 generation 与目标消息 |
| 生成时不可审核 | UI busy 门与核心 Agent 互斥锁；重复点击不产生第二次请求 |
| 意见与建议简短明确 | 提示要求证据、严重程度、具体建议；不宣称未运行的测试通过 |

## 执行与安全边界

- 独立规划请求没有工具，不改变执行 Agent 的工具、Policy、Sandbox 或 Secret 权限。
- 独立模型使用一个已经配置的 Provider 和实际模型，不走 premium/MoA/跨厂商 fallback。留空时保留原有路由语义，因此可能包含路由器原本的多阶段请求；全部成功请求 receipts 可在 usage 中查看。
- 手动审核是工具为空的模型请求。即使模型返回原生工具调用，也拒绝该结果，绝不调用执行工具。审核文本只能作为意见展示。
- 核心与独立 GUI Provider 请求经过现有 `HttpTransport`、厂商适配、凭据最后阶段注入及 Secret 输出处理；GUI Gateway 路径继续由现有 Gateway 处理。支持独立规划/复审 Provider 的 OpenAI 与 Anthropic 协议。
- GUI 现有流式聊天执行路径仍要求 OpenAI 兼容 Provider；原生 Anthropic 执行可使用任务/核心 Agent/VSCode。此次没有扩大 GUI 的整个执行协议栈。
- 审核材料是这次完成的回复/代码，不能据此声称读取了所有仓库文件、运行了测试或证明代码没有问题。`20,000` 字符以保护后的文本计数。
- 本机桌面客户端允许用户明确配置本地模型端点。没有复制 always Web 服务的公网域名白名单/内网禁止策略，也不声称具备该 SSRF 边界；认证重定向仍被拒绝。
- HTTP 响应上限 `2 MiB`，审核反馈字符上限 `16,000`；GUI 的连接/TLS/响应等待可取消。DNS 解析仍依赖系统解析器，不宣称能够中断系统 DNS。
- 原路由在最终失败后仍等待退避的延迟已修复：没有下一次重试时不再等待。测试验证取消独立 Provider 的 header 等待可以在两秒内结束。
- 审核成功与失败都不改写原执行结果。收到但被拒绝的核心审核响应仍保留已返回用量、记入账本并写入失败审计。

## 自动化验证

新增核心测试先在没有独立阶段/复审实现时失败，然后实现；覆盖双模型顺序、不会修改执行路由、无自动审核、禁用/重复调用/错误目标、Secret、截断、原生工具拒绝、失败用量及 CLI 审计。

GUI 测试使用真实 Tk 与本机 HTTP：两种厂商协议、Key 仅在认证 header、正文 Secret 虚拟化、取消等待、后台线程、设置保存、布局保持、迟到结果隔离、五档 DPI 与退出入口。VSCode 测试覆盖选择持久化、IPC、取消后的会话隔离、审核内容 HTML 注入和窄屏布局。

所有厂商请求验证均使用离线模型或本机 stub，没有向 GPT/Claude 等真实厂商发付费请求。交付源码、测试和翻译，不打包或发布。

本次结果：核心 unittest 全量发现 **193 项通过**，原有 selftest **569/569 通过**；GUI 相关模块 **43 项通过**（规划、阶段模型、导航、客户端、Secret 与翻译），VSCode 编译及 **16 项测试通过**。测试集合有重叠，不相加作为独立测试总数。

复现命令：

```powershell
python -m unittest discover -s forge -t . -p 'test_*.py' -q
python -m forge.selftest
# 在 forge-gui 目录
python run_offline_checks.py test_phase_models test_task_planning test_navigation test_forge_client test_secret_boundary test_i18n.TranslationTests
# 在 vscode-extension 目录
npm test
```
