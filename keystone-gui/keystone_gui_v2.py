"""Launch revon from the former desktop entry point."""
from pathlib import Path
import sys


def main():
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "revon-gui"))
    from revon_gui_v2 import main as launch
    return launch()


if __name__ == "__main__":
    raise SystemExit(main())
