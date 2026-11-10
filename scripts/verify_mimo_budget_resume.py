"""Live MiMo budget-pause continuation: real file reads, calculator and wire receipts."""
from __future__ import annotations
import argparse
import json
from pathlib import Path
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from verify_mimo_long_context import LongRecorder, load_baseline
from verify_mimo_live import provider_for, router_for, MODELS
from forge import model
from forge.cache_metrics import cache_coverage
from forge.cache_state import CacheWarmth
from forge.context_transaction import valid_tool_pairs
from forge.loop import Agent, LoopLimits
from forge.policy import Policy, Mode
from forge.secrets import redact
from forge.tools import build_builtin_registry


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--live', action='store_true')
    parser.add_argument('--model', choices=MODELS, default=MODELS[0])
    parser.add_argument('--request-limit', type=int, default=16)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    if not args.live or not 1 <= args.request_limit <= 24:
        parser.error('Requires --live and request-limit 1..24')
    backup = Path((ROOT / 'reports/mimo-token-backup.txt').read_text(encoding='utf-8').strip())
    before, _ = load_baseline(backup)
    provider = provider_for(args.model, Path.home() / '.forge')
    provider.route_capabilities = {'contextWindow': 16000, 'maxOutputTokens': 4096}
    recorder = LongRecorder(args.request_limit, retries=2)
    result = {'model': args.model, 'requests': recorder.rows, 'samples': [], 'baseline_dir': str(backup)}
    args.output.parent.mkdir(parents=True, exist_ok=True)
    try:
        with tempfile.TemporaryDirectory(prefix='forge-mimo-pause-') as temporary:
            root = Path(temporary)
            ws = root / 'workspace'
            ws.mkdir()
            (ws / 'values.json').write_text('{"a":17,"b":25}', encoding='utf-8')
            (ws / 'AGENTS.md').write_text('Use actual file observations. Do not invent file contents. '
                'Use calculator for arithmetic; never write files or run shell commands. '
                'When all requested work is complete, follow the user\'s final output format.', encoding='utf-8')
            model._WARMTH = CacheWarmth(root / 'warmth.json')
            for strategy, cls in [('baseline', before), ('improved', Agent)]:
                agent = cls(home=root / 'home', workspace=ws, router=router_for(provider, recorder),
                    registry=build_builtin_registry(expose=['read_file', 'read_range', 'file_outline',
                        'grep', 'list_dir', 'calculator']), project_rules=True,
                    policy=Policy(workspace=ws, mode=Mode.READ_ONLY, non_interactive=True),
                    limits=LoopLimits(max_steps=1, context_chars=24000))
                start = len(recorder.rows)
                task = ('Read values.json with read_file. Use calculator to add its a and b integers. '
                    'Return only the numeric sum after the actual calculation; do not guess the file contents.')
                try:
                    recorder.stage = 'pause-' + strategy + '-before'
                    first = agent.run(task)
                    paused = first.stopped == 'max_steps' and [s.tool for s in first.steps] == ['read_file']
                    agent.limits.max_steps = 6
                    recorder.stage = 'pause-' + strategy + '-resume'
                    second = agent.run('Continue the original request to completion. Use actual prior tool observations '
                        'if available; otherwise read values.json. Use calculator and return only the numeric sum.',
                        history=[{'role': 'user', 'content': task},
                                 {'role': 'assistant', 'content': first.text}])
                    steps = first.steps + second.steps
                    ok = (paused and second.stopped == 'final' and second.text.strip() == '42'
                        and any(s.tool == 'calculator' and s.meta.get('value') == 42 for s in second.steps)
                        and all(s.decision == 'ok' for s in steps))
                    assert all(valid_tool_pairs(p['messages']) for p in recorder.sent[start:])
                    rows = recorder.rows[start:]
                    good = [r for r in rows if r.get('usage')]
                    result['samples'].append({'strategy': strategy, 'ok': ok, 'paused': paused,
                        'final': second.text, 'stopped': second.stopped,
                        'prefix_reused': any(e['type'] == 'cache_prefix_reused' for e in second.events),
                        'checkpointed': any(e['type'] == 'cache_prefix_checkpointed' for e in first.events),
                        'file_reads': sum(s.tool == 'read_file' for s in steps),
                        'tools': [s.tool for s in steps], 'requests': len(rows),
                        'input_tokens': sum(r['usage']['prompt_tokens'] for r in good),
                        'output_tokens': sum(r['usage']['completion_tokens'] for r in good),
                        'coverage': cache_coverage(good)})
                    args.output.write_text(json.dumps(redact(result), ensure_ascii=False, indent=2)+'\n', encoding='utf-8')
                finally:
                    agent.secret_scope.close()
    except Exception as exc:
        result['error'] = redact(type(exc).__name__ + ': ' + str(exc))[:500]
    result['ok'] = len(result['samples']) == 2 and all(s['ok'] for s in result['samples'])
    args.output.write_text(json.dumps(redact(result), ensure_ascii=False, indent=2)+'\n', encoding='utf-8')
    print(json.dumps({'ok': result['ok'], 'attempts': len(recorder.rows)}, ensure_ascii=False))
    return 0 if result['ok'] else 1


if __name__ == '__main__':
    raise SystemExit(main())
