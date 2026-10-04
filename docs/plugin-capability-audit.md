# 插件能力链路审查矩阵

2026-10-04；“原行为”描述本次改动前的实现。此表只描述源码与测试，不代表已打包发布。

| 攻击 / 故障场景 | 原行为 | 风险 | 修复 / 当前限制 | 测试 |
|---|---|---|---|---|
| 安装即被误认为具备能力 | 目录条目仅声明，界面只显示启用 | 用户误以为功能生效 | 分开显示安装/知悉/启用/授权/可调用；声明条目明确不可执行 | UI pending_confirmation；builtin 生命周期 |
| 知悉等于全部信任 | Python ack 后可执行任意宿主访问 | 授权粒度过大 | GUI 知悉与 grant 分离，每项能力及每个范围单独选择 | install_ack_enable_grant；grant_dialog |
| 插件绕过 Policy | GUI 直接调用 Python worker | Python 可访问进程/文件/网络 | Agent 只接入不执行 Python 的 broker，所有 target 调用 Forge 网关 Policy | python_import_never_runs；real_gateway_policy |
| 授予 repo.write 覆盖只读策略 | 没有 grant 层 | 插件自行决定权限 | grant 不改变 Policy；真实只读网关返回 deny | grant_cannot_override；real_gateway_policy |
| Policy 拒绝插件别名但底层 read_file 允许 | 只检查底层工具 | 别名/插件/能力的拒绝规则失效 | 网关逐项裁决别名、plugin 命名空间、能力，再裁决底层 target | real_gateway_policy_and_audit_on_http_path |
| 旧网关忽略插件上下文 | 原协议没有插件 Policy 上下文 | 新前端误以为拒绝规则生效 | 协议版本探测，旧服务不发送插件 POST 请求 | older_gateway_cannot_silently_ignore_plugin_context |
| Policy ASK 尚未批准却执行 | 原 ToolRegistry 只拦截 DENY | 没有审批也能写入 | 注册表对未解决 ASK 返回 requires_approval，不执行 handler | unresolved_ask_never_executes_plugin_target |
| 未声明 / 未支持的能力 | permissions 只是告知 | 越权访问 | 拒绝未声明能力；process.exec/network 不能 grant | undeclared_grant；unsupported_process_network |
| 多实例状态不一致 | 已有状态锁与刷新 | 新增授权可能被过期实例复活 | grants 使用原有事务与代次；跨实例撤销生效 | cross_instance_grant_revoke |
| 更新或运行期间修改文件 | 原指纹信任门保护 Python | 旧授权可能继承新包 | 完整包指纹进入 ack/grant；变动撤销启用及所有授权 | file_change；disable_uninstall_update；ack_review |
| 禁用 / 卸载后旧 runtime 调用 | 已有 Python proxy 检查 | 新 broker 保存旧元数据 | 每次准入重新检查；注册 handler 本身也检查 | saved_registry_handler；cross_instance；disable_uninstall_update |
| 同名工具 / 内置重名 | 原 worker 已拒绝冲突 | 新别名可能覆盖内置 | 整组校验后注册；拒绝内置、forge_ 前缀别名、重复名 | collisions；duplicate_names；原 adversarial 冲突测试 |
| 参数/schema 畸形、超大请求 | Python schema 已有有界校验 | 新 target 参数扩张 | 封闭平面 schema + 宿主参数类型 + 256 KiB 请求限额 | collisions；arguments_and_result_limits |
| 不可序列化 / 超大结果 | 原 worker 限额 | 桥错误导致对话中断 | 校验 JSON，结果限额，错误转为单次工具失败 | arguments_and_result_limits；audit_success |
| 目录穿越、绝对路径、链接、设备 | Python 无路径边界 | 窃取/修改宿主文件 | target 路径规范化并限制到当前授权工作区 | path_escape；Windows path edge tests |
| 工作区内硬链接关联外部文件 | 规范路径仍落在工作区 | 读写相同 inode 越界 | 拒绝链接数大于 1 的文件和非普通文件 | hardlink_cannot_read_or_modify_file_outside_workspace |
| 工作区或对话切换 | Python 工具无 scope | 授权跨会话泄漏 | grant 绑定规范 workspace + 可选 session；发送时固定上下文 | workspace_and_session_scope；GUI scope tests |
| 快速创建对话时毫秒 ID 碰撞 | 同毫秒可能生成相同 session ID | 不同对话共享 session grant | 新 ID 使用随机 UUID；已有历史 ID 继续可读 | new_dialogs_do_not_reuse_grant_scope_when_clock_matches |
| 修改对话授权但仍继承工作区写权限 | 原来没有逐项授权 | 界面给出虚假的“收窄” | 显示工作区继承提示；逐范围编辑，全部撤销入口 | scope_inheritance UI test |
| 并发调用与撤销竞态 | 原 worker 使用锁及代次 | revoke 返回后继续新调用 | 新 broker 状态锁线性化，撤销后准入拒绝 | revoke_and_invocation_have_a_linear_order |
| Audit 无法写入 | 旧显示日志错误静默 | 执行无留痕 | 严格准入审计，写失败不执行；结果失败不能回滚宿主操作 | audit_unavailable；audit_success |
| 把配置/授权目录包含在 workspace | 文件根授权未区别控制文件 | 插件改写授权或宿主代码 | 禁止访问 Forge home；禁止改写当前宿主 GUI/框架代码和启动入口 | forge_control_state；plugin_cannot_rewrite_its_host_source |
| 结果审计失败或 RPC 失败后的盲目重试 | 错误没有执行阶段信息 | 重复产生副作用 | 已 dispatch 后的异常附 execution_may_have_completed | result_audit_failure_reports_possible_side_effects |
| import/register 卡死、超时、崩溃 | 原进程树隔离/超时 | 主 GUI 或 worker 失控 | GUI 不 import Python；旧诊断边界保留原故障回归 | python_import_never_runs；原 runtime/adversarial 套件 |
| 把 RPC timeout 宣称为终止执行 | 旧 Python 20 秒实际能杀 worker 树 | 新 RPC 不可杀宿主 I/O | 明确 3 秒 RPC 仅限制等待；目标只允许有界文件类工具 | request limits；真实 HTTP 路径 |
| 伪称沙箱 / 签名验证 | 原架构文档宣称未实现的保障 | 用户在错误安全认知下授权 | 文档与 UI 明确：指纹校验、受控 broker；无 OS 沙箱和签名 verifier | 代码路径检查；UI boundary labels |
| 插件 Policy/Hook 自行放权 | 原架构为计划草案 | 不受审查的扩展执行 | 保留声明类型并纳入指纹；未实现的类型不激活 | contributions_are_fingerprinted_but_not_executed |
| 市场退出改变 sidebar | 旧导航已经修复 | 新入口可能重挂载 | 复用原工具视图与固定返回路径 | main_entry_opens_market_and_returns；原 navigation 套件 |
| 200% DPI 筛选按钮被裁切 | 原搜索及分类挤在同一行 | “集成”按钮文字被吃掉 | 搜索和类别分成两行；检查所有固定按钮实际宽度 | market_controls_fit_at_all_supported_scales |

签名验证、任意代码的真正系统沙箱、多类型贡献执行器均未实现；不能把“知悉”或进程隔离
描述成这些能力。旧 Python `PluginRuntime` 是兼容/诊断入口，直接使用该 API 仍是不受 Policy
限制的本机代码执行；生产 GUI/Agent 路径禁止它。
