# F-1 状态栏竞态修复与复验 · 2026-10-13

分支 `feature/coding-agent-ux`，修复父提交 `d751c74`。采用验收要求方案 a；本轮不推送、不合并。集成顺序仍为 WorkBuddy 布局域先合、Agent 工作流域后合。

## 原因与修复

真实竞态路径：`handle_event` 安排 `_schedule_refresh(800)` → 用户点击旧版本卡片 Revert → 检查点预览发现磁盘版本变化，写入「拒绝恢复」提示 → 延迟 `refresh_project()` 的扫描完成 → `_project_scan` 无条件写 Git 摘要，提示消失。保存确认、错误等其他状态提示也受同一路径影响。

`__init__` 在 `_build` 后给既有 `status` 变量注册写入监听；使用 `time.monotonic()` 记录语义提示时间。新增 `_status_written` 与 `_set_project_status`：成功项目扫描距最近提示不足 2 秒时跳过状态文本写入，但继续更新文件树、Git 变更列表、选择与待处理扫描。2 秒后再次完成的扫描可以更新状态文本；没有额外到期覆盖定时器。背景摘要写入不会续期提示窗口，后续用户提示仍正常立即生效。扫描错误沿用既有错误通道，仍可显示新的错误。

本轮 app.py 既有方法仅修改 `__init__`、`_project_scan`，新增两个方法；这是 F-1 要求所需的公共状态反馈修复。未批量迁移其他 status 写入点，未改变恢复、保存或权限裁决。八保护方法 `_build/_build_project/_build_editor/apply_layout/place_sashes/_panes_resized/capture_layout/toggle_ide` 与 `51f6f82` 的 AST 全一致；`workspace.py/gui_theme.py/chat_widgets.py` 及 WorkBuddy 新布局模块零修改。

## 回归与验收

增强原 `test_revert_card_refuses_new_disk_revision`：保留真实 Runtime 编辑与拒绝恢复断言，并强制在拒绝后调用真实扫描完成回调，确认新文件仍出现在文件树、拒绝提示保留、磁盘外部版本不被覆盖。新增 1 项测试覆盖保存/拒绝/错误提示、0/0.8/1.999 秒内不覆盖、2 秒边界允许更新，以及连续背景刷新不延长保护窗口。时间模拟只围绕同步状态写入/扫描回调，不影响真实异步测试的等待条件。

环境为 Windows、完整 Python 3.12.10（本机 `AppData/Local/Programs/Python/Python312/python.exe`）；Tk 8.6.15。未设置 TCL_LIBRARY/TK_LIBRARY，未替换或修复 Tcl 安装。真实隐藏 Tk 窗口、离线模型 fixtures、实际 Runtime/Git/文件操作。

| 验收项 | 结果 |
|---|---|
| 拒绝恢复用例，10 个独立 Python 进程串行运行 | 10/10，全部退出码 0 |
| 四套件连续第 1 轮 | 64/64，0 失败、0 错误、0 跳过 |
| 四套件连续第 2 轮 | 64/64，0 失败、0 错误、0 跳过 |
| 八保护方法 AST | 8/8 与基线一致 |
| git diff --check | 通过 |

四套件范围：`tests/test_coding_agent_ux.py`（12）、`forge-gui/development_mode/test_agent_panel.py`（17）、`forge-gui/development_mode/test_app.py`（23）、`forge-gui/test_workspace_wiring.py`（12）。不将第一轮的全部 502 项或 selftest 574 项计入本轮复验；本轮严格使用集成方指定范围。

复现命令（一次运行完成 ×10 + 两轮，无失败重试或过滤）：

```powershell
Set-Location 'C:\Users\匡溯昀\forge-coding-agent-ux'
& 'C:\Users\匡溯昀\AppData\Local\Programs\Python\Python312\python.exe' -B tools/verify_coding_agent_f1.py --output 'C:\Users\匡溯昀\forge-coding-agent-f1-recheck'
```

完整机器记录：`reports/coding-agent-f1-validation.json`，含逐次命令、退出码、数量、耗时、日志 SHA-256 与源码 SHA-256。原始日志目录：`C:\Users\匡溯昀\forge-coding-agent-f1-validation`。两轮分别耗时 56.906 秒、57.766 秒；10 次拒绝用例各约 2.7–3.2 秒。

## 间歇失败史与报告勘误

集成方此前独立复验：拒绝恢复单跑 5 次有 1 次被 `(detached / no Git)` 摘要覆盖，属于本次修复的真实 F-1；历史同名 TclError 属安装环境抖动，不能混为同一原因。原交付「Unittest 502 通过，2 项平台条件跳过」仅代表当时单次运行，不能证明稳定通过；原报告已加勘误入口。本次按连续两轮口径重新申报，12 次验收进程未出现 Tcl 安装错误，不宣称环境已修复。

修复开发时首次两项定向测试曾因新增文件树断言使用未 resolve 的 Windows 临时路径而失败；改用与扫描一致的 `marker.resolve()` 后两项通过，随后启动上述完整验收，验收期间未改源码、未重试失败。该失败是新断言的路径规范化问题，独立于 F-1 与 Tcl 安装抖动。
