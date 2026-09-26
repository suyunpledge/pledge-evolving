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
    "secret_store",
    "interaction_model",
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
    pathex=[str(GUI_DIR)],
    binaries=[],
    datas=[],
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
