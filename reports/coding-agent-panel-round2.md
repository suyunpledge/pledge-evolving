# Agent 面板示例图增量 · 第二轮交付 · 2026-10-14

工作树 `C:\Users\匡溯昀\forge-coding-agent-ux`，分支 `feature/coding-agent-ux`，基线 `998d3fc`。本轮按派单中的文字细节实现；当前聊天未附可读取的示例图，不申报像素比对。沿用共享深色配色、字体、图标与 tooltip，`gui_theme.C` 值及文件均未修改。

## 实现与数据依据

1. 增加「AI Agent」标题栏及新对话、历史、设置图标。新对话在现有工作队列和 Runtime lane 内创建新 Session，旧日志保留；保持 Runtime、工作区文件、输入草稿，清空旧对话上下文、撤销重试指针并回到只读。运行中拒绝更换 Session。历史/设置调用已有恢复与模型角色设置入口。
2. 标签改为「对话 / 任务 / 子 Agent」，底部事件内容改为「变更事件」。选择保留三个独立标签：任务列表是计划/工具/测试证据，子 Agent 是运行身份树与详情，合并会压缩窄栏阅读空间。主 Agent 与 Review Agent 仍保留在运行树中，不过滤真实运行记录。
3. 每条实时消息增加灰色 hh:mm，使用传入事件时间或真实显示时间。历史消息按已获准恢复的 Session 原始 ts 匹配，过滤子 Agent、保留消息顺序，读取上限 16 MiB，匹配最近 100 条主对话；不把恢复时刻冒充过去。旧日志没有时间、或 continuation 的嵌入历史没有保留原 ts 时明确显示「时间未记录」。该读取只补元数据，不创建第二事件流。
4. 任务清单显示待办、进行中、完成三种标记及共享语义色。计划项没有实际阶段事件，继续待办，不按整体完成自动勾满；工具步骤直接消费真实动作状态，未知/失败单独显示。
5. 增加「任务规划」卡片：none/low/medium/high 分级（包括「中等 (Medium)」）、任务冻结权限、实际审批或拒绝状态、「查看变更」。卡片级别源于本次发送配置/真实 task_plan.level；授权源于真实 TaskSnapshot/config/action 证据。修改下一任务的权限/规划不会改写旧卡片；恢复卡片明确为历史授权、当前恢复只读。卡片查看变更绑定自己的 TaskEvidence，调用已有 Diff 展示接口，旧卡片不会跳到新任务 Diff。正式任务/实际计划出现时才创建卡片，避免发送后的临时空 task_id 再生成一张重复卡；查看变更回调只绑定一次，显示字段不变时不重复配置控件。
6. 输入区增加不会发送给 Runtime 的占位 Label、蓝色发送按钮、模型徽标、「创建子 Agent」及角色下拉。agent 映射原 default，complex/simple 保持已有角色；真实运行测试确认角色传入 TaskSnapshot。子 Agent 仍通过原 delegate/spawn 路径，不伪造创建成功。模型名来自配置或实际 usage，未硬编码 GLM-5.3。原 Enter/Shift+Enter、IME、权限确认与 F-1 提示保护保持。

## 变更边界

app.py 既有方法仅修改 `_build_agent/_toggle_events_tab/send/render_evidence/handle_event`；新增 `_agent_role_selected/_agent_model_selected/_view_agent_changes/_new_agent_conversation`。八保护方法与 `998d3fc` AST 一致。`layout.py/bottom_panel.py/explorer.py/welcome.py/editor_area.py/workspace.py/gui_theme.py/chat_widgets.py` 零修改；Runtime/证据/core 源码零修改。底部容器只使用原 `output/output_views` 接口；M5 接线说明见 `docs/coding-agent-panel-api.md`。

新增 4 个 Agent 面板测试，覆盖标题动作/新 Session 保留、真实与缺失历史时间、卡片冻结/逐动作授权/旧卡片 Diff/新任务恢复边界、占位/角色/实际模型；扩展原运行中切换测试检查不能新建 Session。两项旧标签契约按本轮中文文案更新，保护方法测试基线改为 `998d3fc`。专注切换等待真实输入映射，授权恢复等待原卡片按钮更新；保留全部原业务断言。真实 Tk 隐藏窗口、真实 Runtime/Session/Git/文件，离线模型 fixtures。

## 最终验收结果

冻结源码在原系统 Python 3.12.10 / Tk 8.6.15 上串行完成，最终命令退出码 0；没有失败重试、跳过或过滤。验收期间源码未再变化，最终 SHA-256 已逐文件复核一致。

| 验收项 | 结果 | 耗时 |
|---|---|---:|
| 独立 Agent 面板套件 | 21/21，0 失败、0 错误、0 跳过 | 62.594 秒 |
| 四套件连续第 1 轮 | 68/68，0 失败、0 错误、0 跳过 | 206.031 秒 |
| 四套件连续第 2 轮 | 68/68，0 失败、0 错误、0 跳过 | 171.094 秒 |
| 八保护方法 AST | 8/8 与 `998d3fc` 一致 | — |
| WorkBuddy 文件边界、app.py 既有方法白名单、git diff --check | 全部通过 | — |

指定四套件分别为 12 + 21 + 23 + 12 项。两轮各完整运行同一范围，本次 3 个验收进程均未出现 Tcl 安装错误、超时或原生异常退出，不宣称本机环境已修复。

机器记录：`reports/coding-agent-panel-round2-validation.json`，包含环境、完整命令、测试数量、退出码、日志及源码 SHA-256、保护方法与域边界结果。原始日志目录：`C:\Users\匡溯昀\forge-coding-agent-panel-round2-final-system-validation`。必须按下文记录的有界事件泵条件复现，不能把这些数字当作未适配的原无界事件泵结果。

## 复现

```powershell
Set-Location 'C:\Users\匡溯昀\forge-coding-agent-ux'
& 'C:\Users\匡溯昀\AppData\Local\Programs\Python\Python312\python.exe' -B tools/verify_coding_agent_panel_round2.py --output 'C:\Users\匡溯昀\forge-coding-agent-panel-round2-recheck'
```

工具串行运行独立 Agent 面板套件，以及指定四套件连续两轮；单次验收命令没有失败重试或环境错误过滤。检查八保护方法 AST、app.py 既有方法白名单、WorkBuddy 文件边界；记录逐次命令、退出码、测试数量、日志和源码 SHA-256。完整四套件包括 `tests/test_coding_agent_ux.py`、`development_mode.test_agent_panel`、`development_mode.test_app`、`test_workspace_wiring`。

验证条件必须同时复现：Development GUI 的有界等待上限从 20 秒调整为 60 秒，step 明确服务真实 timer/idle 回调，再处理最多 16 次窗口事件；串行运行脚本仅在测试进程中把 `test_layout_dpi.pump` 和 `test_workspace_wiring.pump` 替换为同样的有界事件泵，保留原 seconds、真实事件处理与所有原断言，两个 WorkBuddy 测试文件零修改。原 `root.update()` 在事件不断到达时没有返回时限，不能用外层 until 保证测试推进。脚本每进程 600 秒上限，超过时计作失败；每 120 秒输出诊断堆栈，不计成通过或跳过。因此本报告中的通过数据指这个明确记录的验证脚本条件，不声称原无界事件泵在此机器上稳定通过。

开发记录：第一次旧 GUI 套件运行 40 项，2 项仍期待英文标签而失败，已按用户要求更新文案断言；随后 Agent 面板 21/21 通过。源码复查补了恢复后新任务首帧的卡片隔离与陈旧 task_plan 排除断言。开发复验四套件第一轮 68 项有 1 项在专注切换后立即检查 input.winfo_ismapped 而失败，改为等待真实映射；另一轮在 Workspace wiring 导航测试长时间不推进，约 412 秒时主动结束，未计通过。该导航用例单独加堆栈诊断，最终 1/1 通过但耗时约 220 秒，堆栈包括未改的 Work Surface 构建、共享辅助函数 root.update 与 Tk 销毁。另两次单独编辑/恢复诊断在 20 秒内未显示变更卡片而失败；诊断显示文件已真实写为 print(2)、Runtime busy=false、GUI 队列仍有 2 个任务、界面证据停在 write_file started，清理也曾超时。将等待上限调整到 60 秒并加强恢复按钮更新条件后，原真实编辑/授权恢复用例通过，才重新启动最终整轮验收。这些失败/中止未被排除后拼成连续通过记录；最终记录使用新的目录与最终源码哈希。历史 Tcl 安装抖动与此前 F-1 的扫描覆盖竞态分别记录，本轮保留 F-1 两项回归用例。

后续诊断记录：系统 Tk 曾使独立面板进程超过 600 秒，按超时失败记录；一次完整四套件第 1 轮 68/68 通过，但第 2 轮在 Workspace 关闭测试期间以退出码 3221225477（0xC0000005）结束，无完整测试总结，未计通过。Codex 自带 Python 3.12.14 / Tcl/Tk 8.6.12 的对照暴露新增 IconButton 默认 highlightthickness 的 Tcl_Obj 类型问题，已在本域新按钮显式设置 1，和现有共享按钮用法一致；主题文件未改。对照环境面板 21/21 通过，但四套件两轮各有 5 个共享主题数值转换错误，未用其申报整轮通过。另一次新增占位测试单次 step 后检查显示状态而失败，改为等待真实 <<Modified>> 更新；随后冻结源码回到原 Python 3.12.10 做最终验收。所有对照失败与进程中止均单独保留，没有当作通过、跳过或自动重试过滤。

隐藏 Tk 自动检查不能代替人工对照示例图、真机可见窗口的字号、图标、滚动或 DPI 观感；本轮不宣称这些人工视觉验收已完成。未推送、未合并；合并顺序保持 WorkBuddy 布局域先合、Agent 域后合。

---

## 验收记录（集成方，2026-10-14 10:02）

**Agent 面板示例图增量（第二轮）：通过 ✅**

【集成方独立复验（与交付方口径完全一致的 verify_coding_agent_panel_round2.py，输出目录 `forge-coding-agent-panel-round2-recheck`）】

- 独立 Agent 面板套件：21/21，0 失败 / 0 错误 / 0 跳过（38.3s）
- 四套件连续两轮：68/68 + 68/68，0 失败 / 0 错误 / 0 跳过（94.9s / 100.6s）
- 八保护方法 AST 与基线 998d3fc 一致（_build/_build_project/_build_editor/apply_layout/place_sashes/_panes_resized/capture_layout/toggle_ide）
- app.py 既有方法白名单合规：仅 _build_agent/_toggle_events_tab/send/render_evidence/handle_event；新增 4 方法在申报清单内
- WorkBuddy 域零触碰（layout.py/bottom_panel.py/explorer.py/welcome.py/editor_area.py/workspace.py/gui_theme.py/chat_widgets.py 无改动）
- 交付后源码一致性：7 个涉改文件 SHA-256 与机器验证记录逐文件比对全部一致（验收期间源码未再变化）
- 3 个验收进程零 Tcl 安装错误、零超时、零原生异常退出（validation.json accepted=true）

【域边界裁决】

- 保留「对话 / 任务 / 子 Agent」三标签 + 底部「变更事件」的理由成立（窄栏阅读空间、真实运行树不过滤），符合派单"由你判断"的授权
- 时间戳语义合规：真实事件 ts 优先、恢复 Session 匹配原始 ts、缺失显示「时间未记录」、不把恢复时刻冒充过去
- M5 接线说明已阅（docs/coding-agent-panel-api.md），WorkBuddy 四标签重构落地后按该说明对接

【后续】

- 分支维持不推送、不合并；集成顺序不变：WorkBuddy 布局域（M1/M2 已过，M3 Explorer 进行中）先合，Agent 域后合
- 交付方报告中的诚实披露（开发期失败史、对照环境差异、无人工视觉验收）已阅，不构成验收阻塞
- 等待 WorkBuddy M3-M6 交付后触发合并窗口
