"""MiMo 长工具输出、跨轮续接和压缩对照；显式 --live，只有隔离的合成数据。"""
from __future__ import annotations

import argparse
import copy
import hashlib
import importlib.util
import json
import re
from pathlib import Path
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from verify_mimo_live import Recorder, provider_for, router_for, MODELS, digest
from forge import model
from forge.cache_metrics import cache_coverage
from forge.cache_state import CacheWarmth
from forge.compaction import ContextBudget
from forge.context_transaction import valid_tool_pairs
from forge.development.runtime import DEVELOPMENT_GUIDANCE
from forge.loop import Agent, LoopLimits
from forge.memory import MemoryStore
from forge.policy import Mode, Policy, Sandbox, ToolEffect
from forge.secrets import redact
from forge.tools import ToolSpec, ToolResult, build_builtin_registry


def final_value(text):
    """Keep strict formatting separate from an unambiguous final JSON value."""
    body = text.strip()
    try:
        return json.loads(body), 'plain_json', True
    except ValueError:
        pass
    blocks = list(re.finditer(r'```json\r?\n(.*?)\r?\n```', body, re.S))
    if len(blocks) == 1 and not body[blocks[0].end():].strip():
        try:
            value = json.loads(blocks[0][1])
            strict = not body[:blocks[0].start()].strip()
            return value, 'fenced_json' if strict else 'prose_fenced_json', strict
        except ValueError:
            pass
    return None, 'invalid', False


class LongRecorder(Recorder):
    def complete(self, provider, chosen, messages, **options):
        options.setdefault('max_tokens', 4096)
        return super().complete(provider, chosen, messages, **options)


def load_baseline(directory):
    def load(name, relative):
        spec = importlib.util.spec_from_file_location(name, directory / relative)
        module = importlib.util.module_from_spec(spec)
        sys.modules[name] = module
        spec.loader.exec_module(module)
        return module
    # 只执行本轮修改前已保存的本地宿主代码，不从模型或网络加载代码。
    loop = load('forge._long_baseline_loop', 'forge/loop.py')
    budget = load('forge._long_baseline_compaction', 'forge/compaction.py')
    return loop.Agent, budget.ContextBudget


def dataset():
    batches = []
    for batch in range(5):
        items = [{'id': f'B{batch + 1}-R{row + 1:03}', 'quantity': 1 + (batch + row) % 7,
                  'description': f'Warehouse stock item {row + 1:03} in audit batch {batch + 1}'}
                 for row in range(48)]
        header = {'batch': batch + 1, 'units': sum(r['quantity'] for r in items),
                  'end_marker': f'AUDIT-BATCH-{batch + 1}-COMPLETE'}
        batches.append((items, header))
    return batches


def cursor(index):
    return 'start' if index == 0 else hashlib.sha256(f'fixture-cursor-{index}'.encode()).hexdigest()[:12]


def run_case(cls, budget_cls, provider, recorder, root, sample, strategy, archives,
             scoped_rules=False, result_views=False):
    ws = root / 'long-case'
    ws.mkdir(exist_ok=True)
    checkpoint = ws / 'checkpoint.json'
    checkpoint.write_text(json.dumps({'processed': 0, 'total_units': 0,
        'batches': [], 'next_cursor': 'start'}), encoding='utf-8')
    (ws / 'AGENTS.md').write_text(
        'Process batches only with process_batch and the returned cursor. Read checkpoint.json to verify '
        'the persisted results and recover the cursor after context compaction. Never change the checkpoint directly. '
        'Do not infer success from an excerpt summary. Return a JSON object with processed and total_units only. '
        'Use native tools, not XML. Process one batch per model response.\n', encoding='utf-8')
    if scoped_rules:
        with (ws / 'AGENTS.md').open('a', encoding='utf-8') as stream:
            stream.write('Audit controls: process directories in numeric order. Before each batch, read '
                'batch-N/request.json with read_file; its scoped project guidance supplies the required policy_tag. '
                'A directory rule applies only to that batch. Use the returned next_cursor for the next batch. '
                'Checkpoint headers must remain in processing order, with no skipped or repeated batch. '
                'Reported units are the sum of persisted headers, not the number of item rows. '
                'If context is compacted, recover the cursor from checkpoint.json and read the next batch request. '
                'Do not edit any input, project guidance, or checkpoint directly. '
                'Stop at the requested count even if further batch directories exist.\n')
        for index in range(5):
            directory = ws / f'batch-{index+1}'
            directory.mkdir(exist_ok=True)
            (directory / 'request.json').write_text(json.dumps({'batch': index+1,
                'input_kind': 'warehouse_audit', 'checkpoint': 'checkpoint.json'}), encoding='utf-8')
            (directory / 'AGENTS.md').write_text(
                f'For batch {index+1} only, set process_batch.policy_tag to audit-scope-{index+1}-verified. '
                'The batch must be committed using its current resume cursor. '
                'Verify the persisted batch header before reporting completion.\n', encoding='utf-8')
    memory = MemoryStore(root / f'memory-{sample}.md').load()
    memory.remember('Audit processing must be verified against the workspace checkpoint. '
        'The checkpoint is authoritative for the processed count and resume cursor.', kind='user', source='human')
    batches = dataset()
    position, quota = [0], [3]
    executed = []

    def process(args, ctx):
        index = position[0]
        if index >= quota[0]:
            return ToolResult(False, error='Requested batch count reached. Verify checkpoint.json and finish.',
                meta={'execution_state': 'not_started', 'error_code': 'batch_limit'})
        if args.get('cursor') != cursor(index):
            return ToolResult(False, error='Incorrect cursor; no batch processed. Read checkpoint.json for next_cursor.',
                meta={'execution_state': 'not_started', 'error_code': 'cursor_conflict'})
        if scoped_rules and args.get('policy_tag') != f'audit-scope-{index+1}-verified':
            return ToolResult(False, error=f'Incorrect policy_tag for batch {index+1}; cursor unchanged, no batch processed. '
                f'Use the exact tag in batch-{index+1}/AGENTS.md, not input_kind.',
                meta={'execution_state': 'not_started', 'error_code': 'scope_mismatch'})
        items, header = batches[index]
        headers = [batches[i][1] for i in range(index + 1)]
        state = {'processed': index + 1, 'total_units': sum(h['units'] for h in headers),
                 'batches': headers, 'next_cursor': cursor(index + 1)}
        pending = checkpoint.with_suffix('.pending')
        pending.write_text(json.dumps(state), encoding='utf-8')
        pending.replace(checkpoint)
        position[0] += 1
        executed.append(index + 1)
        return ToolResult(True, json.dumps({'items': items, 'batch_result': header,
            'next_cursor': cursor(index + 1)}), meta={'changed': True})

    registry = build_builtin_registry(expose=['process_batch', 'read_file', 'calculator'] +
                                     (['artifact_read'] if result_views else []))
    registry.register(ToolSpec('process_batch',
        'Process one audit batch using its resume cursor, persist checkpoint.json, and return its item details and next cursor. '
        'Call only once per response; get the next cursor from this result or read_file checkpoint.json.',
        process, read_only=False, result_fields=(('batch_result', 'next_cursor')
            if result_views and strategy == 'improved' else ()), schema={'cursor': {'type': 'string'}, **(
            {'policy_tag': {'type': 'string', 'description': 'Required per-batch tag from scoped project guidance.'}}
            if scoped_rules else {})}), trusted=True, effect=ToolEffect.WRITE)
    agent = cls(home=root / 'home', workspace=ws, router=router_for(provider, recorder), registry=registry,
        policy=Policy(workspace=ws, mode=Mode.ACCEPT_EDITS, sandbox=Sandbox.WORKSPACE_WRITE, non_interactive=True),
        limits=LoopLimits(max_steps=20 if scoped_rules else 12, context_chars=16000, max_tool_result=8000),
        project_rules=True, memory=memory, extra_system=DEVELOPMENT_GUIDANCE)
    agent.compactor = budget_cls(max_chars=16000, keep_tail=6)
    history, turns = [], []
    start = len(recorder.rows)
    try:
        for turn, target in enumerate((3, 5), 1):
            quota[0] = target
            recorder.stage = f'long-{sample}-{strategy}-turn-{turn}'
            prompt = ('Start at cursor start. Process exactly 3 audit batches sequentially.' if turn == 1 else
                'Resume the same task: first read checkpoint.json to recover its next_cursor; process exactly 2 more batches, reaching 5 total.')
            prompt += (' Use process_batch once per response and its returned next_cursor. '
                'Read checkpoint.json after processing to verify it. Return only JSON with processed and total_units. '
                'Use native tool calls; do not output XML. Do not modify the checkpoint yourself.')
            if scoped_rules:
                prompt += (' Before EACH batch read batch-N/request.json (N is the batch number). '
                    'Use that directory\'s scoped project guidance for process_batch.policy_tag. '
                    'Do not reuse another directory\'s tag. Read each batch request exactly once.')
            expected = {'processed': target, 'total_units': sum(batches[i][1]['units'] for i in range(target))}
            contract = {'criteria': [{'id': 'count', 'kind': 'env_probe', 'env': 'workspace', 'probe': 'json_value',
                'input': {'path': 'checkpoint.json', 'pointer': '/processed'}, 'expect': target},
                {'id': 'units', 'kind': 'env_probe', 'env': 'workspace', 'probe': 'json_value',
                 'input': {'path': 'checkpoint.json', 'pointer': '/total_units'}, 'expect': expected['total_units']}]}
            call_start = len(recorder.rows)
            report = agent.run(prompt, history=history, acceptance=contract)
            final, final_format, strict_format = final_value(report.text)
            actual = json.loads(checkpoint.read_text(encoding='utf-8')) if checkpoint.exists() else {}
            ok = (report.stopped == 'final' and final == expected and actual.get('processed') == target
                  and actual.get('batches') == [batches[i][1] for i in range(target)]
                  and any(s.tool == 'read_file' for s in report.steps))
            clean_execution = all(s.decision == 'ok' for s in report.steps)
            if scoped_rules:
                expected_paths = [f'batch-{i}/request.json' for i in range(1 if turn == 1 else 4, target+1)]
                actual_paths = [s.args.get('path') for s in report.steps if s.tool == 'read_file'
                                and s.args.get('path') in expected_paths]
                ok = ok and all(path in actual_paths for path in expected_paths)
                clean_execution = clean_execution and actual_paths == expected_paths
            assert recorder.sent[call_start]['messages'][-1]['content'].startswith(prompt)
            compactions = [e for e in report.events if e['type'] == 'compaction']
            for step in report.steps:
                if step.meta.get('model_view'):
                    identifier = step.meta['artifact']['id']
                    raw, _ = agent.artifacts._load(identifier)
                    destination = archives.parent / (archives.name.removesuffix('-archives') + '-artifacts')
                    destination.mkdir(parents=True, exist_ok=True)
                    path = destination / (identifier + '.json')
                    path.write_bytes(raw.encode('utf-8'))
                    step.meta['model_view']['export_path'] = str(path.resolve())
            for event in compactions:
                if event.get('committed'):
                    archive = json.loads(Path(event['archive']['path']).read_text(encoding='utf-8'))
                    assert valid_tool_pairs(archive['messages'])
                    archives.mkdir(parents=True, exist_ok=True)
                    path = archives / (event['archive']['archive_id'] + '.json')
                    path.write_text(json.dumps(archive, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
                    event['archive']['export_path'] = str(path.resolve())
            assert all(valid_tool_pairs(p['messages']) for p in recorder.sent[call_start:])
            prefix_count = len(getattr(agent, '_compaction_prefix', ()))
            prefix = recorder.sent[start]['messages'][:prefix_count]
            stable_wire_prefix = bool(prefix_count) and all(
                p['messages'][:prefix_count] == prefix for p in recorder.sent[start:])
            if strategy == 'improved':
                assert stable_wire_prefix
            turns.append({'turn': turn, 'ok': ok, 'stopped': report.stopped, 'final': final,
                'clean_execution': clean_execution,
                'tool_errors': sum(s.decision != 'ok' for s in report.steps),
                'final_format': final_format, 'strict_format': strict_format,
                'final_text': report.text[:500], 'stable_wire_prefix': stable_wire_prefix,
                'preserved_wire_prefix_messages': prefix_count,
                'failure_detail': report.text[:500] if not ok else '',
                'compactions': compactions, 'deferred': sum(e['type'] == 'compaction_deferred' for e in report.events),
                'prefix_reused': any(e['type'] == 'cache_prefix_reused' for e in report.events),
                'observation_reused': any(e['type'] == 'cache_observation_reused' for e in report.events),
                'requests': copy.deepcopy(report.usage.get('requests', [])),
                'acceptance_checks': [e for e in report.events if e['type'] == 'acceptance_checked'],
                'tools': [{'tool': s.tool, 'decision': s.decision, 'result_chars': len(s.result),
                           'model_view': s.meta.get('model_view'), 'artifact': s.meta.get('artifact'),
                           'artifact_retention': s.meta.get('artifact_retention')} for s in report.steps]})
            print(json.dumps({'sample': sample, 'strategy': strategy, 'turn': turn, 'ok': ok,
                'compactions': sum(e.get('committed', False) for e in compactions)}, ensure_ascii=False), flush=True)
            history += [{'role': 'user', 'content': prompt}, {'role': 'assistant', 'content': report.text}]
            if not ok:
                break
    finally:
        agent.secret_scope.close()
    rows = recorder.rows[start:]
    good = [r for r in rows if r.get('usage')]
    return {'sample': sample, 'strategy': strategy, 'ok': len(turns) == 2 and all(t['ok'] for t in turns),
        'turns': turns, 'processed_batches': executed, 'requests': len(rows),
        'clean_execution': all(t['clean_execution'] for t in turns),
        'strict_format': all(t['strict_format'] for t in turns),
        'committed_compactions': sum(e.get('committed', False) for t in turns for e in t['compactions']),
        'max_message_chars': max((r['message_chars'] for r in good), default=0),
        'initial_payload_digest': rows[0]['payload_digest'] if rows else None,
        'initial_tools_digest': rows[0]['tools_digest'] if rows else None,
        'initial_context_prefix_digest': digest(recorder.sent[start]['messages'][:4]) if rows else None,
        'initial_system_digest': digest(recorder.sent[start]['messages'][0]) if rows else None,
        'initial_fixed_data_digest': digest(recorder.sent[start]['messages'][1:4]) if rows else None,
        'input_tokens': sum(r['usage']['prompt_tokens'] for r in good),
        'output_tokens': sum(r['usage']['completion_tokens'] for r in good),
        'coverage': cache_coverage(good),
        'revision_coverage': cache_coverage([r for t in turns for r in t['requests']]),
        'seconds': round(sum(r['seconds'] for r in rows), 3)}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--live', action='store_true')
    parser.add_argument('--model', choices=MODELS, default=MODELS[1])
    parser.add_argument('--request-limit', type=int, default=48)
    parser.add_argument('--retries', type=int, choices=(0, 1, 2), default=1,
                        help='Transport retries only; all HTTP attempts count toward request-limit')
    parser.add_argument('--scoped-rules', action='store_true', help='Discover and obey five directory-scoped rules')
    parser.add_argument('--result-views', action='store_true',
                        help='Same current Agent in both arms; compare full output vs declared result fields')
    parser.add_argument('--baseline-dir', type=Path, help='Local before-edit source backup to compare')
    parser.add_argument('--context-window', type=int, default=16000,
                        help='Explicit local route gate; not the model\'s advertised window')
    parser.add_argument('--strategies', nargs='+', choices=('baseline', 'improved'),
                        default=['baseline', 'improved', 'improved', 'baseline'])
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    if not args.live or not 1 <= args.request_limit <= 64:
        parser.error('Requires --live and request-limit 1..64')
    if not 8192 <= args.context_window <= 131072:
        parser.error('context-window must be 8192..131072')
    backup = None
    baseline_agent, baseline_budget = Agent, ContextBudget
    if not args.result_views:
        backup = args.baseline_dir or Path((ROOT / 'reports/mimo-long-backup.txt').read_text(encoding='utf-8').strip())
        baseline_agent, baseline_budget = load_baseline(backup)
    provider = provider_for(args.model, Path.home() / '.forge')
    # 这是测试宿主的较小窗口约束，用于触发真实压缩；不宣称测试了模型的 1M 极限。
    provider.route_capabilities = {'contextWindow': args.context_window, 'maxOutputTokens': 4096}
    recorder = LongRecorder(args.request_limit, retries=args.retries)
    result = {'model': args.model, 'mode': 'live', 'context_chars': 16000,
              'local_context_window': args.context_window,
              'output_tokens': 4096,
              'scoped_rules': args.scoped_rules,
              'result_views': args.result_views,
              'max_steps_per_turn': 20 if args.scoped_rules else 12,
              'comparison': 'Before-edit loop/compaction vs current; same task, tools and fixed data; system/projection may differ',
              'baseline_dir': None if args.result_views else str(backup),
              'requests': recorder.rows, 'samples': []}
    if args.result_views:
        result['comparison'] = 'Same current Agent and full result bytes; host-selected result_fields disabled/enabled'
    args.output.parent.mkdir(parents=True, exist_ok=True)
    try:
        with tempfile.TemporaryDirectory(prefix='forge-mimo-long-') as tmp:
            root = Path(tmp)
            model._WARMTH = CacheWarmth(root / 'warmth.json')
            for index, strategy in enumerate(args.strategies, 1):
                cls, budget = (baseline_agent, baseline_budget) if strategy == 'baseline' else (Agent, ContextBudget)
                sample = run_case(cls, budget, provider, recorder, root, index, strategy,
                                  args.output.parent / (args.output.stem + '-archives'), scoped_rules=args.scoped_rules,
                                  result_views=args.result_views)
                result['samples'].append(sample)
                args.output.write_text(json.dumps(redact(result), ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    except Exception as exc:
        result.update(error_type=type(exc).__name__, error_detail=redact(str(exc))[:500])
    result['identical_initial_payloads'] = len({s['initial_payload_digest'] for s in result['samples']}) == 1
    result['identical_initial_tools'] = len({s['initial_tools_digest'] for s in result['samples']}) == 1
    result['identical_initial_context_prefix'] = len({s['initial_context_prefix_digest'] for s in result['samples']}) == 1
    result['identical_initial_system'] = len({s['initial_system_digest'] for s in result['samples']}) == 1
    result['identical_initial_fixed_data'] = len({s['initial_fixed_data_digest'] for s in result['samples']}) == 1
    result['ok'] = (len(result['samples']) == len(args.strategies) and all(s['ok'] for s in result['samples'])
                    and result['identical_initial_tools'] and result['identical_initial_fixed_data'])
    args.output.write_text(json.dumps(redact(result), ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    print(json.dumps({'ok': result['ok'], 'requests': len(recorder.rows), 'error': result.get('error_detail')}, ensure_ascii=False))
    return 0 if result['ok'] else 1


if __name__ == '__main__':
    raise SystemExit(main())
