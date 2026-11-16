# Source launcher deliberately contains no frozen UI or Qt dependency.
from pathlib import Path
ROOT = Path(SPECPATH)
a = Analysis([str(ROOT / 'source_launcher.py')], pathex=[str(ROOT)], binaries=[], datas=[],
             hiddenimports=[], hookspath=[], hooksconfig={}, runtime_hooks=[],
             excludes=['tkinter', 'PySide6', 'PyQt6', 'PIL', 'numpy', 'pytest'], noarchive=False)
pyz = PYZ(a.pure)
exe = EXE(pyz, a.scripts, a.binaries, a.datas, [], name='forge-desktop', debug=False,
          bootloader_ignore_signals=False, strip=False, upx=False, console=False,
          icon=str(ROOT / 'forge.ico'))
