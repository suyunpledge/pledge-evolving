# Coding 发送未就绪：根因、恢复与交互链路优化

日期：2026-10-18。基线：`14edfeb653f9a9d71f5398cc808a1c238d70cd55`。
分支：`fix/coding-runtime-readiness`。
工作树：`C:/Users/匡溯昀/forge-backend-workflow`。

## 这次提示的真实含义

上一轮模型参数类型错误的修复已经由主线 `8fd7d2e` 集成。
本次提示来自其就绪守卫，不是发送按钮本身仍然无效。
只读检查发现 `~/.forge-development/development.json` 的 `project` 和 `session`
均为空；Coding 还没有选定项目，自然未调用 Runtime 初始化。
用户重复点击只会在相同未就绪状态下重复追加同一条提示。

还存在下一步会遇到的独立问题：宿主 Work 使用 `~/.forge/forge.patch.json`
与 `secrets.json`，Coding 使用 `~/.forge-development`，后者没有自己的模型配置。
旧实现把“执行状态隔离”连同“模型设置来源”一起切断，初始化仅得到打包示例模型，
并没有接入用户已保存的桌面模型设置。

对真实设置只做本地解析，没有发送供应商请求：得到 **18 个 Provider、28 个模型**，
默认路由通过结构与密钥存在性检查。没有输出、复制或修改原始密钥。
该结果不能证明网络、额度或供应商认证正常。

## 已落实的修复

| 优先级 | 问题与源码证据 | 修复 |
|---|---|---|
| P1 | `development_mode/app.py:send` 未就绪只提示；宿主只在有保存项目时调用 `open_project` | 首次发送引导选择目录，异步初始化后仅提交原始未变更草稿一次；取消保留草稿 |
| P1 | `forge_gui_v2.py:_build_coding_surface` 只传独立 home；`EditorBridge.initialize` 只从该 home 建路由 | 显式传入宿主模型设置来源，经 allowlist 投影生成独立路由；执行权限与状态仍隔离 |
| P1 | `DevelopmentRuntime.initialize` 在 checkpoint / 记忆状态写入之前设置 ready | 初始化成功后才提交 ready；失败关闭部分 bridge、重建可重试实例并保留脱敏原因 |
| P1 | 载入错误后没有发送恢复动作，重复提交始终停在泛化提示 | 修复后再次发送可重试已选择项目；线程启动异常也复位 loading |
| P1 | 尚未初始化就绪的待发送意图不在原取消 token 生命周期内 | 取消覆盖加载中及加载结束、idle 自动发送前的窗口；换草稿阻止自动发送 |
| P2 | 桌面设置保存后 Coding 路由与模型目录保持旧值 | 空闲新提交前刷新模型，保留 Agent 会话、历史、Checkpoint 与 Policy；失败原子保留旧路由并拒绝该次发送 |
| P2 | 只用 runtime.ready 无法识别空模型目录、无效默认模型、缺少 API 地址或密钥 | 添加公开 readiness 元数据及具体配置错误；不创建伪执行任务，不消耗失败草稿 |
| P2 | 重复点击追加相同拒绝提示 | 对连续相同且仍可见的提示去重；状态栏与会话原因仍可见 |
| P1 | UI worker 仅接住 Exception；Agent 的 KeyboardInterrupt/SystemExit 可终止线程而不回传完成状态 | 捕获 worker 边界的 BaseException，回传中断原因并释放 UI 提交状态；不自动重放中断任务 |
| P2 | 新 Coding 工作台可能仍使用 ModeState 的 General 默认值 | 宿主首次挂载明确选择 development，仅改变 Coding 独立状态 |

## 公开接口和隔离边界

详见 [接线契约](../docs/coding-runtime-readiness.md)。

- `DevelopmentWorkbench(..., model_settings_home=work_home)`：可信宿主参数，默认省略保持旧独立行为。
- `ModelSettingsSource`：仅传递已启用 Provider 和模型路由；兼容桌面省略 `name` 的旧格式。
- `EditorBridge(..., model_router_factory=...)`：只替换模型路由构造；不是 RPC 参数，不能替换执行 Config。
- `DevelopmentRuntime.readiness(explicit_model=None)`：返回 `ready/code/message`。
- `DevelopmentRuntime.refresh_models()`：持有原执行 lane，在完整构建成功后原子替换模型；运行中不刷新。

独立 Coding 模型配置优先；否则读取宿主保存的模型行。模型表达式使用私有映射解析，
不会将密钥写入进程环境，不复制 Work 配置/密钥文件，不导入 Work Policy、工具 expose、
插件、Session、记忆或 checkpoint。Connector 凭据不参与模型投影。
默认仍为 read-only，用户选定项目不会自动授予写权限。

没有重写 Agent、调度器、GUI 主框架或布局；宿主接线只增加模式初始化与模型来源参数。
原 VS Code 初始化、凭据向导及 RPC 路径保持兼容。

## 修改清单

| 文件 | 修改 |
|---|---|
| `forge/development/models.py` | 宿主模型投影、私有凭据解析、结构就绪检查 |
| `forge/development/runtime.py` | 初始化提交/失败清理、公开 readiness、lane 内模型刷新 |
| `forge/vscode_bridge.py` | 可信宿主路由工厂接口、模型目录构建、原子刷新 |
| `forge-gui/development_mode/app.py` | 首次发送准备与恢复、草稿/取消守卫、配置刷新与通知去重 |
| `forge-gui/forge_gui_v2.py` | 仅 `_build_coding_surface` 的模式与模型来源接线 |
| `forge-gui/run_development.py` | 修正启动器说明，区分模型设置读取与执行状态隔离 |
| `tests/test_development_readiness.py` | 12 项后端隔离与恢复测试，含真实两轮 Agent 和文件写入 |
| `forge-gui/development_mode/test_readiness.py` | 11 项真实 Tk 交互与恢复测试，含真实模型中断后的下一次发送 |
| `forge-gui/development_mode/test_submission.py` | 旧未就绪用例适配新的“可重试”流程 |
| `forge-gui/test_workspace_wiring.py` | 真实 ForgeGuiApp 首次发送与配置隔离集成测试 |
| `tools/verify_coding_send.py` | 将新增交互测试和全部相关源码纳入现有串行验收 |
| `docs/coding-runtime-readiness.md` | 稳定接口、接线与兼容性说明 |

## 验证

最终运行结果与源码/日志摘要由相邻 `coding-runtime-readiness-validation.json` 记录。
使用 Windows 11、Python 3.12.10、Tk 8.6.15、PyYAML 6.0.3，GUI 套件串行运行。

| 最终验证范围 | 通过 | 跳过 | 失败 / 错误 |
|---|---:|---:|---:|
| 发送、就绪、workbench、Agent、Workspace、视觉集成 | 100 | 0 | 0 / 0 |
| Coding 布局与原保护方法 | 64 | 0 | 0 / 0 |
| `tests/` 全量发现 | 168 | 1 | 0 / 0 |
| `forge/` 全量发现 | 228 | 1 | 0 / 0 |
| Forge selftest | 574 | 0 | 0 |
| 指定 GUI 适配器回归 | 120 | 0 | 0 / 0 |

各行按运行范围列出，不累计为互不重复的用例数，也不将指定 GUI 范围称为全仓全部 GUI 测试。
两项跳过对应 Windows 账户/平台符号链接限制。日志目录为
`C:/Users/匡溯昀/forge-readiness-validation`，命令、时长、原始日志与 SHA-256
均记录在验证 JSON。可复跑：

```powershell
python -B tools/verify_coding_send.py --output C:/Users/匡溯昀/forge-readiness-recheck
```

首个串行驱动退出码为 **1**：44 项 App/Agent 中有 1 项暴露自定义 Router 兼容问题，
之后源码又增加中断边界修复，所以源码一致性检查也正确报告 false。
没有把该原始驱动改写为一次性成功。后端全量和 selftest 的独立驱动退出码为 0；
修正就绪检查后，并在最后 UI worker 中断修复之后，重跑全部相关 GUI 100 项，退出码为 0。
模型投影、共享 Bridge 与 Runtime 在后端完整运行后保持相同内容摘要；
最后变化仅为 GUI worker 捕获中断及新增对应 GUI 测试，已由 100 项重跑覆盖。

最终 GUI 日志仍有 **3 条**删除后的 Tk `<lambda>` 回调警告；
`forge/` 全量日志有既有未关闭子进程管道的 `ResourceWarning`。均无失败断言，
本轮没有据此宣称全部组件销毁和进程资源回收警告均已清理。

新增后端闭环真实经过 DevelopmentRuntime → EditorBridge → Agent → ActionExecutor，
读取并把 `one.py` 从 `print(1)` 改为 `print(2)`，随后执行第二轮对话，验证任务身份不同、
模型路由正确、会话与执行状态隔离。模型供应商传输由离线 transport 控制。
新增 GUI 测试从真实按钮 `.invoke()` 开始，另验证连续三轮输入保持原会话和历史。

测试过程发现并修正的问题：Windows 临时目录长/短路径的比较需要先 resolve；
ConfigError 有自己的异常类型；真实宿主集成暴露了省略 Provider name 的旧配置格式，
据此增加兼容与独立回归。未通过删除断言掩盖真实失败。
完整 GUI 回归还发现既有借用宿主的自定义 Router 使用自己的传输契约，即使具有空
`providers` 属性也不等同于标准 ModelRouter 的空配置。就绪检查现仅对标准路由
执行结构检查，保留可信自定义宿主的契约，并新增独立回归；原失败测试保留其全部断言。

新增验收共 24 项：后端 12、Tk 交互 11、真实桌面宿主首发 1。
真实配置的启动探针见相邻 `coding-runtime-readiness-local-probe.json`：
无供应商请求、独立执行存储、read-only，原始源文件字节未改变。

## 提交与接入

实施提交：`848fb0a`（`fix(coding): initialize and recover agent interactions reliably`）。
验证与报告在紧随的文档提交归档，可用 `git log -2 --oneline fix/coding-runtime-readiness` 查询。

`coding-runtime-readiness.patch` 包含五个生产文件，已对当前主线完成
`git apply --check --ignore-space-change` 验证；没有实际应用。
主线宿主仅 `_build_coding_surface` 方法发生接线变化；八个受保护布局方法与基线 AST 一致。
未修改插件市场、Desktop Awareness、GUI 布局或主题；未推送，未合并主分支。

## 使用与已知限制

加载修复后，首次在 Coding 发送会打开项目选择器。选择实际项目后，未改变的原始草稿
会继续提交；取消或更改草稿则保留内容，等待下一次明确发送。
初始化或模型设置错误会保留真实原因，修正并保存后再次发送即可重试。
需要修改代码时仍应明确选择工作区写入权限。

源码变更在独立工作树，当前主工作区和已运行的 Python GUI 进程不会自动加载它。
按此前边界未直接合并主分支；接入后需在任务空闲且未保存内容已妥善保留时重新加载 GUI。

- 不能保证供应商网络、账号、额度或认证持续可用；此类错误仍由原执行链路报告。
- 路由刷新按设置文件 mtime/size 识别正常保存操作；刻意同时保持二者的外部改写需重新打开应用。
- 运行中与已排队请求保留已接受的配置，不在中途替换模型。
- 取消是合作式停止，不回滚已执行工具，不重放未知结果。
- 草稿、待发送意图和队列不提供应用崩溃后的持久恢复。
