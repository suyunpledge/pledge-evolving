"""Small frozen launcher: load the registered working tree on every launch."""
import ctypes
import json
import os
from pathlib import Path
import subprocess
import sys


def resolve_launch(executable, arguments, environment=None):
    env = os.environ if environment is None else environment
    args = list(arguments)
    candidates = []
    if '--source-root' in args:
        index = args.index('--source-root')
        if index + 1 >= len(args):
            raise ValueError('--source-root requires a directory')
        candidates.append(Path(args[index + 1]))
        del args[index:index + 2]
    if env.get('FORGE_SOURCE_ROOT'):
        candidates.append(Path(env['FORGE_SOURCE_ROOT']))
    launch_dir = Path(executable).resolve().parent
    config = launch_dir / 'forge-source.json'
    if config.is_file():
        value = json.loads(config.read_text(encoding='utf-8-sig')).get('sourceRoot')
        if value:
            path = Path(value)
            candidates.append(path if path.is_absolute() else launch_dir / path)
    candidates.extend(list(launch_dir.parents)[:5])
    for candidate in candidates:
        root = candidate.resolve()
        gui = root / 'forge-gui'
        if not (gui / 'forge_qt.py').is_file():
            continue
        runtime = root / '.venv' / 'qt' / 'Scripts' / 'pythonw.exe'
        if not runtime.is_file():
            raise FileNotFoundError(f'Qt runtime is missing. Run {gui / "desktop" / "install-qt.ps1"}')
        legacy = '--legacy' in args
        if legacy:
            args.remove('--legacy')
        return [str(runtime), str(gui / ('forge_gui_v2.py' if legacy else 'forge_qt.py')), *args], gui
    raise FileNotFoundError('Forge source tree was not found. Set sourceRoot in forge-source.json beside this EXE.')


def main():
    try:
        executable = sys.executable if getattr(sys, 'frozen', False) else __file__
        command, cwd = resolve_launch(executable, sys.argv[1:])
        return subprocess.call(command, cwd=cwd, creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
    except Exception as exc:
        message = str(exc)
        if sys.platform == 'win32':
            ctypes.windll.user32.MessageBoxW(None, message, 'Forge 启动失败', 0x10)
        elif sys.stderr:
            print(message, file=sys.stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
