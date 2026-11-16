"""Bounded search and command previews with exact original output retention."""
import json
import re


FIELDS = ('summary', 'preview', 'notes')
HELP = ' Large replies may show a preview; use _retained with artifact_read/artifact_grep for all rows.'


def command_metadata(output, exit_code):
    """Keep command outcome visible while retaining the exact complete output."""
    meta = {'exit': exit_code, 'execution_state': 'completed'}
    if len(output) < 1400:
        return meta
    rows = output.splitlines()
    errors = [row for row in rows if re.match(r'^(?:FAIL:|ERROR:|\w*(?:Error|Exception):|FAILED\b)', row)]
    candidates = errors or rows
    chosen = candidates if len(candidates) <= 8 else [*candidates[:4], '[... omitted rows ...]', *candidates[-4:]]
    preview = '\n'.join(chosen)
    meta['_result_view_source'] = json.dumps({
        'summary': {'exit_code': exit_code, 'output_characters': len(output),
                    'nonzero_exit': exit_code != 0},
        'preview': preview[:1000],
        'notes': ['Preview only. Retrieve exact details with artifact_grep or artifact_read only if needed; '
                  'continue pages using next_offset. Nonzero exit means command failure.']}, ensure_ascii=False)
    return meta


def search_result(body, *, summary, rows, notes=()):
    from .tools import ToolResult
    meta = dict(summary)
    if len(body) >= 1400:
        preview, size = [], 0
        candidates = rows if len(rows) <= 8 else [*rows[:4], '[... omitted rows ...]', *rows[-4:]]
        for row in candidates:
            if size + len(row) + 1 > 800:
                continue
            preview.append(row)
            size += len(row) + 1
        meta['_result_view_source'] = json.dumps({
            'summary': {**summary, 'returned_rows': len(rows),
                        'preview_rows': sum(row != '[... omitted rows ...]' for row in preview)},
            'preview': '\n'.join(preview), 'notes': list(notes)}, ensure_ascii=False)
    return ToolResult(True, body, meta=meta)
