# 16 · Work Mode 与多 Agent 工作台 —— 产品架构审查与设计提案

- **审查性质**：只读源码审查 + 架构设计，**未修改任何源码**
- **版本基线**：`main @ 01c5e16`，工作区干净
- **上一轮 QA 的约束继承**：本报告不涉及 `reports/15-独立QA回归审查-2026-10-12.md` 中缺陷的修复，但设计需与之兼容（尤其 P0/P1 四项）
- **核心命题**：Work Mode 与 Coding Mode **共享基础运行时，保持独立信息架构**

---

## 0. 审查方法与关键结论速览

全部结论均以 `文件:行号` 为据；不存在的东西会明确写「不存在」，不以设计稿充作现状。

| 关注点 | 现状判定 | 一句话结论 |
|---|---|---|
| Agent 独立模型配置 | ✅ **已实现且完备** | 五级优先级链已落地（`runtime.py:192-197`） |
| Agent 间通信授权 | ⚠️ **仅父子，无 A2A** | 授权边界正确（宿主 seam），但缺少横向通信模型 |
| 任务编排 | ❌ **无编排引擎** | 三条多 Agent 路径并存且状态模型不一致 |
| 进度展示 | ⚠️ **有数据无呈现** | `TaskEvidence` 已采集 `depth`/`usage`，UI 全不渲染 |
| 定时任务 | ✅ **已存在** | SQLite `jobs` + 2s 轮询；但隔离过强、无 cron、仅 GUI 存活期 |
| 任务恢复 | ✅ **设计最成熟的一块** | 明确"不重放 pending 动作"，语义诚实 |
| 产物管理 | ⚠️ **投影有、索引无** | `TaskEvidence` 是内存投影，无跨会话产物台账 |

**最大架构债**：三条多 Agent 执行路径（进程内 spawn / 子进程 Federation / 单 lane 工作台）**状态模型互不相同**，这是"多 Agent 工作台"无法成立的根因。

---

## 1. 现状盘点（As-Is）

### 1.1 Work Mode 与 Coding Mode 的真实形态

两者**不在同一层**，这一点当前实现是对的，也是设计要延续的基础。

**Work Mode = 信息架构层**

```
forge/development/state.py:15-17   WorkMode(GENERAL | DEVELOPMENT)
forge/development/state.py:31-42   ModeState._data = {mode, project, recent, roles,
                                                      agent_models, tabs, session, layout}
forge/development/state.py:163-173 task_snapshot() → TaskSnapshot(mode, model, role,
                                                      permission, roles, agent_models)
```

持久化到 `~/.forge-development/development.json`，原子落盘（`state.py:75-85`，`tmp` + `fsync` + `os.replace`）。

**一条已经写死的关键不变量**（`state.py:25-26`）：

```python
# A mode can never grant permissions. The host supplies separately approved policy.
permission: str = 'read-only'
```

> 这句话就是"共享运行时 vs 独立信息架构"的分界线：**模式切换只改变信息取向，绝不授予权限**。设计必须守住。

**Coding Mode = 能力 Bundle 层（无自有状态）**

```
bundles/modes/coding.json    policy:coding   mode=acceptEdits, sandbox=workspace-write,
                                             allow=[...], ask=[shell_exec]
                             loop:coding     maxSteps=40, maxDepth=3, spawnBudget=16
                             thinking:mode   smart
                             tools:expose    [...]
forge/cli.py:51-55           --coding → bundles + [coding.json]
forge/cli.py:1004-1005       仅 CLI 参数入口
```

Coding Mode **不写 `ModeState`、不落任何自有状态**，纯粹是在 bundle 链上叠加配置，与 General/Development 正交。

⚠️ **缺口**：`--coding` **只有 CLI 入口，GUI 无对应开关**。用户在 GUI 里无法启用 Coding Mode，二者当前没有交集界面。

### 1.2 三条并存的多 Agent 路径（核心架构债）

| # | 路径 | 实现 | 并发 | 会话归属 | 状态可见性 |
|---|---|---|---|---|---|
| 1 | `spawn_subagent` | `loop.py:463-561` `_spawn_handler` | **串行**（`child.run(task)` 内联，`loop.py:532`） | 共享父 session（`loop.py:510`） | 事件折叠进父流（`loop.py:542-548`） |
| 2 | `Federation` | `federation.py:202,313` `dispatch_many` | **真并发**（线程 + 子进程 CLI worker） | 无 Session 接入 | `WorkerResult` 一次性返回 |
| 3 | 工作台 lane | `runtime.py:40` `_lane` | **互斥**（单任务） | 项目专属 Session | `TaskEvidence` 投影 |

三者的**任务标识、进度语义、失败语义、产物归属互不对齐**。任何"工作台统一管理多 Agent"的方案，第一步都是把它们收敛到一个任务模型。

### 1.3 各关注点现状细节

**Agent 独立模型配置（已实现）**

`runtime.py:192-197` 的 `child_route` 就是优先级链：

```
显式指派(child_model) > 具名 Agent 覆盖(overrides[name]) > 角色(subagent) > 继承路由(inherited)
```

配置入口：`ModeState.configure_role`（五角色 `state.py:32`）+ `configure_agent_model`（按名覆盖 `state.py:139-143`），均经 `_validate` 拒绝含凭据的标识符（`state.py:124-125`），并由 `phase_router`（`phase_models.py:14-24`）校验该模型确实在当前 provider 目录中。

> 注意 `runtime.py:158-161`：模型必须属于**本隔离 home 的 catalog**，否则 `ValueError('Model is not configured in this isolated home')`。这条保障了"独立"语义。

**进度展示（有数据无呈现）**

链路完整：`runtime._publish`（`runtime.py:73-80`）→ `_emit`（`60-71`）→ `DevelopmentWorkbench.enqueue_event`（`app.py:25-28`）→ `queue.Queue`（`app.py:29`）→ `_drain`(`app.py:284-306`) → `handle_event`(`760-805`) → `TaskEvidence.apply`(`evidence.py:20-93`) → `render_evidence`(`app.py:738-758`)。

但呈现层三重缺失：
- `grep Progressbar forge-gui/*.py` → **零命中**，没有任何进度条
- `evidence.py:34-41` 采集了 `depth`，`app.py:742-744` 只渲染成扁平文本行 `f"{agent} · {status} · {run_id[:8]} · {model}"`，**父子层级树从不渲染**
- `evidence.py:42-45` 采集了 `usage/tokens`，`evidence.py:90-91` 采集了 `checkpoints` —— **UI 全不展示**

**定时任务（已存在，但能力过弱）**

```
forge/desktop_services.py:32-41   CREATE TABLE jobs(id, data, due, interval, enabled, status, result, started)
forge-gui/desktop_features.py:73-121  线程 Forge-Desktop-Jobs，while not stop.wait(2): claim_due()
desktop_services.py:120-132      claim_due: BEGIN IMMEDIATE，回收 >300s 的 running
desktop_features.py:96-104        Policy(READ_ONLY, READ_ONLY, non_interactive),
                                  LoopLimits(max_steps=6, spawn_budget=0), 独立 session
```

授权模型是**创建时一次性**（`desktop_features.py:251,264-270` 人工添加），运行期不再审批。**不存在** cron 表达式、不存在独立守护进程、Forge 关闭即停。

**任务恢复（设计最成熟）**

`runtime.py:377-426 recovery()` + `428-460 restore_history()`，要点：
- 只允许恢复**隔离 IDE 会话**（`runtime.py:380-381` 路径必须在 `home/sessions` 下）
- 旧日志**逐字节保留**，坏尾标记为 `incomplete_tail` 而非重放
- `TaskEvidence.from_events(recovered=True)`（`evidence.py:95-118`）把 `running→unknown`、`executing→interrupted` —— **主动承认不确定性**
- `evidence.py:107-117` 明确注释：「历史上的意图不是正在运行的 worker」
- `runtime.py:440` `last_snapshot = None` —— **恢复不能恢复写授权**

**产物管理**

`TaskEvidence`（`evidence.py:6-18`）九个投影槽：`plan / actions / agents / runs / tests / changed / checkpoints / diffs / reviews / phase`。另有 `CheckpointStore`（影子 git）、`FileRevisionGuard`（`guard.py`）、变更文件 Treeview（`app.py:142-145`）。

缺口：这些是**内存投影**，无 `task_id` 索引的持久台账；会话重开后产物元数据不可检索（只能靠 journal 重放）。

---

## 2. 功能架构（目标态）

### 2.1 分层与"共享 / 独立"边界

```
┌─────────────────────────────────────────────────────────────┐
│  信息架构层（互相独立，禁止互写）                              │
│                                                              │
│  ┌── Work Mode IA ────────┐   ┌── Coding Mode IA ──────────┐ │
│  │ ModeState / TaskSnapshot│   │ Bundle 叠加（无自有状态）   │ │
│  │ mode · layout · roles   │   │ policy:coding / loop:coding│ │
│  │ agent_models · project  │   │ thinking:mode / tools:expose│ │
│  │ session · recent · tabs │   │                            │ │
│  │ → development.json      │   │ → bundles/modes/*.json     │ │
│  └────────────────────────┘   └────────────────────────────┘ │
│        │ 只读 TaskSnapshot          │ 只读 bundle 配置         │
├────────┴────────────────────────────┴────────────────────────┤
│  共享基础运行时 Shared Kernel（唯一实现，不得分叉）            │
│  Agent/loop · ToolRegistry + Policy/Decision · Session        │
│  CheckpointStore · SecretScope · ModelRouter/Transport        │
│  TaskEvidence 投影引擎 · Federation lane                      │
└─────────────────────────────────────────────────────────────┘
```

**四条铁律（建议写进 CLAUDE.md / 架构守卫）**

1. **模式不授权限** —— 沿用 `state.py:25-26`。Coding bundle 的 `acceptEdits + workspace-write` **只能**由宿主显式应用策略时产生，ModeState 切换永远不产生它。
2. **单向依赖** —— 信息架构层**单向读取** bundle/运行时；运行时**不得反向读** `ModeState`（现状 `runtime.py` 持 `self.state` 仅为读取快照，符合）。
3. **快照隔离** —— 每次任务以 `TaskSnapshot` 冻结取出（`state.py:163-173`），运行期状态变更不影响进行中任务（已实现，见 `tests/test_development_mode.py:52-57` 的冻结语义）。
4. **Coding Mode 不得持久化** —— 它是一次性 bundle 叠加，`--coding` 退出即失效，不写入 `development.json`。

### 2.2 统一任务模型（解决三条路径分裂）

引入**唯一的任务实体**，让 spawn / Federation / lane 三种执行都产出同一种事件：

```
Task { task_id, parent_task_id, work_mode, coding_bundle[],
       role, permission, model_selection, budget,
       phase, lane_id, session_path, artifact_refs[] }
Agent { agent_id, task_id, parent_agent_id, depth,
        model_pair, policy_ref, budget_consumed, phase }
```

关键：**`task_id` 与 `agent_id` 分离**。现状 1 个 task 只挂 1 个 `last_task_id`（`runtime.py:45`），多 Agent 下不够。

### 2.3 七大能力模块

| 模块 | 现状落点 | 目标 |
|---|---|---|
| M1 模型配置 | `ModeState.roles` + `agent_models`（已实现） | 扩展到 policy/budget/tool-surface 三元组，同一优先级链 |
| M2 通信授权 | `child_router_factory` / `child_observer`（宿主 seam） | 三级通信模型 L0/L1/L2 + 显式授权矩阵 |
| M3 任务编排 | 单 lane 互斥 | LanePool + DAG-lite（父等待子，兄弟可并行） |
| M4 进度展示 | TaskEvidence → tk.Text 扁平文本 | Agent 树 + 时间线 + 预算条（挖出已有 `depth`/`usage`） |
| M5 定时任务 | SQLite `jobs`，只读 6 步 | 与 Work Mode 快照绑定，可选授权级别，独立 lane |
| M6 任务恢复 | `recovery`/`restore_history`（成熟） | 扩展到 Agent 粒度 + 产物一致性校验 |
| M7 产物管理 | 内存投影 | `task_id` 索引的持久台账 |

---

## 3. 状态流转设计

### 3.1 Mode 层状态机（已存在，保持不变）

```
            switch('general')                  switch('development')
   ┌────────── GENERAL  ◄────────────────────►  DEVELOPMENT ────────┐
   │                                                                 │
   │ · mode 只作为任务取向，不授权限（state.py:25-26）                │
   │ · 每次 task_snapshot() 冻结出 TaskSnapshot                       │
   └─────────────────────────────────────────────────────────────────┘
              ▲                                    ▲
              │ 只读 layout/roles/agent_models      │ 每任务绑定 deferred tasks
```

- 切换是**原子**的（`state.py:87-96`：先 `_save()` 落盘，再通知 listener）
- 只有 `task_snapshot()`（`state.py:163-173`）能把 mode 的取向带进运行时；`permission` 默认恒为 `read-only`
- 现状 `runtime.py` 在多处以 `snap.mode == WorkMode.DEVELOPMENT` 做分支（如 `163/173/176/184/188/198`），即 **General 模式下 "tools/镜像/roles" 全部不生效** —— 这个开关语义要保留，不要让 General 悄悄获得 Development 的能力

### 3.2 任务生命周期（目标态，兼容现状 phase 取值）

`TaskEvidence.phase` 现值：`not_started / ready / planning / executing / testing / completed / failed / cancelled / interrupted`（`evidence.py:17,116`，`runtime.py:220-222`）。

```
        ┌──────────► interrupted ◄─── (process crash / recovery)
        │                ▲
not_ready ──run()──► ready ──► planning ──► executing ──┬─► testing ──┐
                                    │                   │             │
                                    │  (spawn child)    └─────────────┴─► completed
                                    ▼                                   │
                              awaiting_child ──(all children done)───────┘
                                    │
                                    └──► failed / cancelled
```

新增 `awaiting_child`：现状父 Agent 没有等待子集合的屏障——`loop.py:532` 的 `child.run(task)` 是**内联串行**调用，父在子返回前无法继续自己的循环，因此也不需要这个状态。一旦引入并行 lane（兄弟子 Agent 可同时执行），父就必须显式等待子集合，这才需要 `awaiting_child`。

**终止语义（必须保留的现有诚实设计）**：
- `cancel()`（`runtime.py:82-88`）只置 `threading.Event`，"不假装 worker 已退出"
- 恢复时 `running → unknown`（`evidence.py:104-112`）而非 `failed`
- **pending 动作永不自动重放**（`runtime.py:201-202` 注入 "never replay a pending action automatically"）

### 3.3 Agent 子状态机（新增）

```
idle ──spawn──► running ──┬──► completed ──(result → sanitise → parent)
                          ├──► failed
                          ├──► cancelled
                          └──► unknown   ← 恢复时未知结局，必须落到这个态
```

`unknown` 不是 failed —— 这是现有设计最值得保留的一点，多 Agent 下更要强调：**一个子 Agent 结局未知时，父任务的产物不能标记为可信**。

> ⚠️ **与 QA-04 的耦合**：`loop.py:542-548` 折叠子事件时未做 `sanitise_child_output`（仅 `loop.py:559` 对 `report.text` 做了）。本设计的 L1 通信层依赖该净化，**修复 QA-04 是启用多 Agent 通信的前置条件**。

---

## 4. 界面方案

### 4.1 信息架构（三栏保持不变，重排优先级）

现状三栏（`app.py:98-108`）：项目树 230 / 编辑器 650 / Agent 420，纵向 split 为 tabs(weight 4) + output(weight 1)。

**建议改 output 内部组织**（`app.py:176-188` 五页 Diff/终端/测试/问题/输出）：

```
输出区 Notebook 重排为：
  ┌─ 时间线 ─┬─ Agent ─┬─ 产物 ─┬─ Diff ─┬─ 终端/测试 ─┐
  │ 默认首屏 │  树     │  台账  │        │             │
```

- **时间线**（新）：取代现在的裸事件 JSON 页，按 `ordinal` 渲染 `plan → tool → test → review → checkpoint`
- **Agent 树**（新，关键）：把 `evidence.py:34-41` 已采集的 `depth` 渲染成树
  ```
  ▸ forge (executing)          2m14s · 12.4k tok · budget 3/16
    ├─ tester   (completed)    41s  · 3.1k tok
    └─ reviewer (running)      …    · 1.8k tok
  ```
- **产物台账**（新）：`changed / checkpoints / diffs / reviews` 四张表（`checkpoints` 目前采集了但完全没渲染）

### 4.2 定时任务 UI（现状存在，建议重构其配置面板）

现状 `desktop_features.py:251,264-270` 只有"添加定时任务"。建议改成：

```
定时任务 ─ 新增：绑定 Work Mode 快照
  名称 / 提示词 / 工作区
  触发：[ 间隔 ▾ | cron（新增） ]
  绑定模式：[ General | Development ]     ← 新增
  执行授权：[ 只读(现状默认) | workspace-write（需显式勾选） ]
  模型角色：[ default ▾ ]     步数上限：[ 6 ]（只读档）/ [ 40 ]（写档）
  ☐ 每次执行前人工确认        ← 现状缺失，写档必须勾
```

理由：现状硬编码 `READ_ONLY + max_steps=6 + spawn_budget=0`（`desktop_features.py:96-104`）导致定时任务**不可能执行任何真实编码工作**，与 Coding Mode 完全脱节。

### 4.3 Coding Mode 的 GUI 入口（新增，现状完全没有）

侧栏新增一个**任务级**的能力开关（不是模式开关，避免违反"模式不授权限"）：

```
运行配置（每任务）
  模式：[ General | Development ]
  能力包：☐ Coding（policy:coding + loop:coding + tools:expose）
  权限：[ read-only | workspace-write ]   ← 宿主显式授予，与模式无关
```

这样 Coding Mode 保持"不写 ModeState"，只是在运行时把 `bundles/modes/coding.json` 叠加到该次任务。

### 4.4 与 V2 双工作空间的关系

`feature/visual-system-2-dual-workspace` **已合并入 main**（`git log main..<该分支>` 为空），因此本设计的 Agent 树 / 时间线应直接复用主 GUI 现有视觉契约，不在 Workbench 里另起一套组件方言。

> 说明：本轮未逐行核验 V2 视觉规范本身，故不对其内部契约作任何断言。

---

## 5. 接口依赖

### 5.1 现有依赖（不可破坏）

| 依赖 | 落点 | 约束 |
|---|---|---|
| `ModeState.task_snapshot()` | `state.py:163-173` | 冻结语义，被 `tests/test_development_mode.py:52-57` 锁定 |
| `DevelopmentRuntime.run/cancel/review` | `runtime.py:132/82/243` | 单 lane，非阻塞 `acquire(False)` |
| `TaskEvidence.apply` | `evidence.py:20-93` | 投影，只读，**不得回写运行时** |
| `_publish` → `_emit` → `emit` → UI 队列 | `runtime.py:73-80` → `app.py:25-28` | 单向；`runtime.py:66-71` 已吞掉 listener 异常 |
| 事件自愈 | `runtime.py:66-71` listener 异常被吞 | "被销毁的视图不得改变执行或日志真相"，UI 崩溃不影响任务 |

> **查无此项（已核验，勿当作既有能力）**：`loop.py` 中**不存在** `checkpoint2/3/4` 钩子，也不存在任何 `notify()` 形式的 Agent mailbox（grep 零命中）。也就是说，**当前没有任何 Agent↔Agent 的通用消息通道**——子 Agent 只能通过"返回结果"与父通信（详见 §2.3 M2）。这是本设计要新增的部分，不是现状。

### 5.2 建议新增的稳定接口

```
# 统一任务模型
class TaskRegistry:           # 取代 runtime.tasks dict
    create(...) -> Task
    get(task_id) -> TaskView
    children(task_id) -> list[TaskView]
    artifacts(task_id) -> ArtifactIndex

# 通信授权
class CommPolicy:
    can_message(src_agent, dst_agent, channel) -> Decision
    # L0 none / L1 report-to-parent / L2 named-peer
    # 默认 L0，升级需宿主显式授予

# Lane 池（收敛三条路径）
class LanePool:
    acquire(task_id, *, budget) -> Lane       # lane 数上限可配
    # 现状等价物：runtime._lane（单 lane）、federation.dispatch_many（线程）

# 产物台账
class ArtifactStore:
    index(task_id) -> dict[str, list[Artifact]]
    # 持久：~/.forge-development/artifacts/<task_id>.json
```

**依赖方向（严格单向）**：

```
UI(app.py)  →  DevelopmentRuntime  →  TaskRegistry / LanePool  →  Agent/loop（Shared Kernel）
                     ↓
              TaskEvidence（只读投影）  →  ArtifactStore（只读落盘）
```

禁止：`loop.py` 反向 import `development/*`（现状 `runtime.py` → `loop.py` 方向正确，不可倒置）。

---

## 6. 验收标准

### 6.1 架构红线（回归门禁，必须自动校验）

| # | 验收项 | 判定方式 |
|---|---|---|
| A1 | Coding bundle 不写入 `ModeState` | 运行时叠加 `bundles/modes/coding.json` 后，`development.json` 字节不变 |
| A2 | Mode 切换不授予权限 | 切 Development 后 `Policy.mode`/`sandbox` 与 General 完全一致；`runtime.py:156` 记 `work_mode` 但不改 policy |
| A3 | 快照冻结 | 取快照后再改 state，`frozen.roles` 与 `frozen.model` 不变（`tests/test_development_mode.py:52-57`） |
| A4 | 依赖方向 | 静态检查：`forge/loop.py` 等 runtime core 不得 import `forge.development` |
| A5 | 模型必须在 catalog | 任务模型不在隔离 home catalog → `ValueError`（`runtime.py:158-161`） |

### 6.2 七大关注点验收

**M1 Agent 独立模型配置**
- [ ] 同一任务下，父/子 Agent 可配置不同 model，且各自 `phase_router` 校验通过
- [ ] 优先级链可验证：显式指派 > 具名覆盖 > 角色 > 继承（构造 4 个用例，断言最终 `router.primary`）
- [ ] 配置含凭据字符串被 `state.py:124-125` 拒绝

**M2 Agent 间通信授权**
- [ ] **前置**：QA-04 已修复 —— 子事件进入父流前全部过 `sanitise_child_output`
- [ ] 默认 L0：未显式授权时，子 Agent 无法向任何 peer 发消息
- [ ] L1：`$.agents[].parentId` 之外**只能**向父报告；报告内容经 sanitise
- [ ] L2 需宿主矩阵授权，且拒绝时可审计（落到 session）
- [ ] `child_router_factory` / `child_observer` 保持**宿主 seam**，未被注册为 Agent 工具

**M3 任务编排**
- [ ] 三条路径收敛：`spawn_subagent` / Federation / lane 均产出同一 `task_id` 事件结构
- [ ] 父任务在所有子 `terminal`（含 `unknown`）前不得进入 `completed`
- [ ] lane 池上限可配；超额时行为明确（排队或拒绝，不得静默串行）
- [ ] 预算扣减在并发下不丢更新（⚠️ 昨日 QA 未复现 QoS 丢失，但 `BudgetTracker` 非原子，需压测补证据）

**M4 进度展示**
- [ ] Agent 树按 `depth` 渲染父子层级（`evidence.py:34-41` 已有数据）
- [ ] 展示每 Agent 的 model / tokens / budget consumed（`evidence.py:42-45` 已有数据）
- [ ] 展示 checkpoint 列表（`evidence.py:90-91` 已有数据，当前未渲染）
- [ ] **存在进度指示**（当前 `grep Progressbar` 零命中）
- [ ] 主动 Worker 侧 UI 不谎报静止为"完成"（沿用 `evidence.py:1` 注释原则）

**M5 定时任务**
- [ ] 任务可绑定 Work Mode 快照（现为硬编码只读 6 步）
- [ ] 写权限任务必须勾选"每次执行前人工确认"
- [ ] 每次执行有独立 lane + 独立 session（现状已满足 `desktop_features.py:104`）
- [ ] 并发/recovery：`claim_due` 的 300s 回收与 `started` 乐观锁有测试覆盖（现状 `test_desktop_services.py:113-122`）
- [ ] cron 表达式（如纳为目标）必须 fail-closed，解析失败不执行

**M6 任务恢复**
- [ ] 旧日志逐字节保留（`runtime.py:383-386`）
- [ ] 坏尾标记 `incomplete_tail`，**不重放**（`evidence.py:95-118`）
- [ ] 恢复后 `last_snapshot=None`，写授权不被继承（`runtime.py:440`）
- [ ] 多 Agent 粒度：每个子 Agent 的 pending 动作落到 `unknown`
- [ ] 恢复来源限制在隔离 IDE 会话（`runtime.py:380-381`）

**M7 产物管理**
- [ ] 产物按 `task_id` 持久索引，重开 GUI 后可检索
- [ ] `changed / checkpoints / diffs / reviews` 四类齐全且可在 UI 打开
- [ ] 产物引用不含密钥明文（全部经 `redact`/`protect`）

### 6.3 非功能性验收

- **不变式回归**：现有 **569/569 selftest 保持全绿**（本轮已实测复现，见 `reports/15`），新增能力须同步新增 selftest 用例
- **共享可变状态**：任何跨 Agent 共享的可变状态必须标注 owner 与锁；不得再出现"边遍历边回调"的无锁监听器表
- **进度心跳**：长时无输出的 worker 需要周期性事件，静止不得被 UI 推断为"完成"（沿用 `evidence.py:1` 注释原则）
- **诚实性**：结局未知必须渲染为 Unknown，**不得**降级显示为空或 succeeded
- **不谎称能力**：`design.md` §四 已明确"没做的就不写在功能里"，本设计的 M2/M3 在落地前不得在文档或 UI 中暗示其已可用

---

## 7. 建议实施顺序

1. **修 QA-04**（子事件进入父流前过 `sanitise_child_output`，`loop.py:542-548`）—— 它是开启任何 Agent 间通信的**前置条件**，不修就谈多 Agent 等于放大注入面。
2. **统一任务模型 + TaskRegistry** —— 收敛三条路径（§1.2），这是最大的架构债。
3. **先确认非目标是否变更** —— `design.md:92` 已把"单机单会话串行"写成正式非目标；LanePool 是对它的**显式变更**，需设计评审而非当作缺陷修复。
4. **LanePool 与 CommPolicy** —— 编排与授权。
5. **GUI：Agent 树 + 时间线 + 产物台账** —— 数据（`depth`/`usage`/`checkpoints`）已全部采集，**只差呈现**，是投入产出比最高的一步。
6. **定时任务与 Work Mode 对齐** —— 解除硬编码只读 6 步限制，但必须绑定显式授权。
7. **Coding Mode GUI 入口** —— 保持"每任务能力叠加"，不得升级为模式开关。

---

## 附：现状重要常量

| 项 | 值 | 位置 |
|---|---|---|
| 角色集合 | `default, complex, review, simple, subagent` | `state.py:32` |
| 布局 presets | `agent [.17,.49]` / `editor [.17,.76]` / `focus` | `app.py:1051-1055` |
| layout 默认 | `fractions [.2,.6]`, `output .72` | `state.py:41-42` |
| 权限模型选型理由 | CodeBuddy 二维矩阵 + Codex 三档沙箱 + 命令黑名单；**天花板钳制**：父 `plan` 的子代理永远拿不到 `bypassPermissions` | `design.md:21-33` |
| 工具名 grammar | 冻结于 2026-09-14，MCP 前缀用双下划线 `server__tool` | `contract.md:79-88`（对应 `mcp_bridge.py:191-193`） |
| 明确的非目标 | 无向量记忆、无内核级沙箱、无插件签名校验、**无分布式（单机单会话串行）** | `design.md:88-92` |

> ⚠️ `design.md:92` 的"**单机单会话串行，没有跨机调度**"是当前**写进设计文档的正式非目标**。因此 §2.3 的 LanePool 不是"补遗漏"，而是**对既有非目标的显式变更**，需要走设计评审而非当作 bug 修。
