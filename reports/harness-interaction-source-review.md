# DeepSeek Harness 官方交互源码调研

范围：只读检查本机已安装的官方 `@deepseek-ai/dsh-client-ui-conversation` 包及官方仓库文档，未读凭据、会话、配置内容，未修改 Forge。安装包身份由 `package.json` 核验：`@deepseek-ai/dsh-client-ui-conversation` `0.1.0-rc.8`，repository 为 `https://github.com/deepseek-ai/deepseek-harness`，directory `packages/client/ui-conversation`，license `MIT`。包中 `lib/client.js` 的 bundler 区域注释保留了上游模块名（如 `lib/types/client/chat/ChatView.js`）；以下行号是此已安装构建文件的行号，不是 TS 原文件行号。未取得与该构建对应的 Git commit SHA，因此不将它冒称为精确 commit 的源码快照。

只读取公开代码路径：

- `C:\Users\匡溯昀\.dsh\profiles\node_modules\@deepseek-ai\dsh-client-ui-conversation\package.json`
- 同包 `lib/client.js`

## 实际 renderer 源码证据

### 1. 跟随滚动由所有权状态控制，读者能随时暂停

在 `lib/client.js:5567-5581`，`ChatView` 分别维护 `atBottomRef`、React `atBottom`、`observedTopRef`、分页语义锚点 `anchorRef` 和 `followSigRef`。注释明确 `followSigRef` 用来防止因为滚动状态 UI 重渲染而把惯性滚动再吸到底部（5578-5581）。这是真正运行的组件代码，不只是设计说明。

- `5588-5595`：显式 `toBottom` 清除分页锚点、写入底部 scrollTop、同步 observed-top 和 pinned 状态，并清除持久化滚动位置。
- `5623-5634`：分页插入旧消息后，使用锚定行的前后位置差修正 `scrollTop`，保留读者看到的内容位置。
- `5637-5644`：追加用户/steering 消息时滚到底；流式状态只在 tip 变化且仍 pinned 时跟随。
- `5645-5669`：仅当真实交付位置偏离最近程序写入/接收的 observed-top（>0.5px）时，才把滚动归因为读者；距底部 25px 内重新进入 pinned，离开则释放跟随。阅读者事件会更新分页锚点，并保存滚动位置。
- `5670-5682`：监听主 scrollport 的 `scroll` 事件，不按设备分别监听 wheel/touch/键盘。
- `5683-5706`：`ResizeObserver` 仅当仍 pinned 时在容器尺寸变化后跟随；不对每个 token 另行滚动。
- `5707-5721`：停止旧记录加载后清分页锚点；点击加载更多前记录当前可见锚点及其相对顶部位置。
- `5776-5788`：离开底部时显示显式“回到底部”按钮，点击走 `toBottom`，而非每次自动夺回滚动位置。

上游模块可对应到 `packages/client/ui-conversation/src/client/chat/ChatView.tsx`（bundle 本地实际代码函数名 `ChatView`）；官方已实现说明见 [sticky composer / conversation scroll note](https://github.com/deepseek-ai/deepseek-harness/blob/master/.agents/notes/archived/bug-fix/2026-07-29-sticky-composer-conversation-scroll.md) 和 [observed-top ledger note](https://github.com/deepseek-ai/deepseek-harness/blob/master/.agents/notes/implemented/bug-fix/2026-08-06-reader-scroll-attribution-observed-top-ledger.md)。文档仅用于解释演进；行为结论以上面的安装包 renderer 源码为依据。

**Forge 修复前对照：** `forge-gui/chat_widgets.py:206-209` 的 `near_bottom()` 固定用 viewport 末端比例 `>=0.90`；`forge_gui_v2.py:6644-6647` 每个流式 delta 都检查该比例再滚动。长历史时这会把“离底还有约一屏但读者正在看旧消息”误判为跟随状态。官方通过持久 `atBottom` 所有权和对输入滚动的归因来区分，另有显式回底按钮。

### 2. 用户输入草稿只在请求成功后清除，失败保留供修正

`lib/client.js:1405-1411` 的 `beginSubmit` 注释明确接受 claim 后序列化草稿图片，只有 success 才清理和释放图片；serialize、transport 或 handler failure 会保留草稿和图片供修正。实现位于 `1412-1445`：异步提交前捕获图片 id；仅 success 分支释放已提交图片（1420-1424）；error outcome 和 promise rejection 都 dispatch 失败 settlement（1425-1441）；被取消/过期的 attempt 通过 abort signal 被 `dead()` 丢弃（1443-1445）。这提供了可重试而不丢输入的契约。

**Forge 对照：** `forge_gui_v2.py:6901-6908` 在 `_do_send` 开始后立即清空输入框与 `send_var`。但 `_chat_failed` / `_chat_cancelled` 已在输入框为空时恢复原消息，且附件仅在成功时清除；不能将它描述为完全没有失败恢复。已确认缺口是回复上的重试按钮忽略消息绑定、覆盖新草稿并自动发送，本轮针对该缺口修复。

### 3. 明确区分停止、发送、队列/steer 与忙碌状态

`lib/client.js:3498-3501` 派生当前 draft、附件及空状态；`3545-3551` 分别计算 locked、machineBusy、可 steer queue 等状态，而不是把所有异步状态折成一个 busy 布尔量。按钮行为在 `3775-3785`：主按钮随 agent 状态明确显示 Stop 或 Send；生成中点击走 stop，空输入或 machine busy 时禁用正常发送。实际按钮通过 `4035-4042` 绑定 disabled、mouseDown 和 click 行为。

`lib/client.js:256-260` 的 session `cancel()` 将失败作为明确错误抛出；`792-806` 的 `beginAttempt()` 为每次提交保存序号、AbortSignal、draftSnapshot、mode；`927-937` release 时 abort 当前 attempt 并把 machine 复位到 plain。这样取消不会误把迟到结果当成功，也有每次请求对应的 draft 快照。

**Forge 对照：** Forge 已有独立 stop 事件及忙碌按钮；对应前端位于 `chat_widgets.py:2417-2426`、`2454-2460`，传输端 cancellation 在 `forge_client.py`。可借鉴的是将 submitting、stopping、failed 的 UI/数据状态维持到相应异步 settlement 后，避免丢失草稿或迟到完成状态覆盖取消状态。

### 4. 焦点转换避免页面跳动，输入长草稿时只滚 composer 自己

`lib/client.js:3565-3585` 根据 textarea selection/caret 与镜像文本 Range 计算可见区域，必要时只调整 composer `scrollRef` 的 scrollTop。`3586-3596` 在 composer 解锁或 session 切换时把焦点放回 textarea，并用 `focus({preventScroll:true})` 防止浏览器把整个对话滚动到输入框；草稿变为非空时只揭示 caret。`3767-3770` 点击相关控件时阻止默认焦点切换，再将焦点送回 composer。IME composition 独立跟踪并延迟结束（3533-3541）。

**Forge 对照：** Forge 在 [`chat_widgets.py`](C:/Users/匡溯昀/pledge-evolving/forge-gui/chat_widgets.py) 暴露 `focus_entry()` 并支持 IME wrapper，但建议对焦点恢复流程采用 `preventScroll` 和仅滚动输入区的思路，避免长草稿 caret 自动显现时带动 transcript。

## 文档证据（与源码分开）

官方 Desktop README `apps/desktop/README.md` 的 Welcome 说明 记载 Welcome 页空白 key、document focus、稍后设置不保存、返回清空未保存输入，以及复制失败仍允许重试。这些是桌面流程说明，不是上述 composer renderer 的逐行实现证据。官方仓库页面将许可证列为 MIT；本机包的 `package.json` 也声明 MIT。浅克隆遇到 GitHub pack 下载卡住后已停止，没有以社区 fork 替代官方证据。
