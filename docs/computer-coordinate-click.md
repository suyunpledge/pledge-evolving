# 视觉坐标单击

## 使用

从本工作树启动 `python forge-gui/forge_gui_v2.py`，进入 Work Mode 顶部「电脑操作」。绑定窗口后勾选「允许视觉坐标单击」。权限模式必须允许输入；READ_ONLY、PLAN、只读 Sandbox 和显式 deny 不会因勾选而升级。

两条流程均已接入：

- 本地操作：截图预览 → 在预览中选择落点 → 检查确认窗口中的红色落点标记 → 确认单击 → 查看更新后的截图。无需把图片发送给模型。
- 模型定位：选择豆包 Evolving 等已配置视觉模型、勾选截图发送授权 → 在问题栏填写目标 →「定位目标」→ 检查标记 →「点击模型落点」→ 确认 → 查看新截图。Work Agent 也可依次调用定位与点击工具。

点击会移动系统鼠标，并尝试激活选定窗口。目标点必须实际可见且属于绑定窗口及其校验过的子窗口；遮挡、菜单、鼠标捕获或用户正按住鼠标/修饰键时拒绝派发。Windows 不允许程序激活窗口时，不绕过系统限制；被遮挡的目标需要先由用户移到前面，再重新观察。仅支持客户区的左键单击，不点标题栏、系统关闭按钮或其他顶层弹窗。

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

## 执行与失效

1. 校验参数、策略、作用域、观察 ID、有效期、原图范围和客户区范围。一次尝试消费旧观察，失败后也需重新观察。
2. 重新截图，比较 HWND/PID 对应目标、尺寸、位置、DPI 和客户区像素摘要；检查目标子窗口。
3. 展示具体截图、落点和意图，等待人工确认。拒绝/关闭/取消均不派发输入。
4. 取得跨进程桌面输入锁，再尝试激活目标、重新截图和复核原生子窗口。不同目标任务也串行输入，等待审批期间不占用桌面输入锁。
5. 对照虚拟桌面的物理坐标、最终命中窗口、用户输入状态和取消状态，单批提交鼠标移动/按下/释放三个 SendInput 事件。宿主线程 DPI 上下文在读取及映射后恢复；支持目标应用 DPI aware/unaware 和虚拟桌面负原点。
6. 作废本 ComputerSession 其他作用域的旧观察及原生控件 ref，取得点击后截图。输入部分提交、调用异常、取消后可能已输入、或回读失败都保留 `unknown`，停止后续动作，不自动重放；部分提交只尝试释放可能已按下的左键。

已记录 action_id 的重复请求由 ActionExecutor 查询已有结果。不同 action_id 也不能复用已消费的观察。窗口绑定和坐标观察不跨重启恢复；这不构成任意 GUI 操作的 exactly-once 保证。

## 边界

为避免标题栏激活颜色造成无关失效，比较客户区像素摘要，而非全 PNG 摘要。客户区任意变化仍会拒绝，包括动画、时钟、光标、焦点样式或异步更新；暂不做宽松图像匹配。模型定位精度受模型影响，确认界面显示实际落点，不把模型坐标当成可信操作指令。

Windows UIPI/安全桌面、受保护或黑图窗口、不可见目标不绕过；不支持右键、双击、拖拽、滚轮或键盘输入。新的顶层弹窗必须另选目标。跨进程锁只协调 Forge，不能锁住用户、第三方程序或窗口自身状态，最终检查到系统输入之间仍有微小竞态。

实现依据：[SendInput](https://learn.microsoft.com/en-us/windows/win32/api/winuser/nf-winuser-sendinput)、[窗口命中检查](https://learn.microsoft.com/en-us/windows/win32/api/winuser/nf-winuser-windowfrompoint)、[DPI 点映射](https://learn.microsoft.com/en-us/windows/win32/api/winuser/nf-winuser-physicaltologicalpointforpermonitordpi)。
