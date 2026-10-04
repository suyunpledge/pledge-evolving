# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller spec：把 forge-gui 打成单文件桌面应用（无控制台窗口）。

构建：
    pyinstaller --clean --noconfirm desktop/forge-desktop.spec
或直接跑 desktop/build-desktop.ps1（会先生成图标再打包）。
"""
from pathlib import Path

GUI_DIR = Path(SPECPATH).resolve().parent   # desktop/ -> forge-gui/
APP_NAME = "forge-desktop"
ICON = GUI_DIR / "desktop" / "forge.ico"

# 同目录的兄弟模块：显式声明，避免静态分析漏掉
HIDDEN = [
    "gui_theme",
    "chat_widgets",
    "workspace",
    "sysmon",
    "config_model",
    "forge_client",
    "http_transport",
    "sub_agent",
    "secret_store",
    "interaction_model",
    "brand_marks",
    "model_picker",
    "ui_icons",
    "plugin_market",
    "plugin_runtime",
    "plugin_capabilities",
    "compat_sources",
    # 宿主 Forge 包：plugin_capabilities 运行期 import forge.tools / forge.policy
    # （能力代理的本地别名注册表 + Policy 类型）。不加则冻结 exe 里插件全灭。
    "forge",
    "forge.config",
    "forge.guard",
    "forge.policy",
    "forge.tools",
]

# 运行期用不到的重量级包，排掉可显著减小体积
EXCLUDES = [
    "numpy", "pandas", "scipy", "matplotlib", "PIL", "cv2",
    "PyQt5", "PyQt6", "PySide2", "PySide6", "wx",
    "IPython", "jupyter", "notebook",
    "pytest", "_pytest", "unittest", "doctest",
    "setuptools", "pip", "wheel", "pkg_resources",
    "tkinter.test", "test", "distutils",
    "sqlite3", "curses", "multiprocessing",
]

a = Analysis(
    [str(GUI_DIR / "forge_gui_v2.py")],
    pathex=[str(GUI_DIR), str(GUI_DIR.parent)],  # parent = repo root (forge package)
    binaries=[],
    # 界面资源（品牌标志 + 模型厂商标识）。打包后由 _MEIPASS/assets 提供。
    # 注意：brands/ 是子目录，*.png 通配不会递归，必须单独列一条。
    datas=[
        (str(GUI_DIR / "assets" / "*.png"), "assets"),
        (str(GUI_DIR / "assets" / "brands" / "*.png"), "assets/brands"),
        (str(GUI_DIR / "assets" / "ui" / "*.png"), "assets/ui"),
        (str(GUI_DIR / "assets" / "ui" / "manifest.json"), "assets/ui"),
        # 插件市场的内置离线目录（打包后 plugin_market 从 _MEIPASS 读）
        (str(GUI_DIR / "catalog.builtin.json"), "."),
    ],
    hiddenimports=HIDDEN,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=EXCLUDES,
    noarchive=False,
    optimize=0,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name=APP_NAME,
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=False,          # GUI 应用：不弹控制台
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=str(ICON) if ICON.is_file() else None,
)
