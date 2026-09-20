# 微信 IM 通道

让 forge 通过微信公众号/机器人收发消息：用户在微信里发一句话，forge 跑完 agent 再把结果发回去。

## 传输与鉴权

微信通道走腾讯 iLink bot 的 HTTP API，不是自己实现的协议栈。

| 项 | 值 |
|---|---|
| Base URL | `https://ilinkai.weixin.qq.com` |
| 鉴权 | `Authorization: Bearer <token>`，token 来自扫码登录，属于 **bot 账号**而非用户 |
| 标记头 | `AuthorizationType: ilink_bot_token`、`iLink-App-Id: bot`、`X-WECHAT-UIN`（随机） |
| 收消息 | `POST ilink/bot/getupdates`（长轮询，带不透明游标） |
| 发消息 | `POST ilink/bot/sendmessage` |
| 输入态 | `POST ilink/bot/sendtyping`（尽力而为，失败不影响回复） |
| 配置 | `POST ilink/bot/getconfig`（取 typing ticket，也用于探测凭据） |

## 快速开始

### 1. 拿到凭据

两种方式，任选其一。

**A. 复用已有的 OpenClaw 微信登录**（推荐，省一次扫码）：

```bash
python run.py channel weixin import
# 或指定来源
python run.py channel weixin import --source "C:\Users\<你>\.openclaw-autoclaw\openclaw-weixin\accounts\<id>-im-bot.json"
```

导入会写到 `~/.forge/channels/weixin/default.json`（权限 600），命令末尾会打印可直接粘贴的配置行。

**B. 自己扫一次码**：在 OpenClaw 侧完成 `channels login --channel openclaw-weixin` 后，再用方式 A 导入；forge 本身不内置二维码渲染。

### 2. 写配置行

写进用户层 `~/.forge/forge.patch.json`（`import` 命令会打印现成的 JSON）：

```json
{
  "id": "channel:weixin",
  "name": "channel:weixin",
  "config": {
    "enabled": true,
    "accountId": "default",
    "tokenFile": "~/.forge/channels/weixin/default.json",
    "sessionScope": "per-peer",
    "botAgent": "forge/0.8.0"
  }
}
```

### 3. 探测凭据（不发消息）

```bash
python run.py channel weixin status
```

成功时：

```json
{
  "channel": "weixin",
  "account": "69586441fb75",
  "base_url": "https://ilinkai.weixin.qq.com",
  "token_source": "file:~/.forge/channels/weixin/default.json",
  "token_present": true,
  "reachable": true,
  "ret": 0,
  "has_typing_ticket": true
}
```

`status` 打的是只读接口 `getconfig`，不会触发任何出站消息。

### 4. 跑起来

```bash
# 常驻
python run.py channel weixin serve

# 冒烟：只轮询 3 次就退出
python run.py channel weixin serve --max-messages 3

# 冒烟：空闲 60 秒后退出
python run.py channel weixin serve --idle-seconds 60
```

## 命令总览

```
python run.py channel                          列出已注册/已配置的通道
python run.py channel list                     同上
python run.py channel weixin status            探测凭据（只读）
python run.py channel weixin import            导入已有凭据
python run.py channel weixin serve             启动收发循环
```

## 配置项

| 键 | 默认 | 说明 |
|---|---|---|
| `enabled` | `false` | **默认关闭**。不开就不加载 |
| `accountId` | `default` | 一个账号一套游标/去重状态 |
| `baseUrl` | iLink 正式地址 | 换测试环境时才需要改 |
| `tokenEnv` | — | 存放 token 的**环境变量名**（最安全） |
| `tokenFile` | — | 含 `token` 字段的 JSON 文件 |
| `token` | — | 内联 token（`doctor` 会告警，最后手段） |
| `botAgent` | `forge` | 出站请求的自我声明，仅用于后台日志归因 |
| `sessionScope` | `per-peer` | 会话隔离粒度，见下 |
| `pollTimeoutMs` | `30000` | 长轮询超时 |
| `sendTimeoutMs` | `20000` | 发消息超时 |
| `chunkLimit` | `2000` | 单条消息字符上限，超出自动分片 |
| `stateDir` | `~/.forge/channels/weixin` | 游标与去重状态的落盘位置 |

**token 解析优先级**：`tokenEnv` → `tokenFile` → `token`。三者都没有时**构造即报错**，而不是起一个发不出消息的通道。

## 会话隔离

`sessionScope` 决定「谁跟谁共享上下文」。多账号或群聊场景下这一步不做对，会出现两个用户串话。

| scope | session key 形态 | 适用 |
|---|---|---|
| `shared` | `channel:weixin` | 单人自用 bot |
| `per-peer`（默认） | `channel:weixin:<account>:<对方>` | 每个私聊/群独立一条线 |
| `per-account-channel-peer` | `channel:weixin:<account>:<群或对方>:<对方>` | 多账号 + 群聊，最严 |

## 设计要点（每条都来自实际踩过的坑）

### 长轮询超时是"没消息"，不是故障

服务端会把请求一直握着直到有消息或窗口到期。**窗口到期会以 socket read
timeout 收场，这是正常路径**。早期实现把它当传输错误抛出，结果空闲时每次轮询
都刷一条错误日志 + 退避 2 秒——而空闲是常态。

现在超时被转成 `PollTimeout`，`poll()` 接住它并返回空批次。真实 API 验证：
空闲窗口返回 0 条消息、无异常。

### 响应封套的 `ret` 与 `errcode` 都要看

iLink 的响应同时带 `ret`（通用返回码）和 `errcode`（更具体）。只查 `errcode`
的话，非零 `ret` 会被当成"成功但没消息"——**静默丢消息**。现在两者任一非零都报错。

### 轮询窗口自适应

服务端在响应里回传 `longpolling_timeout_ms` 作为下次建议值。现在会被采纳，
并钳制在 5s–60s 之间（防止异常建议把循环卡死或忙转）。

### 游标必须落盘

长轮询返回的是不透明 buffer。丢了它只有两种结果：重放历史（重复回复）或跳过消息。
`Cursor` 每次成功轮询后立即写盘。**失败时绝不推进游标**——否则下一轮会跳过整段消息。

### msg_id 必须去重

消息面是 **at-least-once**。不去重的话，一次重投就会让 bot 回答同一条消息两次——
在群里看起来就是 bot 在自言自语。`SeenSet` 是有界集合（默认 2000 条，超出淘汰最旧）。

### 长回复必须分片

消息面有长度上限，超长回复会被整体拒绝，用户一个字都收不到。`chunk_text` 按
段落 → 换行 → 空格的优先级切分，保证既送得出去又读得顺。`send()` 返回实际投递的
分片数，调用方可以区分"发出去了"和"没东西可发"。

### agent 只构建一次，不每条消息重建

早期的 `serve` 每条消息都调一次 `build_agent`——重新读配置、重扫能力目录、重开
记忆库、重建整个工具注册表。现在共享部分（policy / tools / memory / capabilities）
只建一次；只有 per-lane 的 session 在变。

### 每个会话一条独立的日志

session *key* 把会话在逻辑上分开了，但早期所有 key 都写进同一个
`home/sessions/current.jsonl`——即逻辑隔离、物理混一起。现在每个 key 映射到
`home/sessions/channels/<key>.jsonl`。`SessionIndex` 扫整个目录，所以
`forge sessions` 仍然索引得到。

### 单实例锁

同一账号起两个 `serve` 会互相抢游标并双重回复。现在用锁文件（放在游标旁边）
拒绝第二个进程；持锁进程已死时锁可回收，锁文件损坏按陈旧处理而非致命。

### 游标/去重状态损坏不致命

状态文件被写坏时降级为空并继续，而不是拒绝启动。一个损坏的 JSON 不该让 bot 起不来。

### 机器人回声必须过滤

`message_type` 只接受 `1`（USER）。不过滤的话，自己发的消息会被当成新输入，形成死循环。

### 只有媒体的消息给占位符

纯图片消息会转成 `[图片]` 交给 agent，让它可以回一句「这个我还看不了」，而不是沉默。
语音消息优先用服务端的转写文本。

### 失败要么响亮要么吞掉，不要模糊

- 构造时缺 token → **抛异常**（起不来比假装能发好）
- 发消息失败 → **抛异常**（调用方决定丢弃还是重投）
- 输入态失败 → **静默返回 false**（它只是锦上添花，不该阻断回复）
- 轮询超时 → **返回空批次**（这是正常路径，不是失败）

### token 不进日志

所有异常信息与日志行都过 `_redact()`；URL 不落日志（查询串可能带签名）。

## 已知边界

- **不自带扫码登录**。凭据需从别处获得（当前从 OpenClaw 导入）。
- **媒体只做占位**。图片/文件/视频不走 CDN 下载，agent 拿到的是 `[图片]` 这类标签。
- **单进程**。同一账号起两个 `serve` 会被锁文件拦住（不会静默双跑）。
- **`errcode -14`（会话超时）不自动重登**：报错要求人工续期。
- **未做 AT 提及解析**：群聊里任何消息都会触发，没有「只在被 @ 时响应」的开关。
- **未做发送限流**：连续长回复可能撞上服务端风控。

## 排错

| 症状 | 原因 | 处理 |
|---|---|---|
| `no weixin token configured` | 配置行缺 token 来源 | 跑 `channel weixin import`，或设 `tokenEnv` |
| `weixin channel is not enabled` | `enabled` 不是 `true` | 改配置行 |
| `getupdates error -14` | 凭据过期 | 重新扫码/重新导入 |
| `reachable: false` | 网络或 token 无效 | 先用 `status` 看 `error` 字段 |
| 回复没收到 | 分片数超出限流 | 降低 `chunkLimit` 或缩短回复 |
| 同一个问题答两次 | 去重状态被清 | 检查 `stateDir` 是否可写 |

## 测试覆盖

`tests/test_channels.py`（10 项，全部离线）：

1. `session_for` 三种 scope + 未知 scope 回落
2. `chunk_text`：空串 / 短文本 / 段落优先 / 无断点硬切 / `limit<=0` 报错
3. `Cursor` 持久化 + 损坏降级
4. `SeenSet` 去重 + 上限淘汰 + 持久化 + 损坏降级
5. token 解析优先级（env > file > inline）+ 无来源报错
6. `_client_version` 编码（含非法输入）
7. 通道注册表（含未知通道报错）
8. 入站归一化：文本 / 语音转写 / 媒体占位 / 混合 / 群 id / **机器人回声过滤** / 无发送者过滤 / 空文本过滤
9. 出站报文体形状 + 分片 + 空收件人报错 + `base_info` 合并
10. 凭据导入往返 + 无 token 凭据拒绝

`tests/test_channels_opt.py`（9 项，全部离线，覆盖优化后的行为）：

1. 长轮询超时 → 返回空批次（不是异常）
2. 非零 `ret` 报错；`ret=0` 且无消息是干净的空轮询
3. 非零 `errcode` 报错
4. 自适应轮询窗口 + 钳制（1ms→5s，10⁹ms→60s）
5. 游标只在成功时推进（失败不推进）
6. 跨轮询的 msg_id 去重
7. `send()` 返回分片数（空文本返回 0）
8. serve 锁：活跃持有者拒绝二次启动 / 死进程的锁可回收 / 损坏锁按陈旧处理
9. `_ServeStats` 记账

测试不发起任何网络请求：`WeixinChannel.__init__` 只做配置解析，真实收发由 `serve` 承担。
单独的真机只读探针（不发送、不消费）见 `.openclaw/tmp/probe_poll.py`。
