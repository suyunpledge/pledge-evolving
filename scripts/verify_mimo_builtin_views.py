"""Paid MiMo A/B: actual builtin searches, disk continuation, compaction and omitted-evidence lookup."""
import argparse
import copy
import json
from pathlib import Path
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from verify_mimo_live import MODELS, provider_for, router_for
from verify_mimo_long_context import LongRecorder, final_value
from forge import model
from forge.cache_metrics import cache_coverage
from forge.cache_state import CacheWarmth
from forge.compaction import ContextBudget
from forge.context_transaction import digest, valid_tool_pairs
from forge.loop import Agent, LoopLimits
from forge.policy import Mode, Policy, Sandbox
from forge.secrets import redact
from forge.tools import build_builtin_registry


TOOLS = ['list_dir', 'find_files', 'file_outline', 'grep', 'read_range',
         'read_file', 'write_file', 'artifact_read', 'artifact_grep']
VALUES = {'alpha': 'RELEASE-ALPHA-781', 'beta': 'RELEASE-BETA-926'}


def fixture(workspace):
    workspace.mkdir(exist_ok=True)
    for folder, value in VALUES.items():
        directory = workspace / folder
        directory.mkdir(exist_ok=True)
        for i in range(71):
            (directory / f'inventory_record_{i:03}.txt').write_text(f'inventory record {i}\n', encoding='utf-8')
        lines = []
        for i in range(90):
            lines += [f'def component_inventory_rule_{i:03}():',
                      f'    # AUDIT_CONTROL {folder.upper()}-CODE-{i:03}-VERIFIED' if i < 40 else '    # inventory rule',
                      f'    return {i}', '']
        lines += ['def release_marker():', f'    return "{value}"', '']
        (directory / 'module.py').write_text('\n'.join(lines), encoding='utf-8')
    for name in ('alpha-result.json', 'beta-result.json', 'lookup-result.json'):
        (workspace / name).unlink(missing_ok=True)


def history_fixture():
    history = []
    for i in range(6):
        history += [{'role': 'user', 'content': f'Prior warehouse audit {i}: record closed checks.'},
                    {'role': 'assistant', 'content': '\n'.join(
                        f'Closed check {i}-{j:03}: historical inventory snapshot was reviewed; '
                        'these records are context only, not current release evidence.' for j in range(16))}]
    return history


def run_case(provider, recorder, root, strategy, output):
    workspace = root / 'workspace'
    fixture(workspace)
    source_digest = digest({str(p.relative_to(workspace)): p.read_text(encoding='utf-8')
                            for p in workspace.rglob('*') if p.is_file()})
    home = root / strategy
    history = history_fixture()
    start = len(recorder.rows)
    turns, all_steps, previous_last = [], [], None
    for turn, folder in enumerate(('alpha', 'beta', 'lookup'), 1):
        recorder.stage = f'builtin-{strategy}-turn-{turn}'
        registry = build_builtin_registry(expose=TOOLS, result_views=strategy == 'improved')
        agent = Agent(home=home, workspace=workspace, router=router_for(provider, recorder), registry=registry,
            policy=Policy(workspace=workspace, mode=Mode.ACCEPT_EDITS, sandbox=Sandbox.WORKSPACE_WRITE,
                          non_interactive=True), project_rules=False,
            limits=LoopLimits(max_steps=15, context_chars=9000))
        agent.compactor = ContextBudget(max_chars=9000, keep_tail=4)
        if folder != 'lookup':
            expected = {'folder': folder, 'release': VALUES[folder]}
            filename = folder + '-result.json'
            prompt = (f'Continue the warehouse code audit for {folder} only. First inspect {folder} with list_dir, '
                f'find_files(pattern="*.txt",root="{folder}"), file_outline(path="{folder}/module.py"), and '
                f'grep(pattern="AUDIT_CONTROL",root="{folder}"). All four discovery tools are required. '
                'Then locate release_marker and read its actual return value using a targeted search or line read. '
                f'Write {filename} containing exactly {{"folder":"{folder}","release":<actual value>}}. '
                'Read that saved file to verify it. Do not change source or inventory files. '
                'Tools can be batched. Finish with only the saved JSON value.')
        else:
            expected = {'folder': 'beta', 'audit_code': 'BETA-CODE-025-VERIFIED'}
            filename = 'lookup-result.json'
            prompt = ('Recheck AUDIT_CONTROL record index 025 in beta from the earlier broad grep result. '
                'When that output was retained, retrieve it with artifact_grep or artifact_read; use the retained '
                'references in prior tool output or the compaction summary. Otherwise search beta/module.py. '
                'Find its actual audit code, not a code from the short preview. '
                'Write lookup-result.json containing exactly {"folder":"beta","audit_code":<actual code>}. '
                'Read the saved file to verify it. Finish with only the saved JSON value.')
        contract = {'criteria': [
            {'id': key, 'kind': 'env_probe', 'env': 'workspace', 'probe': 'json_value',
             'input': {'path': filename, 'pointer': '/' + key}, 'expect': value}
            for key, value in expected.items()]}
        sent_start = len(recorder.sent)
        try:
            report = agent.run(prompt, history=history, acceptance=contract)
            value, fmt, strict = final_value(report.text)
            actual = json.loads((workspace / filename).read_text(encoding='utf-8')) if (workspace / filename).exists() else None
            discovery_ok = folder == 'lookup' or all(any(s.tool == name and all(
                s.args.get(k, s.args.get('path') if k == 'root' else None) == v for k, v in args.items())
                for s in report.steps) for name, args in [
                    ('list_dir', {'path': folder}), ('find_files', {'root': folder, 'pattern': '*.txt'}),
                    ('file_outline', {'path': folder+'/module.py'}), ('grep', {'root': folder, 'pattern': 'AUDIT_CONTROL'})])
            verification = any(s.tool == 'read_file' and s.args.get('path') == filename for s in report.steps)
            clean = all(s.decision == 'ok' for s in report.steps)
            ok = report.stopped == 'final' and value == expected and actual == expected and discovery_ok and verification
            compactions = [e for e in report.events if e['type'] == 'compaction' and e.get('committed')]
            for event in compactions:
                archive = json.loads(Path(event['archive']['path']).read_text(encoding='utf-8'))
                assert valid_tool_pairs(archive['messages']) and digest(archive['messages']) == archive['archive_digest']
                directory = output.parent / (output.stem + '-archives')
                directory.mkdir(parents=True, exist_ok=True)
                path = directory / (archive['archive_id'] + '.json')
                path.write_text(json.dumps(archive, ensure_ascii=False, indent=2)+'\n', encoding='utf-8')
                event['archive']['export_path'] = str(path.resolve())
            steps = []
            for step in report.steps:
                row = {'tool': step.tool, 'args': step.args, 'decision': step.decision,
                       'result': step.result, 'meta': step.meta}
                if step.meta.get('artifact'):
                    identifier = step.meta['artifact']['id']
                    raw, _ = agent.artifacts._load(identifier)
                    directory = output.parent / (output.stem + '-artifacts')
                    directory.mkdir(parents=True, exist_ok=True)
                    path = directory / (identifier + '.txt')
                    path.write_bytes(raw.encode('utf-8'))
                    row['artifact_export'] = str(path.resolve())
                steps.append(row)
            all_steps.extend(steps)
            assert all(valid_tool_pairs(payload['messages']) for payload in recorder.sent[sent_start:])
            first_messages = recorder.sent[sent_start]['messages'] if len(recorder.sent) > sent_start else []
            continued = previous_last is not None and first_messages[:len(previous_last)] == previous_last
            turns.append({'turn': turn, 'ok': ok, 'clean_execution': clean, 'strict_format': strict,
                'format': fmt, 'final': value, 'stopped': report.stopped, 'tools': steps,
                'compactions': compactions, 'cache_prefix_reused': any(e['type'] == 'cache_prefix_reused' for e in report.events),
                'exact_continuation_of_last_request': continued,
                'acceptance': [e for e in report.events if e['type'] == 'acceptance_checked']})
            previous_last = copy.deepcopy(recorder.sent[-1]['messages'])
            history += [{'role': 'user', 'content': prompt}, {'role': 'assistant', 'content': report.text}]
            print(json.dumps({'strategy': strategy, 'turn': turn, 'ok': ok,
                              'compactions': len(compactions), 'tools': len(steps)}, ensure_ascii=False), flush=True)
            if not ok:
                break
        finally:
            agent.secret_scope.close()
    requests = recorder.rows[start:]
    good = [r for r in requests if r.get('usage')]
    native_reads = [s for s in all_steps if s['tool'] in {'artifact_read', 'artifact_grep'}]
    source_unchanged = digest({str(p.relative_to(workspace)): p.read_text(encoding='utf-8')
        for directory in VALUES for p in (workspace / directory).rglob('*') if p.is_file()}) == source_digest
    observed = any('BETA-CODE-025-VERIFIED' in s['result'] for s in native_reads)
    return {'strategy': strategy, 'ok': len(turns) == 3 and all(t['ok'] for t in turns)
            and source_unchanged and (strategy != 'improved' or observed), 'turns': turns,
        'clean_execution': all(t['clean_execution'] for t in turns),
        'strict_format': all(t['strict_format'] for t in turns),
        'input_tokens': sum(r['usage']['prompt_tokens'] for r in good),
        'output_tokens': sum(r['usage']['completion_tokens'] for r in good),
        'coverage': cache_coverage(good), 'successful_requests': len(good), 'attempts': len(requests),
        'initial_payload_digest': requests[0]['payload_digest'] if requests else None,
        'committed_compactions': sum(len(t['compactions']) for t in turns),
        'source_unchanged': source_unchanged, 'omitted_record_observed': observed}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--live', action='store_true')
    parser.add_argument('--model', choices=MODELS, default=MODELS[0])
    parser.add_argument('--strategies', choices=('baseline', 'improved'), nargs='+', default=['baseline', 'improved'])
    parser.add_argument('--request-limit', type=int, default=48)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    if not args.live or not 1 <= args.request_limit <= 64:
        parser.error('Requires --live and request-limit 1..64')
    provider = provider_for(args.model, Path.home() / '.forge')
    provider.route_capabilities = {'contextWindow': 16000, 'maxOutputTokens': 4096}
    recorder = LongRecorder(args.request_limit, retries=1)
    result = {'model': args.model, 'samples': [], 'requests': recorder.rows,
              'local_context_window': 16000, 'context_chars': 9000,
              'comparison': 'Same current builtin handlers and schemas; result_views disabled/enabled; fresh Agent each user turn'}
    args.output.parent.mkdir(parents=True, exist_ok=True)
    try:
        with tempfile.TemporaryDirectory(prefix='forge-builtin-live-') as temporary:
            root = Path(temporary)
            model._WARMTH = CacheWarmth(root / 'warmth.json')
            for strategy in args.strategies:
                result['samples'].append(run_case(provider, recorder, root, strategy, args.output))
                args.output.write_text(json.dumps(redact(result), ensure_ascii=False, indent=2)+'\n', encoding='utf-8')
    except Exception as exc:
        result['error'] = redact(type(exc).__name__ + ': ' + str(exc))[:500]
    result['identical_initial_payloads'] = len(result['samples']) > 1 and len({s['initial_payload_digest'] for s in result['samples']}) == 1
    result['ok'] = len(result['samples']) == len(args.strategies) and all(s['ok'] for s in result['samples'])
    args.output.write_text(json.dumps(redact(result), ensure_ascii=False, indent=2)+'\n', encoding='utf-8')
    print(json.dumps({'ok': result['ok'], 'attempts': len(recorder.rows)}, ensure_ascii=False))
    return 0 if result['ok'] else 1


if __name__ == '__main__':
    raise SystemExit(main())
