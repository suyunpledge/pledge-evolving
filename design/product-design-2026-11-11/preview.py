"""Isolated native preview: no gateway, credentials, or real desktop preferences."""
import sys
import argparse
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / 'forge-gui'))
from test_layout_dpi import isolated_app

parser = argparse.ArgumentParser()
parser.add_argument('--compact', action='store_true')
parser.add_argument('--workspace', action='store_true')
parser.add_argument('--title', default='Forge · Product Design Preview')
options = parser.parse_args()
size = (960, 700) if options.compact else (1440, 900)
with isolated_app(1, size, repo=REPO) as (root, app, errors):
    root.title(options.title + (' · Compact' if options.compact else ''))
    root.attributes('-alpha', 1)
    app.model_var.set('mimo')
    app._show_chat_start()
    if options.workspace:
        app._open_workspace('file_tree')
    root.protocol('WM_DELETE_WINDOW', root.quit)
    root.mainloop()
    if errors:
        raise RuntimeError(errors)
