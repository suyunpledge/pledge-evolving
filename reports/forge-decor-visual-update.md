# Forge hero 静态装饰更新

## 改动

仅修改生产文件 `forge-gui/decor.py`。保留 `hero_banner(parent, width, height, *, bg, accent, accent2, seed)` 调用兼容，新增可选 `spark="#F5BC74"`。新增可复用 `forge_motif(...)`：以 indigo 棱面核心、独立暖色火种、断续轨道和 Agent 分叉节点替代 orb/星座语汇；原 Forge logo 不变。整幅点阵也由两条克制横轨取代。所有绘制为同步 Canvas items，无动画或 `after` 任务。

## Canvas item 数与尺寸

- `forge_motif`: 固定 21 项。
- `hero_banner`: 固定 25 项（21 项 motif + 2 条横轨 + 2 条端部切线），不随画布尺寸增长。
- 绘图尺度按 `min(height * 0.68, width * 0.27)` 推导，主图居中；横轨位置已根据真实 Tk 边界检查上移。
- Tk Canvas 坐标跟随 Tk 屏幕单位/DPI 映射；装饰层不再重复乘系统 DPI。相对坐标和线宽随尺寸缩放，线宽下限为 1 unit。

## 验证

新增 `forge-gui/test_brand_decor.py`，单个测试方法、单个 Tk root（单 Python/Tk 进程）覆盖 480×112、640×220、96×40；逐项检查 bbox 均落在 Canvas 内、item 数为 25、请求尺寸不变，且 `after info` 为空。使用 Python 3.12 执行通过。首次真实边界检查发现端部横轨略出底边，已上移并重跑通过。`git diff --check` 通过。
