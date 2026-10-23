# Computer Use 观察与执行闭环

基线 `bb125e9`，独立分支 `feature/computer-use-observe-act`。主检出未改动、未合并。环境 Windows / Python 3.12；仅使用标准库，没有新增 pip 依赖。

## 本轮落实

参考 [OpenAI Computer Use 集成流程](https://developers.openai.com/api/docs/guides/tools-computer-use-integration) 的驱动接口和执行后反馈，以及 [OpenClaw 浏览器工具](https://docs.openclaw.ai/tools/browser/agent-tools) 的观察引用与目标绑定，新增可以实际运行的第一阶段原生控件后端。

1. `forge/computer_use.py`：新 ComputerSession 与 `computer_observe/computer_act`。作用域隔离的观察 ID、唯一元素引用、状态指纹、过期检查、单次引用消费、明确支持的动作集合、宿主逐动作审批、撤销 Event、输入前取消检查、动作后新观察。任一输入会使其他会话旧观察作废，防止外观没变时复用旧引用。
2. `forge/computer_win32.py`：宿主显式绑定窗口 HWND 和所属 PID；读取 Windows 标准控件，支持 Button 点击、Edit 中文文本替换。拒绝越界/失效目标，不向全局焦点发送输入；密码字段不读取且不可填，readonly Edit 不可填，自绘或其他控件不公布动作。
3. `forge/gateway.py`：新增可选 `computer` 宿主能力参数，在发现及执行上下文中传递；当前能力撤销或替换会阻止旧 run 的新操作。HTTP 调用不能附加/构造能力对象；Policy、授权、公共 ActionExecutor、动作日志和 Work unknown 停机继续有效。
4. `tests/test_computer_use.py`：覆盖单次引用、外部状态变化、过期、无审批/审批期间变化、READ_ONLY/PLAN、缺失能力、输入前后取消、撤销、输入/回读失败、跨作用域、并发重用、本地 HTTP 能力撤销，以及实际 Windows 控件执行。
5. [公开宿主接线与能力边界](../docs/computer-use-host-api.md)：提供 Gateway 接入示例和状态处理表。没有重写 GUI、插件市场、Desktop Awareness 或 Coding Mode。

## 真实执行证据

测试创建属于测试进程的隐藏窗口，包含原生 Edit 和自动复选按钮：读取控件状态 → 替换为中文内容 → 点击按钮 → 回读文本和选中状态。再经公共 ActionExecutor 完成一次 observe→type_text→post-observation，并核对持久化日志中两条 tool_completed。最后销毁测试窗口，确认旧窗口不能继续观察。没有枚举或操作用户应用，也没有全局鼠标/键盘输入。

真实本地 HTTP 测试验证受保护作用域 A 的观察不能被 B 使用；A 可在新的 run 中消费观察；宿主清除能力后原 run 无法再执行。故障驱动测试在输入已发生后注入异常，确认返回 unknown 且旧引用无法重放。

## 验证结果

- Computer Use 专项 12/12 通过；最终严格观察校验后再次通过。
- `unittest discover -s tests -v`：218 项，217 通过、1 符号链接权限跳过，110.249 秒。
- `unittest discover -s forge -t . -v`：229 项，228 通过、1 符号链接权限跳过，36.576 秒。
- 全量 `run.py selftest`：573/574。唯一失败仍是已有中文 README 硬编码检查数守卫，没有修改守卫来掩盖失败。
- `git diff --check` 通过。

仅在测试进程配置 `NO_PROXY=localhost,127.0.0.1`，避免本机 HTTP fixture 经环境代理转发；未更改用户持久网络配置。首轮专项的只读观察 fixture 未提供显式观察 grant，随后按宿主接口补齐；HTTP fixture 最初使用无效 scope 字符串，改成已有协议要求的 32 位小写 hex。均为测试接线修正，未放宽权限或 scope 校验。

日志保存在用户目录：`forge-computer-use-targeted-final.log`、`forge-computer-use-backend.log`、`forge-computer-use-core.log`、`forge-computer-use-selftest.log`。

## 兼容性及限制

- 新能力显式 opt-in；默认启动不附着电脑工具。现有 Work 已可自动消费配置后的 Gateway 工具，但本轮没有新增 GUI 窗口选择器。接入方必须从可信窗口选择记录取得 HWND/PID，并提供审批；缺失审批不会输入。
- 本阶段是 Windows 标准控件自动化，尚不支持截图视觉/坐标点击、完整 UIA 树、浏览器 DOM、Electron 自绘界面、热键及拖拽；不是完整 Codex/OpenClaw Computer Use 的替代品。没有依赖或复刻 Codex 私有运行时。
- 动作成功表示派发确认并取得新状态，不代表用户业务目标完成。`changed` 仅表示观察差异，模型仍应检查目标是否满足。
- 输入或后续观察失败保留 unknown，不自动重放、不自动关闭应用、不自动回滚外部动作。没有任意操作的 exactly-once 保证。
- 驱动使用有界控件消息/观察预算；宿主审批仍需自行响应取消。跨进程锁仅协调 Forge，不能锁住用户和其他软件；观察到输入间仍存在外部状态竞态。
- 修改共享执行边界仅限 Gateway 的可选宿主能力传递与撤销检查；不改默认权限，不把电脑控制默认赋予 Code 模式或子 Agent。

修改清单：`forge/computer_use.py`、`forge/computer_win32.py`、`forge/gateway.py`、`tests/test_computer_use.py`、`docs/computer-use-host-api.md`、本报告。
