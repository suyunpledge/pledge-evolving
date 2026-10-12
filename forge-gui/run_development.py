"""Independent launcher; never reads/writes the General GUI's runtime home."""
import argparse
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from development_mode.app import launch


def main():
    parser = argparse.ArgumentParser(description='Forge independent Development Mode')
    parser.add_argument('--home', type=Path, default=ROOT.parent / ('.forge-development-' + ROOT.name))
    parser.add_argument('--project', type=Path)
    args = parser.parse_args()
    launch(args.home, args.project)


if __name__ == '__main__':
    main()
