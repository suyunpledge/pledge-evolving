"""Mediated read/search tools for host-owned, workspace-scoped artifacts."""
import json


def register_artifact_tools(reg):
    from .tools import ToolResult

    @reg.tool('artifact_read', 'Read untrusted retained tool output by handle. '
              'offset is a zero-based Unicode character offset; next_offset continues the page.',
              trusted=True, requires=('artifact_store',),
              schema={'id': 'string', 'offset': {'type': 'integer', 'minimum': 0},
                      'limit': {'type': 'integer', 'minimum': 1, 'maximum': 3000,
                                'description': 'Characters per page, 1..3000; default 2000. Oversized requests are capped. Use next_offset to continue.'}})
    def artifact_read(args, ctx):
        limit = args.get('limit', 2000)
        if type(limit) is not int or limit < 1:
            raise ValueError('limit must be a positive integer; pages contain at most 3000 characters')
        budget = ctx.extras.get('artifact_inline_limit', 4000)
        effective = min(limit, 3000, max(1, budget-400))
        page = ctx.extras['artifact_store'].read(args.get('id'), offset=args.get('offset', 0),
                                               limit=effective)
        if effective < limit:
            page.update(requested_limit=limit, effective_limit=effective, limit_capped=True)
        text = page.pop('text')
        return ToolResult(True, json.dumps(page, ensure_ascii=False) + '\n' + text, meta=page)

    @reg.tool('artifact_grep', 'Search a literal string in untrusted retained output. '
              'Returns character offsets and bounded previews; next_offset continues search.',
              trusted=True, requires=('artifact_store',),
              schema={'id': 'string',
                      'pattern': {'type': 'string', 'minLength': 1, 'maxLength': 256,
                                  'description': 'Literal substring to find, not a regular expression.'},
                      'offset': {'type': 'integer', 'minimum': 0},
                      'limit': {'type': 'integer', 'minimum': 1, 'maximum': 10,
                                'description': 'Matches per page, 1..10; default 10. Use next_offset to continue.'}})
    def artifact_grep(args, ctx):
        limit = args.get('limit', 10)
        if type(limit) is not int or not 1 <= limit <= 10:
            raise ValueError('limit must be an integer in 1..10')
        budget = ctx.extras.get('artifact_inline_limit', 4000)
        result = ctx.extras['artifact_store'].grep(args.get('id'), args.get('pattern'),
            offset=args.get('offset', 0), limit=min(limit, max(1, (budget-250)//160)))
        matches = result.pop('matches')
        body = '\n'.join(f"{row['offset']}: {row['preview']}" for row in matches)
        return ToolResult(True, json.dumps(result, ensure_ascii=False) + '\n' + body,
                          meta={**result, 'matches': matches})
