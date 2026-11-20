"""Build Forge's Lucide icon masks; no SVG renderer needed at runtime.

Build only: pip install Pillow resvg-py==0.5.0
Run: python forge-gui/desktop/make_lucide_assets.py
Vendored SVGs and license: assets/ui/lucide (lucide-static 0.468.0).

Icons with no Lucide counterpart (Forge's own glyphs such as anvil / spark /
timeline) are rendered from ``ui_icons.ICONS`` geometry through the same
pipeline and stroke width, so every icon is an exact-colour antialiased mask.
"""
import base64
import io
import json
from pathlib import Path
import zlib

import sys

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
    'globe': 'globe', 'layers': 'layers',
}


def forge_svg(shapes):
    """ui_icons.ICONS shape list (24-unit grid) -> an SVG in Lucide's style."""
    def pts(values):
        return ' '.join(f'{x:g},{y:g}' for x, y in zip(values[::2], values[1::2]))
    parts = []
    for kind, c in shapes:
        if kind == 'line':
            parts.append(f'<polyline points="{pts(c)}"/>')
        elif kind in ('circle', 'dot'):
            cx, cy, rx, ry = (c[0] + c[2]) / 2, (c[1] + c[3]) / 2, abs(c[2] - c[0]) / 2, abs(c[3] - c[1]) / 2
            fill = ' fill="#ffffff"' if kind == 'dot' else ''
            parts.append(f'<ellipse cx="{cx:g}" cy="{cy:g}" rx="{rx:g}" ry="{ry:g}"{fill}/>')
        elif kind == 'rect':
            x1, y1, x2, y2 = c
            parts.append(f'<rect x="{x1:g}" y="{y1:g}" width="{x2 - x1:g}" height="{y2 - y1:g}" rx="2"/>')
        else:  # 'poly' outline / 'fill' solid, both closed
            fill = ' fill="#ffffff"' if kind == 'fill' else ''
            parts.append(f'<polygon points="{pts(c)}"{fill}/>')
    return ('<svg xmlns="http://www.w3.org/2000/svg" width="24" height="24" viewBox="0 0 24 24" '
            'fill="none" stroke="#ffffff" stroke-width="1.75" stroke-linecap="round" '
            'stroke-linejoin="round">' + ''.join(parts) + '</svg>')


def sources():
    for key, source in MAPPING.items():
        svg = (ROOT / 'lucide' / f'{source}.svg').read_text(encoding='utf-8')
        svg = svg.replace('stroke-width="2"', 'stroke-width="1.75"')
        yield key, svg.replace('currentColor', '#ffffff')
    sys.path.insert(0, str(ROOT.parents[1]))
    from ui_icons import ICONS
    for key in sorted(set(ICONS) - set(MAPPING)):
        yield key, forge_svg(ICONS[key])


def build():
    masks = {}
    for key, svg in sources():
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
               'stroke_width': 1.75, 'mapping': MAPPING,
               'forge_drawn': sorted(set(masks) - set(MAPPING)), 'masks': masks}
    (ROOT / 'lucide-masks.json').write_text(json.dumps(payload, separators=(',', ':')), encoding='utf-8')
    print(f'Built {len(masks)} icons at 57 sizes (8–64px).')


if __name__ == '__main__':
    build()
