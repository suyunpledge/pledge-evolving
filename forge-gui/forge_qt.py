"""Forge's Qt entry point. Runtime dependencies stay separate from legacy Tk."""
from pathlib import Path
import os
import subprocess
import sys

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(1, str(HERE.parent))


def main():
    try:
        import PySide6
    except ImportError:
        # The dedicated installer places Qt here; no global Python packages change.
        runtime = HERE.parent / ".venv" / "qt" / "Scripts"
        executable = runtime / ("pythonw.exe" if Path(sys.executable).stem.lower() == "pythonw" else "python.exe")
        if executable.is_file() and executable.resolve() != Path(sys.executable).resolve():
            return subprocess.call([str(executable), str(Path(__file__).resolve()), *sys.argv[1:]],
                                   creationflags=0x08000000 if sys.platform == "win32" else 0)
        raise SystemExit("Install Qt with desktop/install-qt.ps1, or pip install -r requirements-qt.txt")
    from qt_frontend.app import main as qt_main
    return qt_main()


if __name__ == "__main__":
    raise SystemExit(main())
