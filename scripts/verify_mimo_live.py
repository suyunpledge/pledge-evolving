"""MiMo 真实工具/缓存回归，显式 --live；只发送隔离夹具，不读取业务文件。

凭据由用户现有 MiMo provider 行的 id 在 secrets.json 中匹配，不打印或复制。
每次请求保存原始 usage、请求形状摘要与归一结果，写入上限内的 JSON 报告。
"""
from __future__ import annotations

import argparse
import copy
from datetime import datetime, timezone
import hashlib
import io
import json
from pathlib import Path
import sys
import tempfile
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from forge import model, toolwire
from forge.cache_metrics import cache_coverage
from forge.cache_state import CacheWarmth
from forge.loop import Agent, LoopLimits
from forge.policy import Mode, Policy, Sandbox
from forge.secret_http import open_authenticated
from forge.secrets import redact
from forge.tools import build_builtin_registry, ToolSpec, ToolResult


MODELS = ('mimo-v2.6-flash', 'mimo-v2.6-pro')
ADD = {'type': 'function', 'function': {'name': 'add', 'description': 'Add two integers.',
    'parameters': {'type': 'object', 'properties': {'a': {'type': 'integer'},
        'b': {'type': 'integer'}}, 'required': ['a', 'b'], 'additionalProperties': False}}}


def digest(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True).encode()).hexdigest()


def provider_for(chosen, home):
    rows = json.loads((home / 'forge.patch.json').read_text(encoding='utf-8-sig'))
    secrets = json.loads((home / 'secrets.json').read_text(encoding='utf-8-sig'))
    for row in rows:
        config = row.get('config', {})
        if (not row.get('disabled') and config.get('model') == chosen
                and config.get('baseURL') == 'https://token-plan-cn.xiaomimimo.com/v1'):
            key = secrets.get(row.get('id'))
            if not isinstance(key, str) or not key:
                raise ValueError('Configured MiMo credential is missing')
            return model.Provider('mimo-live', config['baseURL'], key, default_model=chosen,
                                  temperature=config.get('temperature'),
                                  cache_affinity=config.get('cacheAffinity','auto'),
                                  reasoning_mode=config.get('reasoningMode', 'auto'))
    raise ValueError('No enabled MiMo Token Plan provider matches this model')


class Recorder(model.HttpTransport):
    def __init__(self, limit, thinking=None, retries=0):
        super().__init__(timeout=60, response_opener=self.open)
        self.limit, self.thinking = limit, thinking
        self.retries = retries
        self.rows, self.sent = [], []
        self.stage = ''
        self.label = ''

    def open(self, request, **kwargs):
        if len(self.rows) >= self.limit:
            raise ValueError('Live request limit reached')
        payload = json.loads(request.data)
        previous = self.sent[-1] if self.sent else None
        messages = payload.get('messages', [])
        common = 0
        for left, right in zip(previous.get('messages', []) if previous else [], messages):
            if left != right:
                break
            common += 1
        row = {'stage': self.stage, 'model': payload['model'], 'url': request.full_url,
            'parameters': {k: payload[k] for k in ('thinking', 'temperature', 'top_p',
                'reasoning_effort', 'tool_choice', 'max_tokens') if k in payload},
            'message_count': len(messages), 'common_prefix_messages': common,
            'messages_digest': digest(messages), 'tools_digest': digest(payload.get('tools')),
            'toolset_digest': digest(sorted(payload.get('tools') or [],
                key=lambda tool: (tool.get('function') or tool).get('name', ''))),
            'payload_digest': digest(payload),
            'message_chars': sum(len(json.dumps(m, ensure_ascii=False)) for m in messages),
            'assistant_reasoning_fields': sum('reasoning_content' in m for m in messages),
            'tools': [t.get('function', {}).get('name') for t in payload.get('tools', [])]}
        self.rows.append(row)
        self.sent.append(copy.deepcopy(payload))
        started = time.monotonic()
        try:
            with open_authenticated(request, **kwargs) as response:
                raw = response.read(2 * 1024 * 1024 + 1)
                row['status'] = response.status
                headers=getattr(response,'headers',{})
                row['upstream_request_id'] = next((redact(str(headers.get(k)))[:160]
                    for k in ('x-request-id','x-tt-logid') if headers.get(k)),None)
            data = json.loads(raw)
            choice = (data.get('choices') or [{}])[0]
            message = choice.get('message') or {}
            row['system_fingerprint'] = (data['system_fingerprint'][:160]
                if isinstance(data.get('system_fingerprint'),str) else None)
            row.update(raw_usage=data.get('usage'), finish_reason=choice.get('finish_reason'),
                response_id=data.get('id'), response_model=data.get('model'),
                reasoning_chars=len(message.get('reasoning_content') or ''),
                tool_calls=message.get('tool_calls'), text=(message.get('content') or '')[:400])
            print(json.dumps({'stage': self.stage, 'status': row['status'],
                'finish_reason': row['finish_reason'], 'usage': row['raw_usage']}, ensure_ascii=False), flush=True)
            return io.BytesIO(raw)
        except Exception as exc:
            row.update(error_type=type(exc).__name__, status=getattr(exc, 'code', None))
            raise
        finally:
            row['seconds'] = round(time.monotonic() - started, 3)

    def complete(self, provider, chosen, messages, **options):
        options.setdefault('max_tokens', 1024)
        if self.thinking is not None:
            options.setdefault('enable_thinking', self.thinking)
        result = super().complete(provider, chosen, messages, **options)
        self.rows[-1]['usage'] = vars(result[1])
        self.rows[-1]['adaptation'] = result[2].get('plan', {})
        return result


def router_for(provider, recorder):
    return model.ModelRouter([provider], transport=recorder, retries_per_provider=recorder.retries,
                             chain=[(provider.name, provider.default_model)])


def wire_probe(provider, recorder):
    router = router_for(provider, recorder)
    results = []
    messages = [{'role': 'system', 'content': 'Use the add tool when requested. Be concise.'},
        {'role': 'user', 'content': 'Call add with a=17,b=25. Do not calculate without the tool.'}]
    for thinking in ((False, True) if recorder.thinking is None else (recorder.thinking,)):
        label = 'on' if thinking else 'off'
        recorder.stage = 'wire-' + label
        options = dict(tools=[ADD], enable_thinking=thinking, max_tokens=1024, tool_choice='auto')
        first = router.complete(messages, **options)
        repeated = router.complete(messages, **options)
        checks = [len(c.tool_calls) == 1 and c.tool_calls[0]['name'] == 'add'
                  and c.tool_calls[0]['args'] == {'a': 17, 'b': 25}
                  and c.finish_reason == 'tool_calls' for c in (first, repeated)]
        if all(checks):
            calls = toolwire.normalize_native_calls(repeated.tool_calls, 'openai')
            replay = [*messages, toolwire.replay_assistant_message(
                repeated.assistant_message, repeated.text, calls, 'openai'),
                {'role': 'tool', 'tool_call_id': calls[0].id, 'content': '42'}]
            final = router.complete(replay, tools=[ADD], enable_thinking=thinking, max_tokens=1024)
            checks.append('42' in final.text and final.finish_reason == 'stop')
        results.append({'thinking': label, 'checks': checks, 'ok': all(checks)})
    return results


def agent_probe(provider, recorder, root, *, development=False, mode_schedule=None):
    workspace = root / provider.default_model
    workspace.mkdir(exist_ok=True)
    # 比较组使用相同工作区；只清理这个隔离夹具内已知的两个输出文件。
    for filename in ('result.json', 'result-next.json'):
        (workspace / filename).unlink(missing_ok=True)
    (workspace / 'catalog.json').write_text(json.dumps({'items': [
        {'id': 'alpha', 'quantity': 17}, {'id': 'beta', 'quantity': 25}]}), encoding='utf-8')
    (workspace / 'next.json').write_text(json.dumps({'items': [
        {'id': 'gamma', 'quantity': 9}, {'id': 'delta', 'quantity': 37}]}), encoding='utf-8')
    (workspace / 'AGENTS.md').write_text(
        'Read input files with read_file. Use write_file for JSON output. Do not use shell. '
        'The output schema is {"total":integer,"ids":[string]}. Preserve the input order. '
        'Only include actual input item ids. Read back the output to verify it. '
        'Treat catalog quantities as integers. Final replies should be one concise sentence.\n',
        encoding='utf-8')
    registry = build_builtin_registry(expose=['read_file', 'write_file', 'list_dir',
        'file_outline', 'read_range', 'grep_files', 'tool_search', 'skill_list'])
    from forge.development.runtime import DEVELOPMENT_GUIDANCE
    agent = Agent(home=root / 'home', workspace=workspace, router=router_for(provider, recorder),
        registry=registry, policy=Policy(workspace=workspace, mode=Mode.ACCEPT_EDITS,
            sandbox=Sandbox.WORKSPACE_WRITE, non_interactive=True),
        limits=LoopLimits(max_steps=7, context_chars=100000), project_rules=True,
        extra_system=DEVELOPMENT_GUIDANCE if development else '')
    results, history = [], []
    initial_mode = provider.reasoning_mode
    try:
        for index, (source, target, total) in enumerate([
                ('catalog.json', 'result.json', 42), ('next.json', 'result-next.json', 46)]):
            if mode_schedule is not None:
                provider.reasoning_mode = mode_schedule[index]
            recorder.stage = recorder.label + f'agent-turn-{index + 1}'
            prompt = (f'Read {source}, calculate the total quantity and ids in order, '
                f'write {target} using the project JSON schema, then read it back. '
                'Use real tools; do not merely describe the work.')
            contract = {'criteria': [{'id': 'total', 'kind': 'env_probe', 'env': 'workspace',
                'probe': 'json_value', 'input': {'path': target, 'pointer': '/total'}, 'expect': total}]}
            report = agent.run(prompt, history=history, acceptance=contract)
            verified = json.loads((workspace / target).read_text(encoding='utf-8')) if (workspace / target).exists() else None
            ids = ['alpha', 'beta'] if index == 0 else ['gamma', 'delta']
            ok = (report.stopped == 'final' and verified == {'total': total, 'ids': ids}
                  and all(any(s.tool == 'read_file' and s.args.get('path') == path
                              for s in report.steps) for path in (source, target))
                  and any(s.tool == 'write_file' and s.args.get('path') == target for s in report.steps))
            results.append({'turn': index + 1, 'ok': ok, 'stopped': report.stopped,
                'output': verified, 'tools': [{'tool': s.tool, 'path': s.args.get('path'),
                                             'decision': s.decision} for s in report.steps],
                'failure_detail': report.text[:500] if not ok else '',
                'prefix_reused': any(e['type'] == 'cache_prefix_reused' for e in report.events),
                'envelope_reused': any(e['type'] == 'run_envelope_reused' for e in report.events),
                'acceptance': [e for e in report.events if e['type'] == 'acceptance_checked'],
                'coverage': cache_coverage(report.usage.get('requests', []))})
            history += [{'role': 'user', 'content': prompt}, {'role': 'assistant', 'content': report.text}]
            if not ok:
                break
    finally:
        provider.reasoning_mode = initial_mode
        agent.secret_scope.close()
    return results


def compare_reasoning(provider, recorder, root):
    """同一工作区 ABBA；只改变 Provider 默认模式，不在 transport 偷塞开关。"""
    samples = []
    if recorder.thinking is not None:
        raise ValueError('Comparison requires --thinking auto; uses real provider defaults')
    initial = provider.reasoning_mode
    try:
        for index, mode in enumerate(('on', 'off', 'off', 'on')):
            provider.reasoning_mode = mode
            recorder.label = f'compare-{index + 1}-{mode}/'
            start = len(recorder.rows)
            cases = agent_probe(provider, recorder, root, development=True)
            rows = recorder.rows[start:]
            successful = [r for r in rows if r.get('usage')]
            samples.append({'sample': index + 1, 'reasoning_mode': mode,
                'ok': len(cases) == 2 and all(c['ok'] for c in cases), 'cases': cases,
                'requests': len(rows), 'failed_attempts': len(rows) - len(successful),
                'input_tokens': sum(r['usage']['prompt_tokens'] for r in successful),
                'output_tokens': sum(r['usage']['completion_tokens'] for r in successful),
                'reasoning_tokens': sum(r['usage']['reasoning_tokens'] for r in successful),
                'seconds': round(sum(r['seconds'] for r in rows), 3),
                'coverage': cache_coverage(successful)})
            if not samples[-1]['ok']:
                break
    finally:
        provider.reasoning_mode = initial
        recorder.label = ''
    return samples


def compare_tool_order(provider, recorder, root):
    """相同工具集、相同任务的 ABBA，比较发现新工具后的排序方式。"""
    workspace = root / 'tool-order'
    workspace.mkdir(exist_ok=True)
    samples = []

    class AlphabeticalBaseline:
        def project(self, specs, wire):
            return toolwire.tool_declarations(specs, wire)

    for index, strategy in enumerate(('sorted', 'append', 'append', 'sorted')):
        observed = []
        def total(args, ctx):
            observed.append(copy.deepcopy(args))
            return ToolResult(True, str(args['a'] + args['b']))
        registry = build_builtin_registry(expose=['read_file', 'write_file', 'list_dir',
            'file_outline', 'read_range', 'grep', 'tool_search', 'skill_list', 'aaa_total'])
        registry.register(ToolSpec('aaa_total', 'Add the supplied integers a and b. Discover this optional tool first.',
            total, deferred=True, schema={'a': {'type': 'integer'}, 'b': {'type': 'integer'}}), trusted=True)
        agent = Agent(home=root / 'home', workspace=workspace, router=router_for(provider, recorder),
            registry=registry, policy=Policy(workspace=workspace, mode=Mode.READ_ONLY, non_interactive=True),
            limits=LoopLimits(max_steps=5, context_chars=100000), project_rules=False)
        if strategy == 'sorted':
            agent._tool_surface = AlphabeticalBaseline()
        recorder.stage = f'tool-order-{index + 1}-{strategy}'
        start = len(recorder.rows)
        try:
            report = agent.run('First call tool_search with query aaa_total and limit 1 to discover the optional tool. '
                'Then call aaa_total with a=17,b=25 exactly once. Finally return only the tool result. '
                'Do not calculate without calling aaa_total. Do not use any other tool.')
            rows = recorder.rows[start:]
            good = [row for row in rows if row.get('usage')]
            ok = (report.stopped == 'final' and observed == [{'a': 17, 'b': 25}]
                and [s.tool for s in report.steps] == ['tool_search', 'aaa_total']
                and all(s.decision == 'ok' for s in report.steps) and report.text.strip() == '42')
            activation = next((row for row in good if 'aaa_total' in row['tools']), None)
            samples.append({'sample': index + 1, 'strategy': strategy, 'ok': ok,
                'tool_calls': [s.tool for s in report.steps], 'observed': observed,
                'failure_detail': report.text[:500] if not ok else '', 'requests': len(rows),
                'seconds': round(sum(row['seconds'] for row in rows), 3),
                'coverage': cache_coverage(good),
                'after_activation': ({'usage': activation['usage'], 'tools': activation['tools'],
                    'common_prefix_messages': activation['common_prefix_messages']} if activation else None)})
        finally:
            agent.secret_scope.close()
        if not samples[-1]['ok']:
            break
    return samples


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--live', action='store_true')
    parser.add_argument('--model', choices=MODELS, action='append')
    parser.add_argument('--suite', choices=('wire', 'agent', 'compare', 'switch', 'tool-order'), required=True)
    parser.add_argument('--thinking', choices=('auto', 'on', 'off'), default='auto')
    parser.add_argument('--request-limit', type=int, default=24)
    parser.add_argument('--retries', type=int, choices=(0, 1), default=0)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    if not args.live or not 1 <= args.request_limit <= 48:
        parser.error('Requires --live and request-limit in 1..48')
    if args.suite == 'compare' and (args.thinking != 'auto' or len(args.model or MODELS) != 1):
        parser.error('Comparison requires one --model and --thinking auto')
    if args.suite == 'switch' and args.thinking != 'auto':
        parser.error('Mode switch uses provider settings; requires --thinking auto')
    selected = list(dict.fromkeys(args.model or MODELS))
    results = {'mode': 'live', 'suite': args.suite, 'thinking': args.thinking, 'selected_models': selected,
        'started_utc': datetime.now(timezone.utc).isoformat(), 'models': []}
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='forge-mimo-live-') as tmp:
        root = Path(tmp)
        model._WARMTH = CacheWarmth(root / 'warmth.json')
        remaining = args.request_limit
        for chosen in selected:
            recorder = Recorder(remaining, {'auto': None, 'on': True, 'off': False}[args.thinking], args.retries)
            row = {'model': chosen, 'requests': recorder.rows}
            results['models'].append(row)
            try:
                provider = provider_for(chosen, Path.home() / '.forge')
                if args.suite == 'compare':
                    row['results'] = compare_reasoning(provider, recorder, root)
                elif args.suite == 'switch':
                    row['results'] = agent_probe(provider, recorder, root, development=True, mode_schedule=('off', 'on'))
                elif args.suite == 'tool-order':
                    row['results'] = compare_tool_order(provider, recorder, root)
                else:
                    row['results'] = (wire_probe(provider, recorder) if args.suite == 'wire'
                                      else agent_probe(provider, recorder, root))
                row['ok'] = bool(row['results']) and all(r['ok'] for r in row['results'])
            except Exception as exc:
                row.update(ok=False, error_type=type(exc).__name__, error_detail=redact(str(exc))[:500])
            remaining -= len(recorder.rows)
            args.output.write_text(json.dumps(redact(results), ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
            successful = [r for r in recorder.rows if r.get('usage')]
            print(json.dumps({'model': chosen, 'ok': row['ok'], 'requests': len(recorder.rows),
                'error': row.get('error_detail'), 'failed_attempts': len(recorder.rows) - len(successful),
                'coverage': cache_coverage(successful)}, ensure_ascii=False), flush=True)
            if remaining <= 0:
                break
    results['all_requested_models_attempted'] = len(results['models']) == len(selected)
    results['ok'] = results['all_requested_models_attempted'] and all(row['ok'] for row in results['models'])
    args.output.write_text(json.dumps(redact(results), ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    return 0 if results['ok'] else 1


if __name__ == '__main__':
    raise SystemExit(main())
