# Development Mode v1 — isolated host

Branch: `feature/development-mode`, based on `2cd74e5`. Worktree:
`C:\Users\匡溯昀\forge-development-mode`. No main checkout edits or automatic merge/push.

## Architecture

- `forge/development/state.py`: UI-independent GENERAL/DEVELOPMENT state, versioned independent preferences, layout, project paths, model roles and frozen per-task snapshots.
- `forge/development/editor.py`: human-only editable buffers, encoding/newlines, explicit conflicts, atomic staging and project/secret-store boundary checks. Never an Agent tool.
- `forge/development/runtime.py`: existing `EditorBridge → build_agent → Agent → ToolRegistry → Policy/Secret/Sandbox → Session/Checkpoint`. One lane; mode changes never mutate a running Agent. No new scheduler.
- `forge-gui/development_mode/`: independent ttk host, project adapter over existing `workspace.py`, code tabs, real event and diff/output views.

No changes to shared GUI, navigation, providers, connectors, plugins, knowledge or evolution. Design tokens are imported from `gui_theme.py`, not forked. Runtime stores and tests use separate homes. No interactive shell; commands remain core tool requests and policy ASK is not bypassed by the output panel. This host defaults to read-only every launch. Explicit human workspace-write selection is separate from work mode and applies only to a future task.

## Integration contract for AutoClaw

The independent launcher does not replace General Mode GUI. General Mode in the demo host hides IDE panels while retaining its single Agent conversation. AutoClaw can consume `ModeState.subscribe`, mount `DevelopmentWorkbench` in a persistent container and pass an existing `DevelopmentRuntime` (or an `EditorBridge` facade implementing initialize/run/review/history/observe). Keep the parent and existing chat widgets; do not reparent the General sidebar. Capture task policy/model before execution; changes apply to subsequent tasks. Host ownership decides shutdown, never close an injected Agent on mode switch.

`WorkspaceSource` wraps existing scan/status/diff helpers without modifying `workspace.py`; the editable document model is new because the existing preview is read-only. All scans, reads, diffs, runtime initialization and Agent calls run outside Tk; results are queued to Tk. Actual tool events drive progress and file refresh, never natural-language claims.

Model priority: explicit task → Agent choice → role/default Development model → existing global router. Unknown models fail rather than silently route elsewhere. `subagent` default is retained as an integration hint only: the current core does not expose a safe general child-default setter, so the host must not claim it takes effect. Review uses the existing manual review API. Knowledge/evolution extension lists remain host interfaces, not new engines.

Recovery reads existing `Session` and `workflow_status`; pending actions are uncertain and never auto-replayed. Recovery requires human initiation, preserves real dialogue, and continues in a new journal. No exactly-once resumption claim.

Human editing is not model permission. Secret-bearing documents are sanitized by the existing bridge before attachment. Raw buffers are not persisted to preferences. Users must save or deliberately discard on exit. Mode/layout switches retain unsaved buffers in memory. Atomic saves recheck content fingerprints, but this is not a cross-process compare-and-swap filesystem transaction: an unrelated writer in the final check/replace interval remains a platform limitation.

## Run

PowerShell, from this isolated checkout:

```powershell
& 'C:\Users\匡溯昀\AppData\Local\Programs\Python\Python312\python.exe' `
  'C:\Users\匡溯昀\forge-development-mode\forge-gui\run_development.py' `
  --home 'C:\Users\匡溯昀\.forge-development-home' `
  --project 'C:\path\to\your-project'
```

The launcher uses this independent home by default, restores the last project/layout, and never copies General credentials automatically. Configure the existing Forge provider schema in this home's `forge.patch.json` (use `python run.py --home <independent-home> setup` for the existing CLI setup). No new provider settings page is created. Keys remain env references or the existing trusted `secrets.json` format. Do not use the AutoClaw runtime home. `pythonw.exe` can run the same launcher if no console window is desired.

On the first launch select `development` in the header. General in this demo host means the same conversation without IDE panels; integration into the complete General desktop is deliberately left to its owner. No files in its source tree have been edited.

## Honest capability limits

- The core presently denies `shell_exec` for secret-protected agents before command approval. The IDE does not disable that boundary, create a shell, or claim it can run tests that were denied. Terminal/test panels display genuine tool records, including denials. There is no synthetic pass/fail count. A mediated test executor must be supplied by the runtime owner before this host can execute tests itself.
- Core `tool_call` results are capped (currently 1,500 chars); this UI preserves what the core publishes, not an invented full process stdout. Full artifacts need a supported runtime artifact interface.
- No LSP, debugger, interactive terminal, new knowledge/evolution engine or rollback approval UI is added. Existing checkpoints are project-scoped; viewing Diff does not approve/rollback anything.
- Subagent default is a visibly marked, saved integration hint; no existing child router is overwritten. Existing spawn/subagent events are displayed as published by the core.
- Atomic edit guards ordinary external changes and resolves paths before save. It cannot lock an unrelated program against the final check/replace race. Mixed newline styles are normalized to the document's detected newline on save.
- Provider requests are tested offline; no paid vendor request is made. Human screenshot validation is pending because the native observation tool fails to initialize.

## Verification

Tests were first added before implementation and failed because `forge.development` did not exist. Subsequent tests cover mode/layout and all-role snapshots, read-only defaults, safe history recovery, unchanged unknown tool actions, opaque Secret payloads, encoding/newlines, dirty buffers, external hash conflicts, failed writes, secret-store paths, three layouts, five Tk scaling values, actual Agent events and Tk callback destruction.

Use:

```powershell
python -m unittest discover -s tests -p test_development_mode.py -q
$env:PYTHONPATH="$PWD;$PWD\forge-gui"
python -m unittest development_mode.test_app -v
python -m unittest forge.test_secret_architecture forge.test_read_scope forge.test_execution_review forge.test_phase_models forge.test_task_planning forge.test_vscode_bridge -q
```

All tests use temporary independent homes, projects and offline routers/local HTTP fixtures. Tk interaction tests create real mapped windows with alpha 0; they are geometry/event checks, not screenshot visual acceptance or actual Windows system-DPI changes.

## 本轮交付与验收记录

只在 `feature/development-mode` 的独立 worktree 新增源码、测试与本文档；未修改 AutoClaw 的通用 GUI 或其他负责模块，未合并、未推送、未打包。主工作目录随后出现的 `forge_gui_v2.py`、`memory.py` 与 `knowledge.py` 改动属于并行开发，未触碰。

| 范围 | 实现与边界 |
| --- | --- |
| 模式状态 | General/Development 独立于面板展开；事件通知、版本化持久化；任务模式、模型角色及权限在开始时冻结 |
| IDE 布局 | Agent 优先、编辑器优先、编辑器/Agent 专注；拖动比例持久化；模式往返保留组件 parent、同一 Agent 及未保存文本 |
| 项目 | 复用 workspace 文件扫描、Git status/diff；目录折叠、文件名搜索、近期项目、上次项目恢复；切项目撤销前项目写入授权 |
| 编辑 | 多标签、严格编码、显式其他编码、保存、关闭确认、外部内容指纹检测、冲突保留；不静默覆盖未保存内容 |
| 编程任务 | 复用 EditorBridge/Agent，适应任务大小的开发指导与四档规划；真实事件显示工具、子 Agent、计划与执行结果 |
| 审查 | 真实 Git diff，显示实际操作来源和 authorization 元数据；查看不等于批准；复用手动模型复审 |
| 长任务 | 复用 Session/workflow_status，用户选择恢复真实历史；未知结果工具操作不自动重放，新日志继续 |
| 安全 | 默认只读、显式写入选择、项目切换重置、SecretStore 路径禁止编辑/附加、完整 Agent payload 通过既有 Secret 层 |
| 扩展 | 上下文来源与结果观察接口；子 Agent 默认模型是标明未执行的预留配置，知识库/演化由原负责人接入 |

新增核心测试最终 18 项：17 通过，1 因当前 Windows 账户不能创建符号链接而跳过；目录越界和 SecretStore 绕过用例通过。新增 Tk 工作台测试最终 7 项通过，覆盖 760×540、1200×760、1920×1080 及五档 Tk scaling。现有核心边界回归 103 项全部通过；较早另一批 98 项存在重叠，不能相加。编译、启动参数和 diff 格式检查通过。

调试期间修复了销毁后布局回调、跨测试线程回收 Tcl 变量、项目权限继承和保存编码失败状态；最终复测没有残留 Tcl 回调错误。Windows 短路径与规范路径比较导致的新测试失败改为规范路径比较，不改变生产边界判断。

原生观察工具初始化仍报 `failed to write kernel assets: 系统找不到指定的路径。 (os error 3)`，没有完成截图验收。真实 GUI 通过 Tk 程序化布局和交互测试，不据此声称所有屏幕/系统缩放均视觉通过。当前也没有安全的原生命令执行能力，故终端/测试为真实事件展示，不冒充可执行终端；需要运行时负责人提供受保护的执行接口后再接入。
