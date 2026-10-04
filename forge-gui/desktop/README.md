# forge-gui 桌面应用打包

把 `forge-gui` 从「一个 Python 文件」打成可双击运行的 Windows 桌面应用（单文件 exe，无控制台窗口）。

## 快速构建

```powershell
# 在 forge-gui/ 目录下
powershell -NoProfile -ExecutionPolicy Bypass -File desktop\build-desktop.ps1
```

产物：`desktop/dist/forge-desktop.exe`（约 11.8 MB，内置 Python 运行时 + tkinter + 品牌资源）。

## 依赖（仅构建期）

| 组件 | 用途 |
|---|---|
| Python 3.10+ | 构建环境（**必须带 tkinter**，即官方安装版，不是内嵌版） |
| PyInstaller ≥ 6 | 打包 |
| Pillow | 生成图标与界面标志（`make_icon.py`） |

运行期不需要 Python 外的任何依赖——exe 自带解释器与 tkinter。

## 文件说明

| 文件 | 作用 |
|---|---|
| `forge-logo.png` | **品牌原图**（正方形、黑底）。换标志改这张，然后重跑 `make_icon.py` |
| `make_icon.py` | 从原图生成 `forge.ico`（多尺寸应用图标）+ `../assets/forge-logo-*.png`（界面用，黑底已抠透明） |
| `forge-desktop.spec` | PyInstaller 配方：单文件、无控制台、内嵌 `assets/*.png`、排除用不到的重型包 |
| `build-desktop.ps1` | 一键构建（探测构建 Python → 生成图标/资源 → 打包 → SHA256 → 冒烟启动） |
| `forge.ico` | 已生成的应用图标 |

## 换标志怎么做

1. 用新图替换 `desktop/forge-logo.png`（正方形最好，黑底或透明底都行；脚本会按内容自动裁正方形）
2. 跑 `python desktop/make_icon.py`（若换了文件名，把路径作为参数传入）
3. 重新打包：`powershell -File desktop\build-desktop.ps1 -SkipIcon`

脚本会一并更新三处品牌位置：
- **应用图标**：`forge.ico`（黑底圆角磁贴 + 居中标志，7 档尺寸 16→256）
- **界面顶栏标志**：`assets/forge-logo-{52,78,104}.png`（黑底抠透明，按 DPI 选最接近的尺寸）
- **对话里的 Forge 头像**：同一张界面资源（由主程序注入 `chat_widgets.set_brand_avatar`）

原图缺失时自动回退到程序化绘制的 Indigo「F」图标，构建不会失败。

## gateway 的启动与自愈（打开即用，无需手动点）

跟 AutoClaw 一致：**窗口一打开，gateway 自动启动**，不需要用户决定要不要开。

| 行为 | 说明 |
|---|---|
| 打开即自启 | 窗口建好后约 0.4s 自动拉起（等 UI 就绪，不抢启动时间） |
| 崩溃自动拉起 | gateway 意外退出 → 自动重启，退避 2/4/8/16/30 秒；60 秒内最多 5 次，超限暂停并提示（防重启风暴） |
| 手动停不复活 | 右键菜单「停止 gateway」后不再自动拉起，直到再次点「启动」 |
| 关窗即清理 | 关闭窗口会连带结束 gateway **及其子进程**（`taskkill /T /F`），不留占端口的僵尸 |
| 僵尸自清 | 若端口被上次遗留的本程序 gateway 占着，启动前会识别并清掉（只认命令行含 `run.py` + `gateway` 的进程，不误杀别的服务） |

按钮语义：主按钮 = **确保在运行**（在线时显示「⟳ 重启」，离线时「▶ 启动」）；
停止放在**右键菜单**里。状态灯另有三态：「启动中」「在线 (端口)」「重启中(Ns)」。

不想自动启动时可以设环境变量：`FORGE_NO_AUTOSTART=1`（测试与特殊场景用）。

> 需要机器上装有 Python 3.10+：gateway 是 `python run.py gateway` 子进程，
> 必须靠真实解释器跑（打包的 exe 只负责 GUI）。

## 打包后与「Python 运行」的差异（已在代码里处理）

1. **找 Forge 仓库**：exe 旁边没有 `run.py`，探测顺序为 `FORGE_REPO` 环境变量 →
   exe 同目录记住的路径（`forge-desktop.json`）→ exe 同级与上层 → 用户目录下的
   `pledge-evolving` / `forge` → `.openclaw/tmp` 副本。在 GUI 里点「选择 Forge 目录」
   后会把选择**写进 `forge-desktop.json` 记住**，下次免选。

2. **找解释器跑 `run.py`**：打包后 `sys.executable` 是 GUI 自己（直接拿去跑
   `run.py gateway` 会又弹一个 GUI）。所以用 `_python_exe()`：`FORGE_PYTHON` 环境变量 →
   用户安装目录的 Python3*（`%LOCALAPPDATA%\Programs\Python`、`C:\Python3*`）→ PATH。
   **桌面版需要机器上装有 Python**（forge 是 Python 框架，跑 `run.py` 必须有解释器）。

3. **资源定位**：界面标志在 `_MEIPASS/assets`（打包态）或 `forge-gui/assets`（源码态），
   由 `_asset_path()` / `load_brand_logo()` 统一处理。运行期只用 `tk.PhotoImage`，
   **不依赖 Pillow**（尺寸靠预生成的整数倍 + subsample/zoom）。

4. **配置落点**：`forge-desktop.json` 优先写在 exe 同目录；目录不可写时退到 `~/.forge/`。

## 自检

```powershell
# 环境与资源诊断（打包态最容易漏的就是这里）
forge-desktop.exe --diagnose        # 生成 exe 旁的 forge-diagnose.json
python forge-gui\forge_gui_v2.py --diagnose   # 源码态同样可用
```

诊断输出含 `python_exe` / `run_py` / `brand_logo` / `brand_logo_ok` / `brand_logo_px`，
可用来判断「解释器是否找对、仓库是否找到、品牌资源是否打进去了」。

GUI 回归测试（临时用户目录，不读取真实密钥、不请求模型）：

```powershell
python run_offline_checks.py
python run_offline_checks.py test_ui_ergonomics
```

## 2026-10-02 图标与表情修正

- 保留 Forge 主图标；导航、文件树、工具状态及操作按钮使用统一的 45 种矢量图标。
- Windows Tk 无法稳定绘制彩色 emoji，因此消息中 1382 个受支持的单字符表情使用预生成的彩色 PNG；复制仍还原原始 Unicode 文本，代码和链接保持原文。
- 未收录的组合表情（例如部分肤色、旗帜和 ZWJ 序列）保留原始文本，不替换为不同的表情；这些序列的显示仍取决于系统字体。
- `make_ui_assets.py` 在构建时使用 Pillow 和系统 emoji 字体生成资源，运行时仅依赖 Tk。构建脚本自动执行该步骤，spec 同时打包图集和索引。
- 验证命令：`python -m unittest test_ui_icons test_chat_widgets -v`。

## GPU 监测闪窗修正

资源监测器会定期运行 `nvidia-smi`。Windows 下只设置 `capture_output=True`
仍会创建控制台，因此该调用显式使用 `CREATE_NO_WINDOW`，保留 GPU 监测和两秒超时。
`python -m unittest test_sysmon -v` 验证首次与重复探测、失败后停止重试，
并在 Windows 上启动真实控制台程序确认 `GetConsoleWindow()` 返回零。

## 已知取舍

- **单文件模式**首次启动有约 1–3 秒解包时间（exe 内部的运行时解到临时目录）；
  想更快可改 spec 为 one-dir。
- 未做安装包（Inno Setup / MSI）与代码签名；内部分发够用，外发需自行签名避免 SmartScreen 提示。
- 3D 细节丰富的标志在 16px 下会糊成一团橙色——这是位图缩放的固有代价；
  若要求小尺寸清晰，需要另做一版简化图形。
