"""MiMo must retrieve an omitted item through the real artifact tool, not guess a summary."""
import argparse
import json
from pathlib import Path
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from verify_mimo_live import MODELS, provider_for, router_for
from verify_mimo_long_context import LongRecorder, dataset, cursor, final_value
from forge import model
from forge.cache_state import CacheWarmth
from forge.context_transaction import valid_tool_pairs
from forge.loop import Agent, LoopLimits
from forge.policy import Mode, Policy, ToolEffect
from forge.secrets import redact
from forge.tools import ToolSpec, ToolResult, build_builtin_registry


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--live', action='store_true')
    parser.add_argument('--model', choices=MODELS, default=MODELS[0])
    parser.add_argument('--request-limit', type=int, default=10)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    if not args.live or not 1 <= args.request_limit <= 12:
        parser.error('Requires --live and request-limit 1..12')
    recorder = LongRecorder(args.request_limit, retries=1)
    provider = provider_for(args.model, Path.home() / '.forge')
    result = {'model': args.model, 'requests': recorder.rows, 'ok': False}
    try:
        with tempfile.TemporaryDirectory(prefix='forge-view-lookup-') as temporary:
            root = Path(temporary)
            model._WARMTH = CacheWarmth(root / 'warmth.json')
            items, header = dataset()[0]
            raw = json.dumps({'items': items, 'batch_result': header, 'next_cursor': cursor(1)})
            expected = {'id': items[-1]['id'], 'quantity': items[-1]['quantity']}
            registry = build_builtin_registry(expose=['load_batch', 'artifact_read'])
            registry.register(ToolSpec('load_batch', 'Load the first warehouse audit batch.',
                lambda a,c: ToolResult(True, raw), result_fields=('batch_result', 'next_cursor')),
                trusted=True, effect=ToolEffect.READ)
            agent = Agent(home=root / 'home', workspace=root, router=router_for(provider, recorder),
                registry=registry, policy=Policy(workspace=root, mode=Mode.READ_ONLY, non_interactive=True),
                limits=LoopLimits(max_steps=8), project_rules=False)
            try:
                recorder.stage = 'retained-item-lookup'
                report = agent.run('First call load_batch. Find the quantity of item B1-R048 in the original '
                    'full item records. Retrieve retained output when the current view omits the items. '
                    'Do not guess from the batch header. Return only JSON with id and quantity.')
                value, fmt, strict = final_value(report.text)
                reads = [s for s in report.steps if s.tool == 'artifact_read' and s.decision == 'ok']
                observed = any(expected['id'] in s.result for s in reads)
                result.update(ok=report.stopped == 'final' and value == expected and observed
                    and all(s.decision == 'ok' for s in report.steps), final=value, expected=expected,
                    final_format=fmt, strict_format=strict, stopped=report.stopped,
                    omitted_record_observed=observed, tools=[s.tool for s in report.steps],
                    views=[s.meta.get('model_view') for s in report.steps if s.tool == 'load_batch'],
                    source_characters=len(raw))
                assert all(valid_tool_pairs(p['messages']) for p in recorder.sent)
            finally:
                agent.secret_scope.close()
    except Exception as exc:
        result['error'] = redact(type(exc).__name__ + ': ' + str(exc))[:500]
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(redact(result), ensure_ascii=False, indent=2)+'\n', encoding='utf-8')
    print(json.dumps({'ok': result['ok'], 'attempts': len(recorder.rows)}, ensure_ascii=False))
    return 0 if result['ok'] else 1


if __name__ == '__main__':
    raise SystemExit(main())
