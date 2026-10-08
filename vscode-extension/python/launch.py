"""Trusted extension launcher; Python -I keeps workspace imports off sys.path."""
import argparse
from pathlib import Path
import runpy
import sys

parser = argparse.ArgumentParser()
parser.add_argument('--engine-root', default='')
options = parser.parse_args()
if options.engine_root:
    root = Path(options.engine_root).expanduser().resolve()
    if not (root / 'forge' / 'vscode_bridge.py').is_file():
        sys.stderr.write('Forge source directory does not contain forge/vscode_bridge.py\n')
        raise SystemExit(2)
    sys.path.insert(0, str(root))
try:
    runpy.run_module('forge.vscode_bridge', run_name='__main__')
except ModuleNotFoundError:
    sys.stderr.write('Install the current Forge package or configure forge.enginePath.\n')
    raise SystemExit(2) from None
