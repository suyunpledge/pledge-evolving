# Computer Use 宿主接口（第一阶段）

后续已落实 [截图视觉与 GUI 窗口选择器](computer-screenshot-vision.md)：Work Mode 提供本地窗口绑定、预览与独立视觉模型分析。以下保留原生输入宿主接口；GUI 视觉面板不默认授予此输入能力。

参考 [OpenAI Computer Use 集成流程](https://developers.openai.com/api/docs/guides/tools-computer-use-integration) 的动作后回读，以及 [OpenClaw 浏览器工具](https://docs.openclaw.ai/tools/browser/agent-tools) 的目标绑定/观察引用。本实现使用 Forge 现有工具协议，不依赖 OpenAI SDK 或 Codex 的私有运行时，不调用外部 Agent CLI。

## 实际支持

- `ComputerSession`：作用域隔离、单次观察引用、30 秒默认过期、状态指纹、逐动作宿主审批、取消/撤销检查、动作后新观察；失败不重放。
- `WindowsControlDriver`：宿主指定的 HWND 和 PID，Windows 标准 Button/Edit 控件读取、按钮消息点击、完整文本替换。支持中文，不依赖活动窗口、全局鼠标坐标或剪贴板；屏蔽密码和只读输入框，其他控件不公布可执行动作。
- `computer_observe` / `computer_act`：复用 ToolRegistry、Policy、ActionExecutor、Gateway 身份与持久化协议；READ_ONLY/Sandbox/显式 deny 仍然约束动作。没有能力对象时，工具不可执行。

## 宿主接线

仅可信宿主从用户选择的窗口记录中取得 HWND/PID，不能从模型参数构造目标。普通 GUI/CLI 启动默认不开放电脑输入控制。其他宿主可按以下接口接线，无需依赖 Agent 内部字段或修改布局框架。

```python
from forge.computer_use import ComputerSession, register_computer_tools
from forge.computer_win32 import WindowsControlDriver
from forge.gateway import GatewayConfig, serve
from forge.tools import build_builtin_registry

# selected_hwnd / selected_pid 来自宿主明确选择，approved_policy 来自现有权限流程。
driver = WindowsControlDriver(selected_hwnd, selected_pid)

def approve(target_id, action):
    # 对具体目标、动作与完整 text 展示确认；取消/关闭确认时返回 False。
    # 此回调可能在后台线程执行；GUI 通过现有 UI 消息队列交互。
    return confirm_selected_action(target_id, action)

computer = ComputerSession(driver, approve=approve)
registry = build_builtin_registry()
register_computer_tools(registry)
cfg = GatewayConfig(upstream=upstream, registry=registry,
    workspace=str(workspace), home=runtime_home, policy=approved_policy,
    gateway_token=gateway_token, computer=computer)
server = serve(cfg)
```

`GET /v1/tools` 将在能力附着时公布工具。现有 Work 模式已自动加载 Gateway 工具，沿用其 unknown 停机和工具卡显示。宿主给 Policy 显式允许只读观察时可以使用 `allow=('computer_observe',)`；这不会允许 `computer_act` 写入。Code 模式不要默认共享该 registry/capability。

其他宿主使用 `create_execution_context(..., capabilities={'computer': computer})`，经公共 `ActionExecutor` 执行，保持 run/task/action/event 身份可查询。直接调用 driver 会跳过 Policy/日志，不应用作 Agent 接口。

## 调用与结果

先调用 `computer_observe({})`，其 JSON content 包含：

```json
{"observation_id":"opaque-id","target":"host-target","revision":"state-digest",
 "captured_at":123.0,"state":{"elements":[
   {"ref":"element-id","text":"当前内容","actions":["type_text"]}]}}
```

再调用：

```json
{"observation_id":"opaque-id","ref":"element-id","action":"type_text","text":"新内容"}
```

成功返回新的观察、`verification=post_observation` 和 `changed`。`changed` 仅描述观察差异，不证明用户任务目标已经完成。`type_text` 是替换完整 Edit 值，不是键盘追加。

任一输入会作废同一 ComputerSession 其他会话持有的观察，即使界面看似没变化。每个动作请求必须使用所属受保护作用域的观察；Gateway 客户端应保持同一 `X-Forge-Secret-Scope`。既有 ForgeGatewayClient 已这样工作。

| 状态 | 含义 | 下一步 |
| --- | --- | --- |
| `stale_observation/not_started` | ID 已消费、过期、目标变化或其他动作作废了观察 | 重新观察，不能猜引用 |
| `approval_required/not_started` | 缺少宿主动作批准 | 交由宿主确认，不自动提权 |
| `cancelled/not_started` | 输入前取消 | 没有派发输入 |
| `capability_revoked/not_started` | 宿主撤销 | 重新选择/授权目标 |
| `computer_outcome_unknown/unknown` | 输入或输入后回读失败 | 停止执行，观察/查询目标，不重放 |
| `cancelled_after_dispatch/unknown` | 输入可能完成后才取消 | 检查实际状态 |

切换窗口或撤销电脑控制时先调用 `computer.revoke()`，再清除/替换 `cfg.computer`；撤销 Event 不等待审批锁。仅替换 cfg 会阻止后续新动作，但不能替代对已经进入审批中的能力对象的撤销。

## 限制

原生控件驱动不提供视觉坐标输入、UIA 全树、浏览器 DOM/标签页适配、热键或拖拽。截图与视觉分析由后续接口提供，但不会把截图坐标自动转换为点击。不能声称达到 Codex/OpenClaw 全部能力。标准控件消息不是物理点击；应用是否接受及业务效果必须根据新状态判断。

控件消息等待有 500ms 上限，观察最多 64 个子控件、3 秒检查预算及完整 4096 字符缓冲区；超过预算拒绝观察，避免截断状态用于动作。宿主审批回调仍需自行响应取消。进程/窗口身份和回读缩小误操作风险，但无法使观察与任意第三方应用状态变化原子化；协作锁也不约束用户和其他程序。

不自动关闭用户窗口、重启应用或回滚电脑动作。目标选择和审批是权限边界，不是 OS 沙箱。敏感或有外部副作用的操作仍由宿主确认具体意图。
