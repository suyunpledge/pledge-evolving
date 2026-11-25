"""Interpreter selection shared by the desktop launcher and its service process."""
from pathlib import Path
import shlex
import sys


def qt_python(root, *, windowed=False, platform=None):
    platform = sys.platform if platform is None else platform
    environments = Path(root) / '.venv'
    if platform == 'win32':
        return environments / 'qt' / 'Scripts' / ('pythonw.exe' if windowed else 'python.exe')
    return environments / f'qt-{platform}' / 'bin' / 'python'


def service_python(executable, *, platform=None):
    platform = sys.platform if platform is None else platform
    path = Path(executable)
    if platform == 'win32' and path.name.lower() == 'pythonw.exe':
        return path.with_name('python.exe')
    # On POSIX this can be a venv symlink. Preserve its path and site-packages.
    return path


def install_hint(root):
    if sys.platform == 'win32':
        return str(Path(root) / 'keystone-gui' / 'desktop' / 'install-qt.ps1')
    return shlex.join(['bash', str(Path(root) / 'setup.sh'), 'qt'])
