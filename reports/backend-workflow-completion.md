# 后端审查与执行能力补齐

日期：2026-10-18。基线：`main @ 14b266a0c78a400fdba8091b4f58c9fefec9b938`。
独立工作树：`C:/Users/匡溯昀/forge-backend-workflow`。
分支：`feature/backend-workflow-completion`。未合并主线、未推送。
实现提交：`b4936d2a88ed94ef1c89a1ef1fb230e23e540bb1`。

主工作区存在他人未提交的 GUI 修改。本轮从已提交 HEAD 建立工作树，没有复制或覆盖
那些修改；因此验收不代表已经测试了这些未提交 GUI 修改的组合。

## 审查判断

对照既有 Runtime Convergence 报告、客户端声明的“流式推送/取消/参数面板”，复核
`forge/loop.py`、执行协议、Session、Development Runtime 和 `client/server.py`。
内核已有的权限、ActionExecutor、文件冲突、检查点不重复实现。Graph 的 BaseChannel /
BaseGraphSaver 中 NotImplementedError 是基类契约，已有具体实现，不算未完成产品功能。

本轮发现并落地的是客户端已宣称、但尚未正确贯通后端的能力：

| 优先级 | 基线证据 | 落地结果 |
|---|---|---|
| P1 | `client/server.py:416-427` 只在 Agent.run 前后检查 `_CANCEL_FLAGS` | 每任务 Event 传入核心 Agent/子 Agent，取消承认请求，终态由实际 RunResult 决定 |
| P1 | `client/server.py:429-441` 结束后才收集步骤，所有返回报告都标 done | 执行中 observer 发布真实工具事件；max_steps 等失败与未知结果不再显示成功 |
| P1 | `client/server.py:366-370` 将 base/coding 名当作文件路径 | 调用已有 bundle compose，coding 叠加既有模式层 |
| P1 | `client/server.py:373-386` 两次 model 行补丁相互覆盖；thinking 无实际消费者 | 合并完整 model 行，显式模型实际绑定 transport，thinking 进入真实内核配置 |
| P1 | `client/server.py:178-235` 仅内存字典，外部可取得可变引用 | 复用 Session 构建持久视图，返回防御性快照，重启未完成项只读标为 detached/unknown |
| P2 | `client/server.py:477-500` 数组下标游标，截断后丢事件，无 SSE id | 持久 cursor、event_id、断线续读、200 条分页、缓存淘汰后回读磁盘 |
| P2 | `client/server.py:608-615` 校验 workspace 前创建任务 | 请求形状、枚举、预算、工作区全部先验证，再持久创建任务 |
| P2 | `client/server.py:649` --workspace 未被执行/浏览器端使用 | 宿主明确指定项目，任务与文件预览共用目录边界 |
| P2 | SSE 终态仍发 keep-alive；cancel POST 不读 body | 关闭终态 SSE、读取取消请求体，避免客户端等待 EOF/连接重置 |

以上行号指实施基线。当前接口与边界见 `docs/client-runtime-api.md`。

## 修改与接口边界

- `forge/client_runs.py`：任务视图、原子状态/事件提交、去重、查询、防御性快照、取消 token、
  历史重建与游标分页。它是客户端视图日志，不取代权威 Agent 动作日志。
- `client/server.py`：配置合并、参数校验、真实 observer/token 接线、统一结果、SSE/JSON
  事件查询、工作区接线、worker 生命周期。仍使用现有 build_agent 和动作执行器。
- `forge/loop.py`：兼容增加 `run_request` 可选参数；宿主身份接入现有执行生命周期。
  有 Session 时按同一日志中的 run_id 记录并同步准入，不重放已提交身份。
- `tests/test_client_runtime.py`：离线模型传输，真实 Agent/文件/HTTP/Git/子进程与跨进程验证。
- `tools/verify_client_workflow.py`：可复现且保留所有产物的真实 HTTP 执行闭环。
- `docs/client-runtime-api.md`、本报告与验证 JSON：接口、兼容性和验收证据。

没有修改 GUI、插件市场、Desktop Awareness；没有新增 Runtime 调度器或授权旁路。
`RunRequest` 仅提供关联身份，Policy/Registry/expose 与宿主能力仍由内核决定。

## 验收

最终结果以相邻 `backend-workflow-validation.json` 为准。新增定向验证涵盖：

| 套件 | 总数 | 通过 | 跳过 | 失败/错误 |
|---|---:|---:|---:|---:|
| 新增客户端专项（已包含在 tests/） | 26 | 26 | 0 | 0 |
| tests/ 全量发现 | 157 | 156 | 1 | 0 |
| forge/ 全量发现 | 229 | 228 | 1 | 0 |
| 指定 10 个 GUI 接口模块 | 120 | 120 | 0 | 0 |
| Forge 全量 selftest（check 数） | 574 | 574 | 0 | 0 |

每个命令退出码均为 0。两项跳过对应符号链接的平台/账号条件；GUI 结果不代表整个 GUI
目录全量。selftest 的 check 数不与 unittest 用例数相加。
环境：Windows 11 `10.0.26200`、完整系统 Python 3.12.10、PyYAML 6.0.3，Git 版本及
原始日志路径/哈希保存在验证 JSON。归档时逐一重新校验了完整回归记录中的源码 SHA-256。

复现：

```powershell
& 'C:/Users/匡溯昀/AppData/Local/Programs/Python/Python312/python.exe' -B -m unittest discover -s tests -p test_client_runtime.py -v
& 'C:/Users/匡溯昀/AppData/Local/Programs/Python/Python312/python.exe' -B tools/verify_rc1.py --output 'C:/path/to/new-validation'
& 'C:/Users/匡溯昀/AppData/Local/Programs/Python/Python312/python.exe' -B tools/verify_client_workflow.py --output 'C:/path/to/new-closed-loop'
```

专项执行证据如下：

- 真实 HTTP 取消到达运行中 Agent，模型返回后不再写入；两个真实 worker 的取消隔离。
- 只读工具权限拒绝；指定模型实际 transport 选择；配置组合不丢字段。
- 模型后续调用阻塞期间，前一个工具步骤已可查询。
- 未知部分写入不会因后续模型 final 文本变为成功；max_steps 不伪装成功。
- 终态 SSE 关闭、Last-Event-ID 重连、JSON 游标一致、缓存淘汰回读。
- 历史记录重启、截断尾部修复、同步失败不发布、秘密保护、同日志身份重复准入拒绝。
- 多线程任务隔离与 3 个独立进程的记录隔离。
- 创建任务→真实 Agent read/write→真实 Git Diff→子进程 unittest→检查结果→checkpoint
  恢复原文件→重建任务历史。模型响应为脚本化 transport，没有调用付费模型服务。

另一次独立闭环产物保留在 `C:/Users/匡溯昀/forge-backend-workflow-closed-loop`，
run `33977115014a44e79aed1285b15824ac`，含两个真实工具 action、真实 diff.patch、
通过的 tests.log、执行日志和 evidence.json。恢复后 `VALUE = 1`，历史重建成功。
仓库内归档摘要为 `backend-workflow-closed-loop.json`。

初期验证还发现并修正了 SSE keep-alive 导致读端等待 EOF、取消 POST 未读 body 导致偶发
连接重置两项生产问题。测试夹具曾误用不存在的 FILE_WRITE/restore 名称，已改用既有
WRITE/rollback；这两项属于夹具错误。首次按 tests.* 点名导入失败后改用上述 discovery
命令。最终全量结果不包含这些初期失败，不以局部通过代替完整回归。

## 兼容性与剩余边界

- 新任务 ID 为 32 位 hex；路由继续接受旧 12 位 ID，旧内存历史无法追溯恢复。
- 保留旧 HTML 的 step/state 事件和 done/error/cancelled 状态；增加标准结果和关联身份。
- 显式模型优先于自动策略；记录 routing_mode。取消已终态任务改为 HTTP 409。
- 请求项目必须位于宿主 --workspace 指定目录内，不能再从请求选择整个仓库任意目录。
- 已有 GUI Gateway 接线、完整 Web 编辑/保存/冲突 API、PTY/LSP 以及所有 Development
  测试/恢复发布的统一协议迁移仍未完成；这些不是本轮后端客户端补齐已交付的能力。
- 单个服务器拥有自己的活动 worker；不提供跨服务器接管或取消。重建的未知状态是
  本宿主缺少活动执行句柄，不是另一个进程已经退出的证明。
- 任意模型/原生 handler 无法保证立即中断。服务器关闭最多等 5 秒，未知结果必须检查。
- 原始日志是追加式保留，启动/历史分页仍为线性扫描；没有实现自动磁盘清理或索引。
- 原有客户端网络认证模式保持，没有将该后端宣称为可直接暴露公网的多租户服务。
- Agent loop 成功不等于每个动作/测试通过；应继续检查关联证据。没有 exactly-once、
  自动回滚或未知副作用自动重放的承诺。
