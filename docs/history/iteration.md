# 迭代说明 · 30 分钟一轮的自动循环（v1 已上线）

> 对应你的要求：「所有智能体做完之后，三个智能体 + 千问分别审查一次；有条件联系 stepclaw 实测跑通；每 30 分钟交付一次结果并拉起所有智能体迭代一轮。」

## 现在已经自动跑的部分（不需要任何人动手）

**定时任务「框架迭代-30分钟轮」已创建**（每 30 分钟，App「定时」面板可见，默认单次 5 分钟）。
每轮执行 `iterate_round.py`，四个阶段：

| 阶段 | 做什么 | 第 1 轮实测 |
| --- | --- | --- |
| VERIFY | 全量自检 + 模块闸门 | 233/233 全绿；9/11 健康，2 份隔离（原因入账） |
| REVIEW | 便宜通道交叉审查（deepseek-flash 直连 API，单轮几百 token） | ✅ 已给出裁决：优先补自检证据契约 |
| LEDGER | 本轮记录追加进 `iteration-ledger.jsonl`（append-only） | ✅ |
| HANDOFF | 写圆桌喂料 `roundtable/feed-r###.json`，给四个目标实例 | ✅（见下） |

第 1 轮审查员的原话：

> 优先修：让自检输出可验证证据。两个被隔离模块失败原因完全相同——没有 per-case (name, ok, detail) 行或 SELFTEST_CASES 声明。233/233 全过说明不是功能缺陷，而是证据契约缺失；改一处公共自检骨架即可同时解隔离两个模块，收益最高、成本最小。

这条裁决我认为是对的，已经放进待办第一位。

## 「拉起所有智能体」这一段：现在只能到一半

实测（不是猜）：`sessions_send` 直发 `deepseek` 实例被拒，原文——

```
Session send visibility is restricted. Set tools.sessions.visibility=all
```

所以当前每轮的 HANDOFF 阶段做的是：**把喂料文件写好、放在约定路径**，等开关一开，四个实例
（qwen / deepseek / 45·mimo / stepclaw）就能读到并追加回复到 `roundtable/replies.md`。
循环本身不依赖这个开关——开关打开的那一刻，多实例评审自动接入，脚本一行不用改。

## 需要你做的两件事（都是一次性的）

1. **开跨 agent 开关**（路线 A）：
   ```json5
   tools: {
     sessions: { visibility: "all" },
     agentToAgent: { enabled: true, allow: ["deepseek", "45", "qwen"] },
   }
   ```
2. **stepclaw 的实测**：它不在本机 AutoClaw 的 agents.list 里（是独立进程）。若要它参与实测，
   用 [第三轮任务书](briefs-round3.md) 中转，产出照旧走贡献闸门。

## 各轮去哪看结果

- **App「定时」面板**：每轮的中文回报（round 号 + verify + review 一句话）
- `agent-forge/iteration-ledger.jsonl`：机器可读的全量轮次账本
- `agent-forge/roundtable/feed-r###.json`：每轮喂料；`replies.md`：各实例回复（开关开后）
