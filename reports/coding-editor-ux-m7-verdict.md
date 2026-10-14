# M7 状态栏与头部增量 · 集成方验收裁决 · 2026-10-14

交付提交 `3af3d6f`（2026-10-14 18:01:15 +0800，WorkBuddy）。
集成方于 18:07 巡检轮发现并独立复验；主 worktree 干净（HEAD 即交付
提交，无并行改动），故直接在主 worktree 验收，无需 detached worktree。

任务书模块 M7「状态栏与头部增量」：状态栏字段扩展、头部「在线 (n)」
绿点、品牌火花对齐示例图橙色。交付时任务书 M2–M7 全模块到齐。

## 独立复验结果（集成方环境：系统 Python 3.12.10 / Tk 8.6.15）

| 验收项 | 结果 | 备注 |
|---|---|---|
| `forge.selftest` | 574/574 | run.py selftest 直跑，退出码 0 |
| `test_app` | 23/23（114.3s） | 未复现存量 tcl8.6 IO 抖动 |
| `test_layout_regression` | 64/64（242.4s） | 56 存量 + 8 新增 StatusBarTests |
| AST 边界审计 | app.py 0 删 4 增 6 改 | 与申报清单逐项一致，见下 |
| 域边界 | 改动仅 app/editor_area/layout/test_layout_regression/ui_icons 五文件 | 51f6f82 保护方法在位；Codex 域零触碰 |

复现口径：`sys.path` 前插仓库根 + forge-gui 后 unittest（与 M4/M5/M6
裁决一致）。

## AST 审计明细（9c54ca8 → 3af3d6f）

- `app.py`：removed=NONE；added = `_sync_editor_status / _on_editor_cursor /
  _sync_ready_status / _sync_online_status`；changed = `_build /
  _document_loaded / _refresh_status_fields / close_current /
  handle_event / show_document_state`——与申报「6 改 4 增 0 删」逐项
  吻合。
- `editor_area.py`：added = `EditorArea.notify_cursor`；changed =
  `__init__ / _after_caret / set_text`（构造参数 +on_cursor，KeyRelease/
  ButtonRelease 同一汇聚点）——与申报一致。
- `layout.py`：added = `StatusBar.set_indicator / _paint_indicator`（Canvas
  oval 圆点，规避 '●' 跨字体渲染不稳）；changed = `NavigationBar._build /
  StatusBar._build / StatusBar.__init__`（docstring 同步）——与申报一致。
- `ui_icons.py`：方法级零改动（模块级 ICONS +flame，颜色由调用方经
  brand_spark token 注入，不写死）——与申报一致。
- 删除：0。

## 数据源真实性核查（由 64 项回归背书 + 代码走读）

- 编码/换行：Document.encoding/newline 真实读盘值；语言：扩展名映射，
  未命中显示 Plain 不猜。
- 光标：EditorArea insert 索引现算；notify_cursor 公开路径与真实
  KeyRelease/ButtonRelease 事件在 `_after_caret` 同一汇聚点（测试走
  公开路径的等价性申报理由成立）。
- 就绪态：runtime.busy/ready 现读三态（忙黄/就绪绿/未就绪灰），不持
  第二份状态。
- 在线状态：runtime 无 peers/presence 概念 → 「离线」+灰点，不伪造
  计数；`_sync_online_status` 为唯一接入点——与「无诊断器/在线数据时
  不伪造」的派单红线一致。
- 警告恒 0（本项目无独立诊断器，不伪造）；问题 = TaskEvidence
  count_problems 与底部徽标同源。

## 功能面抽查

- EDITOR_FIELDS 编辑器状态组（cursor/encoding/eol/language/ready/
  warnings/problems）位于 spacer 与瞬态消息之间，反序 pack 阅读顺序
  正确。
- 事件驱动刷新：任务开始/结束 → runtime 忙闲；失败记录类事件 → 底部
  问题徽标与状态栏 problems 同源刷新（handle_event 单点）。
- 标签切换/无文件/关闭最后一个标签（无 TabChanged 场景）均补刷新路径。
- 头部在线状态帧 + tooltip；品牌火花 flame 图标 fg=brand_spark（与
  chat 空态主视觉同源同色）。
- StatusBarTests 8 例覆盖：文档驱动字段/无文件占位/光标通知/就绪三态
  与圆点色/问题真实计数/头部离线不伪造/火花位置与颜色/两尺寸组内
  不越界。

## 已知边界（接受）

- 不可见无焦点窗口合成 KeyRelease 不触发绑定（Tk 原生限制）——测试走
  notify_cursor 公开路径，链路同一。
- 在线计数接入真实 presence 数据时只改 `_sync_online_status` 一处。

## 结论与合并窗口备注

**M7 通过验收。WorkBuddy 编码编辑器域 M2–M7 全模块验收完成。**

合并窗口时序快照（本裁决入库时）：

- 主仓 main 已于 13:17 合入布局域一段（M1-M3，c67a120）、13:57 完成
  Agent 域收尾（daead5f，含 Codex 二轮验收态对齐）、18:17 合入布局域
  二段（M4-M6 嫁接整合，a7293e3，六页制基）。
- M7 交付基线为 9f6e97c（M4-M6 二段合并前），属跨基线交付：并入 main
  时需按 a7293e3 整合裁决同款手法处理（bottom_panel 六页制键名、
  StatusBar 若与六页制状态栏有交叠需再嫁接），集成方本轮不代跑，
  待 WorkBuddy 集成进程完成其合并后复验（18:19 起主仓 selftest 进程
  运行中）后进行。
- 合并完成后：clash 7897 推送（origin/main）。

## 裁决

**M7 验收通过。WorkBuddy 布局域 M2–M7 全模块完成；Agent 域（Codex）
两轮均已验收通过且已并入 main。剩余动作：M7 并入 main（嫁接）→
合并后复验 → clash 7897 推送。**
