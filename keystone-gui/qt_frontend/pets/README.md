# 桌面宠物包 / Desktop pet packs

Keystone 自带三只原创占位宠物：`crab`（像素小蟹）、`robot`（小机器人）、`whale`（圆滚滚鲸鱼）。
形象都是为本项目绘制的 SVG，可随时换成你自己的图片或精灵图。

## 放在哪里

| 位置 | 说明 |
| --- | --- |
| `qt_frontend/pets/<id>/` | 内置宠物，随程序发布 |
| `<home>/gui/qt/pets/<id>/` | 你的宠物包；`<home>` 为 `keystone.compat.default_home()`（已有 `~/.forge` 时沿用它） |

- `<id>` 即文件夹名，只能用小写字母、数字、`-`、`_`（最长 32）。
- 与内置宠物同名（如 `<home>/gui/qt/pets/crab/`）即**替换**该宠物的形象；包无效时自动退回内置形象。
- 其它名字是新宠物，出现在「桌面宠物」设置里。设置里的「打开宠物包文件夹」会建好并打开这个目录，改完点「重新载入」。

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

## 动效与性能

- 空闲时只有低频定时器（眨眼、闲逛、入睡），窗口隐藏时全部暂停。
- 开启「减少动态效果」（或 `KEYSTONE_REDUCE_MOTION=1`）时不走路、不播放帧动画、GIF 停在首帧，只按状态换图。
