"""Real Agent/files/HTTP; scripted transport, no paid model or external network."""
from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch
import urllib.error
import urllib.request

from client import server as client
from forge.client_runs import RunStore
from forge.execution import RunRequest
from forge.loop import Agent, LoopLimits, build_agent
from forge.model import Usage
from forge.policy import Policy, Mode, ToolEffect
from forge.routing import SmartRouter
from forge.session import Session
from forge.test_task_planning import Router
from forge.tools import build_builtin_registry, ToolSpec


class ClientRuntimeTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name).resolve()
        self.ws = self.root / 'project'
        self.ws.mkdir()
        self.home = self.root / 'home'
        self.store = RunStore(self.home / 'sessions' / 'client')
        self.addCleanup(self.store.close)
        self.addCleanup(patch.stopall)
        patch.object(client, 'FORGE_HOME', self.home).start()
        patch.object(client, 'SECRETS_FILE', self.home / 'secrets.json').start()
        self.calls = []

    def transport(self, answers, *, blocked=None, release=None, block_call=1):
        owner = self
        class Transport:
            def __init__(self):
                self.answers = iter(answers)
                self.count = 0
            def complete(self, provider, model, messages, **options):
                self.count += 1
                owner.calls.append((model, messages))
                if blocked is not None and self.count == block_call:
                    blocked.set()
                    if not release.wait(15):
                        raise RuntimeError('test transport gate timed out')
                return next(self.answers), Usage(10, 5, 'offline', model)
        return Transport()

    def use_transport(self, transport):
        original = SmartRouter.from_config
        patch.object(SmartRouter, 'from_config', side_effect=lambda cfg: original(cfg, transport=transport)).start()
        original_build = build_agent
        def build(**kwargs):
            kwargs['mount_contrib'] = False
            return original_build(**kwargs)
        patch('forge.loop.build_agent', side_effect=build).start()

    def run_task(self, *, task='change value', **options):
        rec = self.store.create(task)
        self.worker(rec['id'], task=task, **options)
        return self.store.get(rec['id'])

    def worker(self, rid, *, task='change value', **options):
        client.run_forge(rid, task, options.get('model'), options.get('strategy'),
            options.get('thinking'), options.get('bundle'), options.get('policy_mode'),
            options.get('max_steps'), options.get('max_depth'), options.get('spawn_budget'),
            self.ws, store=self.store)

    def server(self):
        srv = client.ClientServer(('127.0.0.1', 0), workspace=self.ws, store=self.store)
        thread = threading.Thread(target=srv.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(srv.server_close)
        self.addCleanup(srv.shutdown)
        return 'http://127.0.0.1:' + str(srv.server_address[1])

    def http(self, base, path, payload=None, *, raw=False, headers=None):
        req = urllib.request.Request(base + path,
            data=json.dumps(payload).encode() if payload is not None else None,
            headers={'Content-Type': 'application/json', **(headers or {})})
        try:
            response = urllib.request.urlopen(req, timeout=10)
        except urllib.error.HTTPError as exc:
            response = exc
        with response:
            body = response.read().decode()
            return response.status, body if raw else json.loads(body)

    def wait_terminal(self, rid):
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            rec = self.store.get(rid)
            if rec['state'] in {'done', 'error', 'cancelled'}:
                return rec
            time.sleep(.02)
        self.fail('worker did not reach a terminal state')

    def test_config_options_compose_without_overwriting_each_other(self):
        cfg = client.compose_run_config(self.ws, client.CATALOG[1]['label'], 'economy', 'on', 'coding', 'read-only')
        self.assertEqual(cfg.get('model', 'primary'), ['deepseek', client.CATALOG[1]['model']])
        self.assertEqual(cfg.get('model', 'routing')['strategy'], 'economy')
        self.assertTrue(cfg.get('model', 'routing')['tiers'])
        self.assertTrue(cfg.get('model', 'fallback'))
        self.assertEqual(cfg.get('thinking', 'mode'), 'on')
        self.assertEqual(cfg.get('policy', 'mode'), 'read-only')

    def test_base_and_coding_are_selectors_not_paths(self):
        for mode in ('base', 'coding'):
            cfg = client.compose_run_config(self.ws, bundle=mode)
            self.assertIsNotNone(cfg.row('policy'))
        self.assertEqual(client.compose_run_config(self.ws, bundle='coding').get('policy', 'mode'), 'acceptEdits')

    def test_invalid_controls_fail_before_creating_records(self):
        base = self.server()
        for value in [[], {'task': {}}, {'task': 'x', 'thinking': 'maybe'},
                      {'task': 'x', 'policy_mode': 'readonly'}, {'task': 'x', 'model': 'missing'},
                      {'task': 'x', 'bundle': '../base.json'}, {'task': 'x', 'max_steps': True},
                      {'task': 'x', 'max_depth': -1}, {'task': 'x', 'workspace': str(self.root)}]:
            with self.subTest(payload=value):
                status, _ = self.http(base, '/api/run', value)
                self.assertEqual(status, 400)
        self.assertEqual(self.store.list_recent(), [])

    def test_selected_model_is_the_actual_transport_model_even_with_strategy(self):
        self.use_transport(self.transport(['finished']))
        rec = self.run_task(model=client.CATALOG[1]['label'], strategy='balanced')
        self.assertEqual(rec['state'], 'done', rec['error'])
        self.assertEqual(self.calls[0][0], client.CATALOG[1]['model'])
        self.assertEqual(rec['routing_mode'], 'explicit')

    def test_read_only_request_cannot_write(self):
        path = self.ws / 'value.py'
        path.write_text('VALUE = 1\n')
        self.use_transport(self.transport([
            '<tool_call>{"tool":"write_file","args":{"path":"value.py","content":"VALUE = 2\\n"}}</tool_call>', 'finished']))
        rec = self.run_task(policy_mode='read-only')
        self.assertEqual(path.read_text(), 'VALUE = 1\n')
        actions = [e for e in rec['events'] if e['type'] == 'tool_completed']
        self.assertEqual(actions[0]['authorization'], 'deny')

    def test_nonfinal_max_steps_is_not_reported_as_success(self):
        (self.ws / 'value.py').write_text('VALUE = 1\n')
        self.use_transport(self.transport(['<tool_call>{"tool":"read_file","args":{"path":"value.py"}}</tool_call>']))
        rec = self.run_task(max_steps=1)
        self.assertEqual((rec['state'], rec['outcome'], rec['error_code']), ('error', 'failed', 'max_steps'))

    def test_queued_cancel_does_not_construct_agent(self):
        rec = self.store.create('queued')
        self.store.request_cancel(rec['id'])
        with patch('forge.loop.build_agent') as builder:
            self.worker(rec['id'])
        self.assertFalse(builder.called)
        result = self.store.get(rec['id'])
        self.assertEqual((result['state'], result['execution_state']), ('cancelled', 'not_started'))

    def test_http_cancel_reaches_running_agent_before_tool_dispatch(self):
        blocked, release = threading.Event(), threading.Event()
        self.addCleanup(release.set)
        self.use_transport(self.transport([
            '<tool_call>{"tool":"write_file","args":{"path":"never.txt","content":"unsafe"}}</tool_call>'],
            blocked=blocked, release=release))
        base = self.server()
        self.addCleanup(release.set)
        _, response = self.http(base, '/api/run', {'task': 'cancel me', 'policy_mode': 'acceptEdits'})
        rid = response['run_id']
        self.assertTrue(blocked.wait(10))
        status, acknowledgement = self.http(base, f'/api/run/{rid}/cancel', {})
        self.assertEqual(status, 200)
        self.assertTrue(acknowledgement['cancel_requested'])
        self.assertEqual(self.store.get(rid)['state'], 'cancelling')
        release.set()
        rec = self.wait_terminal(rid)
        self.assertEqual((rec['state'], rec['outcome']), ('cancelled', 'cancelled'))
        self.assertFalse((self.ws / 'never.txt').exists())

    def test_step_is_queryable_while_next_model_call_is_blocked(self):
        (self.ws / 'value.py').write_text('VALUE = 1\n')
        blocked, release = threading.Event(), threading.Event()
        self.addCleanup(release.set)
        self.use_transport(self.transport([
            '<tool_call>{"tool":"read_file","args":{"path":"value.py"}}</tool_call>', 'finished'],
            blocked=blocked, release=release, block_call=2))
        rec = self.store.create('live step')
        thread = threading.Thread(target=self.worker, args=(rec['id'],), daemon=True)
        thread.start()
        self.assertTrue(blocked.wait(10))
        live = self.store.get(rec['id'])
        self.assertEqual(live['state'], 'running')
        self.assertEqual(len(live['steps']), 1)
        self.assertTrue(live['steps'][0]['action_id'])
        release.set()
        thread.join(10)
        final = self.store.get(rec['id'])
        self.assertEqual(len(final['steps']), 1)
        for event in final['events']:
            self.assertEqual(event['run_id'], rec['id'])
            self.assertEqual(event['task_id'], rec['task_id'])

    def test_completed_task_cannot_be_reexecuted(self):
        self.use_transport(self.transport(['finished']))
        rec = self.run_task()
        with patch('forge.loop.build_agent') as builder:
            self.worker(rec['id'])
        self.assertFalse(builder.called)

    def test_sse_resume_and_json_cursor_use_same_stable_stream(self):
        self.use_transport(self.transport(['finished']))
        rec = self.run_task()
        base = self.server()
        cursor = rec['events'][0]['cursor']
        status, page = self.http(base, f'/api/run/{rec["id"]}/events?format=json&after={cursor}')
        self.assertEqual(status, 200)
        status, text = self.http(base, f'/api/run/{rec["id"]}/events', raw=True,
                                 headers={'Last-Event-ID': str(cursor)})
        events = [json.loads(line[6:]) for line in text.splitlines() if line.startswith('data: ')]
        streamed = [e for e in events if 'cursor' in e]
        self.assertEqual([e['event_id'] for e in streamed], [e['event_id'] for e in page['events']])
        self.assertTrue(all(e['cursor'] > cursor for e in streamed))
        self.assertEqual(events[-1]['type'], 'terminal')
        self.assertIn('id: ', text)
        self.assertEqual(self.http(base, f'/api/run/{rec["id"]}/events?after=-1')[0], 400)

    def test_missing_run_sse_returns_http_404(self):
        self.assertEqual(self.http(self.server(), '/api/run/' + '0' * 32 + '/events')[0], 404)

    def test_configured_workspace_is_used_for_file_preview(self):
        (self.ws / 'visible.txt').write_text('configured project')
        status, rec = self.http(self.server(), '/api/file?path=visible.txt')
        self.assertEqual((status, rec['text']), (200, 'configured project'))

    def test_completed_record_and_event_identity_survive_restart(self):
        self.use_transport(self.transport(['finished']))
        rec = self.run_task()
        self.store.close()
        recovered = RunStore(self.store.directory)
        self.addCleanup(recovered.close)
        self.assertEqual(recovered.get(rec['id']), rec)
        self.assertEqual(recovered.list_recent()[0]['task_id'], rec['task_id'])

    def test_unfinished_restart_is_unknown_and_never_replayed(self):
        rec = self.store.create('interrupted')
        self.store.transition(rec['id'], state='running', execution_state='running')
        self.store.close()
        recovered = RunStore(self.store.directory)
        self.addCleanup(recovered.close)
        snapshot = recovered.get(rec['id'])
        self.assertEqual((snapshot['state'], snapshot['outcome']), ('error', 'unknown'))
        self.assertTrue(snapshot['requires_inspection'])
        self.assertIsNone(recovered.claim(rec['id']))
        self.store = recovered
        status, text = self.http(self.server(), f'/api/run/{rec["id"]}/events', raw=True)
        self.assertEqual(status, 200)
        self.assertIn('execution_detached', text)

    def test_partial_write_failure_remains_unknown_despite_final_model_text(self):
        self.use_transport(self.transport([
            '<tool_call>{"tool":"partial_write","args":{}}</tool_call>', 'finished']))
        previous = __import__('forge.loop', fromlist=['build_agent']).build_agent
        def build(**kwargs):
            agent = previous(**kwargs)
            def handler(args, ctx):
                (ctx.workspace / 'partial.txt').write_text('partial side effect')
                raise RuntimeError('handler interrupted after writing')
            agent.registry.register(ToolSpec('partial_write', 'verified test writer', handler),
                                    trusted=True, effect=ToolEffect.WRITE)
            return agent
        with patch('forge.loop.build_agent', side_effect=build):
            rec = self.run_task(policy_mode='acceptEdits')
        self.assertTrue((self.ws / 'partial.txt').exists(), rec['error'])
        self.assertEqual((rec['state'], rec['outcome'], rec['execution_state']), ('error', 'unknown', 'unknown'))
        self.assertTrue(rec['requires_inspection'])

    def test_real_file_diff_test_and_checkpoint_restore(self):
        from forge.checkpoint import CheckpointStore
        def git(*args):
            return subprocess.run(['git', *args], cwd=self.ws, check=True, capture_output=True,
                                  text=True, creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0)).stdout
        git('init', '--quiet')
        git('config', 'user.name', 'Forge Test')
        git('config', 'user.email', 'forge-test@localhost')
        (self.ws / 'value.py').write_text('VALUE = 1\n')
        (self.ws / 'test_value.py').write_text(
            'import unittest\nfrom value import VALUE\nclass Tests(unittest.TestCase):\n'
            '    def test_value(self): self.assertEqual(VALUE, 2)\n')
        git('add', '.')
        git('commit', '--quiet', '-m', 'baseline')
        self.use_transport(self.transport([
            '<tool_call>{"tool":"read_file","args":{"path":"value.py"}}</tool_call>',
            '<tool_call>{"tool":"write_file","args":{"path":"value.py","content":"VALUE = 2\\n"}}</tool_call>', 'finished']))
        rec = self.run_task(policy_mode='acceptEdits')
        self.assertEqual(rec['state'], 'done', rec['error'])
        diff = git('diff', '--', 'value.py')
        self.assertIn('+VALUE = 2', diff)
        result = subprocess.run([sys.executable, '-B', '-m', 'unittest', 'discover'], cwd=self.ws,
            capture_output=True, text=True, creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('Ran 1 test', result.stderr)
        checkpoints = [e.get('meta', {}).get('checkpoint') for e in rec['events'] if e['type'] == 'tool_completed']
        commit = next(c for c in checkpoints if c)
        checkpoint = CheckpointStore(self.home, self.ws)
        self.assertTrue(checkpoint.rollback(commit))
        self.assertEqual((self.ws / 'value.py').read_text(), 'VALUE = 1\n')
        self.store.close()
        restored = RunStore(self.store.directory)
        self.addCleanup(restored.close)
        self.assertEqual(restored.get(rec['id'])['outcome'], 'succeeded')

    def test_two_real_workers_cancel_only_the_selected_task(self):
        blocked, release = threading.Event(), threading.Event()
        self.addCleanup(release.set)
        self.use_transport(self.transport(['finished'], blocked=blocked, release=release))
        first = self.store.create('first')
        thread = threading.Thread(target=self.worker, args=(first['id'],), daemon=True)
        thread.start()
        self.assertTrue(blocked.wait(10))
        # The second worker gets its own transport and Agent; no selected-panel state.
        other = self.transport(['second completed'])
        from forge.model import ModelRouter
        def second_router(cfg):
            return ModelRouter.from_config(cfg, transport=other)
        with patch.object(SmartRouter, 'from_config', side_effect=second_router):
            second = self.run_task(task='second')
        self.assertEqual(second['state'], 'done', second['error'])
        self.store.request_cancel(first['id'])
        release.set()
        thread.join(10)
        self.assertFalse(thread.is_alive())
        self.assertEqual(self.store.get(first['id'])['outcome'], 'cancelled')
        self.assertEqual(self.store.get(second['id'])['outcome'], 'succeeded')

    def test_separate_processes_keep_durable_runs_isolated(self):
        script = '''
from forge.client_runs import RunStore
import sys
s = RunStore(sys.argv[1])
r = s.create(sys.argv[2])
for i in range(5): s.push_event(r['id'], {'type': 'tick', 'owner': sys.argv[2]})
s.transition(r['id'], state='done', outcome='succeeded')
s.close()
'''
        processes = [subprocess.Popen([sys.executable, '-B', '-c', script,
            str(self.store.directory), str(i)], cwd=Path(__file__).resolve().parents[1],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0)) for i in range(3)]
        for process in processes:
            stdout, stderr = process.communicate(timeout=30)
            self.assertEqual(process.returncode, 0, stderr.decode(errors='replace'))
        recovered = RunStore(self.store.directory)
        self.addCleanup(recovered.close)
        records = recovered.list_recent()
        self.assertEqual(len(records), 3)
        self.assertEqual(len({r['run_id'] for r in records}), 3)
        for record in records:
            self.assertEqual(record['state'], 'done')
            ticks = [e for e in record['events'] if e['type'] == 'tick']
            self.assertEqual([e['owner'] for e in ticks], [record['task']] * 5)

    def test_truncated_projection_tail_preserves_committed_history(self):
        rec = self.store.create('finished')
        self.store.transition(rec['id'], state='done', outcome='succeeded')
        self.store.close()
        path = next(self.store.directory.glob('client-*.jsonl'))
        with path.open('ab') as stream:
            stream.write(b'{"interrupted":')
        restored = RunStore(self.store.directory)
        self.addCleanup(restored.close)
        self.assertEqual(restored.get(rec['id'])['state'], 'done')
        self.assertTrue(list(path.parent.glob('*.incomplete-*')))

    def test_immutable_snapshots_and_durable_paging_after_cache_eviction(self):
        with patch('forge.client_runs.EVENT_LIMIT', 3):
            rec = self.store.create('many events')
            for i in range(10):
                self.store.push_event(rec['id'], {'type': 'tick', 'value': i})
        snapshot = self.store.get(rec['id'])
        snapshot['events'].clear()
        self.assertEqual(len(self.store.get(rec['id'])['events']), 3)
        cursor, ids, values = 0, [], []
        while True:
            page = self.store.event_page(rec['id'], cursor, limit=2)
            self.assertFalse(page['reset_required'])
            ids += [e['event_id'] for e in page['events']]
            values += [e['value'] for e in page['events'] if e['type'] == 'tick']
            cursor = page['next_after']
            if not page['has_more']:
                break
        self.assertEqual(values, list(range(10)))
        self.assertEqual(len(set(ids)), 11)

    def test_projection_sync_failure_does_not_publish_undurable_task(self):
        with patch.object(Session, 'sync', side_effect=OSError('disk failure')):
            with self.assertRaises(OSError):
                self.store.create('must not execute')
        self.assertEqual(self.store.list_recent(), [])

    def test_concurrent_runs_do_not_share_identity_or_cancel_token(self):
        def create(index):
            rec = self.store.create('task ' + str(index))
            for i in range(5):
                self.store.push_event(rec['id'], {'type': 'tick', 'owner': index})
            return rec
        with ThreadPoolExecutor(max_workers=4) as pool:
            records = list(pool.map(create, range(8)))
        self.assertEqual(len({r['task_id'] for r in records}), 8)
        self.store.request_cancel(records[0]['id'])
        for i, rec in enumerate(records):
            token = self.store.claim(rec['id'])
            self.assertEqual(token.is_set(), i == 0)
            events = self.store.get(rec['id'])['events']
            self.assertTrue(all(e['run_id'] == rec['id'] for e in events))
            self.assertTrue(all(e.get('owner', i) == i for e in events))

    def test_host_request_identity_does_not_leak_into_next_agent_run(self):
        agent = Agent(home=self.home, workspace=self.ws, router=Router(['one', 'two']),
            registry=build_builtin_registry(), policy=Policy(mode=Mode.READ_ONLY, workspace=self.ws))
        self.addCleanup(agent.secret_scope.close)
        request = RunRequest(entry='test')
        first = agent.run('one', run_request=request)
        second = agent.run('two')
        self.assertEqual((first.run_id, first.task_id), (request.run_id, request.task_id))
        self.assertNotEqual(second.run_id, first.run_id)
        self.assertEqual(second.task_id, second.run_id)

    def test_host_request_cannot_replay_existing_journal_after_restart(self):
        request = RunRequest(entry='client')
        path = self.home / 'sessions' / 'identity.jsonl'
        for i in range(2):
            router = Router(['finished'])
            agent = Agent(home=self.home, workspace=self.ws, router=router,
                registry=build_builtin_registry(), policy=Policy(mode=Mode.READ_ONLY, workspace=self.ws),
                session=Session(path))
            self.addCleanup(agent.secret_scope.close)
            with agent.session:
                if i == 0:
                    agent.run('first', run_request=request)
                    ctx = agent.execution_context()
                    self.assertEqual(ctx.run.task_id, request.task_id)
                    self.assertEqual(ctx.run.entry, 'client')
                else:
                    with self.assertRaisesRegex(ValueError, 'already submitted'):
                        agent.run('replay', run_request=request)
                    self.assertEqual(router.calls, [])

    def test_projection_redacts_secrets_before_memory_and_disk_publication(self):
        from forge.secrets import SecretScope
        scope = SecretScope()
        self.addCleanup(scope.close)
        secret = 'fixture-secret-' + 'x' * 32
        scope.reference(secret)
        rec = self.store.create('Do not expose ' + secret)
        self.store.push_event(rec['id'], {'type': 'note', 'text': secret, 'api_key': secret})
        self.assertNotIn(secret, json.dumps(self.store.get(rec['id'])))
        for path in self.store.directory.glob('*.jsonl'):
            self.assertNotIn(secret, path.read_text(encoding='utf-8'))


if __name__ == '__main__':
    unittest.main()
