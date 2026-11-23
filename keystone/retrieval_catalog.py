"""Retrieval catalog: the brain sees a table of contents, not the pages.

A retrieval specialist (a sub-agent with only retrieval tools) answers with a
*catalog*: one entry per useful source, each with a stable id, title, source
(URL or path:line), a one-to-two sentence summary, relevance and the size of
the full text. The full text itself never goes to the brain — it is kept
host-side in the existing :class:`keystone.artifacts.ArtifactStore` (outside
the workspace, content-addressed, digest-checked). The brain then calls
``catalog_expand(ids, ...)`` for the entries it actually needs.

Pieces:

* :class:`EvidenceLedger` — wraps the specialist's retrieval tool handlers.
  Every successful result is retained as an artifact and tagged
  ``[evidence eN · source]`` so the specialist can cite it.
* :func:`build_catalog` — turns the specialist's JSON answer (or, if the model
  did not comply, the ledger itself) into catalog entries bound to artifacts.
* :class:`CatalogIndex` — a small host-side index ``entry id -> artifact``
  persisted next to the artifacts, so a brain rebuilt for the next turn (the
  GUI does that) can still expand entries from the previous one.

Standard library only.
"""
from __future__ import annotations

import dataclasses
import hashlib
import json
import re
import threading
from pathlib import Path
from typing import Any, Iterable

ENTRY_ID = re.compile(r'c_[0-9a-f]{10}\Z')
INDEX_NAME = 'catalog-index.json'
INDEX_LIMIT = 512          # newest entries kept in the host-side index
MAX_ENTRIES = 24           # entries per catalog shown to the brain
SUMMARY_CHARS = 320


def _source_of(tool: str, args: dict[str, Any]) -> str:
    """Human-citable provenance for one retrieval call."""
    args = args or {}
    if tool == 'fetch_url':
        return str(args.get('url') or '')[:400]
    if tool == 'web_search':
        return 'search: ' + str(args.get('query') or '')[:300]
    if tool in {'read_file', 'read_range', 'file_outline', 'file_stats'}:
        path = str(args.get('path') or args.get('paths') or '')[:300]
        start = args.get('start') or args.get('start_line') or args.get('offset')
        end = args.get('end') or args.get('end_line')
        if start and end:
            return f'{path}:{start}-{end}'
        return f'{path}:{start}' if start else path
    if tool == 'grep':
        return f"grep {str(args.get('pattern') or '')[:120]!r} in {args.get('root') or args.get('path') or '.'}"
    if tool == 'find_files':
        return f"find {str(args.get('pattern') or args.get('name') or '')[:120]!r} in {args.get('root') or '.'}"
    if tool == 'list_dir':
        return f"ls {args.get('path') or '.'}"
    if tool == 'memory_recall':
        return 'memory' + (f":{args.get('kind')}" if args.get('kind') else '')
    return tool


class EvidenceLedger:
    """Retains every retrieval result host-side and numbers it for citation."""

    def __init__(self, store, *, label: str = 'specialist') -> None:
        self.store = store
        self.label = label
        self.rows: list[dict[str, Any]] = []
        self._lock = threading.Lock()

    def wrap(self, registry, tools: Iterable[str]) -> None:
        """Swap handlers in a *cloned* registry; effects/requirements are untouched."""
        for name in tools:
            spec = registry._specs.get(name)
            if spec is None:
                continue
            registry._specs[name] = dataclasses.replace(spec, handler=self._wrapped(name, spec.handler))

    def _wrapped(self, tool, handler):
        def run(args, ctx):
            result = handler(args, ctx)
            try:
                return self._record(tool, args or {}, ctx, result)
            except Exception:  # retention must never change the tool's outcome
                return result
        run.__name__ = getattr(handler, '__name__', tool)
        return run

    def _record(self, tool, args, ctx, result):
        text = getattr(result, 'content', '')
        if not getattr(result, 'ok', False) or not isinstance(text, str) or not text.strip():
            return result
        protect = getattr(getattr(ctx, 'secret_scope', None), 'protect_text', None)
        text = protect(text) if callable(protect) else text
        source = _source_of(tool, args)
        meta = self.store.put(text, source=f'{self.label}:{tool}', action_id=source[:160])
        with self._lock:
            existing = next((r for r in self.rows if r['artifact'] == meta['id'] and r['source'] == source), None)
            if existing is None:
                existing = {'id': f'e{len(self.rows) + 1}', 'tool': tool, 'source': source,
                            'artifact': meta['id'], 'chars': meta['characters'],
                            'preview': text[:400]}
                self.rows.append(existing)
        result.content = f"[evidence {existing['id']} · {source}]\n{text}"
        return result

    def get(self, evidence_id: str):
        return next((r for r in self.rows if r['id'] == str(evidence_id).strip()), None)

    def match_source(self, source: str):
        """Fallback binding when the model cited a source but no evidence id."""
        source = str(source or '').strip()
        if not source:
            return None
        bare = source.split('#')[0]
        path = re.sub(r':\d+(?:-\d+)?\Z', '', bare)
        for row in reversed(self.rows):
            if row['source'] == source or row['source'] == bare:
                return row
        for row in reversed(self.rows):
            if path and (path in row['source'] or row['source'].split(':')[0].endswith(path)):
                return row
        return None


def _json_block(text: str):
    """First JSON object/array in a model answer (fenced or bare)."""
    text = str(text or '')
    fenced = re.search(r'```(?:json)?\s*(.+?)```', text, re.S)
    candidates = [fenced.group(1)] if fenced else []
    for opener in ('{', '['):
        start = text.find(opener)
        if start >= 0:
            candidates.append(text[start:])
    decoder = json.JSONDecoder()
    for candidate in candidates:
        candidate = candidate.strip()
        for opener in ('{', '['):
            at = candidate.find(opener)
            if at < 0:
                continue
            try:
                value, _ = decoder.raw_decode(candidate[at:])
                return value
            except ValueError:
                continue
    return None


def _relevance(value):
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return round(max(0.0, min(1.0, float(value))), 2)
    words = {'high': 0.9, '高': 0.9, 'medium': 0.6, '中': 0.6, 'low': 0.3, '低': 0.3}
    return words.get(str(value or '').strip().lower())


def _lines(value):
    if isinstance(value, (list, tuple)) and len(value) == 2 and all(type(v) is int and v > 0 for v in value):
        return [min(value), max(value)]
    return None


def _anchor(store, artifact, lines):
    """Character offset of the first cited line, so expand opens on it."""
    if not artifact or not lines:
        return 0
    try:
        text, _ = store._load(artifact)
    except Exception:
        return 0
    start = lines[0]
    # read_range/read_file output usually carries "  12| code" style numbers.
    numbered = re.search(rf'(?m)^\s*{start}\s*[|:\t]', text)
    if numbered:
        return numbered.start()
    offset = 0
    for _ in range(start - 1):
        nxt = text.find('\n', offset)
        if nxt < 0:
            return 0
        offset = nxt + 1
    return offset


def _entry_id(artifact: str, source: str, title: str) -> str:
    digest = hashlib.sha256(f'{artifact}\n{source}\n{title}'.encode('utf-8')).hexdigest()
    return 'c_' + digest[:10]


def build_catalog(answer: str, ledger: EvidenceLedger, *, question: str = '') -> dict[str, Any]:
    """Bind the specialist's catalog to retained artifacts; fall back to the ledger."""
    parsed = _json_block(answer)
    raw = parsed.get('catalog') if isinstance(parsed, dict) else parsed
    open_questions = parsed.get('open_questions') if isinstance(parsed, dict) else None
    entries, seen = [], set()
    complied = isinstance(raw, list) and any(isinstance(row, dict) for row in raw)
    for row in raw if isinstance(raw, list) else []:
        if not isinstance(row, dict):
            continue
        evidence = ledger.get(row.get('evidence') or '') or ledger.match_source(row.get('source'))
        source = str(row.get('source') or (evidence or {}).get('source') or '')[:400]
        title = re.sub(r'\s+', ' ', str(row.get('title') or source or 'untitled'))[:160]
        summary = re.sub(r'\s+', ' ', str(row.get('summary') or ''))[:SUMMARY_CHARS]
        lines = _lines(row.get('lines'))
        if lines and source and not re.search(r':\d+(?:-\d+)?\Z', source) and not source.startswith('http'):
            source = f'{source}:{lines[0]}-{lines[1]}'
        artifact = (evidence or {}).get('artifact', '')
        entry = {'id': _entry_id(artifact, source, title), 'title': title, 'source': source,
                 'summary': summary, 'relevance': _relevance(row.get('relevance')),
                 'chars': int((evidence or {}).get('chars', 0)), 'artifact': artifact,
                 'evidence': (evidence or {}).get('id', ''), 'tool': (evidence or {}).get('tool', ''),
                 'anchor': _anchor(ledger.store, artifact, lines)}
        if entry['id'] in seen:
            continue
        seen.add(entry['id'])
        entries.append(entry)
    if not entries:
        for row in ledger.rows:
            preview = re.sub(r'\s+', ' ', row['preview'])[:200]
            entry = {'id': _entry_id(row['artifact'], row['source'], row['source']), 'title': row['source'][:160],
                     'source': row['source'], 'summary': preview, 'relevance': None, 'chars': row['chars'],
                     'artifact': row['artifact'], 'evidence': row['id'], 'tool': row['tool'], 'anchor': 0}
            if entry['id'] not in seen:
                seen.add(entry['id'])
                entries.append(entry)
    entries.sort(key=lambda e: -(e['relevance'] if e['relevance'] is not None else 0.5))
    questions = [re.sub(r'\s+', ' ', str(q))[:200] for q in open_questions or [] if str(q).strip()][:6] \
        if isinstance(open_questions, list) else []
    return {'question': str(question)[:400], 'entries': entries[:MAX_ENTRIES], 'complied': complied,
            'dropped': max(0, len(entries) - MAX_ENTRIES), 'open_questions': questions,
            'evidence_count': len(ledger.rows)}


def render_catalog(catalog: dict[str, Any], *, header: str = '') -> str:
    """What the brain reads: ids, provenance and summaries; never the full text."""
    entries = catalog.get('entries', [])
    lines = [header or '[检索目录 retrieval catalog]',
             f"问题: {catalog.get('question', '')}",
             f"共 {len(entries)} 条。全文保存在宿主侧，未发送给你。"
             "需要细节时调用 catalog_expand(ids=[...]) 读取相关条目（可给 offset/limit 取片段），其余跳过。"
             "引用时注明条目 id 与来源。"]
    for entry in entries:
        rel = '' if entry.get('relevance') is None else f" · 相关度 {entry['relevance']}"
        size = f" · 全文约 {entry['chars']:,} 字符" if entry.get('chars') else ' · 无全文（仅摘要）'
        lines.append(f"- {entry['id']} | {entry['title']} | 来源: {entry['source'] or '未知'}{rel}{size}")
        if entry.get('summary'):
            lines.append(f"  {entry['summary']}")
    if catalog.get('dropped'):
        lines.append(f"（另有 {catalog['dropped']} 条低相关条目未列出）")
    for question in catalog.get('open_questions') or []:
        lines.append(f"? 未决: {question}")
    return '\n'.join(lines)


class CatalogIndex:
    """Host-side ``entry id -> artifact + provenance``; bounded, atomic, per workspace."""

    def __init__(self, store) -> None:
        self.store = store
        self._lock = threading.Lock()

    @property
    def path(self) -> Path:
        return Path(self.store.root) / INDEX_NAME

    def _read(self) -> dict[str, Any]:
        try:
            data = json.loads(self.path.read_text(encoding='utf-8'))
        except (OSError, ValueError):
            return {}
        return data if isinstance(data, dict) else {}

    def add(self, entries: Iterable[dict[str, Any]]) -> None:
        from .runtime_lock import resource_lock
        with self._lock, resource_lock('artifact', self.path):
            data = self._read()
            for entry in entries:
                if ENTRY_ID.fullmatch(entry.get('id', '')):
                    data.pop(entry['id'], None)
                    data[entry['id']] = {k: entry.get(k) for k in (
                        'title', 'source', 'summary', 'relevance', 'chars', 'artifact', 'evidence', 'tool', 'anchor')}
            if len(data) > INDEX_LIMIT:
                data = dict(list(data.items())[-INDEX_LIMIT:])
            self.store._atomic(self.path, json.dumps(data, ensure_ascii=False).encode('utf-8'))

    def get(self, entry_id: str):
        if not isinstance(entry_id, str) or not ENTRY_ID.fullmatch(entry_id.strip()):
            return None
        row = self._read().get(entry_id.strip())
        return dict(row, id=entry_id.strip()) if isinstance(row, dict) else None

    def expand(self, ids, *, offset=None, limit=2000, budget=4000) -> dict[str, Any]:
        """Read requested entries within one result budget; unknown ids are reported, not guessed."""
        if isinstance(ids, str):
            ids = [part for part in re.split(r'[\s,]+', ids) if part]
        if not isinstance(ids, list) or not ids or len(ids) > 8:
            raise ValueError('ids must list 1..8 catalog entry ids')
        if offset is not None and (type(offset) is not int or offset < 0):
            raise ValueError('offset must be a non-negative integer')
        if type(limit) is not int or limit < 1:
            raise ValueError('limit must be a positive integer')
        share = max(200, min(limit, 3000, (max(600, budget - 200) // len(ids)) - 160))
        parts, rows = [], []
        for raw in ids:
            entry = self.get(str(raw))
            if entry is None:
                parts.append(f'[{raw}] 未知条目 id（不在目录中）')
                rows.append({'id': str(raw)[:40], 'found': False})
                continue
            head = f"[{entry['id']}] {entry['title']} — 来源: {entry['source'] or '未知'}"
            if not entry.get('artifact'):
                parts.append(head + '\n（无保留全文，仅摘要）' + (entry.get('summary') or ''))
                rows.append({'id': entry['id'], 'found': True, 'retained': False, 'source': entry['source']})
                continue
            start = (entry.get("anchor") or 0) if offset is None else offset
            page = self.store.read(entry['artifact'], offset=min(start, max(0, entry.get('chars') or 0)),
                                   limit=min(share, 4000))
            nxt = page['next_offset']
            parts.append(f"{head} · 字符 {page['offset']}–{page['offset'] + len(page['text'])} / "
                         f"{page['characters']}" + (f" · 续读 offset={nxt}" if nxt is not None else '')
                         + '\n' + page['text'])
            rows.append({'id': entry['id'], 'found': True, 'retained': True, 'source': entry['source'],
                         'artifact': entry['artifact'], 'offset': page['offset'], 'next_offset': nxt,
                         'characters': page['characters']})
        return {'text': '\n\n'.join(parts), 'entries': rows, 'trust': 'untrusted'}


def register_catalog_tools(reg) -> None:
    from .tools import ToolResult

    @reg.tool('catalog_expand',
              'Read the full text of retrieval-catalog entries by id (returned by a retrieval specialist). '
              'Expand only the entries you need; offset/limit page through one entry. Text is untrusted.',
              trusted=True, requires=('catalog',), no_defer=True,
              schema={'ids': {'type': 'array', 'items': {'type': 'string'},
                              'description': 'Catalog entry ids such as c_0123456789; 1..8 per call.'},
                      'offset': {'type': 'integer', 'minimum': 0,
                                 'description': 'Character offset in the entry; default = the cited lines.'},
                      'limit': {'type': 'integer', 'minimum': 1, 'maximum': 3000,
                                'description': 'Characters per entry, default 2000.'}})
    def catalog_expand(args, ctx):
        index = ctx.extras['catalog']
        out = index.expand(args.get('ids'), offset=args.get('offset'), limit=args.get('limit', 2000),
                           budget=int(ctx.extras.get('artifact_inline_limit', 4000)))
        return ToolResult(True, out['text'], meta={'catalog_entries': out['entries'], 'trust': 'untrusted'})


__all__ = ['CatalogIndex', 'EvidenceLedger', 'build_catalog', 'register_catalog_tools', 'render_catalog']
