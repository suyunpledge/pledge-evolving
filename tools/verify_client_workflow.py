"""Persistent HTTP -> Agent -> Diff -> test -> checkpoint smoke, offline model.

Run with a NEW --output directory. No production workspace or credentials used.
"""
import argparse
import hashlib
import json
from pathlib import Path
import subprocess
import sys
import threading
import time
from unittest.mock import patch
import urllib.request

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from client import server as client
from forge.checkpoint import CheckpointStore
from forge.client_runs import RunStore
from forge.model import Usage
from forge.routing import SmartRouter


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    output = args.output.resolve()
    if output.exists():
        parser.error('--output must be a new directory')
    project, home = output / 'project', output / 'control'
    project.mkdir(parents=True)
    path = project / 'value.py'
    path.write_text('VALUE = 1\n', encoding='utf-8')
    (project / 'test_value.py').write_text(
        'import unittest\nfrom value import VALUE\nclass Tests(unittest.TestCase):\n'
        '    def test_value(self): self.assertEqual(VALUE, 2)\n', encoding='utf-8')
    def command(*values):
        return subprocess.run(values, cwd=project, capture_output=True, text=True,
                              creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
    for values in [('git', 'init', '--quiet'), ('git', 'config', 'user.name', 'Forge Verification'),
                   ('git', 'config', 'user.email', 'forge@localhost'), ('git', 'add', '.'),
                   ('git', 'commit', '--quiet', '-m', 'baseline')]:
        command(*values).check_returncode()

    class Transport:
        def __init__(self):
            self.answers = iter([
                '<tool_call>{"tool":"read_file","args":{"path":"value.py"}}</tool_call>',
                '<tool_call>{"tool":"write_file","args":{"path":"value.py","content":"VALUE = 2\\n"}}</tool_call>',
                'Changed VALUE to 2; run the acceptance test.'])
        def complete(self, provider, model, messages, **options):
            return next(self.answers), Usage(10, 5, provider.name, model)
    transport = Transport()
    factory = SmartRouter.from_config
    store = RunStore(home / 'sessions' / 'client')
    server = client.ClientServer(('127.0.0.1', 0), workspace=project, store=store)
    base = 'http://127.0.0.1:' + str(server.server_address[1])
    def http(route, body=None):
        request = urllib.request.Request(base + route,
            data=json.dumps(body).encode() if body is not None else None,
            headers={'Content-Type': 'application/json'})
        with urllib.request.urlopen(request, timeout=10) as response:
            return json.loads(response.read())
    with patch.object(client, 'FORGE_HOME', home), patch.object(client, 'SECRETS_FILE', home / 'secrets.json'), \
         patch.object(SmartRouter, 'from_config', side_effect=lambda cfg: factory(cfg, transport=transport)):
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            submitted = http('/api/run', {'task': 'Read value.py, set VALUE to 2, then finish.',
                                         'bundle': 'coding', 'policy_mode': 'acceptEdits', 'thinking': 'off'})
            rid = submitted['run_id']
            deadline = time.monotonic() + 30
            while time.monotonic() < deadline:
                rec = http('/api/run/' + rid)
                if rec['state'] in {'done', 'error', 'cancelled'}:
                    break
                time.sleep(.05)
            if rec['state'] != 'done' or rec['outcome'] != 'succeeded':
                raise RuntimeError('Acceptance run did not succeed: ' + json.dumps(rec))
            diff = command('git', 'diff', '--', 'value.py')
            diff.check_returncode()
            if '+VALUE = 2' not in diff.stdout:
                raise RuntimeError('Expected a real Git diff')
            (output / 'diff.patch').write_text(diff.stdout, encoding='utf-8')
            tested = command(sys.executable, '-B', '-m', 'unittest', 'discover', '-v')
            (output / 'tests.log').write_text(tested.stdout + tested.stderr, encoding='utf-8')
            tested.check_returncode()
            actions = [e for e in rec['events'] if e['type'] == 'tool_completed']
            commit = next(e['meta']['checkpoint'] for e in actions if e.get('meta', {}).get('checkpoint'))
            changed = path.read_text(encoding='utf-8')
            if not CheckpointStore(home, project).rollback(commit):
                raise RuntimeError('Checkpoint restore failed')
            restored = path.read_text(encoding='utf-8')
            if restored != 'VALUE = 1\n':
                raise RuntimeError('Restore did not recover original contents')
        finally:
            server.shutdown()
            server.server_close()
            store.close()
    recovered = RunStore(home / 'sessions' / 'client')
    try:
        snapshot = recovered.get(rid)
        evidence = {'run_id': rid, 'task_id': rec['task_id'], 'outcome': rec['outcome'],
            'action_ids': [e['action_id'] for e in actions], 'sessionPath': rec['sessionPath'],
            'checkpoint': commit, 'changed_text': changed, 'restored_text': restored,
            'tests_exit_code': tested.returncode, 'history_recovered': snapshot['outcome'] == 'succeeded',
            'model_transport': 'scripted offline; real Agent, tools, HTTP, Git and subprocess tests',
            'artifacts': {name: {'path': str(output / name),
                'sha256': hashlib.sha256((output / name).read_bytes()).hexdigest()}
                for name in ['diff.patch', 'tests.log']}}
        (output / 'evidence.json').write_text(json.dumps(evidence, ensure_ascii=False, indent=2), encoding='utf-8')
        print(json.dumps(evidence, ensure_ascii=False))
    finally:
        recovered.close()
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
