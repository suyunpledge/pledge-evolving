"""revon's Qt entry point. Runtime dependencies stay separate from legacy Tk."""
from pathlib import Path
import subprocess
import sys

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(1, str(HERE.parent))

from desktop.runtime_paths import install_hint, qt_python


def main():
    try:
        import PySide6
    except ImportError:
        # The dedicated installer places Qt here; no global Python packages change.
        executable = qt_python(HERE.parent, windowed=Path(sys.executable).stem.lower() == "pythonw")
        # Two POSIX virtual environments may link to the very same Python binary.
        if executable.is_file() and Path(sys.prefix).resolve() != executable.parent.parent.resolve():
            return subprocess.call([str(executable), str(Path(__file__).resolve()), *sys.argv[1:]],
                                   creationflags=0x08000000 if sys.platform == "win32" else 0)
        raise SystemExit(f"Install the revon Qt runtime with {install_hint(HERE.parent)}")
    from qt_frontend.app import main as qt_main
    return qt_main()


if __name__ == "__main__":
    raise SystemExit(main())
