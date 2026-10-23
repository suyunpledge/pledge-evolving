# 视觉坐标点击：复审与稳定性修复

审查基线：`ee7b4fb`。分支：`feature/computer-coordinate-click`。工作树：`C:\Users\匡溯昀\forge-agent-execution-audit`。开始时工作区干净；本轮未合并 main、未改主检出或 GUI 主框架。

## 审查结果与处理

| 优先级 | 基线证据及影响 | 修复位置 | 验证 |
| --- | --- | --- | --- |
| P1 | `runtime_lock.resource_lock` 对所有资源执行 `Path(...).resolve()`；`computer_coordinates` 用相对字符串 `desktop`、各电脑入口用 `win32:PID:HWND`。不同工作目录会生成不同锁文件，跨工作树输入可能并发。 | `forge/runtime_lock.py:41` 增加 `named_resource_lock`；`forge/computer_use.py:57`、`forge/computer_coordinates.py:75` 和 `ComputerLane.capture` 统一接入。文件路径锁保持原键值。 | 两个独立子进程使用不同 cwd，分别验证目标锁、桌面锁等待父进程释放；原跨进程 Session 和文件版本竞争回归通过。 |
| P1 | `ComputerPanel.work` 遇到取消先返回“已停止；可重新执行”，会覆盖点击后的 `unknown`；`dispatch_click` 失败后仍留着旧 frame 和模型落点。 | `forge-gui/computer_panel.py:200`、`:355` 保留点击回执，清除已尝试的观察；未知结果提示可能已经生效，不允许从旧落点再次派发。 | GUI 测试模拟输入后取消及未知回执，断言状态不被覆盖，旧观察和落点清空，重复按下不会再次调用工具。 |
| P2 | 会话 RLock、进程内资源锁和 OS 锁等待不检查取消；直接调用 visual 的宿主撤销也没有传到批准回调。 | `forge/runtime_lock.py:25` 增加可取消获取；`ComputerSession.operation` 合并取消和撤销；截图、批准、激活和输入使用同一组合 token。 | 会话锁/桌面锁阻塞时分别取消或撤销；独立进程持锁时取消；直接会话撤销使批准回调及时退出。均未发送输入。 |
| P2 | 确认回调直接写 `accepted=True`，与 worker 超时/取消没有原子的首次决定，也未在确认瞬间校验截止时间。 | `forge-gui/click_approval.py:14` 新增带线程锁的首次决定，校验截止时间、取消与关闭。 | 过期、已拒绝、已取消的请求再次确认均为 false。 |
| P2 | `ApprovalView.poll → show` 的图片或 Tk 异常会中断轮询，遗留弹窗并让后续请求超时。 | `forge-gui/click_approval.py:41` 失败时拒绝并清理当前确认，继续队列轮询。 | 用无效 PNG 触发真实 Tk 渲染失败；随后正常 PNG 的请求仍能显示并确认。 |

## 已核验并保留的机制

- READ_ONLY/PLAN、只读 Sandbox、deny/ask、未开启点击能力均拒绝输入；只读工具不增加点击权限。
- 原始 PNG 像素、客户区限制、窗口/子窗口绑定、DPI 与负原点映射、过期及像素变动复核。
- 部分 SendInput 仅尝试释放按键；未知输入或回读结果不自动重放；相同 action_id 查询已有结果。
- 宿主撤销、窗口重新绑定、模型配置或上传授权变化使旧 lane 失效；图片不写入动作日志。
- Work Agent 的未知回执停止后续工具和下一次模型调用，同一 run 的不同工具保持独立 action_id。已有实现和测试正常，本轮未重复改写。
- 视觉模型继续使用已配置的提供商和端点，未增加 fallback 或再次发起付费模型请求。

原生 API 返回表示输入事件插入，并不证明用户业务完成；本轮继续保留此区别。依据：[微软 SendInput](https://learn.microsoft.com/en-us/windows/win32/api/winuser/nf-winuser-sendinput)、[MOUSEINPUT 坐标协议](https://learn.microsoft.com/en-us/windows/win32/api/winuser/ns-winuser-mouseinput)。

## 本轮验证

环境：Windows，Python 3.12.10，Tk；使用本机既有依赖。测试进程设置 `PYTHONIOENCODING=utf-8`、`NO_PROXY=localhost,127.0.0.1`，未修改持久环境。GUI 套件使用 `PYTHONPATH=.;forge-gui`。

| 套件/命令 | 结果 | 用时 |
| --- | --- | --- |
| 专项：命名锁、坐标点击、截图视觉 | **39/39** | 18.421 秒 |
| `python -m unittest discover -s tests -v` | **257 项：256 通过、1 符号链接权限跳过** | 128.634 秒 |
| `python -m unittest discover -s forge -t . -v` | **229 项：228 通过、1 符号链接权限跳过** | 36.272 秒 |
| `python -m unittest discover -s forge-gui -p test_interactions.py -v` | **24/24** | 41.724 秒 |
| `python run.py selftest` | **573/574** | 见日志 |
| `git diff --check` | 通过 | — |

后台新增 8 个测试方法，专项包含在后台统计内，不重复累计。后台和核心回归并行运行，GUI 在原生点击用例结束后启动；用时受并行负载影响，不用于性能对比。

真实自建窗口再次完成：截图 → 已知测试落点确认 → 原生点击 → 客户区显示 DONE → 新截图回执；DPI aware/unaware 两种目标均通过，移动窗口及复用旧观察均拒绝。自动确认只用于本进程创建的测试窗口，没有操作用户应用。Work Agent 的定位/点击/回执关联通过离线模型和真实执行器验证；本轮未重新调用豆包服务，前一轮在线证据不能算成本轮新测试。

全量 selftest 唯一失败仍为 `global:readme-no-hardcoded-counts`，文件 `README.zh.md`，与基线已记录失败一致；未放宽守卫或修改 README，不能宣称全量检查全绿。首次专项命令使用 `tests.*` 模块名时因目录不是包导入失败，随后改为 `PYTHONPATH=.;tests;forge-gui` 下的模块名运行；39 项实际执行通过，导入错误不计为测试通过。

日志在 `C:\Users\匡溯昀\`：`forge-coordinate-review-targeted.log`、`forge-coordinate-review-backend.log`、`forge-coordinate-review-core.log`、`forge-coordinate-review-gui.log`、`forge-coordinate-review-selftest.log`。

## 文件与兼容性

- `forge/runtime_lock.py`：新增命名资源锁、可取消获取，原文件锁契约不变。
- `forge/computer_use.py`：组合取消 token、统一电脑操作锁入口；`computer_observe`/`computer_act` 沿用原工具协议。
- `forge/computer_coordinates.py`：稳定的桌面锁和贯穿生命周期的撤销信号。
- `forge/computer_win32.py`：修正模块说明，使其反映已支持的物理输入；未修改原生输入算法。
- `forge-gui/computer_panel.py`：取消回执呈现、旧落点清理、共享 token 接线；停止按钮文案覆盖点击和分析。
- `forge-gui/click_approval.py`：原子确认、异常隔离与清理。
- `tests/test_named_resource_lock.py`（新增）、`tests/test_computer_coordinates.py`、`tests/test_computer_vision.py`：回归用例。
- `docs/computer-coordinate-click.md`：新增宿主锁、取消、升级及 GUI 状态说明。
- 本报告。

`TargetCancellation` 现由 `forge.computer_use` 提供，原 `computer_panel.TargetCancellation` 导入仍兼容。`RunRequest`、`ActionRequest`、工具参数和结果字段不变；无新 pip 依赖。

## 部署和已知限制

使用独立分支的 GUI；升级需要退出并重启所有旧版 Forge 实例，旧进程不认识新的锁命名协议，不支持跨版本滚动互斥。命名锁协调同一用户及系统临时目录下的 Forge 进程；不提供跨用户、不同临时目录或不同机器的桌面互斥。

客户区严格像素比较仍可能拒绝动画、光标和焦点变化。仍仅支持单次左键点击；受保护窗口、UIPI、安全桌面不绕过。锁不能阻止用户/第三方程序改变桌面，命中检查与系统输入之间仍有竞态。第三方宿主回调必须遵守取消 token；无法强杀任意 Python 回调。未知状态仍需用户检查现场，不承诺任意工具 exactly-once。
