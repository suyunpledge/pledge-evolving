"""Bake portable Qt shader packages (SPIR-V, HLSL, GLSL and Metal)."""
from pathlib import Path
import subprocess
import sys


def build():
    import PySide6
    package = Path(PySide6.__file__).parent
    candidates = [package / "qsb.exe", package / "qsb", package / "Qt" / "libexec" / "qsb"]
    qsb = next((p for p in candidates if p.is_file()), None)
    if qsb is None:
        raise RuntimeError("Qt Shader Tools qsb was not found; install requirements-qt.txt")
    folder = Path(__file__).parent / "shaders"
    for source in sorted(folder.glob("*.frag")):
        subprocess.run([str(qsb), "--qt6", "-o", str(source.with_suffix(".frag.qsb")), str(source)],
                       check=True, creationflags=0x08000000 if sys.platform == "win32" else 0)


if __name__ == "__main__":
    build()
