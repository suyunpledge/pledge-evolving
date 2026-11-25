# 视觉坐标输入：单击、双击、右键、滚动、打字、按键

## Windows 后台填写与保存

在 Work 中选择「通用助手」或已开放 `computer_observe` 和 `computer_act` 的角色。打开「电脑操作」，绑定目标窗口，选择「后台控件操作」。手动批准模式可逐步确认。要连续执行，请切到「完全访问」，点击「授权此窗口后台连续执行」，核对窗口后确认。

Agent 通过 Windows 辅助功能读取控件、设置文本及触发按钮。每次操作都使用最新观察中的控件引用。按钮回执表示调用已派发，应用可能稍后才完成保存，请通过应用的保存状态核对结果。点击「撤销窗口授权」会使旧引用和该次会话的后续窗口调用失效。已经派发的操作可能仍会完成。

只有图片或画布且未暴露辅助功能控件的界面，需要用户切换到「前台鼠标与键盘」才能通过坐标操作。

在已打开的普通 Edge 中选中目标标签页。进入「电脑操作」，刷新窗口列表，点该窗口旁的「选择 Edge 网页」，再授权后台输入。revon 将控件引用绑定到当时的网页。切到其他标签页或导航后，如需操作新页面，请重新选择。若 Edge 尚未提供网页控件树，先显示目标标签并等待其加载，再重新选择。

Edge 窗口完全被遮挡时，网页可能已接收文本，但辅助功能树仍返回旧值。revon 只写入一次。如果等待读回时仍是旧值，会尝试将同一网页字段滚动到视图，再核对原字段的实际值。字段可能在后台页面内滚动。读回失败时，操作返回结果未知，应先核对再继续。此流程使用普通 Edge 的现有配置。需要独立浏览器配置时也可以使用「打开独立后台 Edge」。

在新版 Windows 记事本编辑已命名文件时，先设置文档内容，再使用 `expand` 展开「文件」菜单，从新观察中调用一次「保存」。尚未出现菜单项时可以重新观察。验收时应核对磁盘文件及保存动作的次数。

较长的控件列表会分页。工具回执中的 `next_offset` 可用作下次 `computer_observe` 的 `offset`，界面也提供「下一页后台控件」。操作需使用最新一页的观察编号和控件引用。

排查焦点争用时可运行 `python scripts/verify_background_focus.py --pid <目标应用PID> --pid <revon界面PID> --seconds 60 --output focus.jsonl`。记录包含前台和焦点事件的来源，以及实际前台线程的键盘焦点归属。输入法弹窗、其他程序切换和同一前台窗口内的控件切换可以分别核对；仅凭变化总数无法追溯原因。

## Qt 桌面的本地定位

在 Work 的「电脑操作」中选择窗口，填写「定位目标」，点「本地识别 / 定位」。例如输入按钮的无障碍名称“保存”。目标留空会列出可见控件和状态。VisFunc 在本机先查询该窗口的 Windows UI Automation 控件树，找不到名称时再从本次截图的客户区识别文字。Qt 安装脚本会安装这两项依赖。

- Agent 调用 `computer_screenshot({})` 可取得控件名称、角色和勾选状态。调用 `computer_screenshot({"target":"保存","role":"button"})` 限定控件类型；省略 `role` 可同时查找自绘文字。遇到同名控件时可用返回的自动化 ID 缩小范围。
- 唯一匹配且可用时返回当前截图的 `point` 和 `observation_id`。重名、禁用或 OCR 置信度低时返回对应状态。实际点击、输入和按键沿用窗口授权和逐次确认。操作后的截图附带新控件状态，可直接核对复选框等变化。
- 识别模块直接集成自本机 VisFunc 项目的 `svsg/recognition/ui_elements.py`、`screen_text.py`。窗口截图和识别在有超时、可取消的本地进程内完成。只有显式启用云端视觉并调用 `computer_vision` 才会向视觉模型发送截图。

在「电脑操作」里刷新并选择窗口。手动批准或完全访问模式下，Work 对话可以立即调用 `computer_observe` 读取这个窗口的标准控件。要操作其中一个按钮或输入框，把观察结果的 `observation_id` 和该控件的 `ref` 分别作为 `computer_screenshot` 的 `observation_id`、`control_ref`。它会返回一张新截图的 ID 及原图像素落点 `point.x`、`point.y`。这一步只在本机执行。自绘界面也可以在获得授权后使用 `computer_vision` 定位。

勾选允许输入并重新截图后，可以在预览上选点，检查标记，再点击「点击选中位置」。Work 使用同一套屏幕工具，操作后的截图也会显示在这里。点击和键盘输入继续逐次确认。刷新截图会清除手选落点，更换窗口需要重新授权输入和云端分析。

## 旧版 Tk 界面

从本工作树启动 `python revon-gui/revon_gui_v2.py`，进入 Work Mode 顶部「电脑操作」。绑定窗口后勾选「允许鼠标与键盘输入」。权限模式必须允许输入；READ_ONLY、PLAN、只读 Sandbox 和显式 deny 不会因勾选而升级。

两条流程均已接入：

- 本地操作：截图预览 → 在预览中选择落点 → 检查确认窗口中的红色落点标记 → 确认单击 → 查看更新后的截图。无需把图片发送给模型。
- 模型定位：选择豆包 Evolving 等已配置视觉模型、勾选截图发送授权 → 在问题栏填写目标 →「定位目标」→ 检查标记 →「点击模型落点」→ 确认 → 查看新截图。Work Agent 也可依次调用定位与点击工具。

点击会移动系统鼠标，并尝试激活选定窗口。目标点必须实际可见且属于绑定窗口及其校验过的子窗口；遮挡、菜单、鼠标捕获或用户正按住鼠标/修饰键时拒绝派发。Windows 不允许程序激活窗口时，不绕过系统限制；被遮挡的目标需要先由用户移到前面，再重新观察。指针动作只作用于客户区（单击、双击、右键、滚轮），不点标题栏、系统关闭按钮或其他顶层弹窗。

窗口、模型、授权变化会使旧观察失效。GUI 观察最长有效 90 秒；点击确认最多等待 60 秒，等待期间仍计算观察有效期。截图过期或客户区内容变化时重新观察，不自动重试点击。

## 工具协议

截图数据额外包含物理像素窗口矩形、客户区矩形、DPI、客户区 SHA-256。`ComputerSession.visual.remember(frame, scope)` 生成与受保护作用域绑定的 `observation_id`。元数据被记录，PNG/base64 不进入动作日志或主聊天历史。

定位请求：

```json
{"question":"找到 VERIFY 按钮","target":"绿色矩形中的 VERIFY 按钮"}
```

`computer_vision` 返回 `point={found,x,y,label}` 和 `image.observation_id`。坐标使用**原始完整 PNG 的整数物理像素**，包含窗口边框，以左上角为原点；不是预览缩略图坐标、屏幕坐标或 0–1000 归一化坐标。无法定位时返回 `found:false`，不得点击。格式、坐标范围、布尔值伪装整数等不合法结果直接失败。

点击请求：

```json
{"observation_id":"opaque-id","x":221,"y":176,"reason":"点击已确认的 VERIFY 按钮"}
```

`computer_click` 只有在 GUI 独立点击开关开启时注册；使用宿主确认回调及 `ToolEffect.DELEGATED_WRITE`，继续走 Policy、公共 ActionExecutor 和持久化生命周期。精确读取 grant 只增加读取工具，不增加点击 grant。GUI 使用 `ClickApprovals` 数据队列和 `ApprovalView`，后台线程不调用 Tk，也不持有 Tk 确认回调。

成功结果包含新 `observation_id`、`run_id/task_id/action_id`、`verification=post_screenshot`、`dispatch=send_input`、`changed`。后续业务判断应重新分析新截图；`changed` 仅表示客户区像素不同，API 事件成功插入也不证明业务目标完成。

## 其他指针动作与键盘输入

`computer_click` 可选 `action`：`click`（默认）、`double_click`、`right_click`、`scroll_up`、`scroll_down`；滚动可加 `amount`（1–10 格，默认 3），其他动作带 `amount` 直接拒绝。流程、命中检查和失效规则与单击完全相同，双击在同一 SendInput 批次内提交两次按下/释放。右键菜单是独立顶层窗口，截图看不到，用 `computer_key` 的 up/down/enter/esc 选择。

`computer_type`：`{"observation_id","text","reason"}`，1–2000 字符，以 Unicode 键入当前键盘焦点（先点击输入框）。换行按回车、制表符按 Tab，可能直接提交表单，确认窗口会提示；其他控制字符拒绝。

`computer_key`：`{"observation_id","key","reason","repeat"?}`。允许 enter/tab/esc/backspace/delete/insert/space/方向键/home/end/pageup/pagedown/menu/F1–F12，及 ctrl/alt + 字母或数字、可加 shift；单独字母要用 `computer_type`。`alt+tab`、`alt+f4`、`alt+esc`、`ctrl+esc`、`ctrl+shift+esc`、`ctrl+w`、`ctrl+f4` 与 Win 键永不发送。`repeat` 1–20。

键盘输入与点击的差别：

- 观察复核只比较目标、尺寸、位置和 DPI，不比较客户区像素摘要——输入框中闪烁的光标会让两次截图摘要不同，按键也不落在某个像素上。
- 确认后取得桌面输入锁，要求目标在 1 秒内真正成为前台窗口（属于绑定进程），否则 `not_started`：按键只会发给前台焦点，绝不发给别的窗口。
- 派发前再次检查前台窗口、用户是否按住鼠标/修饰键/Win 键、鼠标捕获或窗口移动；目标自己的菜单模式允许（按键正是用来选菜单项的）。
- 全部按键一次 SendInput 提交；部分提交时只释放已按下的键（含修饰键），结果为 `unknown`，不重放。

每次输入都会返回新截图与新的 `observation_id`，可以直接接下一步（如输入后按回车），不必另行截图。

## 执行与失效

1. 校验参数、策略、作用域、观察 ID、有效期、原图范围和客户区范围。一次尝试消费旧观察，失败后也需重新观察。
2. 重新截图，比较 HWND/PID 对应目标、尺寸、位置、DPI 和客户区像素摘要；检查目标子窗口。
3. 展示具体截图、落点和意图，等待人工确认。拒绝/关闭/取消均不派发输入。
4. 取得跨进程桌面输入锁，再尝试激活目标、重新截图和复核原生子窗口。不同目标任务也串行输入，等待审批期间不占用桌面输入锁。
5. 对照虚拟桌面的物理坐标、最终命中窗口、用户输入状态和取消状态，单批提交鼠标移动/按下/释放三个 SendInput 事件。宿主线程 DPI 上下文在读取及映射后恢复；支持目标应用 DPI aware/unaware 和虚拟桌面负原点。
6. 作废本 ComputerSession 其他作用域的旧观察及原生控件 ref，取得点击后截图。输入部分提交、调用异常、取消后可能已输入、或回读失败都保留 `unknown`，停止后续动作，不自动重放；部分提交只尝试释放可能已按下的左键。

已记录 action_id 的重复请求由 ActionExecutor 查询已有结果。不同 action_id 也不能复用已消费的观察。窗口绑定和坐标观察不跨重启恢复；这不构成任意 GUI 操作的 exactly-once 保证。

GUI 发起点击后立即清除可执行的旧观察和模型落点。点击后的取消或回执异常不会被“已停止”覆盖：`unknown` 明确提示输入可能已经生效，需检查目标并重新截图；已完成的点击随后被取消，会保留已派发的事实而不恢复旧授权下的预览。

确认只接受首次决定，过期、取消、撤销后的确认均无效。确认图片渲染失败时拒绝该请求，后续确认队列仍可使用。

## 宿主锁与取消接口

`named_resource_lock(kind, identity, cancelled=token)` 用于电脑等非文件资源，标识不依赖当前工作目录。现有 `resource_lock(kind, path)` 的文件路径语义保持兼容。窗口锁为 `('computer-target', driver.target_id)`，桌面输入锁为 `('computer-input', 'desktop')`；同一用户、同一系统临时目录下的不同工作树和进程共享它们。

`ComputerSession.operation(cancelled)` 将请求取消与宿主撤销合并，依次取得会话线程锁及跨进程目标锁。视觉点击通过同一 token 等待桌面输入锁、确认和截图；等待阶段每 50 ms 内检查取消（OS 文件锁重试为 10 ms），尚未派发时返回 `cancelled/not_started`。输入后的错误继续为 `unknown`。`TargetCancellation` 从 `revon.computer_use` 导出，原 `computer_panel` 导入方式仍兼容。

升级时应退出并重新启动旧版本 revon 进程。旧版本按工作目录生成电脑锁，不能与新的命名锁协议混用；不承诺跨版本滚动升级互斥。取消不会强制终止不支持取消的第三方宿主回调，实际响应还受调度和驱动影响。

## 边界

为避免标题栏激活颜色造成无关失效，比较客户区像素摘要，而非全 PNG 摘要。客户区任意变化仍会拒绝，包括动画、时钟、光标、焦点样式或异步更新；暂不做宽松图像匹配。模型定位精度受模型影响，确认界面显示实际落点，不把模型坐标当成可信操作指令。

Windows UIPI/安全桌面、受保护或黑图窗口、不可见目标不绕过；不支持拖拽，也不提供原生控件树（UIA）。新的顶层弹窗必须另选目标。跨进程锁只协调 revon，不能锁住用户、第三方程序或窗口自身状态，最终检查到系统输入之间仍有微小竞态。

实现依据：[SendInput](https://learn.microsoft.com/en-us/windows/win32/api/winuser/nf-winuser-sendinput)、[窗口命中检查](https://learn.microsoft.com/en-us/windows/win32/api/winuser/nf-winuser-windowfrompoint)、[DPI 点映射](https://learn.microsoft.com/en-us/windows/win32/api/winuser/nf-winuser-physicaltologicalpointforpermonitordpi)。
