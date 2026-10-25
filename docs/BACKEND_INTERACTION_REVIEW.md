# 后端交互审查与修复

日期：2026-10-25。审查基线：`329e535`，分支：`feature/desktop-chromium`。
沿用独立工作树，不修改主分支和其他 Agent 工作树。

本轮检查 Work 对话发送、工具发现与调用、取消、失败恢复以及桌面 Chromium 生命周期。
保持现有 GUI 框架和权限裁决；没有改动布局、插件市场业务或 Desktop Awareness。
这是一轮围绕实际交互的定向审查，不代表对整个仓库全部功能完成审计。

## 已修复的问题与依据

| 优先级 | 基线行为与源码依据 | 修复与验收 |
| --- | --- | --- |
| P1 | `forge-gui/forge_gui_v2.py::_do_send` 计算了 `result.text` 的回退文本，但最终只保存流式 `acc`，无流式片段时出现空回复 | 完整文本也进入显示与历史；已有流式片段不重复追加 |
| P1 | `_do_send` 先清空输入，初始化失败只更新底部状态 | 走统一失败处理，恢复输入与可重试状态；下一次发送验证成功 |
| P1 | `_chat_failed` 在用户点停止时无条件转为“已停止”，覆盖工具未知结果 | 未知结果保留“待确认”提示，不触发下一工具或模型轮次 |
| P2 | 空文本、无工具调用也作为成功结束 | 明确失败并保留问题；不将空回复归档为成功 |
| P2 | `forge_client.py::list_tools` 不接收取消 token，Work 入口使用默认长超时并静默吞掉发现失败 | 新增兼容的可选取消参数；Work 发现阶段使用 5 秒超时，取消不继续请求模型；失败显示降级说明 |
| P2 | Work 工具提示在整轮结束后才更新；批次中途取消或无进展退出可能丢失本轮可见结果 | 调用前显示工具名与进度；每项回执立即展示，后续取消不隐藏已完成操作 |
| P2 | `forge/browser.py::_BrowserActor.call` 对所有调用直接等待 Future，已取消的排队调用仍等前项结束 | 每 100 ms 检查排队取消；用 Future 原子状态领取执行，取消成功后绝不派发；已领取操作等待实际回执 |
| P2 | `browser_panel.py::navigate` 复用已关闭的 `current` | 下一次显式点击从 manager 获取新会话；不自动重放旧动作、不恢复已撤销授权 |
| P2 | `BrowserPanel.open` 在创建清理控件前分配第五个会话，容量错误使面板无法打开 | 容量满仍可打开面板并关闭全部浏览器；清理后可重新使用 |
| P2 | 面板保持打开时切换项目/聊天/模式，旧面板按钮仍作用于旧分区 | 拒绝过期面板导航和新增授权，明确提示重开面板；仍允许撤销旧分区授权，保留既有后台分区 |

关闭全部浏览器的异步回调还增加了宿主已关闭检查，避免退出后重新创建 manager。

## 接口与兼容性

`ForgeGatewayClient.list_tools(*, timeout=None, cancel_event=None)` 保留原有调用方式；传入取消 token 时将传输取消转换为 `GenerationCancelled`。
工具执行协议、Policy、ActionExecutor 和 Gateway 的写操作语义没有改变。
浏览器排队取消仅在 Future 尚未被执行线程领取时返回 `not_started`。
已经派发的操作不能保证撤销；缺少回执仍为 unknown，不自动重放。
工具卡现在按单项结果即时追加并保留轮次标识，使用原有组件。

## 验证

环境：Windows 11 x64 / Python 3.12.10 / Playwright 1.64.1 / Chromium 156.0.8078.4。
GUI 测试运行真实 Tk 窗口与事件循环，但模型和工具回复使用隔离夹具。
浏览器集成测试使用真实 Chromium 和本机 HTTP 服务，不访问外部账户、不调用付费模型。

| 测试命令/套件 | 结果 | 日志 |
| --- | --- | --- |
| `python -m forge.selftest` | 574/574 | `build/selftest-interaction.log` |
| `unittest discover -s forge-gui -p test_runtime_feedback.py -v` | 6/6 | `build/runtime-feedback.log` |
| `unittest discover -s forge-gui -p test_browser_panel.py -v` | 6/6 | `build/browser-panel-followup.log` |
| `unittest discover -s forge-gui -p test_interactions.py -v` | 24/24 | `build/interactions-followup.log` |
| `FORGE_BROWSER_TEST=1 python -m unittest forge.test_browser -v` | 22/22，含 5 项真实浏览器测试 | `build/browser-interaction-final.log` |

合计 58 项针对性测试通过、无跳过；另有全量 selftest 的 574 项检查通过。
新增队列测试在前一操作保持运行时验证取消调用在 0.7 秒测试期限内返回；不把该测试期限宣传为所有系统负载下的 SLA。
多工具测试在第二项阻塞时验证第一项回执已显示，并验证停止期间返回的 unknown 不会被普通取消提示覆盖。
发送恢复测试验证“初始化失败→输入保留→再次发送→完整回复入历史”的闭环。

## 修改文件

- `forge/browser.py`：排队取消与 Future 执行状态同步。
- `forge-gui/forge_client.py`：可取消工具发现。
- `forge-gui/forge_gui_v2.py`：文本回退、失败恢复、工具即时反馈与未知结果提示。
- `forge-gui/browser_panel.py`：重开、容量清理、分区上下文检查与退出保护。
- `forge/test_browser.py`、`forge-gui/test_browser_panel.py`、`forge-gui/test_runtime_feedback.py`：新增回归。
- `forge-gui/test_interactions.py`：测试客户端同步支持工具发现可选参数。

## 保留的限制

浏览器继续使用独立 Chromium 窗口，Coding Agent 浏览器工具接线不在本轮范围内。
实际线上模型质量、真实网站登录与业务流程尚未做付费/账号端到端验证。
Work 工具批次中已经发出的写操作不能因为用户停止而承诺回滚；不确定状态需要检查目标和日志。
未重新运行仓库所有独立测试套件；仅对上表列出的测试和构建结果作出通过声明。

## 桌面包验证

使用原有图标构建，避免构建环境字体差异改写视觉资源：

```powershell
./forge-gui/desktop/build-desktop.ps1 -PythonExe C:/path/to/python.exe -SkipIcon -SkipBrandMarks -SkipUiAssets
```

PyInstaller 构建成功；冻结 EXE 的 Chromium 启动、匿名管道通信与关闭检查通过（`ok=true, closed=true`）；GUI 启动检查通过。
日志：`build/desktop-build-interaction.log`；浏览器回执：`forge-gui/desktop/dist/browser-smoke.json`。
检查完成后没有发现该工作树目录下残留的浏览器或桌面 EXE 进程。

最终 EXE SHA-256：`335906bfbc110d3cb4754150e668b0da750758d9a45415e0c9135bccdbdd9850`。
本轮分发包：`forge-gui/desktop/dist/forge-desktop-chromium-interaction-windows-x64.zip`。
完整解压后运行 EXE；上一轮 ZIP 保留原名，可区分两个版本。
ZIP 大小：272694188 字节（约 260 MiB）；全部条目 CRC 校验通过。
ZIP SHA-256：`6c740842a1aa64f986758900c98ff6e07a1fe4f3c1bbfa54293267d7ad8098a3`。
