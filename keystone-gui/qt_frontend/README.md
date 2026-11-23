# Qt 原生工作台

`keystone_qt.py` 的所有功能页由 QML 绘制，不再挂载 Tk 子窗口。
`WorkbenchController` 启动 `service_host.py`，通过 JSON 行协议收发页面快照、操作、审批与执行事件。
后台进程不导入 Tk 或创建窗口；网关、定时任务和模型请求不占用 Qt GUI 线程。

- `WorkspacePage.qml` / `WorkbenchField.qml`：延迟创建并保留页面、玻璃表单、文件编辑与结果面板。
- `workspace_service.py`：文件、配置、插件、Agent 设置、知识、演化、计划任务和账户管理，复用原存储。
- `coding_service.py`：独立 `DevelopmentRuntime`，保留任务队列、模型角色、审批、历史和代码回退。
- `gateway_service.py` / `scheduler_service.py`：管理本实例拥有的网关和只读定时任务。
- `computer_service.py` / `computer_runtime.py`：选定窗口、截图、视觉分析、逐次输入审批及对话工具。
- Work 对话整合 `team_service.py`，沿用原 Agent 协作开关与记忆设置，不再打开第二个聊天界面。

文件支持 UTF-8/UTF-16、原换行格式、搜索、查找替换、另存为和原文参考。
保存、合并、回退仍检查磁盘版本；未保存的编辑会阻止 Agent 写入。
旧 `embedded_host.py` 与 Tk 前端只保留兼容代码，Qt 启动路径不再引用它们。

## 回复后的代码修改与回退

Qt 主对话在 AI 回复下方显示本次实际修改的文件、增删行数和「查看修改 / 回退」。
差异在主窗口内打开，可切换文件、横向滚动、复制差异；回退需要确认。
Coding 页的任务变更摘要也提供「回退本次修改」，复用相同的文件快照与版本校验。

## 数据与回退范围

- `FileRevisionGuard` 在实际文件工具执行前后记录字节快照，回执返回 `change_ids`。
  快照在对应运行时 home 的 `changes/` 中，受工具路径保护；用户 Git 仓库和暂存区不参与回退。
- Qt 的 `ReplyChanges` 汇总同一回复的回执，并把引用、脱敏差异、状态保存到会话。
  Gateway 的 `/v1/changes/review` 和 `/v1/changes/revert` 校验当前主机策略。
  UI 另行执行当前桌面权限检查；手动批准模式仍需批准。
- 多次修改同一文件只展示操作前到操作后的最终差异。撤销恢复操作前的内容，
  包括原有未提交代码和换行格式；本次新增文件被移除，删除文件被恢复。
- 回退整批文件前检查所有目标的版本，并使用现有工作区 / 文件写入锁。
  有后续编辑或写入链不连续时拒绝覆盖；Coding 编辑器的未保存草稿也会阻止回退。
  非协作编辑器不受 Keystone 的锁控制，故写入每个文件前还会重新核对内容。
- 不自动重试回退请求。断连、磁盘故障或进程退出后应重新查看差异，确认完成与冲突状态。
  已回退记录有持久标记，重复操作不会删除或覆盖回退后新增的编辑。

## 支持与限制

支持工作区内经 `write_file`、`edit_file`、`apply_patch`、`delete_file`、`edit_config`
产生的文件修改。Shell、连接器、工作区外路径、链接路径及旧历史没有可靠快照时，
不提供自动回退。单文件快照上限 2 MiB；超过 256 KiB 的文件不生成文本预览，
单文件差异最多显示 64 KiB。一个回退请求最多 256 个回执、64 MiB 快照数据。

源码启动器会加载新界面代码；已经运行的 Keystone 和网关需要重启。
旧网关产生的记录只能显示已有差异，不能补造修改前的快照。

## 桌面宠物

「连接与外观 → 桌面宠物」或命令面板（Ctrl+K，搜「宠物」）可显示 / 隐藏宠物、切换和改名。
宠物是独立的无边框透明置顶小窗：拖动移动并记住位置，点击打开 Keystone，右键可换宠物或隐藏。
它跟随 Agent：工作时思考，完成时开心，出错或有待批准的操作时担心；空闲时偶尔走动，久无动静会睡着。
开启「减少动态效果」时不走动、不播放帧动画；隐藏时所有定时器暂停。主窗口关闭时宠物一并退出。

内置三只原创占位宠物 `crab` / `robot` / `whale`（`pets/`）。自定义宠物包放在
`<home>/gui/qt/pets/<id>/`，与内置同名即替换其形象，无效时退回内置；也可在设置里「导入图片文件夹」自动生成（一组表情图，或全身图 + expressions 子文件夹）。格式见 [pets/README.md](pets/README.md)。
是否显示、当前宠物、自定义名字和位置保存在桌面设置文件（`keystone.compat.desktop_config`）的 `"pets"` 键。

## 启动动画

程序启动时先弹出一个小的启动窗口（默认是约 1.4 秒的品牌标志淡入），然后才加载主界面。
主窗口一画出第一帧，启动窗口就淡出（约 0.2 秒），无论动画是否播完，所以它不会推迟可用时间；
加载比动画慢时停在最后一帧。点击或 Esc 跳过。减少动态效果、`--smoke`、`KEYSTONE_NO_SPLASH=1`
或 `"launch": {"enabled": false}` 时不显示。

替换为自己的动画（先匹配者生效）：

1. 桌面设置文件里的 `"launch": {"source": "<路径>"}`（相对路径以 `<home>/gui/qt/launch/` 为基准）；
2. `<home>/gui/qt/launch/launch.<扩展名>`。

支持 `.qml`（根元素为 Item，可读取 `launch.ready`、调用 `launch.skip()`）、视频
`.mp4 .webm .mov .m4v`（静音）、动图 `.gif .webp`、静态图 `.png .jpg .jpeg .svg`。
文件缺失、过大或类型不符时使用默认动画。默认动画只用 Animator，主界面加载时也不卡顿；
视频和 QML 动画在主界面加载期间可能停顿。实现见 `launch.py`。

## 本地验证

从仓库根目录运行，使用 Qt 专用解释器：

```powershell
.\.venv\qt\Scripts\python.exe -m unittest discover -s tests -p 'test_change*.py' -q
$env:PYTHONPATH = (Get-Location).Path
Set-Location keystone-gui
..\.venv\qt\Scripts\python.exe -m unittest qt_frontend.test_changes qt_frontend.test_backend qt_frontend.test_permissions -q
..\.venv\qt\Scripts\python.exe -m unittest qt_frontend.test_rendering -q
..\.venv\qt\Scripts\python.exe -m unittest qt_frontend.test_pets qt_frontend.test_launch -q
..\.venv\qt\Scripts\python.exe -m unittest development_mode.test_agent_panel -q
```

测试只修改临时目录。模型流使用本地替身，实际文件工具、HTTP 网关、版本检查和回退均执行真实代码。
