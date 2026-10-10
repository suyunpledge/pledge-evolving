# Forge 桌面自动化、连接器与修改预览

## 使用入口

主界面右上角 **连接与任务**，或 **更多 → 定时任务 / 连接器 / 代码预览**。
各页可滚动、按钮在窄窗口换行，**返回工作区** / Escape / 关闭均关闭独立面板，主导航的 parent、order 和 dock 不变。
延续现有深色风格，增强按钮轮廓、交互强调色、正文行间距和图标；事前规划入口移到工具行，避免高 DPI 长草稿挤压消息区。

## 连接器范围

| 服务 | 读取 | 创建 / 修改 |
|---|---|---|
| GitHub | 仓库、Issue、PR、文本文件 | 创建/修改 Issue 与 PR，创建/更新仓库文件 |
| Outlook | 邮件摘要 | 发送邮件、创建/修改草稿 |
| 日历 | 事件 | 创建/修改事件 |
| OneDrive | 文件列表 | 创建/替换文本文件，重命名文件 |
| SharePoint | 站点搜索、文件列表 | 创建/替换文本文件 |
| Teams | 已加入团队、频道、频道消息 | 发送/修改频道消息 |
| Excel | 指定工作表区域 | 修改区域值，最多 100×100 个单元格 |

第一版不是 Office 二进制文档编辑器：没有实现 Word/PPT 全格式编辑，也没有实现 Teams 私聊、删除、分页遍历或任意 URL 请求。列表只返回第一页。
GitHub 更新文件需要现有 sha，修改 Teams 消息须满足账户及 Graph API 的编辑限制。Excel 使用 values 矩阵，拒绝公式入口。

### 配置

- GitHub：填写自己的 Token，账户本身需要对应仓库/API 权限。
- Microsoft：填写自己的 Entra **公共客户端**应用 Client ID、Tenant 和所需 delegated scopes，启用设备代码流程；点击登录后按界面中的地址和代码完成授权。
- Microsoft 功能还受到账号类型、租户管理员同意、许可证和 API 限制影响；GUI 保存凭据并不等于所有服务已验证可用。
- **读取、写入、允许 Agent 使用**分别授权，默认关闭。即使关闭 Agent 使用，用户也可在连接器页手动操作已授权的能力。
- 每次写入展示完整结构化内容并确认；授予写入权限不会跳过逐次确认。超过 12,000 字符的确认内容直接拒绝，需拆分操作。
- 凭据走既有 Secret Store；普通 SQLite 配置、工具 schema 和 Gateway 环境中不保存/传递连接器 Token。Windows 文件权限沿用系统 ACL，当前存储不是 Credential Manager。

## 权限与故障边界

连接器 → Forge ToolRegistry → Policy → 账户/能力授权 → 每次写入确认 → HTTP → Audit。

| 场景 | 执行行为 | 验证 |
|---|---|---|
| 未授予 Agent / read / write | 隐藏对应 schema，实际调用再次验证 | 授权组合、旧 runtime 调用测试 |
| Policy 拒绝、只读模式写入 | 即使有账户权限和确认也拒绝 | deny 优先、READ_ONLY 测试 |
| 用户打开确认时撤销授权 | 比较当前授权 revision，拒绝旧请求 | 确认期间撤销测试 |
| OAuth/刷新完成前退出账号 | 凭据更新与账户 revision 在同一数据库写锁内确认，旧回调不能复活账号 | 迟到凭据回调测试 |
| URL / 路径注入 | 固定 GitHub / Graph 主机、编码资源参数、拒绝路径遍历，不提供通用 HTTP 工具 | 资源路径测试 |
| API 返回敏感文件 | GitHub base64 文件先解码，再经过 SecretScope 和 Redactor | 编码 Secret 输出测试 |
| 外部写入含 SecretValue / SecretRef | 拒绝导出 | Secret 检测边界测试 |
| 超大 HTTP 响应 / 重定向 / 上游异常 | 2 MiB 上限、拒绝重定向、只返回受控错误 | 传输边界与错误测试 |
| 定时任务跨实例同时到期 | SQLite 原子 claim，每次只领一个；本次执行在付费请求前持久化 | 双实例并发领取测试 |
| 定时任务中断 | 显示 interrupted、关闭任务，不自动重放付费调用；需手动恢复 | 中断及旧完成回调测试 |
| AI 修改等待确认时被外部编辑 | 哈希版本检查拒绝覆盖 | 预览到执行的变更测试 |
| 文件替换失败 | 原文件保留，清理临时文件 | 原子写失败测试 |
| 工作区切换期间发送 | 保留草稿，等新 Gateway 就绪；不把新项目请求发给旧工作区 | 切换状态测试 |

连接器工具名保留给 Forge，第三方插件不能用同名工具替换连接器。

这里是 Forge 执行层的权限门，不是操作系统级沙箱，不能防御已在同一宿主进程运行的任意恶意 Python 代码。已经发送到服务端的写入无法通过本地撤销追回。
GUI 使用可取消 HTTP 传输和 20 秒请求截止时间；DNS 解析仍受操作系统控制。独立 urllib 后备调用只有 socket timeout，不承诺绝对的进程终止。

## 定时任务

支持一次执行及分钟间隔重复，可指定独立 provider/model、项目目录和任务内容。**仅在 Forge 打开时执行**，没有创建 Windows 常驻服务。
调度 Agent 与前台对话独立，只读、最多六步、每次请求最多 2048 输出 tokens、三分钟取消计时；不自动写文件或调用连接器。失败和实际输出保存在任务列表，不编造对话。
最多 100 条已保存任务。多个 GUI 实例共享同一 home 的数据库；间隔任务不补跑所有错过的周期。

## Agent 模型和通信

现有 **Agent 集群与分工**页可为每个 worker 选择独立 provider/model，或明确跟随主模型。每路复制配置/环境，创建独立 ModelRouter，复用 Vendor Adapter 和 Secret 边界。
同名模型必须绑定 provider；显式配置失效时显示错误，不偷偷回退到另一厂商。
**允许 Agent 相互交流**默认关闭，用户开启后交换本轮实际结果，最多三轮；同伴内容作为不可信 user 数据，不升级为 system 指令。额外交流会增加模型调用费用。
独立模型配置适用于 GUI 的集群/分工 workers；原有 CLI 原生 spawn_subagent 仍遵循核心 router 设置。

## 项目与修改预览

**代码预览 → 选择项目工作区** 独立于 Forge 后端源码目录。真实文件树/Git Diff 使用所选项目；项目切换不改变 Forge 可执行入口。
对话中经 Gateway 中介的 write_file/edit_file/apply_patch/delete_file/edit_config 修改先显示实际预览，允许同意、拒绝或取消；最长等待五分钟。
默认不同步。开启自动同步后仍执行 Policy、Secret validator、路径边界和版本检查。
任务 CLI 模式在关闭同步时使用只读策略；开启后按既有 Policy 写入，当前没有对 CLI 的每一步提供 staged preview。
每次最多 40 个文件，每文件最多 512 KiB，完整预览最多 120,000 字符；超限拒绝并要求拆分，不静默截断后让用户批准。
这仅覆盖 Forge 中介的文件工具；不声称能截获任意 shell 或恶意插件自身的磁盘写入。

## 参考与验证

参考本机 AI Platform 的非写作 sub-agent.ts 和 code-workspace.tsx 的独立 worker、真实结果汇总和工作区预览设计。没有移植其写作模式或明文凭据持久化。

自动化验证命令：

```powershell
python -m forge.selftest
python -m unittest discover -s forge -t . -p 'test_*.py' -q
python forge-gui/run_offline_checks.py
```

新增测试：forge/test_desktop_services.py、forge-gui/test_desktop_features.py。现有 DPI、导航、规划、复审、Secret、插件生命周期测试继续运行。
测试均使用临时目录和模拟 HTTP/模型调用；**未用真实账户登录、未执行真实 GitHub/Microsoft 写入、未产生付费模型调用**。

API 依据：

- [GitHub Issues](https://docs.github.com/en/rest/issues/issues)、[Pull requests](https://docs.github.com/en/rest/pulls/pulls)、[Repository contents](https://docs.github.com/en/rest/repos/contents)
- [Entra device authorization](https://learn.microsoft.com/en-us/entra/identity-platform/v2-oauth2-device-code)
- [Graph sendMail](https://learn.microsoft.com/en-us/graph/api/user-sendmail?view=graph-rest-1.0)、[Create message](https://learn.microsoft.com/en-us/graph/api/user-post-messages?view=graph-rest-1.0)
- [Drive upload](https://learn.microsoft.com/en-us/graph/api/driveitem-put-content?view=graph-rest-1.0)、[Teams channel messages](https://learn.microsoft.com/en-us/graph/api/channel-post-messages?view=graph-rest-1.0)
- [Excel range update](https://learn.microsoft.com/en-us/graph/api/range-update?view=graph-rest-1.0)

### 本次验证记录

- 核心 selftest：569/569。
- 核心单元测试：229 项，228 项通过、1 项平台限制跳过。
- 新增功能、导航、视觉、启动、插件权限组合复验：67 项通过。
- 首次全量桌面运行：483 项，4 个失败和 4 个错误（含两条过时断言/探测桩、测试 Secret 状态互相污染、关闭窗口后 Windows 文件锁和工作区测试替身缺少新接口）。相关项已修复并复验；没有将首次结果改写为“全量全绿”。
- 测试关闭窗口后等待后台文件任务退出；运行中的 UI 关闭路径不阻塞等待线程。离线测试仅在独立测试场景之间恢复模拟 Secret 注册状态，生产 Secret 防护规则未放宽。

- Secret/插件权限/高 DPI 组合复验：46 项通过。
- GUI 模型工具调用 → 用户确认 → 模拟 GitHub 写入全链路：通过；捕获的两次模型请求均不含连接器凭据。
- 尚未重新运行完整桌面套件；首轮失败涉及的相关用例已在组合复验中通过。
