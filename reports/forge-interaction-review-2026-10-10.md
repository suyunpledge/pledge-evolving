# Forge 交互审查与验收 · 2026-10-10

## 范围与来源

本轮由 GPT-6 Luna 阅读 DeepSeek Harness，由 GPT-5.6 Terra 阅读本机 AutoClaw 前端并复审；主 Agent 负责实现、故障复现、权限与状态边界审查、离线验收。修改目标为 Python desktop。

- [DeepSeek 官方产品入口](https://www.deepseek.com/en/harness/) 指向 [deepseek-ai/deepseek-harness](https://github.com/deepseek-ai/deepseek-harness)。Desktop 是承载 Web 客户端的 Electron 外壳，交互实现主要位于客户端包。
- 实际读取本机 `@deepseek-ai/dsh-client-ui-conversation` **0.1.0-rc.8** 的 `lib/client.js`、`package.json` 和 MIT LICENSE。包声明的源码目录为 `packages/client/ui-conversation`。完整 Git 克隆未成功，不将本机版本声称为上游最新提交。
- [Harness 详细来源记录](harness-interaction-source-review.md)；[AutoClaw 2.0.4 前端产物记录](autoclaw-interaction-source-review.md)。AutoClaw 为只读研究压缩 JS，未复制闭源实现，未读取凭据或聊天数据。依赖库包含某能力不等于客户端一定启用了它。

## 采用的交互行为

| 行为 | Forge 原问题 | 本轮实现 |
| --- | --- | --- |
| 用户掌握滚动跟随 | 每个流式片段按整个历史高度的 90% 判断是否追底；长对话上滚数屏仍会被拉回 | 以实际像素距离辅助判断，按 DPI 缩放；滚轮/滚动条离底即暂停跟随；“回到最新”恢复跟随 |
| 主动发送显示新一轮 | 用户在旧消息位置发送，新消息可能留在视野外 | 只有主动发送明确恢复追底；传入流式片段不会解除阅读暂停 |
| 操作绑定具体消息 | 每张回复的重生成按钮均取最后一条 user，忽略被点的回复 | 每张回复绑定自身原始输入及 session；无绑定、已销毁或跨 session 消息不执行 |
| 重试可检查、可修改 | 点击即覆盖当前草稿并自动发送，可能再次调用写工具 | 明确改名“编辑后重试”，只恢复草稿；有文字或附件则保留并提示，用户确认发送后才进入正常 Policy 流程 |
| 附件与敏感输入 | 重试载荷缺少明确归属 | 当前回合保存附件快照的独立副本，prompt 使用 SecretRef；历史仅有合并文本，明确提示恢复为文字快照 |

其余值得借鉴的异步请求身份校验、分页锚点、失败恢复等见来源报告。Forge 已有请求代次与取消逻辑，本轮没有据此扩建新框架。

## 状态和安全审查

- 重试按钮不发送请求、不执行本地命令、不调用工具、不修改已保留的对话历史，也不授予任何权限。它将原问题准备为**新的用户输入**，不是回滚原会话分支。
- 当前回合 prompt 在绑定卡片前经过 `SecretScope.protect_text`；附件来自已有受保护快照。此行为不会新增 Secret resolve 接口或持久化敏感原值。
- 从旧历史载入的 SecretRef 仍受已有 SecretScope 校验；不重新构造已失效映射。
- 显示“回到最新”使用独立覆盖按钮，不重挂载 sidebar 或输入框。继续使用已有 16ms 合并布局回调，没有引入嵌套 `update()` 或逐 token 同步布局。
- 新提示补齐现有十种语言。未更换主题、插件体系或打包产物。

## 测试证据

新增 `forge-gui/test_interaction_intent.py`，已加入默认离线测试入口。

1. 修复前运行最初 6 项测试，出现 7 个失败（含参数化子测试）：错误消息重试、草稿覆盖、无绑定误重试、历史无消息绑定、上滚被拉底等均实际复现。记录 `.codex-interaction-intent-before.log`。
2. 修复后执行 59 项相关回归全部通过，含新测试、消息显示、取消、输入法、滚动、多语言；已有消息几何测试覆盖五档 DPI。记录 `.codex-interaction-regression.log`。
3. Terra 复审提出历史附件表达差异，添加文字快照提示及断言；10 项针对性复验通过。记录 `.codex-interaction-final.log`。
4. 另增主动发送可见性断言，先复现失败（`.codex-send-scroll-before.log`），再修复并运行消息交付与交互测试；最终 13 项全部通过，见 `.codex-interaction-acceptance.log`。

编译检查、多语言覆盖检查及 `git diff --check` 通过。测试使用临时配置与模拟客户端，没有付费厂商调用；不将这些结果描述为全仓库回归或真实联网模型验收。
