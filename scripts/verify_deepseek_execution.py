"""Opt-in DeepSeek live checks using configured credentials and isolated files."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from verify_mimo_live import Recorder, router_for, wire_probe
from forge.model import Provider, HttpTransport, ModelRouter
from forge.loop import Agent, LoopLimits
from forge.compaction import ContextBudget
from forge.policy import Policy, Mode, Sandbox
from forge.tools import build_builtin_registry
from forge.cache_metrics import cache_coverage


class DeepSeekRecorder(Recorder):
    def complete(self, provider, chosen, messages, **options):
        # Observe production defaults; the MiMo wire probe's 1024-token cap
        # must not override Agent/DeepSeek's actual execution output budget.
        result = HttpTransport.complete(self, provider, chosen, messages, **options)
        self.rows[-1]['usage'] = vars(result[1])
        self.rows[-1]['adaptation'] = result[2].get('plan', {})
        return result


def provider_for(chosen, home):
    rows = json.loads((home / 'forge.patch.json').read_text(encoding='utf-8-sig'))
    secrets = json.loads((home / 'secrets.json').read_text(encoding='utf-8-sig'))
    for row in rows:
        config = row.get('config', {})
        if (not row.get('disabled') and config.get('model') == chosen
                and config.get('baseURL', '').rstrip('/') in
                {'https://api.deepseek.com', 'https://api.deepseek.com/v1'}):
            key = secrets.get(row.get('id'))
            if not isinstance(key, str) or not key:
                raise ValueError('Configured DeepSeek credential missing')
            return Provider('deepseek-live', config['baseURL'], key, default_model=chosen,
                            reasoning_mode=config.get('reasoningMode', 'auto'))
    raise ValueError('No enabled official DeepSeek provider matches the selected model')


def totals(rows):
    usage = [r['usage'] for r in rows if r.get('usage')]
    return {'attempts': len(rows), 'successful_requests': len(usage),
            **{k: sum(u.get(k, 0) for u in usage) for k in
               ('prompt_tokens', 'completion_tokens', 'cached_tokens', 'reasoning_tokens')},
            'seconds': round(sum(r['seconds'] for r in rows), 3),
            'coverage': cache_coverage(rows)}


def task_probe(provider, recorder, root, planning, *, deny=False, compact=False, turns=1, planner=None):
    workspace = root / 'workspace'
    workspace.mkdir(exist_ok=True)
    (workspace / 'input.json').write_text('{"items":[17,25,9],"version":7}', encoding='utf-8')
    history = []
    results = []
    for turn in range(turns):
        target = f'result-{turn}.json'
        (workspace / target).unlink(missing_ok=True)
        recorder.stage = f'planning-{planning}/deny-{deny}/compact-{compact}/turn-{turn + 1}'
        start = len(recorder.rows)
        router = (ModelRouter([provider, planner], transport=recorder,
            primary=(provider.name,provider.default_model), retries_per_provider=recorder.retries)
            if planner else router_for(provider, recorder))
        agent = Agent(home=root / 'home', workspace=workspace, router=router,
            registry=build_builtin_registry(expose=['read_file', 'write_file']),
            policy=Policy(workspace=workspace, mode=Mode.ACCEPT_EDITS,
                          sandbox=Sandbox.WORKSPACE_WRITE, deny=('write_file',) if deny else (),
                          non_interactive=True), planning=planning,
            planning_model=(planner.name,planner.default_model) if planner else None, project_rules=False,
            limits=LoopLimits(max_steps=7, context_chars=6000 if compact else 100000))
        if compact:
            agent.compactor = ContextBudget(max_chars=6000, keep_tail=4)
        prompt = (f'Read input.json with read_file. Sum its items and write {target} containing exactly '
                  f'{{"total":51,"version":7,"turn":{turn + 1}}}, using the actual input values. '
                  'Use write_file then read_file to verify the saved JSON. Use actual tools. '
                  'If policy denies writing, stop and explain the denial without bypassing or repeating it. '
                  'Reply in one concise sentence.')
        if compact and not history:
            history = [{'role': 'user', 'content': 'Old unrelated audit records; current task supersedes them.'},
                       {'role': 'assistant', 'content': '\n'.join(
                           f'Closed historical record {i:03}: no current evidence; verify current files.'
                           for i in range(150))}]
        contract = None if deny else {'criteria': [
            {'id': k, 'kind': 'env_probe', 'env': 'workspace', 'probe': 'json_value',
             'input': {'path': target, 'pointer': '/' + k}, 'expect': v}
            for k, v in {'total': 51, 'version': 7, 'turn': turn + 1}.items()]}
        try:
            report = agent.run(prompt, history=history, acceptance=contract)
            actual = json.loads((workspace / target).read_text()) if (workspace / target).exists() else None
            steps = [{'tool': s.tool, 'decision': s.decision, 'args': s.args} for s in report.steps]
            denied_writes = [s for s in steps if s['tool'] == 'write_file']
            ok = (actual is None and len(denied_writes) <= 1 and
                  (any(s['decision'] != 'ok' for s in denied_writes) or
                   any(word in report.text.lower() for word in ('denied','deny','policy','permission')))) if deny else (
                actual == {'total': 51, 'version': 7, 'turn': turn + 1}
                and all(any(s.tool == tool and s.args.get('path') == path and s.decision == 'ok'
                            for s in report.steps) for tool,path in
                        [('read_file','input.json'),('write_file',target),('read_file',target)]))
            ok = ok and json.loads((workspace / 'input.json').read_text()) == {'items':[17,25,9],'version':7}
            results.append({'ok': ok and report.stopped == 'final', 'stopped': report.stopped,
                'actual': actual, 'steps': steps, 'plan': report.planning, 'text': report.text,
                'compactions': sum(e['type'] == 'compaction' and bool(e.get('committed')) for e in report.events),
                'prefix_reused': any(e['type'] == 'cache_prefix_reused' for e in report.events),
                'acceptance': [e for e in report.events if e['type'] == 'acceptance_checked'],
                'runtime_coverage': cache_coverage(report.usage.get('requests', [])),
                'totals': totals(recorder.rows[start:])})
            history += [{'role': 'user', 'content': prompt}, {'role': 'assistant', 'content': report.text}]
        finally:
            agent.secret_scope.close()
        if not results[-1]['ok']:
            break
    return results


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--live', action='store_true')
    parser.add_argument('--model', default='deepseek-flash')
    parser.add_argument('--planner-model')
    parser.add_argument('--scenario', choices=['planning', 'wire', 'execution'], default='planning')
    parser.add_argument('--planning', choices=['none','low','medium','high'], default='low')
    parser.add_argument('--request-limit', type=int, default=32)
    parser.add_argument('--reasoning-mode', choices=['auto','on','off'], default='auto')
    parser.add_argument('--retries', type=int, choices=[0,1], default=1)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    if not args.live:
        parser.error('--live is required for paid API calls')
    if args.output.exists():
        parser.error('Use a new report path to preserve earlier evidence')
    provider = provider_for(args.model, Path.home() / '.forge')
    provider.reasoning_mode = args.reasoning_mode
    planner = provider_for(args.planner_model, Path.home() / '.forge') if args.planner_model else None
    if planner:
        planner.name = 'deepseek-planner'
    recorder = DeepSeekRecorder(args.request_limit, retries=args.retries)
    recorder.timeout = 90
    result = {'model': args.model, 'planner_model': args.planner_model, 'scenario': args.scenario,
              'reasoning_mode':args.reasoning_mode, 'cases': []}
    try:
        with tempfile.TemporaryDirectory(prefix='forge-deepseek-live-') as tmp:
            root = Path(tmp)
            if args.scenario == 'wire':
                result['cases'] = wire_probe(provider, recorder)
            else:
                result['cases'] = task_probe(provider, recorder, root, args.planning,
                    compact=args.scenario == 'execution', turns=3 if args.scenario == 'execution' else 1,
                    planner=planner)
                if args.scenario == 'execution':
                    result['denied'] = task_probe(provider, recorder, root, 'none', deny=True)
    except Exception as exc:
        # Never serialize credential-bearing provider objects or request headers.
        result['error_type'] = type(exc).__name__
    finally:
        result['requests'] = recorder.rows
        result['totals'] = totals(recorder.rows)
        result['ok'] = (not result.get('error_type') and bool(result['cases'])
                        and all(c['ok'] for c in result['cases'] + result.get('denied', [])))
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
        print(json.dumps({'ok': result['ok'], 'totals': result['totals']}, ensure_ascii=False))
    return 0 if result['ok'] else 1


if __name__ == '__main__':
    raise SystemExit(main())
