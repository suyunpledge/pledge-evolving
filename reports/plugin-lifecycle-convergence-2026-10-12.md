# 插件生态架构收敛与生命周期完善 — 2026-10-12

范围：插件与兼容层模块（forge/capability.py、forge-gui/plugin_market.py、
plugin_capabilities.py、plugin_runtime.py、plugin_dependencies.py）。
不涉及 GUI 主框架、Development Mode、Desktop Awareness 内部实现。
共享 Tool Registry（forge/tools.py）仅纯新增一个常量，未改动任何现有行为。

## 一、生命周期现状梳理（收敛前）

两条插件体系并存，职责已明确，本轮不合并：

| 层 | 模块 | 生命周期 |
| --- | --- | --- |
| forge 内核能力 | `forge/capability.py` CapabilityLibrary | 发现→信任→启停→安装（版本化缓存）→上下文注入 |
| GUI 插件市场 | `forge-gui/plugin_market.py` Marketplace | 发现→安装→确认(ack)→启停→授权(grant)→卸载，含审计/日志/回收站 |
| 声明式运行时 | `forge-gui/plugin_capabilities.py` CapabilityRuntime | 加载→调用（经网关二次 Policy 评估） |
| 原生运行时 | `forge-gui/plugin_runtime.py` PluginRuntime | 进程边界加载→调用（看门狗超时/代际失效） |
| 兼容层 | `forge-gui/compat_sources.py` | OpenClaw/Claude/DSH/Codex → 声明式清单（只读旁路源） |
| MCP | `forge/contrib/mcp_bridge.py` + `toolhost.py` | 纯函数：plan/discover/health + 授权（readOnlyHint 优先） |

权限链路（已验证无绕过）：清单声明 capabilities → 用户 grant（仅限
GRANTABLE_CAPABILITIES，且必须已声明）→ 网关 /v1/tools/call 以
`plugin:{id}:{tool}` / 工具名 / 能力名 三重名字再做 Policy 评估
（deny>ask>allow）→ 才进内置注册表执行。插件自声明不产生任何权限。

## 二、本轮修复的缺口

### 1. 【安全】清单自声明信任（forge/capability.py）
- 原问题：`_load()` 直接采信清单里的 `trusted: true` 与
  `provenance: marketplace`。项目树里投放假清单即可让 SKILL.md 正文进入
  上下文注入面（prompt injection）。
- 修复：信任只来自（a）捆绑产品根（root 路径含 "bundled"）或（b）state
  文件（install()/trust() 的宿主决定）。清单字段仅作展示。
- 回归检查：`capability:self-declared-provenance-not-trusted`。

### 2. 【生命周期】安装后无法被重新发现（forge/capability.py）
- 原问题：install() 落盘到 `<cache>/<name>/<version>/`，但 scan() 只扫
  一层，缓存副本永远不可见；且 `_caps` 跨 scan 持久 + setdefault，首次
  注册者永久遮蔽后续安装，已删目录的条目也会残留。
- 修复：scan() 每次从磁盘重建索引；新增 `_load_versioned()` 支持版本化
  布局（取最高版本）。
- 回归检查：`capability:installed-copy-discovered`。

### 3. 【生命周期】uninstall 缺失（forge/capability.py + cli.py）
- 新增 `CapabilityLibrary.uninstall()`：仅允许卸载 install() 装进缓存根
  的能力（产品树拒绝，抛 PermissionError）；删除目录 + 清 state
  （enabled/trusted）+ `state["archived"]` 记账。
- CLI `forge capabilities uninstall <name>` 接入。

### 4. 【依赖】卸载/降级安装击穿依赖方（forge-gui/plugin_market.py）
- 原问题：requires 只在安装/启用被依赖方时检查。卸载依赖或重装旧版本
  后，已启用的依赖方停留在"启用态但依赖失效"，继续暴露工具。
- 修复：`_dependents()`（反向依赖）+ `_cascade_disable_dependents()`
  （级联禁用 + cascade-disable 日志留痕）：
  - uninstall(pid)：卸载后级联禁用启用中的依赖方；
  - install_from_dir()：降级安装击穿启用中依赖方的版本约束时级联禁用；
  - disable(pid)：**不**级联——依赖契约是"已安装且版本满足"
    （plugin_dependencies.py 既有语义），禁用不改变安装集。
- 重新启用被闸门拦下（enable() 复查 requirements，既有行为）。

### 5. 【依赖】reload 运行时复查（forge-gui/plugin_capabilities.py）
- CapabilityRuntime.reload() 加载前对 requires 非空的插件复查
  check_requirements（状态文件被旁路改写、安装后依赖被移走等场景），
  不满足即 skip 记账（"依赖不满足（运行时复查）"）。
- 调用方传入过滤子集时版本映射仍取全量 catalog，避免误判。

### 6. 【去重】内置工具名单唯一事实源（forge/tools.py + plugin_runtime.py）
- tools.py 纯新增 `BUILTIN_TOOL_NAMES` frozenset（19 个内置工具名）。
- plugin_runtime 优先引用并集本地副本（+python 网关名），无 forge 包时
  回退本地清单。两处名单漂移=插件遮蔽内置工具的风险消除。

## 三、测试报告

新增 `forge-gui/test_plugin_lifecycle.py`（13 用例，注册进
run_offline_checks.py）：

- 验收主线 test_discover_to_uninstall：发现（内置目录）→ 安装 →
  权限检查（未 ack 拒启用 / 未 grant 无工具 / grant 越声明拒）→
  加载 → 真实调用（经真网关+真 Policy 读文件 + 审计留痕）→ 禁用 →
  卸载（回收站/索引清除/事件日志全程留痕）。
- 权限拒绝：真实 Policy（只读模式）拒写且不落盘；工作区外路径在派发前拒。
- 依赖：缺失拒装；卸载级联；禁用不级联；降级击穿级联；reload 兜底。
- 重复安装：目录替换、代际递增、ack/授权作废、索引单条、链路可重走。
- 并发：4 线程（装/卸、启/停、reload、catalog）交叉，无异常，终态
  磁盘/索引/启用列表三面对齐。
- 原生插件恢复：崩溃工具返回错误不影响同插件健康工具；超时看门狗
  快速掐断；禁用后陈旧工具立即失效；加载失败按插件隔离并记账。

selftest.py 增补 5 项 capability 检查（自声明信任、安装可见性、卸载
归档/清信任、产品树拒卸）。

全量结果：
- `run.py selftest`：574/574 通过（收敛前 569/570 项通过且无 capability
  卸载覆盖）。
- 插件套件（pytest，9 个文件）：184 passed + 48 subtests。
- `run_offline_checks.py`（隔离 home，插件模块）：184 OK。
- tests/ 目录：45 passed；test_development_mode 单独跑 17 passed。

## 四、已知限制与环境问题

1. test_plugin_market_ui 7 例失败：本机 Python 3.12 的 Tcl 损坏
   （`Can't find a usable init.tcl`），DPI 缩放/对话框用例全部受影响；
   与本轮改动无关（收敛前同样失败）。GUI 验证需在 Tk 正常的机器跑。
2. tests/ 全目录运行时 test_development_mode 1 例失败：其他测试模块
   污染 secrets redaction 全局态（run_offline_checks 有 IsolatedResult
   快照隔离，tests/ 无）；单独运行通过。属 Development Mode/测试基建
   范畴，本轮不动。
3. 原生 Python 插件（executes_code）仍是信任门而非 OS 沙箱：确认指纹
   后的工作进程持有用户全权（文档明示）。Protected Agent 模式下
   secret_isolation=True 直接拒绝原生执行，GUI 对 Agent 只暴露声明式
   运行时——此边界保持不变。
4. capability.py 的依赖/版本冲突检查仅存在于 GUI 市场链路；
   CapabilityLibrary（内核侧）无 requires 概念，保持轻量（其能力
   为文档注入型，无执行面）。
5. 仓库存在并行 Agent 快照回写：本轮中途发生过一次整体回退（12:58:24
   同秒批量恢复），已重放全部修改并复验。后续并行会话请勿直接恢复
   以下文件的本日之前快照：forge/capability.py、forge/cli.py、
   forge/tools.py、forge/selftest.py、forge-gui/plugin_market.py、
   plugin_capabilities.py、plugin_runtime.py、run_offline_checks.py、
   test_plugin_lifecycle.py。
