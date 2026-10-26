# forge 桌面版（电脑）

> 这是桌面版的主文档。手机版（Android）见 [`../forge-android/`](../forge-android/)。

桌面版 = Work（对话）+ Coding Mode（三栏 IDE），跑在 Windows 上。

## 快速开始

```bash
python run.py selftest     # 574/574 离线自检
python forge-gui/forge_gui_v2.py   # 桌面 GUI
```

- 安装/使用：见[仓库根 README](../../README.zh.md)
- Coding Mode 结构：根 README「Coding Mode」一节
- computer use（屏幕观察/操作/坐标点击）：`forge/computer_*.py`
- 桌面版没有连接器市场与手机工具——那是手机版的形态

## 与手机版的关系

同一个 forge 内核（`forge/`，纯标准库，零改动），两个入口：

- 桌面：Tk GUI，全功能 IDE 形态，computer use 桌面控制
- 手机：Kotlin Compose 壳 + PRoot Ubuntu + FastAPI 桥；个人助理 +
  MCP 连接器市场 + 设备工具（短信/电话）

数据不互通（各自独立 `FORGE_HOME`）；远程控制通道（手机 ↔ 桌面 forge）
协议已预留（`forge-android/bridge/PROTOCOL.md` §6），v0.2+ 接通。
