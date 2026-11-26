# PyInstaller spec for the installed Windows build. Run via build-installer.ps1.
#
# Two executables share one runtime folder:
#   Revon.exe         windowed desktop app
#   revon-python.exe  console runtime for the app's child processes
# The app's own modules are deliberately NOT frozen: build-installer.ps1 copies
# them as source into Revon/app. They are listed as hidden imports only so their
# third-party and standard-library dependencies get collected, then dropped
# from the archive below.
from pathlib import Path

from PyInstaller.utils.hooks import collect_data_files, collect_submodules

HERE = Path(SPECPATH)
GUI = HERE.parents[1]
ROOT = GUI.parent
ICON = GUI / 'desktop' / 'revon.ico'
SKIP_DIRS = {'__pycache__', 'tests', 'build', 'dist', 'build-source', 'node_modules', 'installer'}


def own_modules():
    names = set()
    for base, prefix in ((ROOT / 'revon', 'revon'), (GUI, '')):
        for path in base.rglob('*.py'):
            rel = path.relative_to(base)
            if any(part in SKIP_DIRS or part.startswith('.') for part in rel.parts):
                continue
            if path.name.startswith(('test_', '_test')) or path.name == 'conftest.py':
                continue
            parts = list(rel.with_suffix('').parts)
            if parts[-1] == '__init__':
                parts = parts[:-1]
            if prefix:
                parts = [prefix, *parts]
            if parts:
                names.add('.'.join(parts))
    return sorted(names)


OWN = own_modules()
OWN_TOP = {name.split('.')[0] for name in OWN}

datas = []
for package in ('docx', 'pptx', 'openpyxl', 'matplotlib', 'rapidocr_onnxruntime', 'pyte'):
    try:
        datas += collect_data_files(package)
    except Exception:
        pass

hidden = OWN + ['unittest', 'runpy', 'sqlite3', 'xml.etree.ElementTree', 'email', 'http.server', 'wsgiref']
for package in ('comtypes', 'winpty'):
    try:
        hidden += collect_submodules(package)
    except Exception:
        pass

a = Analysis(
    [str(HERE / 'frozen_main.py')],
    pathex=[str(GUI), str(ROOT)],
    datas=datas,
    hiddenimports=hidden,
    excludes=['pytest', 'IPython', 'jupyter', 'PyInstaller'],
    noarchive=False,
)
# The app's own modules run from the copied source tree, never from the archive.
a.pure = [entry for entry in a.pure if entry[0].split('.')[0] not in OWN_TOP]
pyz = PYZ(a.pure)

desktop = EXE(pyz, a.scripts, [], exclude_binaries=True, name='Revon', console=False,
              icon=str(ICON), upx=False)
runtime = EXE(pyz, a.scripts, [], exclude_binaries=True, name='revon-python', console=True,
              icon=str(ICON), upx=False)
COLLECT(desktop, runtime, a.binaries, a.datas, name='Revon', upx=False)
