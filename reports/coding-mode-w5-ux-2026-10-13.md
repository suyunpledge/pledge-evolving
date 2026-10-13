# Coding Mode W5 批次交付 — 2026-10-13

分支：`feature/coding-mode-w5-ux-b1`（基线 main `2bfd038`）。未推送，等待集成验收。
范围：reports/17-前端交互设计审查 剩余项 **B1[P0] + B2/B4[P1] + E1[P1]**，
外加 W4 遗留的一个真缺陷（权限徽标父容器错挂）。C2（主 GUI 导航 emoji→矢量
图标）与 B3（输出区与项目栏去重）本轮未做，见"未实现"。

## 修改文件

| 文件 | 变更 |
| --- | --- |
| `forge-gui/development_mode/app.py` | B1/B2/B4/E1 + 徽标归属修正 + 只读 Text 请求高度 |
| `forge-gui/development_mode/test_app.py` | +6 项 W5 测试（隐藏页判定用 tab state） |

## 变更明细

### B1 [P0] Agent 栏语义分组（纯布局，行为零变化）
- 420px 栏内 14 个可交互元素平铺 → **会话 / 任务 / 模型** 三区，两条
  Separator 划界：会话（状态行+日志+上下文+输入+发送）、任务（规划/权限/
  复审/恢复/子Agent/取消/执行测试）、模型（任务角色+独立模型+回显）。
- 模型类 4 处散置控件收敛：header 的"模型角色"保留（全局角色设置），
  会话组模型下拉 + 模型组（独立模型/任务角色/回显）各行归位。
- **修 W4 缺陷**：权限徽标父容器从工作台顶层（`self`，渲染漂到分栏之外）
  改回 Agent 栏状态行，与 task_label 同排右置。
- 附带修复：`_readonly_text` 请求高度 24 行（≈400px 最小请求）→ 6 行。
  fill/expand 下实际显示不变；此前 760px 窗口里 Agent 栏底部控件会被
  挤出映射（新增两行后必现，原布局已在临界点）。

### B2 [P1] 完整事件 tab 下沉
- 默认隐藏（`log_tabs.hide`），状态行"事件"复选开关按需恢复；隐藏期间
  数据继续写入，开关往返不丢历史（测试覆盖）。

### B4 [P1] chrome 语义去重
- 移除"专注：编辑器 / Agent"按钮（与预设值 focus 语义重叠）；`toggle_focus()`
  方法与状态机原样保留（测试契约不变）。预设值 'agent'/'editor'/'focus'
  不变（持久化兼容）。

### E1 [P1] 发送键位惯例化
- Enter=发送、Shift+Enter=换行、Ctrl+Enter 依旧有效；输入区下方常驻
  键位提示。IME 组合守卫与 InputCard 同款：行内 IME（FORGE_INLINE_IME=1）
  组合期 Enter 交给输入法；默认系统候选窗路径 Tk 不向绑定投递组合期按键。

## 测试结果

- `development_mode/test_app.py`：**22/23 通过 + 1 例漂移型 Tcl 抖动**
  （两轮全量分别为 badge/attachment 例失败，单跑均过——与 W3/W4 报告记录
  的本机 init.tcl 环境抖动同源；W5 新增 6 例全部稳定通过）。
- `test_workspace_wiring`：12/12。`run.py selftest`：574/574。
  i18n 收口：通过（development_mode 不在 6 文件扫描范围，文案风格与该文件
  既有中文一致）。
- 未执行：人眼视觉验收（本机 Tk 资产环境问题依旧，见 W3 报告环境说明）。

## 与其他 Agent 的冲突点

- `development_mode/app.py`：W4 批次同文件（已合入 main，本轮基于其后基线
  改动，无未提交冲突）。后续 W6+ 若再动 `_build_agent`，以本轮三组分区
  结构为准。
- 无共享 Registry/Policy/Runtime 改动；未触碰 forge/development/ 内核。

## 未实现（后续批次）

- **B3 [P1]**：输出区 5 tab 与项目栏"Git 变更"页去重（需与 dock/活动栏
  契约落位一起设计，牵动 apply_layout 权重，独立批次处理）。
- **C2 [P1]**：主 GUI 导航 emoji→矢量图标（forge_gui_v2 NAV_ITEMS 域，
  视觉回归量大，建议单独批次 + 视觉验收机）。
- **D3/E2 [P2]**：恢复会话内联横幅、快捷键发现面板。
- AI 面板统一到 chat_widgets（原 W3 报告 Phase 2 项）仍待做，建议在 B3
  dock 重排之后进行，避免两次重排同一区域。
