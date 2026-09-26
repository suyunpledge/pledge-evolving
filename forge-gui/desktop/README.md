# forge-gui 桌面应用打包

把 `forge-gui` 从「一个 Python 文件」打成可双击运行的 Windows 桌面应用（单文件 exe，无控制台窗口）。

## 快速构建

```powershell
# 在 forge-gui/ 目录下
powershell -NoProfile -ExecutionPolicy Bypass -File desktop\build-desktop.ps1
```

产物：`desktop/dist/forge-desktop.exe`（约 12 MB，内置 Python 运行时 + tkinter）。

## 依赖（仅构建期）

| 组件 | 用途 |
|---|---|
| Python 3.10+ | 构建环境（**必须带 tkinter**，即官方安装版，不是内嵌版） |
| PyInstaller ≥ 6 | 打包 |
| Pillow | 生成图标（`make_icon.py`） |

运行期不需要 Python、不需要第三方库——exe 自带解释器。

## 文件说明

| 文件 | 作用 |
|---|---|
| `make_icon.py` | 生成多尺寸 `forge.ico`（Indigo 底 + 白色 F，与应用内品牌一致） |
| `forge-desktop.spec` | PyInstaller 配方：单文件、无控制台、排除用不到的重型包 |
| `build-desktop.ps1` | 一键构建（生成图标 → 打包 → 校验） |
| `forge.ico` | 已生成的图标（改品牌时重跑 `make_icon.py`） |

## 打包后与「Python 运行」的差异（已在代码里处理）

桌面版多了一层运行环境差异，`forge_gui_v2.py` 里已做兼容：

1. **找 Forge 仓库**：exe 旁边没有 `run.py`，所以探测顺序改为
   `FORGE_REPO` 环境变量 → exe 同目录记住的路径（`forge-desktop.json`）→
   exe 同级与上层 → 用户目录下的 `pledge-evolving` / `forge` → `.openclaw/tmp` 副本。
   在 GUI 里点「选择 Forge 目录」后会把选择**写进 `forge-desktop.json` 记住**，下次免选。

2. **找解释器跑 `run.py`**：打包后 `sys.executable` 是 GUI 自己（直接拿去跑
   `run.py gateway` 会又弹一个 GUI）。所以改为 `_python_exe()`：
   `FORGE_PYTHON` 环境变量 → 用户安装目录的 Python3*（`%LOCALAPPDATA%\Programs\Python`、
   `C:\Python3*`）→ PATH 上的 `python`。**桌面版需要机器上装有 Python**（forge 是 Python 框架，
   跑 `run.py` 必须有解释器）；没装时启动 gateway / 跑任务会失败并在状态栏提示。

3. **配置落点**：`forge-desktop.json` 优先写在 exe 同目录；目录不可写时退到 `~/.forge/`。

## 自检

```powershell
# 语法与探测逻辑（不打包也能验）
python -c "import sys; sys.argv=['x']; sys.path.insert(0,'.'); import forge_gui_v2 as g; print(g._find_run_py(), g._python_exe())"

# 跑 GUI 单元与回归测试（需桌面）
python -m unittest test_gui_review -v
python -m unittest test_interactions -v
```

## 已知取舍

- **单文件模式**首次启动有约 1–3 秒解包时间（exe 内部的 Python 运行时解到临时目录）；
  想更快可以改 spec 为 one-dir（`EXE` 不接收 `a.binaries/a.datas`，改用 `COLLECT`）。
- 未做安装包（Inno Setup / MSI）与代码签名；内部分发够用，外发需自行签名避免 SmartScreen 提示。
- 图标为程序化生成，若要换设计稿直接替换 `forge.ico` 后重新打包。
