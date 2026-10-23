# 截图视觉与窗口选择器实施

基线 `bc9500f`，分支 `feature/computer-vision-window-picker`，工作树 `C:\Users\匡溯昀\forge-agent-execution-audit`。主检出 `pledge-evolving` 未修改、未合并。环境 Windows / CPython 3.12.10、Tk/Win32，未新增 pip 依赖。

## 实现与改动

| 文件 | 改动 |
| --- | --- |
| `forge/computer_capture.py` | 独立进程有界 PrintWindow 采集、GDI 资源释放、PNG 编码、取消/超时及 HWND/PID/几何复核 |
| `forge/computer_win32.py` | 可见窗口列表、选定目标截图接口 |
| `forge/computer_use.py` | 公开只读访问撤销信号，供宿主取消适配 |
| `forge-gui/computer_panel.py` | 窗口选择、绑定/撤销、本地预览、模型选择、发送授权、视觉分析与 Work 工具适配；后台线程不持有 Tk 回调 |
| `forge-gui/phase_client.py` | 独立 VisionClient，沿用选定模型、凭据和 HTTP 适配；两种图像协议、取消、空结果检查、套餐端点保护 |
| `forge-gui/forge_gui_v2.py` | Work 顶部最小入口、每轮能力快照、工具路由及退出撤销 |
| `forge-gui/provider_catalog.py` | 补充火山官方 Coding Plan OpenAI 端点预设；保留旧预设，不改用户配置 |
| `tests/test_computer_vision.py` | 视觉、权限、并发、关闭、原生截图与 GUI 生命周期回归 |
| `forge-gui/test_interactions.py` | 主聊天实际消费本地视觉工具结果的交互回归 |
| `docs/computer-use-host-api.md`、`docs/computer-screenshot-vision.md` | 衔接第一阶段接口、用法、授权及限制 |

未修改 GUI 视觉框架、Coding Mode 核心、插件市场业务或 Desktop Awareness。共享 Runtime/Policy/ActionExecutor 无语义重构；新工具通过已有宿主能力契约接入，不向 Code 模式或子 Agent 默认分发。

## 真实执行

读取现有 GUI 配置确认本机模型 `doubao-seed-evolving` 和端点 `https://ark.cn-beijing.volces.com/api/coding/v3`，不打印或保存密钥。

创建属于测试进程的窗口，白底、验证码 `4729`、左侧红色矩形、右侧蓝色圆形。实际 HWND/PID 截图 → PNG 内存传输 → 现有 Coding Plan 模型 → 检查回答。最终模型返回「顶部验证码：4729；左侧是红色矩形，右侧是蓝色圆形」，耗时 6.83 秒，上游报告输入 1379 / 输出 179 tokens。没有截取或上传用户应用内容，没有切换到标准 API。

另一次完整 Work 交互使用离线模型夹具：发送问题 → 主 Agent 请求 `computer_vision` → 真实本地 ActionExecutor/日志 → 工具结果回填 → 主 Agent 继续回答。验证一次截图、有 run/action 标识、未通过 Gateway 传输图片。真实网络图片调用和聊天编排分别验证，不能据此宣称每个上游主模型均能正确选择视觉工具。

## 验证结果

| 检查 | 结果 |
| --- | --- |
| 截图视觉专项（纳入后台套件） | 14 项全部通过；最后补充 Provider 配置变更清除授权断言后再次 14/14，2.770 秒 |
| `python -B -m unittest discover -s tests -v` | 232 项：231 通过、1 符号链接权限跳过，72.985 秒 |
| `python -B -m unittest discover -s forge -t . -v` | 229 项：228 通过、1 符号链接权限跳过，24.729 秒 |
| `python -B -m unittest discover -s forge-gui -p test_interactions.py -v` | 23 项全部通过，24.507 秒 |
| `python -B run.py selftest` | **573/574**；已有 `global:readme-no-hardcoded-counts` 在 `README.zh.md` 失败 |
| `git diff --check` | 通过 |
| 真实 Coding Plan 图片调用 | 两次成功；最终 6.83 秒，验证码和两种形状颜色正确 |

不能宣称全量回归全绿：selftest 的中文 README 守卫仍失败，本轮没有修改 README 或放宽守卫。套件间可能覆盖相同功能，上表不视为互不重复的测试数量。

专项覆盖：READ_ONLY 可读取、显式 deny 优先、无云端授权不采集上传、问题参数校验、采集前后取消/撤销、两条目标并发隔离、HTTP 两种协议的真实序列化、禁止标准计费端点回退、空响应、模型/Provider 配置变更清除授权、关闭期间丢弃迟到结果、原生 HWND/PID 截图与像素检查、错误 PID、预取消、超时，以及 Work 消费带身份的视觉结果。

调试期间修正了两个夹具问题：关闭测试最初在日志写完前删除临时目录，现等待工作线程退出；原生窗口与 Tk 截图测试在同进程混跑时卡在消息循环，现将真实截图夹具放入独立进程并设 20 秒硬超时，超时仍计失败。最终完整后台运行通过。产品面板同时清理变量 trace/计时器，后台线程通过数据队列返回，避免关闭后操作 Tk。视觉检查还修正了高 DPI 内容裁切。

测试进程设置 `PYTHONIOENCODING=utf-8`、`NO_PROXY=localhost,127.0.0.1`；GUI 套件增加 `PYTHONPATH=.;forge-gui`，未改用户持久环境。日志位于用户目录：`forge-vision-backend-final.log`、`forge-vision-core.log`、`forge-vision-gui-final.log`、`forge-vision-selftest.log`、`forge-vision-live-final.log`。日志只保存测试输出及合成窗口的描述/摘要，不保存截图像素或密钥。合成内容的界面视觉 QA 图片另存为 `forge-window-picker-preview.png`，没有使用真实用户窗口。

## 已知限制

当前交付的是选定窗口的视觉观察能力；GUI 没有授予输入控制。PrintWindow 对 GPU/受保护窗口可能得到黑图，最小化窗口要求先还原。尚无 UIA 全树、浏览器 DOM、视觉坐标点击、热键或拖拽；视觉描述不能当作动作成功证据。更换窗口/模型、关闭面板撤销旧能力；已经上传的请求无法召回。绑定/云端授权不跨重启恢复。

从本工作树启动 `python forge-gui/forge_gui_v2.py` 后使用 Work Mode 顶部「电脑操作」。现有指向主检出的启动方式仍运行主分支版本，本轮没有更改用户快捷方式或直接合并主分支。
