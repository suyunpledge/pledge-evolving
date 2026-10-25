# Desktop Chromium 交付记录

日期：2026-10-25。基线：`6e93dee`。独立分支：`feature/desktop-chromium`。
工作树：`C:/Users/匡溯昀/forge-desktop-chromium`。未合并主分支。

## 交付能力

桌面端「更多 → Chromium 浏览器」可启动发行包自带 Chromium，打开网站或本地开发服务器。
保留现有 Tk GUI，浏览器使用独立窗口，不是嵌入式 WebView。
Work Agent 显式授权后可观察、导航、点击和填写；Coding Mode 可手动使用浏览器。
Coding Agent 工具接线不在本次交付范围内。

浏览器按工作区、模式、聊天分区；不使用系统浏览器资料。撤销授权会取消旧任务授权，模式切换保留浏览器。
动作通过已有 ActionExecutor 持久化身份与结果，宿主确认与 Policy 均生效。
每个会话使用独立辅助进程和串行 Playwright 线程；Windows Job Object 管理所属进程树。
45 秒无回执时回收辅助进程，写操作记为 unknown，禁止自动重试。
观察引用有时效并在导航/操作后失效；不同 run 不能直接借用引用。

## 修改清单

| 文件 | 内容 |
| --- | --- |
| `forge/browser.py` | 浏览器生命周期、进程监督、操作协议、观察引用、分区与完整性校验 |
| `forge/browser_process.py` | Windows 进程树回收与冻结 GUI 的匿名管道支持 |
| `forge/browser_tools.py` | BrowserLane 宿主适配，复用执行上下文/策略/动作执行器 |
| `forge-gui/browser_panel.py` | 地址输入、当前/全部关闭、会话授权 |
| `forge-gui/forge_gui_v2.py` | 菜单入口、Work 工具接线、冲突检测、退出清理、冻结辅助进程入口 |
| `forge-gui/desktop/prepare-browser.py` | 固定依赖版本、构建期下载、真实启动检查、资源哈希清单 |
| `forge-gui/desktop/forge-desktop.spec` | 打包 Playwright 驱动和可信资源清单 |
| `forge-gui/desktop/build-desktop.ps1` | 分发浏览器资源、构建失败保护、冻结浏览器检查、保留现有图标的开关 |
| `pyproject.toml` / `.gitignore` | 可选 browser 依赖、忽略大型运行时目录 |
| `forge/test_browser.py` / `forge-gui/test_browser_panel.py` | 合约、真实浏览器、GUI 接线回归 |
| `docs/DESKTOP_CHROMIUM.md` / 本文 | 使用、接口、构建与验证记录 |

没有修改共享 Policy/ToolRegistry/ActionExecutor 实现，未改变 CLI/Gateway 默认权限，未修改插件市场业务、Desktop Awareness、GUI 布局或视觉资源。

## 验证环境与结果

Windows 11 家庭版中文版 x64，系统版本 10.0.26200；Python 3.12.10；Playwright 1.64.1；Chromium 156.0.8078.4（修订 1248）。

| 检查 | 结果 | 本地日志 |
| --- | --- | --- |
| `python -m forge.selftest`，最终源码复跑 | 574/574 通过 | `build/selftest-final.log` |
| `FORGE_BROWSER_TEST=1 python -m unittest forge.test_browser -v` | 21/21 通过，含 5 项真实浏览器测试；无跳过 | `build/browser-real-final.log` |
| `forge.test_execution_review` | 22/22 通过 | `build/browser-contract.log`（早期浏览器合约和执行回归联合运行） |
| GUI 浏览器接线 | 3/3 通过，包含成功/unknown 两条发送路径 | `build/browser-gui-delivery.log` |
| GUI Gateway 生命周期 | 10/10 通过 | `build/gateway-lifecycle.log` |
| Forge 客户端脚本 | 5/5 通过，包含不可达与错误传播 | `build/client.log` |
| 源码可见 Chromium 窗口启动/关闭 | 通过 | 终端实测 |
| 最终 PyInstaller 构建 | 通过 | `build/desktop-build-final.log` |
| 冻结 EXE 启动 Chromium、匿名管道通信、关闭 | `ok=true, closed=true` | `forge-gui/desktop/dist/browser-smoke.json` |
| 最终 GUI 窗口启动 | 通过；禁止 Gateway 自启动的隔离检查 | `build/desktop-build-final.log` |

真实浏览器只访问本机临时 HTTP 服务和 about:blank。验证了填写→点击→观察、Cookie 隔离、过期引用拒绝、密码/文件字段阻止、超时 unknown、取消与动作身份去重。
没有调用付费模型或外部账户；未宣称端到端线上模型交互及仓库所有独立测试套件均通过。

验证中曾发现测试用 Cookie 名称被现有秘密脱敏器隐藏，已改用不含秘密含义的测试夹具，保留脱敏行为。
GUI 客户端测试采用文件自身入口执行；unittest discovery 的 0 测试结果不计入通过数量。

## 兼容边界与限制

- 核心 Python 依赖保持为空；源码使用浏览器需安装 `[browser]` 并准备运行时，桌面包已包含运行时。
- 分发时必须同时携带 EXE 与 `browsers/`。运行时不会自动下载或借用系统 Chrome/Edge。
- 登录资料为临时存储，关闭分区即清除；最多四个活动分区、每个最多十二个标签页。
- Work Agent 读取需授权；导航/点击/填写逐次确认，BYPASS 不绕过浏览器宿主确认。
- 工具不开放任意 JS/CDP、文件 URL、密码自动填写、上传或下载；iframe 元素暂不枚举。
- 已发出的网页操作无法保证撤销，也不宣称 exactly-once；未知结果需要重新观察后人工决策。
- 实际验证平台为 Windows x64；其他平台和更低 Python 版本未实测。
- Chromium 与 Forge 同机本地管理员权限下的篡改防护不属于本次安全保证；内嵌清单用于发现发行资源缺失或修改。
- 原 GUI 标题仍使用现有 `v0.7.0`，项目元数据为 `0.8.0`，本轮没有修改已有版本标识。

## 构建来源

构建脚本默认使用 Playwright 官方下载流程。此次构建期间 Google 下载连接反复中断，Chromium ZIP 改从 npmmirror 的同版本 Chrome for Testing 镜像获取，再由 Playwright 安装器安装；FFmpeg/Winldd 使用官方 CDN。

- 镜像：`https://cdn.npmmirror.com/binaries/chrome-for-testing/156.0.8078.4/win64/chrome-win64.zip`
- ZIP 大小：207073013 字节。
- 与镜像元数据比对的 MD5：`c0533017d8cb3ad5fb4d869b26fe452f`。
- 下载后测得 SHA-256：`220021ecfdad08c116fc71a153e65e3861ba841c531afc4f7acc7bd5d44b41db`。

上述下载校验不等同于独立的 Google 签名认证；该 Chrome for Testing 可执行文件的 Authenticode 状态为 NotSigned。
冻结 EXE 内嵌本次运行时文件 SHA-256 清单，启动前校验外置浏览器资源。镜像覆盖配置和临时下载脚本不进入源代码或发行包。

## 发行产物

目录：`forge-gui/desktop/dist/`。最终 EXE SHA-256：
`aaef8dbeff69bbe23d359cfd8ddba95878f8d51642f592d9ac5254cd570715c0`。

ZIP 名称：`forge-desktop-chromium-windows-x64.zip`。解压整个压缩包后运行 `forge-desktop.exe`。
ZIP 大小：272689984 字节（约 260 MiB）；运行时文件共 317 个，展开约 502 MiB。
ZIP SHA-256：`06636c43615936b25ad97d85b0c139c038c1d2b3a95dc583e3af174223ad28c0`。
已验证所有 ZIP 条目的 CRC，包内包含使用说明与 Forge LICENSE。
机器可读结果另存于本次构建的 `build/package-result.json`。
源代码提交不包含浏览器二进制、构建缓存或测试日志；需要复核时可在上述工作树查看这些本地产物。
