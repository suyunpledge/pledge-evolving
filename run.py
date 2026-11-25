"""Bootstrap entry point.

The AutoClaw embedded Python uses a ``._pth`` layout, which disables both the
"current directory on sys.path" behaviour and ``PYTHONPATH``. A plain
``python -m revon.cli`` therefore fails with ``No module named 'revon'`` even
from inside this directory. This file inserts the package root explicitly so
the framework is runnable anywhere:

    python run.py selftest
    python run.py doctor
    python run.py run "总结一下 README"
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from revon.cli import main  # noqa: E402

if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
