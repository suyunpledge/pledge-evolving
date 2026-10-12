"""Export a reviewable layout specimen from the actual shared palette/icons."""
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'forge-gui'))
import gui_theme as theme
from ui_icons import ICONS
from visual_system.contracts import DENSITIES, WorkspaceKind


def svg(name):
    parts = []
    for kind, values in ICONS[name]:
        if kind == 'line':
            points = ' '.join(f'{values[i]},{values[i+1]}' for i in range(0, len(values), 2))
            parts.append(f'<polyline points="{points}"/>')
        elif kind in {'circle', 'dot'}:
            x, y, right, bottom = values
            parts.append(f'<ellipse cx="{(x+right)/2}" cy="{(y+bottom)/2}" rx="{(right-x)/2}" ry="{(bottom-y)/2}"' +
                         (' fill="currentColor" stroke="none"' if kind == 'dot' else '') + '/>')
        elif kind == 'rect':
            x, y, right, bottom = values
            parts.append(f'<rect x="{x}" y="{y}" width="{right-x}" height="{bottom-y}"/>')
        else:
            points = ' '.join(f'{values[i]},{values[i+1]}' for i in range(0, len(values), 2))
            parts.append(f'<polygon points="{points}"/>')
    return '<svg viewBox="0 0 24 24" aria-hidden="true">' + ''.join(parts) + '</svg>'


def main():
    value = (ROOT / 'reports/forge-visual-system-2.template.html').read_text(encoding='utf-8')
    tokens = {key.replace('_', '-'): color for key, color in theme.C.items()}
    tokens.update({'r-sm': f'{theme.R_SM}px', 'r-panel': f'{theme.R_PANEL}px',
                   'content-width': f'{theme.CONTENT_WIDTH}px',
                   'work-gutter': f'{DENSITIES[WorkspaceKind.WORK].gutter}px',
                   'coding-gutter': f'{DENSITIES[WorkspaceKind.CODING].gutter}px',
                   'coding-row': f'{DENSITIES[WorkspaceKind.CODING].row_height}px'})
    for name, size in [('ui', theme.FONT_UI[1]), ('small', theme.FONT_SMALL[1]),
                       ('caption', theme.FONT_CAPTION[1]), ('title', theme.FONT_TITLE[1]),
                       ('code', theme.FONT_MONO_SM[1])]:
        tokens['font-' + name] = f'{size * 96 / 72:.3f}px'
    value = value.replace('%%TOKENS%%', ';'.join(f'--{key}:{data}' for key, data in tokens.items()))
    for name in ('chat', 'code', 'task', 'agents', 'tools', 'files', 'search', 'diff', 'play', 'file', 'warning'):
        value = value.replace('%%ICON_' + name.upper() + '%%', svg(name))
    if '%%' in value:
        raise ValueError('Unresolved preview token')
    target = ROOT / 'reports/forge-visual-system-2-preview.html'
    target.write_text(value, encoding='utf-8')
    print(target)


if __name__ == '__main__':
    main()
