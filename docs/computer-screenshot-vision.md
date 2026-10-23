# 截图视觉与 GUI 窗口选择器

## 使用

从本分支运行 `python forge-gui/forge_gui_v2.py`。Work Mode 聊天顶部选择「电脑操作」：

1. 刷新窗口，在列表中选择目标，点击「绑定所选窗口」。标题、PID、HWND 会显示在面板中；绑定不改变窗口焦点。
2. 点击「截图预览」进行本地采集。截图仅保存在内存中，展示尺寸和 SHA-256；尚未勾选云端授权时不会发送图片。
3. 选择视觉模型。已有 `doubao-seed-evolving` 配置会优先选中；不会创建密钥或改写配置。
4. 勾选向所选模型发送截图的授权，输入问题并点击「分析当前窗口」，或回到 Work Mode 让主 Agent 调用 `computer_vision`。
5. 「停止分析」停止面板请求；聊天停止按钮取消对应聊天任务。「撤销授权」、更换窗口、关闭面板会撤销旧目标能力。更换模型会清除云端授权；所选 Provider 配置变更后，下次请求也会清除旧授权。刷新窗口按钮同时更新模型列表。

面板使用现有主题、字体、按钮、滚动和输入组件。高 DPI/小窗口下内容可滚动，底部状态始终可见；不修改 GUI 工作空间框架、Coding Mode、插件市场业务或 Desktop Awareness。

## 模型与套餐

`VisionClient` 复用现有 ModelRouter/HttpTransport、凭据解析和可取消 HTTP，仅调用明确选定的一个 Provider/model，不重试或回退。OpenAI 兼容协议发送 `image_url` data PNG；Anthropic 协议发送 base64 image source。

火山 Coding Plan 的官方端点为 OpenAI `/api/coding/v3`、Anthropic `/api/coding`。`doubao-seed-evolving` 的火山官方域名若配置为标准 `/api/v3`，本界面会拒绝调用并提示改选套餐端点。其余显式选择的第三方 Provider 按原配置使用，不能据此推断任何第三方代理的计费方式。

参考：[Coding Plan 官方配置说明](https://docs.volcengine.com/docs/ark/coding-plan-personal-get-started?lang=zh)。本轮已对本机现有 Coding Plan 配置进行真实图片调用，结果见实施报告；模型可用性和配额仍由服务端返回决定。

## 接口与权限

- `forge.computer_win32.list_windows(exclude_pid=...)`：用户打开面板后枚举最多 128 个带标题的可见顶层窗口；返回 `window/process_id/title`，没有图像或可执行动作。GUI 排除自身进程。
- `WindowsControlDriver.screenshot(cancelled=...)`：绑定 HWND/PID 校验后返回 `{png: bytes, width, height, sha256, target}`。不接受模型指定 HWND/PID，不改变焦点，不回退到全桌面采集。
- `ComputerLane`：GUI 可信宿主适配器。配置、目标、授权、取消 token、工作区在 UI 线程快照，经 `create_execution_context` 和公共 `ActionExecutor` 执行；保留既有 deny/ask/mode/sandbox，仅增加选定窗口的精确读取 grant。与 Coding Mode registry 分离。
- `computer_observe({})`：原生控件观察；受其既有控件数量/文本预算约束。
- `computer_screenshot({})`：本地采集，工具结果只包含尺寸、摘要及目标，主模型不会通过此工具获得图片。
- `computer_vision({"question": "描述可见界面"})`：须云端授权，重新采集当前目标并交给所选视觉模型；返回视觉描述、用量、图像摘要与目标。结果是非可信观察证据，不证明任何操作成功。

每个工具结果携带 `run_id/task_id/action_id/execution_state/authorization`；`<ForgeHome>/gui/computer-use/sessions/runtime/` 中记录意图与结果，不记录 PNG/base64。不会把完整图片加入主聊天历史。面板保存预览仅在内存；窗口绑定和发送授权不跨重启恢复。

工具名由宿主保留，不允许插件别名遮蔽；与附着电脑能力的同名 Gateway 工具冲突时给出明确错误。未绑定时不向 Work 模型公布本地电脑工具。已启动的聊天不会因后来勾选授权而扩大权限，需要发送新消息。

## 截图边界

使用 `PrintWindow`、GDI 和标准库 PNG 编码，独立采集进程最多等待 6 秒，取消后终止采集子进程。最多 1600 万像素、PNG 8 MiB；采集后重验窗口身份和几何。窗口最小化、已关闭、移动、PID 不符、超限或不支持采集时明确失败。

GPU/受保护内容和部分自绘应用可能返回黑图；不能把 PrintWindow 返回成功等同于内容完整。没有全桌面回退、绕过受保护内容、自动坐标点击、键盘热键或拖拽。已有标准控件输入宿主接口继续保留，但本面板不注册 `computer_act`。

撤销会中断本机后续采集和 HTTP 等待，无法召回服务端已经收到的图片。视觉请求有现有 60 秒网络超时；OS DNS 解析仍受系统解析器控制。窗口选择是 Forge 的能力边界，不是操作系统沙箱。
