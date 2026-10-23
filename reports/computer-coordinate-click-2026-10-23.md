# 视觉坐标点击交付

基线 `bb15b96`，独立分支 `feature/computer-coordinate-click`，复用工作树 `C:\Users\匡溯昀\forge-agent-execution-audit`。实现已接入本工作树的 GUI 和 Work Agent，未合并主分支、未更换技术栈或新增 pip 依赖。Windows / Python 3.12.10 / Tk，使用现有豆包 Coding Plan 配置。

## 结果

完成两条实际入口：截图预览选点、视觉模型定位后确认落点；都通过公共执行生命周期派发 Windows 原生左键单击并取得执行后截图。Work Agent 可使用 `computer_vision(question,target)` → `computer_click(observation_id,x,y,reason)` → 再次视觉分析。

新增独立点击开关，默认关闭。逐点确认展示目标、意图、截图与红色落点标记。现有 READ_ONLY/PLAN、只读 Sandbox、deny/ask、取消和撤销边界保留；未给 Code 模式或子 Agent 默认分发能力。GUI 原图预览、缩略图和 DPI 映射统一使用原始 PNG 物理像素。

## 真实闭环证据

创建独立测试进程的自绘 Canvas 窗口，绿色矩形内显示 VERIFY。使用本机 `doubao-seed-evolving`、`/api/coding/v3`：

1. 真实截图，模型返回原图落点 `(221,176)`，输入/输出 tokens 为 1445/416。
2. 测试宿主只批准自建窗口且位于已知测试按钮区域的落点；通过 ActionExecutor + SendInput 实际点击。
3. 目标窗口收到一次点击，客户区位置 `(210,131)`，状态文字变为 DONE；结果 `changed=true`、`verification=post_screenshot`，有 run/task/action/event 身份。
4. 再次截图交给同一模型，回答「英文文字为DONE，矩形填充色为红色，带有黑色外边框」，输入/输出 tokens 为 1348/206。

总计 20.88 秒；未操作或上传用户应用。生产 GUI 使用人工逐次确认，测试自动批准仅限上述自建夹具。原生测试另验证 DPI aware/unaware 两种目标、客户区边界、移动失效以及旧观察不能再次点击。测试夹具临时置顶以保证只命中自建窗口，生产驱动不强制置顶，也不绕过 Windows 前台限制。

另外，真实主 GUI 交互测试用离线模型依次定位、点击并读取回执，确认两项动作处于同一 run、不同 action_id，且本地输入不通过 Gateway 传图片/点击命令。点击确认界面也已渲染和检查，QA 图在用户目录 `forge-coordinate-approval-preview.png`。

## 修改清单

| 文件 | 内容 |
| --- | --- |
| `forge/computer_coordinates.py` | 作用域内截图观察、一次消费、过期、两次截图复核、审批、全局输入串行、未知结果及回读 |
| `forge/computer_use.py` | 绑定 visual 会话，原生控件输入同步作废视觉观察 |
| `forge/computer_win32.py` | 物理几何、线程 DPI 上下文、子窗口映射、命中/按键/捕获检查、SendInput 与部分提交处理 |
| `forge/computer_capture.py` | 采集进程附带几何和客户区像素摘要；公开 PNG bytes 字段继续可用 |
| `forge-gui/computer_panel.py` | 点击开关、原图选点、目标定位标记、确认接线、Work 工具注册和取消 token wait 契约 |
| `forge-gui/click_approval.py` | 有界审批数据队列、带落点的确认窗、拒绝/取消/关闭与计时器清理 |
| `forge-gui/phase_client.py` | 原图坐标定位提示、严格 JSON/类型/越界校验；保留独立模型与套餐路由 |
| `tests/test_computer_coordinates.py`、`tests/fixture_coordinate_window.py` | 策略、失效、未知结果、并发串行、取消、原生 DPI 与自绘目标测试 |
| `tests/test_computer_vision.py` | 预览缩放选点、审批关闭清理 |
| `forge-gui/test_interactions.py` | Work 定位/点击及同 run 关联 |
| `docs/computer-coordinate-click.md` 及原有接口文档 | 用法、字段、公开接线与边界 |

GUI 主框架、Coding Surface、插件市场业务、Desktop Awareness、共享 Policy/ToolRegistry/ActionExecutor 核心实现均未重构。主检出后来出现的其他 QA 未跟踪文件未改动或清理。

## 测试

| 检查 | 结果 |
| --- | --- |
| 后台全量 `unittest discover -s tests -v` | 249 项：248 通过、1 符号链接权限跳过，82.552 秒 |
| 核心全量 `unittest discover -s forge -t . -v` | 229 项：228 通过、1 符号链接权限跳过，25.508 秒 |
| GUI 交互 `unittest discover -s forge-gui -p test_interactions.py -v` | 24/24，28.845 秒 |
| 点击专项（已纳入后台） | 16/16；夹具 EOF 清理补齐后再次通过，7.068 秒 |
| Forge 全量 `run.py selftest` | **573/574**；唯一失败仍为 `global:readme-no-hardcoded-counts` / `README.zh.md` |
| 真实 Coding Plan 定位、输入、视觉回读 | 成功，20.88 秒 |
| `git diff --check` | 通过 |

不能宣称所有检查全绿；未修改 README 或放宽守卫。测试进程设置 UTF-8 和 `NO_PROXY=localhost,127.0.0.1`，GUI 套件额外使用 `PYTHONPATH=.;forge-gui`；未改持久环境。

调试中实际发现：窗口消息 API 成功不代表自绘控件收到点击，故最终改用 SendInput 并检查真实状态；无条件要求前台激活会在 Windows 拒绝抢焦点时误阻断可见目标，最终以严格命中窗口校验决定是否派发，遮挡仍拒绝；模型调度路径还暴露组合取消 token 缺少 `wait()`，已补齐并覆盖测试。未把失败调用报告为成功。

日志：用户目录下 `forge-coordinate-backend-final.log`、`forge-coordinate-core.log`、`forge-coordinate-gui.log`、`forge-coordinate-selftest.log`、`forge-coordinate-live-final.log`。动作日志只保存摘要和身份，不保存 PNG/base64。

## 使用与限制

启动本工作树的 `forge-gui/forge_gui_v2.py`，Work Mode → 电脑操作 → 绑定窗口 → 开启坐标单击 → 截图选点，或授权视觉模型后定位目标 → 点击模型落点 → 确认。详见 [公开用法与协议](../docs/computer-coordinate-click.md)。主分支及原有快捷方式没有直接切换。

现阶段仅客户区左键单击；必须检查实际可见内容与落点。动态画面/焦点样式变化可能导致严格像素检查拒绝旧坐标；受保护黑图、UIPI、安全桌面不绕过。新的顶层弹窗需重新选择。真实鼠标位置会改变；用户正在操作或菜单/捕获活动时拒绝。输入部分完成、取消后可能输入或回读失败保留 unknown，不重放；`changed` 不证明用户业务目标完成。
