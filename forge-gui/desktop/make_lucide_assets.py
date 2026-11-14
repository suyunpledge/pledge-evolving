"""Build Forge's Lucide icon masks; no SVG renderer needed at runtime.

Build only: pip install Pillow resvg-py==0.5.0
Run: python forge-gui/desktop/make_lucide_assets.py
Vendored SVGs and license: assets/ui/lucide (lucide-static 0.468.0).
"""
import base64
import io
import json
from pathlib import Path
import zlib

from PIL import Image
import resvg_py

ROOT = Path(__file__).resolve().parents[1] / 'assets' / 'ui'
MAPPING = {
    'chat': 'message-square', 'task': 'clipboard-check', 'agents': 'bot',
    'tools': 'sliders-horizontal', 'knowledge': 'book-open', 'evolution': 'dna',
    'files': 'folder', 'file': 'file-text', 'code': 'code-xml', 'config': 'settings-2',
    'search': 'search', 'plus': 'plus', 'minus': 'minus', 'close': 'x',
    'more': 'ellipsis', 'chevron_right': 'chevron-right', 'chevron_left': 'chevron-left',
    'chevron_down': 'chevron-down', 'chevron_up': 'chevron-up', 'hexagram': 'list-filter',
    'refresh': 'refresh-cw', 'external': 'external-link', 'sidebar': 'panel-left',
    'workspace': 'panels-top-left', 'diff': 'git-branch', 'send': 'arrow-up',
    'play': 'play', 'stop': 'square', 'check': 'check', 'check_circle': 'circle-check',
    'warning': 'triangle-alert', 'error': 'circle-x', 'info': 'info', 'copy': 'copy',
    'thumb_up': 'thumbs-up', 'thumb_down': 'thumbs-down', 'paperclip': 'paperclip',
    'key': 'key-round', 'model': 'box', 'terminal': 'square-terminal', 'preview': 'eye',
    'image': 'image', 'user': 'user-round', 'edit': 'square-pen', 'folder_open': 'folder-open',
    'save': 'save', 'flame': 'flame', 'clock': 'clock-3', 'connector': 'plug',
    'plugin': 'puzzle', 'context': 'layers', 'computer': 'monitor',
}


def build():
    masks = {}
    for key, source in MAPPING.items():
        svg = (ROOT / 'lucide' / f'{source}.svg').read_text(encoding='utf-8')
        svg = svg.replace('stroke-width="2"', 'stroke-width="1.75"')
        svg = svg.replace('currentColor', '#ffffff')
        # Store every integer size used by 100–200% native DPI layouts. These
        # are alpha masks, so callers get their exact semantic foreground.
        masks[key] = {}
        for size in range(8, 65):
            png = resvg_py.svg_to_bytes(svg_string=svg, width=size * 4, height=size * 4,
                                       skip_system_fonts=True)
            alpha = Image.open(io.BytesIO(png)).getchannel('A').resize(
                (size, size), Image.Resampling.LANCZOS)
            masks[key][str(size)] = base64.b64encode(zlib.compress(alpha.tobytes(), 9)).decode('ascii')
    payload = {'library': 'lucide-static', 'version': '0.468.0',
               'stroke_width': 1.75, 'mapping': MAPPING, 'masks': masks}
    (ROOT / 'lucide-masks.json').write_text(json.dumps(payload, separators=(',', ':')), encoding='utf-8')
    print(f'Built {len(masks)} icons at 57 sizes (8–64px).')


if __name__ == '__main__':
    build()
