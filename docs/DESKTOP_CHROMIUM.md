# Desktop Chromium

Forge 桌面端保留 Tkinter，以应用管理的独立 Chromium 窗口提供浏览能力。
入口：顶部「更多 → Chromium 浏览器」。输入 HTTP(S) 地址后点「打开网页」。
开发服务器地址（如 `http://127.0.0.1:3000`）也可直接打开。

## 会话与授权

- 浏览器按项目绝对路径、Work/Coding 模式和聊天会话分区，最多四个活动分区。
- 每个分区使用独立浏览器与临时存储，不读取 Chrome/Edge 个人资料。关闭浏览器后登录状态清除。
- 切换模式保留已有浏览器。关闭控制面板不关闭浏览器；面板内「关闭当前浏览器并撤销授权」才销毁该分区。
- 「关闭全部浏览器」会回收旧项目/旧聊天的分区并撤销全部浏览器授权。
- Work Mode 需勾选读取授权才向当前 Agent 提供浏览器工具。取消勾选立即取消旧授权；重新勾选不会恢复旧任务授权。
- 导航、点击、填写每次都经过宿主确认，仍受 Policy 的只读、deny、ask 等限制。BYPASS 不能跳过浏览器宿主确认。
- Coding Mode 本轮提供手动浏览和开发服务器预览；Coding Agent 没有隐式获得 Work 浏览器能力。
- 文件 URL、任意 JS/CDP、自动密码填写、文件上传和下载不作为工具开放。账号登录可在浏览器窗口手动完成。

## 后端接口边界

`forge/browser.py` 管理 Chromium 生命周期；每个会话只有一个 Playwright 线程。
浏览器线程在独立辅助进程中运行；45 秒仍无回执时回收本会话进程树，写操作记为 unknown。
`forge/browser_tools.py` 是公开宿主适配器，复用 `ExecutionContext / ActionExecutor / ToolRegistry`。
没有修改 Policy 基线、插件授权、Desktop Awareness 或 CLI/Gateway 默认工具集合。
其他宿主可显式构造 `BrowserLane`，没有 `browser` capability 的上下文无法执行这些工具。

```python
from forge.browser import BrowserSession
from forge.browser_tools import BrowserLane

session = BrowserSession()  # 默认可见窗口；只在首次 open 时启动
lane = BrowserLane(session, home=home, workspace=workspace, policy=policy,
                   cancel=cancel_event, scope=secret_scope, approve=host_confirm)
tools = lane.schemas()
result = lane.call('browser_open', {'url': 'http://127.0.0.1:3000'},
                   action_id='host-generated-action-id')
# 生命周期结束时在后台线程调用 session.close()
```

`host_confirm(name, details, cancellation)` 必须由宿主提供。点击/填写确认包含观察到的页面地址和元素标签。
工具结果包含 `run_id/task_id/action_id/event_id/authorization/execution_state`，事件写入宿主 Session 日志。
`browser_observe` 返回 `page_id/observation_id/elements[].ref`；操作要求本次 run 的观察，最长有效 60 秒。
页面导航、元素标签变化或一次操作都会使引用失效。页面文本始终视为不可信内容。

工具名称：`browser_open`、`browser_observe`、`browser_click`、`browser_fill`。
后三者通过 `page_id` 选择标签页；observe 返回已有标签页列表。iframe 内元素暂不枚举。

取消在排队/执行前阻止调用；已经发出的调用等待有界结果，不能声称已撤销页面操作。
点击/导航超时等不确定结果记录 `unknown`，不自动重试；同一 action_id 可查询/去重，不能保证任意网页 exactly-once。
GUI 常规执行生成新动作身份，因此人工决定是否再次执行前应重新观察网页。

## 构建与分发

```powershell
python -m pip install '.[browser]' pyinstaller pillow
python forge-gui/desktop/prepare-browser.py
powershell -ExecutionPolicy Bypass -File forge-gui/desktop/build-desktop.ps1 -PythonExe C:/path/to/python.exe
```

Playwright 固定为 1.64.1，Chromium 修订由其驱动指定。准备脚本只在构建机下载 Chromium，保留官方许可文件。
发行时必须一起分发 `forge-gui/desktop/dist/forge-desktop.exe` 与同级 `browsers/`，不能只复制 EXE。
Chromium 不嵌入 one-file EXE，避免每次启动解压数百 MB；运行时没有自动下载或系统浏览器回退。
冻结 EXE 内嵌资源哈希清单；启动前校验外置 Chromium 文件集与 SHA-256，拒绝缺失、修改或额外注入的浏览器资源。
构建会从冻结 EXE 真正启动并关闭 Chromium，生成 `dist/browser-smoke.json`，失败即阻止交付。
更新浏览器时同时更新 Python 依赖、准备脚本版本并重新构建整个发行目录。

## 验证

```powershell
python -m unittest forge.test_browser -v
$env:FORGE_BROWSER_TEST='1'
python -m unittest forge.test_browser -v
$env:PYTHONPATH=(Get-Location).Path
python -m unittest discover -s forge-gui -p test_browser_panel.py -v
python -m forge.selftest
```

真实浏览器测试仅访问本机临时 HTTP 服务，无模型费用、外部账号或真实提交。
覆盖导航、文本观察、填写、点击、过期元素、密码阻止、超时 unknown、取消、cookie 隔离和动作身份去重。

依赖依据：[Playwright 浏览器分发](https://playwright.dev/python/docs/browsers)、
[Python 库与 PyInstaller/线程约束](https://playwright.dev/python/docs/library)。
