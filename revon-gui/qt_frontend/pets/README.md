# 桌面宠物包 / Desktop pet packs

revon 自带四只宠物，形象均来自官方或授权素材（署名与许可见各包 `NOTICE.md`）：
`robot`（Codex 小机器人，OpenAI 官方精灵图按状态裁切）、`crab`（Clawd 小螃蟹，
Claude Code 吉祥物的 MIT 像素图标）、`whale`（DeepSeek 小鲸鱼，官方鲸鱼标志）、
`mingyue`（溟月·鲸鱼娘，MIT 开源项目 dsh-whale-musume 的授权立绘与台词）。
可随时换成你自己的图片或精灵图。

## 放在哪里

| 位置 | 说明 |
| --- | --- |
| `qt_frontend/pets/<id>/` | 内置宠物，随程序发布 |
| `<home>/gui/qt/pets/<id>/` | 你的宠物包；`<home>` 为 `revon.compat.default_home()`（已有 `~/.forge` 时沿用它） |

- `<id>` 即文件夹名，只能用小写字母、数字、`-`、`_`（最长 32）。
- 与内置宠物同名（如 `<home>/gui/qt/pets/crab/`）即**替换**该宠物的形象；包无效时自动退回内置形象。
- 其它名字是新宠物，出现在「桌面宠物」设置里。设置里的「打开宠物包文件夹」会建好并打开这个目录，改完点「重新载入」。

## 从图片文件夹导入（不用手写 pet.json）

「桌面宠物」设置里：

- **导入图片文件夹…**：生成一只新宠物（文件夹名取自所选目录）。
- **用图片替换当前形象…**：替换当前选中宠物的形象（例如先选中鲸鱼，再导入你自己的「鲸鱼娘」图片）。

能识别两种目录结构：

1. **一组表情图**：按文件名对应状态，例如 `default`→空闲、`serious`/`think`→思考、`happy`/`smile`→开心、
   `confused`/`puzzled`/`surprised`/`sad`→担心、`sleep`→睡觉、`walk`→走路（也认中文：默认 / 思考 / 开心 / 困惑 …）。
   没有对应的状态显示空闲图；认不出的图片忽略。
2. **全身图 + 表情子文件夹**：所选目录里有全身图（文件名含 `sprite` / `body` / `full` / `立绘`，或目录里只有一张图），
   并有 `expressions/`（或 `faces/`、`表情/`）子文件夹。全身图作为宠物本体，表情图按上面的规则对应状态，
   在思考 / 开心 / 担心 / 睡觉时于身旁弹出（`faces` 键）。

图片会**复制**到 `<home>/gui/qt/pets/<id>/`，`pet.json` 自动生成并带 `"imported": true`；再次导入会覆盖之前导入的包，
但不会覆盖你手写的宠物包。导入的图片只留在你的电脑上，不会进入仓库。

## pet.json

```json
{
  "format": 1,
  "name": "我的猫",
  "size": 96,
  "smooth": true,
  "facing": "right",
  "states": {
    "idle": "idle.png",
    "walk": {"sheet": "walk.png", "frames": 4, "fps": 8, "columns": 4},
    "sleep": "sleep.gif",
    "think": "think.png",
    "happy": "happy.png",
    "worried": "worried.png"
  }
}
```

| 键 | 说明 |
| --- | --- |
| `format` | 固定为 `1`（可省略） |
| `name` | 默认显示名（≤24 字）。在设置里改的名字优先，并保存在桌面设置里，不改动宠物包 |
| `size` | 显示尺寸，32–256 的整数，或 `[宽, 高]`；默认 96 |
| `smooth` | 缩放时是否平滑；像素画可设 `false` |
| `facing` | 图片朝向 `"left"` / `"right"`，往反方向走路时自动镜像 |
| `states` | 必须有 `idle`；其它状态缺省时显示 `idle` |

状态：

| 状态 | 何时出现 |
| --- | --- |
| `idle` | 空闲（偶尔用 `sleep` 帧眨眼） |
| `walk` | 空闲时偶尔在屏幕上走几步 |
| `sleep` | 3 分钟没有动静 |
| `think` | Agent 正在工作 |
| `happy` | 一轮任务顺利完成（约 4 秒） |
| `worried` | 出错（约 8 秒）或有待批准的操作 |

每个状态可以是：

- 图片文件名：`.png .jpg .jpeg .webp .bmp .svg`，或 `.gif`（按 GIF 自身帧率播放）；
- 精灵图对象：`{"sheet": "walk.png", "frames": 4, "fps": 8, "columns": 4}`。帧按从左到右、从上到下排列，
  每帧大小等于 `size`；`frames` 1–64，`fps` 1–30，`columns` 默认等于 `frames`（单行）。

限制：图片必须在宠物文件夹内，单个文件 ≤ 8 MB、边长 ≤ 4096 px，`pet.json` ≤ 64 KB。
校验失败时，设置里会显示原因。

可选的 `faces`：`{"think": "faces/serious.png", "happy": "faces/happy.png", ...}`，键为上面的状态。
处于该状态时在宠物身旁弹出这张脸（空闲和走路时不显示），适合「全身立绘 + 表情头像」这类素材。

## 聊两句（宠物聊天）

左键单击宠物（或右键菜单「聊两句」）会在它头顶展开一个小气泡：输入框 + 发送/停止。
聊天走与主窗口 Chat 模式同一条 `conversation.run` 管线，但**零工具、零审批**（轻量纯对话），
历史只保留最近 16 条且独立于主会话。人设与问候语来自当前宠物包的 `lines.json`（可选，
`persona` + 分时 `greetings` + `idle` 等台词池）；没有该文件的宠物用通用助手人设。
需要操作电脑的正经任务，溟月会提醒主人回主窗口处理。Esc 或再点一下宠物收起气泡；
展开时宠物不闲逛、不打盹，窗口位置记忆始终按收起态折算。

## 互动动作（interactions）

宠物包可选 `interactions`：`{动作: 图片}`，动作键固定为 `pat`（摸头）、`belly`（挠肚皮）、
`tail`（拍尾巴）、`feed`（投喂）、`pickup`（拎起）、`greet`（醒来打招呼）、`celebrate`（庆祝）。
提供后（参考 dsh-whale-musume 的分区摸头与 clawd-on-desk 的拎起反应）：

- **双击**宠物按落点分区触发 pat/belly/tail（上 45% 为头、下 25% 为尾、中间为肚皮）；
- **三连击**触发 celebrate；**滚轮**轻拨触发 pat；右键菜单「互动」可手动触发各动作；
- **拖拽拎起**期间显示 pickup 姿态，落地回弹；从睡眠**醒来**显示 greet；
- 动作姿态闪现约 1.5 秒后回到状态图；待机时每 5–8 分钟从 `lines.json` 的 idle 池冒一句闲聊气泡。

单击仍是开聊/打开主窗口（280ms 内计击数区分多击）。没有 interactions 的宠物包保持原有行为。

## 动效与性能

- 空闲时只有低频定时器（眨眼、闲逛、入睡），窗口隐藏时全部暂停。
- 开启「减少动态效果」（或 `REVON_REDUCE_MOTION=1`）时不走路、不播放帧动画、GIF 停在首帧，只按状态换图。
