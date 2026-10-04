# Forge 插件能力与执行边界

更新：2026-10-04。本文描述当前源码行为；不承诺未实现的沙箱、签名或 CLI。

## 主界面入口

对话页右上角「插件市场」和左栏「工具 / 插件市场」打开同一个市场面板。
「功能开关」仍在相邻标签页；固定返回按钮恢复原来的主布局，不重新创建 sidebar。
扫描、文件指纹、安装和授权在后台线程运行，Tk 主线程只显示结果与确认窗口。

市场支持离线目录、安装本地目录、检查清单、知悉、启停、逐项授权、撤销、卸载和查看调用审计。
内置「工作区文件能力」是可执行的声明式示例。其他旧目录条目如主题或面板，明确显示
“尚未接入受控工具执行”；安装它们不表示对应功能已生效。

## 生命周期

discover → inspect → install → verify → acknowledge → enable → grant → invoke → audit → revoke

| 状态 | 当前含义 |
|---|---|
| installed | 文件已复制到 `~/.forge/plugins/<id>`，未自动执行 |
| verified | 有界扫描并计算当前包文件指纹；**不是发布者身份或签名验证** |
| acknowledged | 用户知悉当前指纹对应的文件和能力声明；没有执行授权 |
| enabled | 可被 Forge 检查和注册；没有具体能力的授权仍不可调用 |
| granted | 用户逐项授予当前文件版本在某工作区或某对话中的能力 |
| active | 当前范围的授权有效，并且 Forge 网关确实提供对应的宿主工具 |

新声明式能力插件必须先知悉再启用。旧格式条目的兼容状态不等于执行权限：授予能力
仍要求当前版本已知悉。安装和更新不自动启用，不继承旧知悉或旧授权。

授权绑定完整文件指纹、清单、工作区规范路径及可选 session ID。默认授权窗口选择
“仅当前对话”，所有对话的授权必须另外选择。当前对话继承工作区授权；编辑对话授权
不会收窄已有工作区授权，窗口明确提示，可通过「撤销授权」移除全部范围。
新对话使用随机唯一 ID，避免毫秒时钟碰撞使不同对话共享授权范围；历史对话 ID 保持兼容。

文件/清单/权限/工具声明变化、禁用、卸载、撤销知悉都会撤销授权。
不同 `Marketplace()` 实例使用同一个状态锁，操作前重新读取状态，避免过期实例复活授权。
调用准入与撤销按状态锁排序：撤销成功后旧 runtime/handler 不能准入新的调用。
已发出的宿主 I/O 若尚未返回，不会因 RPC 超时或撤销被强制终止。

## 声明式工具格式

清单文件名是 `forge-plugin.json`。例如：

```json
{
  "id": "example-repo-reader",
  "name": "工作区读取示例",
  "version": "1.0.0",
  "executes_code": false,
  "capabilities": ["repo.read"],
  "contributions": {
    "tools": [{
      "name": "example_read",
      "target": "read_file",
      "description": "读取已授权工作区中的文件",
      "parameters": {
        "type": "object",
        "properties": {"path": {"type": "string"}},
        "required": ["path"],
        "additionalProperties": false
      }
    }]
  }
}
```

`target` 只能引用宿主固定白名单：

| 能力 | 宿主 target | 状态 |
|---|---|---|
| repo.read | read_file / list_dir | 可逐项授权；限当前工作区 |
| repo.write | write_file / edit_file | 可逐项授权；还需通过网关真实 Policy |
| process.exec | 无 | 尚无受控执行器，不可授权 |
| network.github / 其他网络能力 | 无 | 尚无受控执行器，不可授权 |

旧 `workspace:read` / `workspace:write` 权限规范化为 `repo.read` / `repo.write`；
未知能力、任意 Python、shell 或未声明的能力不产生可执行工具。
参数只支持与目标文件工具匹配的封闭平面 schema；拒绝自定义 callable、代码和未知执行字段。
工具名冲突拒绝整组贡献，不能覆盖 Forge 内置名、`forge_` 别名或先注册的插件工具。

## 真实执行链

```text
manifest + 当前指纹
  → Forge CapabilityRuntime（不 import 插件代码）
  → Forge ToolRegistry 注册工具别名
  → 检查已知悉/启用/授权/工作区/session/代次
  → 检查路径及有界参数
  → 写入调用准入审计
  → Forge gateway /v1/tools/call
  → 网关实际配置的 ToolRegistry + Policy
  → 权限模式与文件路径限制
  → 宿主文件 handler
  → 调用结果、授权裁决审计
```

别名注册表使用额外保守的本地检查，**实际目标工具必须由网关的真实 Policy 再裁决**。
网关还检查插件别名、`plugin:<id>:<tool>` 命名空间和能力名称。因此 `deny: reader_tool`、
`deny: plugin:reader:*` 和 `deny: repo.write` 也会阻止相应调用。
网关通过 `/v1/tools` 的 `plugin_policy_version=1` 声明支持该上下文协议。旧网关不能提供
这一协议时，客户端不发送插件执行请求，市场提示更新并重启；不会静默忽略插件权限上下文。
插件 grant 不修改 Policy allow 列表，不更改 gateway 的 permission profile。
网关无 Policy 时拒绝调用；当前 headless 网关没有交互审批器，未解决的 ASK 降为 DENY。
用户可以按 Forge 原有方式配置 Policy，授权窗口不能自行代替审批。

文件操作限制：规范路径必须留在授权工作区，拒绝越界链接/目录穿越、设备路径和 NTFS
附加数据流；现有文件上限 2 MiB，请求上限 256 KiB，目录上限 2,000 项。
具有多个链接的文件及非普通文件（如管道/设备）不交给插件文件工具，防止硬链接越界或阻塞。
Forge home 中的授权、配置、插件和审计文件不能由插件读取或改写；当前宿主 GUI/框架代码
及启动入口也不能由插件改写，即使这些路径位于授权工作区中。
GUI 使用 3 秒工具 RPC 超时；这是网络等待上限，**不能宣称终止网关内正在执行的 I/O**。
不执行第三方 Python，所以声明式插件不能用 import、子进程或网络调用绕过此链。

该 broker **不是 OS 沙箱**。其他本机进程并发更换文件/链接、底层文件系统 I/O、被篡改的
宿主或网关不在这个边界的保护范围。Forge 的原有路径策略也不是操作系统级资源隔离。

## 多类型贡献

清单保留 `tools / skills / policies / hooks / providers / agents / ui_panels` 一级类型。
每类最多 32 个声明对象，全部进入版本指纹。
当前仅 `tools` 接入宿主执行；其他类型可检查但不加载、不注入模型提示、不挂载面板、不执行 hook，
也不允许插件提供的 `policies` 放宽 Forge Policy。后续需要每种类型独立的受控宿主适配器。

## 旧 Python worker

`plugin_runtime.PluginRuntime` 保留用于旧协议兼容及原有故障隔离回归。
它有指纹信任门、不可变快照、进程树终止、超时及结果限额，**没有 Policy 或 OS 沙箱**。
直接通过该旧 API 执行 Python 仍有本机用户权限。GUI、Agent 对话和市场实际激活路径已经
改用 `plugin_capabilities.CapabilityRuntime`，不调用该旧 API。
当前界面阻止向 `executes_code=true` 插件授予 Agent 执行权限。

## 审计与测试

生命周期记录：`~/.forge/marketplace/log.ndjson`。
插件调用记录：`~/.forge/marketplace/plugin-audit.ndjson`，包括调用 ID、插件、指纹、别名、
宿主目标、工作区、session、准入、dispatch、结果与授权裁决；不保存文件正文或模型提示。
准入审计写入失败会阻止执行。结果阶段磁盘写入失败会报告失败，但不能回滚已完成的文件操作，
先前准入记录仍提供排障线索。
如果已 dispatch 的调用后来失败，会返回 `execution_may_have_completed`，避免调用方把
结果审计失败或网络等待失败误当成“尚未执行”并盲目重试。

离线回归入口：`forge-gui/run_offline_checks.py`，使用临时 home 和本地测试网关。
新增测试见 `test_plugin_capabilities.py`、`test_plugin_market_ui.py`。
攻击与故障矩阵见 [plugin-capability-audit.md](plugin-capability-audit.md)。
