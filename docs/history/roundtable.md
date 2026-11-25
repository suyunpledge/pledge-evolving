# 圆桌会议：怎么让 DeepSeek / MiMo 实例真正「讨论起来」

> 起因：用户提出「到时候你就自己拉起 DeepSeek 和 MiMo 的实例进行讨论，我记得是有群聊功能的」。
> 这份文件记录**实测结论**与两条可行路线，以及各自需要谁动手。

## 一、已核实的事实

本机 AutoClaw 里确实并存多个 agent 实例（`openclaw.json` 的 `agents.list`）：

| id | 名称 | 模型 |
| --- | --- | --- |
| `deepseek` | Deepseek | `deepseek-v4-flash` |
| `45` | mimo | `mimo-v2.5` |
| `qwen` / `glm` / `gpt` / `agent-obcv` | 其它实例 | qwen3.8-flash / GLM / GPT OSS / 豆包 |
| `agent-f5hc9` | 操作员（本人） | `zai/zai_auto` |

## 二、实测：直接驱动别的实例——**当前不通**

尝试方式：用 cron 建定向任务（`agentId: "deepseek"` 与 `agentId: "45"`），让它们各写一个文件到共享目录。

结果：
- 两个任务**都被接受创建**（返回了 job id），但到点后**没有任何运行记录**，共享目录也没生成；
- 任务因 `deleteAfterRun` 自行消失，**没留下任何痕迹**。

根因（config schema 原文）：

- `tools.sessions.visibility` 默认 `"tree"` —— 只允许定位"当前会话 + 自己派生的子会话"；
  可选 `self | tree | agent | all`，说明里明确写着 **"cross-agent still requires `tools.agentToAgent`"**。
- `tools.agentToAgent.enabled` 默认**关闭**，schema 提示：*"Keep disabled or tightly scoped unless
  cross-agent orchestration is intentionally enabled."*

**结论：不是 bug，是默认安全阀。** 我这个操作员实例现在看不到、也够不着另外两个实例。

## 三、路线 A（本地、最快）：打开跨 agent 开关，由我当主持人

需要改的配置（改完热加载，无需重启）：

```json5
{
  tools: {
    sessions: { visibility: "all" },          // 或 "agent"，让会话工具能定位到别的 agent
    agentToAgent: { enabled: true, allow: ["deepseek", "45"] },   // 显式白名单，别开全量
  },
}
```

打开之后我就能：
1. 直接把议题发给 `deepseek` 与 `45` 两个实例，各自独立作答；
2. 把双方发言落在**同一个共享黑板文件**里（`roundtable/transcript.md`），第二轮让它们读到对方观点再回应；
3. 循环 2–3 轮后由我汇总分歧点。

这条路线**零外部依赖**，不占用任何 IM 账号，随时可以跑。

## 四、路线 B（你记忆里的「群聊」）：真房间，多 agent 同群

OpenClaw 确实有这个能力，官方叫 **ambient room events**（环境房间事件），并且文档明确支持
「几个 agent 共享同一个房间」并为每个 agent 单独设策略：

```json5
{
  messages: {
    groupChat: {
      unmentionedInbound: "room_event",   // 未点名的群聊消息当作安静上下文
      visibleReplies: "message_tool",     // 只在显式调用 message 工具时才发言
      historyLimit: 50,
    },
  },
  agents: {
    list: [
      { id: "deepseek", messages: { groupChat: { unmentionedInbound: "room_event" } } },
      { id: "45",       messages: { groupChat: { unmentionedInbound: "room_event" } } },
    ],
  },
}
```

另外房间本身要 `requireMention: false`（否则不点名不响应），并满足该渠道的 `groupPolicy` 与白名单。

**硬限制**：环境房间事件目前只支持 **Discord / Slack / Telegram** 三种；
**微信（openclaw-weixin）不在支持列表里**，飞书也没列。本机现在只启用了一个微信通道，所以：
真群聊需要你提供一个 Discord / Telegram / Slack 的群，并给两个 agent 各配一个账号（两个 bot）进群。
配置我来写，账号得你开。

## 五、建议

**先 A 后 B**：A 今天就能跑通，用来做设计评审、方案对撞、交叉审查这类"要观点不要表演"的讨论；
B 更适合需要你随时插话、而且要长期常驻的房间。

在那之前，**中转模式（你复制粘贴）依然是唯一在跑的多 agent 协作通道**——这条路今天已经产出 11 份贡献模块。
