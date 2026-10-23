# Work / Code 工具调用稳定性优化

日期：2026-10-23。分支：`fix/work-code-tool-reliability`。基线：`83c8ddc`（包含上一轮执行链路修复）；主检出仍为 `main/8dc2944`，本次不合并。

## 参考原则与源码现状

用户明确要求参考能力特点，非直接安装或集成外部产品。

- [OpenClaw 浏览器工具文档](https://docs.openclaw.ai/tools/browser/agent-tools)：通过观察页面、使用观察对应的引用执行动作，并消费返回状态来推进操作。Forge 本轮借鉴其“调用结果决定下一步”的原则，强化 Work 调用边界和中断语义。
- [Claude Code 最佳实践](https://code.claude.com/docs/en/best-practices)：给 Agent 可执行的验证标准，结合测试、退出码与 checkpoint 确认结果。Forge 本轮强化 Code 测试执行的取消和结果证据。

现有 Work 实际入口是 `forge-gui/forge_gui_v2.py` 的聊天工具循环，调用 Gateway、受权插件代理和连接器；现有 Code 入口是 `forge/development/runtime.py`，使用核心 Agent、FileRevisionGuard、Git checkpoint、Diff 和宿主测试器。源码中的 OpenClaw 技能目录、Claude 市场声明不等于外部 Agent 或电脑控制驱动。本轮不修改插件市场、Desktop Awareness、GUI 布局和技术栈。

## Work 修复

1. `forge-gui/work_tools.py` 提供不依赖 Tk 的执行边界，复用核心 toolwire 解析/归一，派发前验证整批调用的结构与 ID；缺失 ID 可本地补齐，重复 ID 拒绝整批。
2. 损坏、截断、非对象、非有限数值参数不会进入回调。保持已知授权拒绝的 `not_started` 状态，允许模型根据明确失败重新生成参数。
3. 返回值必须有严格布尔 `ok` 和可记录 JSON；未知结果即使声称 `ok=True` 也会降为失败。异常或损坏回执按 unknown 保守处理，不自动重放。
4. Work 实际聊天循环遇到 unknown 即停止剩余批次及下一轮模型请求，先显示工具卡；界面提示“工具结果待确认”，提供查看工作区入口，避免将不确定副作用当作普通模型故障引导重试。
5. 工具轮数达到上限时按未完成处理，不再通过普通聊天成功分支归档。
6. `forge-gui/forge_client.py` 在请求前分配 run/action 身份并提交给已有 Gateway ActionExecutor。若响应丢失，GatewayError 和 Work 工具卡保留这两个身份，可携带已有 Gateway 认证查询 `GET /v1/runs/{run_id}/actions/{action_id}`；无自动重发。原 `call_tool` 签名保持不变。

### 实际丢失回执验证

专项测试启动真实本地 Gateway，执行已授权工具写入临时项目，然后在客户端收到响应后注入丢失异常。Work 收到 unknown；使用原 run/action 身份查询持久化回执可确认已成功；实际写入只执行一次。真实 Tk Work 循环另验证同批后续动作及下一轮模型都不再执行。

## Code 修复

`forge/development/testing.py`：

- 启动 Popen 前检查取消，已取消测试完全不启动程序，同时保存 `cancelled/not_started` artifact，保留 task/run 关联。
- 主进程退出 0、但后代仍持有输出管道而未收齐结果时，状态为 `unknown`，不会标为 completed。
- 统一 artifact 持久化路径，取消、失败、超时及未知状态都保留可查证据；passed/failed 数量保持未知，不猜测测试框架输出。

现有“先读再写、观察版本校验、未保存内容冲突、Diff、测试失败/超时/取消、checkpoint 恢复”已经存在，本轮保留并回归，没有重复实现。原有真实编辑→Diff→独立测试进程→Git 恢复用例在后端全套中执行。

## 验证记录

Windows / CPython 3.12，离线模型，真实 Tk、临时文件、子进程和本地 HTTP。未触碰用户正在使用的电脑界面或生产工作区，没有使用用户模型额度。

- 新专项 10/10 通过，1.533 秒。
- 后端全套：206 项，205 通过、1 跳过，69.109 秒；跳过原因是 Windows 无符号链接权限。
- 核心 `forge` unittest：229 项，228 通过、1 跳过，23.059 秒。
- GUI 交互：22/22 通过，21.035 秒；含真实 Work 聊天循环的 unknown 停机和轮数上限测试。
- GUI 运行时：12/12 通过，13.588 秒。
- ForgeGatewayClient 脚本的 5 项断言通过：健康、普通聊天、流式、不可达、错误传播。
- Forge 全量 selftest：573/574。唯一失败仍为基线已有的 `global:readme-no-hardcoded-counts`（README.zh.md 硬编码计数），不宣称全量全绿。
- `git diff --check` 通过。

修复测试环境问题：直接执行 GUI 客户端脚本缺少 repo 根路径，改用从根目录 `runpy.run_path` 执行；原 GUI CLI 转发测试绕过 `cli.main`，缺少主入口通常注入的 home，现补临时 home fixture，不改变产品 CLI。保留原失败日志。

日志均在 `C:\Users\匡溯昀`：`forge-work-code-targeted-final.log`、`forge-work-code-tests-final.log`、`forge-work-code-core.log`、`forge-work-code-interactions-final.log`、`forge-work-code-gui-runtime.log`、`forge-work-code-client-fixed-env.log`、`forge-work-code-selftest.log`。中间记录 `forge-work-code-interactions.log` 和 `forge-work-code-client.log` 包含上述 fixture/环境错误。

## 边界和限制

- 本轮优化已有工具执行链路，没有新增通用电脑点击驱动。要达到 OpenClaw 的完整观察/动作闭环，后续仍需可信浏览器或电脑操作宿主提供 snapshot、目标标识、观察版本及动作后观察；仅有技能文本不能执行这些操作。
- 已发出的网络/外部动作可能在取消后完成。结果未知必须先查询/观察实际目标，不提供任意工具 exactly-once，也不承诺即时停止任意外部进程。
- 本地连接器/插件没有 Gateway 动作日志时，工具卡中的 unknown 仍需人工检查目标；本轮未新建跨进程调度或驱动日志系统。
- 工具格式错误、未知回执和轮数上限更严格地停止任务，可能让以前“看似成功”的交互暴露失败，这是有意的兼容性变化。

修改文件：`forge-gui/work_tools.py`、`forge-gui/forge_client.py`、`forge-gui/forge_gui_v2.py`、`forge/development/testing.py`、`forge-gui/test_interactions.py`、`tests/test_work_code_reliability.py`、本报告。
