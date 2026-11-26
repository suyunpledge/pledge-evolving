"""Entry point of the installed Windows build.

The application ships as plain source files under ``app/`` beside the
executables; the frozen binaries are only its Python runtime (with PySide6,
QtWebEngine and the other wheels baked in). Keeping the source on disk means
QML, Office pages, assets and child scripts are found by their real paths,
exactly as in a checkout.

``Revon.exe`` (windowed) starts the desktop. ``revon-python.exe`` (console,
used for child processes) understands the subset of ``python`` arguments the
app uses: ``<script.py> args``, ``-m module args``, ``-c code`` and the
interpreter flags it passes (``-B``, ``-u`` ...).
"""
import os
from pathlib import Path
import runpy
import sys

ROOT = Path(sys.executable).resolve().parent
APP = ROOT / 'app'
GUI = APP / 'revon-gui'
FLAGS = {'-B', '-u', '-E', '-s', '-S', '-I', '-O', '-OO', '-q'}


def _prepare():
    # Same order revon_qt.py uses in a checkout: GUI modules first, then the kernel.
    for path in (str(APP), str(GUI)):
        if path not in sys.path:
            sys.path.insert(0, path)
    os.environ.setdefault('REVON_INSTALLED', '1')
    sys.dont_write_bytecode = True


def _run_script(path, args):
    script = Path(path).resolve()
    sys.argv = [str(script), *args]
    sys.path.insert(0, str(script.parent))
    runpy.run_path(str(script), run_name='__main__')


def main():
    _prepare()
    args = sys.argv[1:]
    while args and (args[0] in FLAGS or args[0] == '-X'):
        args = args[2:] if args[0] == '-X' else args[1:]
    if args[:1] == ['-m'] and len(args) > 1:
        sys.argv = [args[1], *args[2:]]
        runpy.run_module(args[1], run_name='__main__', alter_sys=True)
        return 0
    if args[:1] == ['-c'] and len(args) > 1:
        sys.argv = ['-c', *args[2:]]
        exec(compile(args[1], '<string>', 'exec'), {'__name__': '__main__'})
        return 0
    if args[:1] == ['--revon-plugin-worker']:
        _run_script(GUI / 'plugin_worker.py', args[1:])
        return 0
    if args and args[0].lower().endswith('.py') and Path(args[0]).is_file():
        _run_script(args[0], args[1:])
        return 0
    # The desktop itself; remaining arguments are the app's own (paths to open, --page ...).
    sys.argv = [sys.argv[0], *args]
    from qt_frontend.app import main as desktop
    return desktop()


if __name__ == '__main__':
    raise SystemExit(main())
