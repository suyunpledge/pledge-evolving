# Coding Mode W3 交付记录 — 2026-10-12

分支：`feature/coding-mode-w3`（基线 `3135de7` = main，W1/W2 与
visual-system-2、development-mode-2 均已合入基线）。未推送，等待集成验收。

## 一、前置检查结论

- git：main @ 3135de7，工作区干净；五个并行 worktree 均为正规 git worktree
  （visual-system-2 / development-mode-2 已合并进 main，其 worktree 仅存分支历史）。
- W1/W2（WorkBuddy，RT 域：checkpoint/evolution/loop/memory/secrets/session/
  tools/registry）已在基线，与 GUI 交付无文件重叠。
- 挂载关系核实：生产入口为 `forge_gui_v2._build_stub_view('development')`
  懒嵌入 DevelopmentWorkbench，bench 为局部变量（P0-01 根因）。
- `forge-gui/visual_system/` 与 feature 分支副本逐字节等价（无漂移）。

## 二、修改文件清单

| 文件 | 变更 |
| --- | --- |
| `forge-gui/forge_gui_v2.py` | Phase 1 接线 + P0-01/P0-03（详见下节） |
| `forge-gui/chat_widgets.py` | P0-03：`_bg_of` 补导入（默认 bg 路径潜伏 NameError） |
| `client/server.py` | P0-02：NameError、secrets.json 机制、Provider 密钥内存解析、死语句 |
| `client/index.html` | P0-02：策略枚举对齐内核（default/acceptEdits/read-only/bypassPermissions）、diag 文案 |
| `forge-gui/visual_system/__init__.py` | P0-04：单一权威来源声明 |
| `forge-gui/test_layout_dpi.py` | isolated_app teardown 后主线程强制 GC（修复跨线程 Tcl 断点异常 0x80000003） |
| `forge-gui/test_workspace_wiring.py` | 新增：12 项双空间接线/P0 回归/Web API 测试 |
| `forge-gui/run_offline_checks.py` | 注册 test_workspace_wiring |

## 三、关键架构变化

### Phase 1：双工作空间生产接线（首次）
- 主体改为 `WorkspaceDeck`（visual_system 组件，未经修改）承载两个持久
  sibling surface：`_build_work_surface`（原侧栏+分栏+视图+工作区**原样迁入**，
  self.split/self.center 引用不变，既有代码零感知）与 `_build_coding_surface`
  （DevelopmentWorkbench 懒构建，独立 ~/.forge-development home 与隔离
  runtime 语义保持）。
- `ModeSwitcher` 进入顶部应用栏（品牌右侧），宿主确认制：`_request_workspace`
  先挂载成功才隐藏原空间（deck 内建该顺序与焦点记录/恢复），失败保留原空间
  并提示。切换只改可见性——会话、草稿、文件、布局、后台任务全保留
  （test_roundtrip 实测：bench 对象身份稳定、输入草稿不丢）。
- 旧 `development` 导航/视图名经宿主 adapter 映射为顶层 Coding 切换
  （`_nav_click`/`_show_view`/`_build_stub_view` 三处统一），彻底杜绝工作台
  被第二次挂载进 Work 内容区（双重 runtime + 同一 state home 冲突）。
- Coding 下收起 Work 专属顶栏控件（☰/工作区/项目 chip，pack 快照可逆恢复，
  fit_topbar 加模式守卫）；品牌 + 切换器 + 网关状态常驻。
- 模式偏好持久化于 desktop config（`workspace_mode`），启动经
  `_restore_workspace_preference` 恢复；异常静默留在 Work。

### P0 修复
- **P0-01**：`self._coding_bench` 持有引用；`_on_close` 先走 bench 自己的
  `can_close()`（未保存草稿询问/任务运行中提示/后台操作等待），用户取消则
  整个退出中止；确认退出补 `capture_layout()` 再进正常退出链。
- **P0-02**：/api/models 与 /api/diag 的 `key_keys_status`→`key_status`
  NameError（boot() 首请求即炸，整个 Web UI 启动不可用）；凭据从无人的
  keys.json 改为内核 secrets.json（protect_store_path + SecretScope.reference
  + 有界读取 + 形状校验；命名映射与 vscode_bridge 一致 name→FORGE_<N>_KEY）；
  Provider 行的 `$expr` 在 run_forge 内存内解析真实值（不写 os.environ——
  TRAE P0-2 的防泄露约束保留；未配置密钥时保留 $expr 诚实解析为空）；
  API 响应只含布尔。策略枚举对齐内核。删除两处死语句。
- **P0-03**：`_bg_of` 导入；工具行新增 `detail`（参数+结果摘要进 tooltip，
  此前主对话工具结果完全不可见）与数值 `elapsed`（此前传字符串致 ToolCard
  耗时汇总恒为 0；桥接路径含人工审批等待，如实计入墙钟）。
- **P0-04**：副本等价核实后，mainline 副本声明为唯一权威源；未动其他 worktree。

## 四、测试结果

- **新增 test_workspace_wiring：12/12 通过**（含一次全绿整跑；另两轮各 1 例
  Tcl init 抖动，见"环境说明"）。覆盖：Work/Coding 往返（对象身份/草稿/
  surface 可见性/无回调异常）、development adapter、偏好持久化与恢复
  （含异常留在 Work）、_on_close 拒绝与放行（capture_layout 计数）、
  隔离 home、TeamTogglePill 默认 bg、_tool_detail 有界、耗时解析、
  /api/models 真实 HTTP（密钥不泄露）、/api/diag、Provider 密钥内存解析
  （模板不污染）、load_keys 命名映射。
- **forge selftest：574/574 通过**。
- test_workbench_ux + test_startup_responsiveness：23/25（2 失败为 emoji
  图集/多解释器既有环境项，与本轮无关，下详）。test_visual_system 全过。
- test_refactor_v3 单跑：42/46（4 例抖动）；chat_widgets/workspace/dev-mode
  test_app 单跑大部分通过、失败集合逐轮漂移。
- i18n 收口检查通过。

### 环境说明（重要，诚实区分）
本机 Windows 的 Tcl/Tk 处于**部分损坏**状态：基础 Tk 可用（我们的接线测试
因此可跑），但 ①频繁创建/销毁解释器会间歇性触发 `can't find usable
init.tcl/tk.tcl`（test_plugin_market_ui 在基线上同样失败 7 例）；②图标图集
在部分控件上加载为空——**基线 main 上 `sidebar_back_btn` image 同样为空**
（最小复现实测），故 test_navigation 在基线亦 57 例失败（分支 71 例，两轮
间漂移 ±50，均为同一环境根因）；③解释器销毁后的 Tk 对象环若留给后续线程
GC 会触发 Windows 原生断点异常 0x80000003（已修：teardown 强制主线程 GC，
该崩溃不再复现）。
**未执行**：真实人眼视觉验收（需 Tk 资产正常的机器）；navigation 类结构
断言在本机无法给出干净信号，建议集成验收机复跑。以上测试无一被标 PASS
而实未运行。

## 五、与其他 Agent 的潜在冲突点

- `forge_gui_v2.py`（本轮最大改动）：GUI 宿主域，Codex 的 workbench 分支已
  合并；后续若有 Agent 基于旧 `_build_stub_view('development')` 嵌入方式
  的认知改动此文件，需要以本分支的 deck 结构为准。
- `client/server.py`/`index.html`：Web UI 域，本轮只做 P0 止血未重写；
  若另有 Web UI 重构计划，以 secrets.json 机制为准。
- `test_layout_dpi.py` 的 gc.collect：对所有用 isolated_app 的测试生效，
  无行为断言变化。
- W1/W2（RT 域）零重叠；development_mode/app.py 与 forge/development/**
未改动**（仅消费）。

## 六、未实现 / 保留限制

- Coding surface 尚无独立活动栏（files/search/git/debug）与底部 dock 四页——
  现用 DevelopmentWorkbench 既有布局（项目树/编辑器+输出/AI 面板），Phase 2
  按 LAYOUTS.coding 契约演进；git/debug 未有能力，不伪装。
- AI 面板仍是工作台自建对话流（Phase 2 统一到 chat_widgets）。
- 逐工具审批卡（PermissionPrompt 接 Policy ASK）为 Phase 3（需挂起通道）。
- 布局预设沿用工作台既有 agent/editor + focus；均衡/编辑器专注/审阅三预设
  的完整独立保存随活动栏落地（当前 capture_layout 比例持久化已生效）。
- 模式切换不迁移会话/权限/密钥（契约 §4），未做 IMe/真实系统 DPI 人工回归。

## 七、下一阶段建议（W4）

1. 恢复正常 Tcl 环境后做一次人眼视觉验收 + navigation 套件干净复跑。
2. Phase 2：AI 面板统一（事件→chat_widgets 适配器）、活动栏与 dock 按契约
   落位、三布局预设与 Coding 独立布局存储 key。
3. Phase 3：合并路由上的 ASK 挂起 → PermissionPrompt 内嵌审批（W2 已补
   session/tools 基础，可与 RT 负责人对齐）。
