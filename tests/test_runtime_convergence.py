"""RC1 contracts: real files, HTTP, Git and separate processes; offline models."""
import concurrent.futures
import contextlib
import hashlib
import io
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch
import urllib.request
import urllib.error

from forge.config import Config
from forge.execution import (ActionExecutor, ActionRequest, EventEnvelope, RunRequest,
                             create_execution_context)
from forge.gateway import GatewayConfig, serve
from forge.loop import Agent, build_agent, LoopLimits
from forge.policy import Policy, Mode, Sandbox, ToolEffect
from forge.session import Session
from forge.tools import ToolContext, ToolRegistry, ToolResult, ToolSpec, build_builtin_registry
from forge.test_task_planning import Router

ROOT = Path(__file__).resolve().parents[1]


class RuntimeConvergenceTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name).resolve()
        self.ws = self.root / 'project'
        self.ws.mkdir()
        self.home = self.root / 'control'
        self.path = self.ws / 'value.txt'
        self.path.write_text('old', encoding='utf-8')
        self.events = []
        self.executor = ActionExecutor()

    def context(self, *, mode=Mode.ACCEPT_EDITS, registry=None, session=True, **kwargs):
        journal = Session(self.home / 'sessions' / (str(time.time_ns()) + '.jsonl'),
                          meta={'cwd': str(self.ws)}) if session else None
        if journal:
            self.addCleanup(journal.close)
        ctx = create_execution_context(home=self.home, workspace=self.ws,
            policy=Policy(mode=mode, workspace=self.ws, non_interactive=True),
            registry=registry or build_builtin_registry(), session=journal,
            subscriber=kwargs.pop('subscriber', self.events.append), **kwargs)
        self.addCleanup(ctx.secret_scope.close)
        return ctx

    def execute(self, ctx, name='write_file', **args):
        return self.executor.execute(ctx, ActionRequest(name, args)).as_tool_result()

    def gateway(self, *, policy=None, registry=None, key='rc1-host-control-test-key'):
        self.gateway_key = key
        cfg = GatewayConfig(upstream='http://127.0.0.1:1', port=0,
            workspace=str(self.ws), home=self.home,
            registry=registry or build_builtin_registry(),
            policy=policy or Policy(mode=Mode.ACCEPT_EDITS, workspace=self.ws, non_interactive=True),
            gateway_token=key)
        server = serve(cfg)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        return cfg, 'http://127.0.0.1:' + str(server.server_address[1])

    def http(self, base, path, body=None, key=None, scope=None):
        key = getattr(self, 'gateway_key', '') if key is None else key
        request = urllib.request.Request(base + path,
            data=json.dumps(body).encode() if body is not None else None,
            headers={'Content-Type': 'application/json', 'Authorization': 'Bearer ' + key,
                     **({'X-Forge-Secret-Scope': scope} if scope is not None else {})})
        try:
            with urllib.request.urlopen(request, timeout=15) as response:
                return response.status, json.loads(response.read())
        except urllib.error.HTTPError as exc:
            return exc.code, json.loads(exc.read())

    def test_unverified_read_only_claim_is_never_a_grant(self):
        for mode in (Mode.READ_ONLY, Mode.ACCEPT_EDITS, Mode.BYPASS):
            with self.subTest(mode=mode):
                registry = ToolRegistry()
                calls = []
                registry.register(ToolSpec('innocent', '', lambda a,c: calls.append(1) or ToolResult(True)))
                result = self.execute(self.context(mode=mode, registry=registry), 'innocent')
                self.assertFalse(result.ok)
                self.assertEqual(calls, [])

    def test_forged_host_filename_does_not_verify_a_tool(self):
        registry = ToolRegistry()
        def attacker(a,c): return ToolResult(True)
        attacker.__code__ = attacker.__code__.replace(co_filename=str(ROOT / 'forge-gui' / 'plugin_capabilities.py'),
            co_qualname='CapabilityRuntime.reload.<locals>.<lambda>')
        registry.register(ToolSpec('forged', '', attacker))
        self.assertFalse(self.execute(self.context(registry=registry), 'forged').ok)

    def test_arbitrary_trusted_writer_is_denied_in_read_only(self):
        registry = ToolRegistry()
        calls = []
        registry.register(ToolSpec('rename_anything', '', lambda a,c: calls.append(1) or ToolResult(True), read_only=False), trusted=True)
        result = self.execute(self.context(mode=Mode.READ_ONLY, registry=registry), 'rename_anything')
        self.assertFalse(result.ok)
        self.assertEqual(result.meta['authorization'], 'deny')
        self.assertEqual(calls, [])

    def test_explicit_allow_does_not_lift_read_only_writer(self):
        registry = ToolRegistry()
        registry.register(ToolSpec('arbitrary', '', lambda a,c: ToolResult(True), read_only=False), trusted=True)
        ctx = self.context(registry=registry)
        tc = ToolContext(Policy(mode=Mode.READ_ONLY, workspace=self.ws, allow=('*',)), self.ws)
        self.addCleanup(tc.secret_scope.close)
        self.assertFalse(ctx.registry.invoke('arbitrary', {}, tc).ok)

    def test_host_confirmed_readers_still_work(self):
        registry = ToolRegistry()
        registry.register(ToolSpec('reader', '', lambda a,c: ToolResult(True, 'read')), trusted=True)
        self.assertTrue(self.execute(self.context(mode=Mode.READ_ONLY, registry=registry), 'reader').ok)
        self.assertTrue(self.execute(self.context(mode=Mode.READ_ONLY), 'read_file', path='value.txt').ok)

    def test_mutating_spec_cannot_change_registered_effect(self):
        registry = ToolRegistry()
        spec = ToolSpec('writer', '', lambda a,c: ToolResult(True), read_only=False)
        registry.register(spec, trusted=True)
        spec.read_only = True
        self.assertFalse(self.execute(self.context(mode=Mode.READ_ONLY, registry=registry), 'writer').ok)

    def test_file_read_effect_respects_scope_for_arbitrary_name(self):
        registry = ToolRegistry()
        registry.register(ToolSpec('peek', '', lambda a,c: ToolResult(True)), trusted=True, effect=ToolEffect.FILE_READ)
        result = self.execute(self.context(registry=registry), 'peek', path=str(self.root / 'outside'))
        self.assertFalse(result.ok)
        by_root = self.execute(self.context(registry=registry), 'peek', root=str(self.root / 'outside'))
        self.assertFalse(by_root.ok)

    def test_native_read_only_claim_cannot_bypass_sandbox(self):
        registry = ToolRegistry()
        registry.register(ToolSpec('native_claim', '', lambda a,c: ToolResult(True), tags=('native',)), trusted=True)
        tc = ToolContext(Policy(mode=Mode.READ_ONLY, workspace=self.ws), self.ws, isolated=False)
        self.addCleanup(tc.secret_scope.close)
        self.assertFalse(registry.invoke('native_claim', {}, tc).ok)

    def test_unknown_effect_stays_conservative(self):
        registry = ToolRegistry()
        registry.register(ToolSpec('unknown', '', lambda a,c: ToolResult(True)), trusted=True, effect=ToolEffect.UNKNOWN)
        self.assertFalse(self.execute(self.context(registry=registry), 'unknown').ok)

    def test_workspace_mismatch_is_rejected(self):
        with self.assertRaises(ValueError):
            create_execution_context(home=self.home, workspace=self.ws, registry=build_builtin_registry(),
                                     policy=Policy(workspace=self.root))
        from forge.checkpoint import CheckpointStore
        with self.assertRaises(ValueError):
            create_execution_context(home=self.home, workspace=self.ws, registry=build_builtin_registry(),
                policy=Policy(workspace=self.ws), checkpoints=CheckpointStore(self.home, self.root))

    def test_factory_never_invents_policy(self):
        with self.assertRaises(ValueError):
            create_execution_context(home=self.home, workspace=self.ws, registry=build_builtin_registry())

    def test_default_journal_survives_executor_recreation(self):
        run = RunRequest(run_id='durable_default', task_id='durable_task')
        ctx = create_execution_context(home=self.home, workspace=self.ws, registry=build_builtin_registry(),
            policy=Policy(mode=Mode.ACCEPT_EDITS, workspace=self.ws), run=run)
        self.addCleanup(ctx.secret_scope.close)
        request = ActionRequest('write_file', {'path': 'value.txt', 'content': 'once'})
        ActionExecutor().execute(ctx, request)
        again = create_execution_context(home=self.home, workspace=self.ws, registry=build_builtin_registry(),
            policy=ctx.policy, run=run)
        self.addCleanup(again.secret_scope.close)
        self.assertTrue(ActionExecutor().execute(again, request).meta['deduplicated'])
        with self.assertRaises(ValueError):
            create_execution_context(home=self.home, workspace=self.ws, registry=build_builtin_registry(),
                policy=ctx.policy, run=RunRequest(run_id=run.run_id, task_id='different_task'))

    def test_agent_cannot_overwrite_runtime_journal(self):
        ctx = create_execution_context(home=self.home, workspace=self.ws, registry=build_builtin_registry(),
            policy=Policy(mode=Mode.ACCEPT_EDITS, workspace=self.ws))
        self.addCleanup(ctx.secret_scope.close)
        result = self.execute(ctx, path=str(ctx.session.path), content='tamper')
        self.assertFalse(result.ok)
        self.assertEqual(ActionExecutor().query(ctx, result.meta['action_id'])['meta']['outcome'], 'failed')

    def test_custom_control_home_is_protected(self):
        self.home = self.ws / 'custom-control'
        result = self.execute(self.context(), path=str(self.home / 'modules' / 'evil.py'), content='unsafe')
        self.assertFalse(result.ok)
        self.assertFalse((self.home / 'modules' / 'evil.py').exists())

    def test_workspace_under_home_is_not_wholly_treated_as_control_code(self):
        ctx = create_execution_context(home=self.root, workspace=self.ws, registry=build_builtin_registry(),
            policy=Policy(mode=Mode.ACCEPT_EDITS, workspace=self.ws))
        self.addCleanup(ctx.secret_scope.close)
        self.assertTrue(self.execute(ctx, path='value.txt', content='allowed project edit').ok)

    def test_missing_capability_not_advertised_or_executable(self):
        ctx = self.context()
        names = {s.name for s in ctx.registry.available_specs(ctx.tool_context())}
        self.assertFalse({'spawn_subagent', 'memory_recall', 'skill_list'} & names)
        for name in ('spawn_subagent', 'memory_recall', 'skill_list'):
            self.assertEqual(self.execute(ctx, name).meta['error_code'], 'missing_capability')

    def test_activation_is_isolated_between_contexts(self):
        registry = ToolRegistry()
        registry.register(ToolSpec('deferred', 'unique_probe', lambda a,c: ToolResult(True), deferred=True), trusted=True)
        a, b = self.context(registry=registry), self.context(registry=registry)
        a.registry.search('unique_probe')
        self.assertIn('deferred', a.registry.names())
        self.assertNotIn('deferred', b.registry.names())
        self.assertNotIn('deferred', registry.names())

    def test_action_lifecycle_durable_before_subscriber(self):
        ctx = self.context(run=RunRequest(parent_run_id='parent'))
        def observer(event):
            self.assertIn(event['event_id'], ctx.session.path.read_text())
            self.events.append(event)
        object.__setattr__(ctx, 'subscriber', observer)
        result = self.execute(ctx, path='value.txt', content='new')
        self.assertTrue(result.ok, result.error)
        self.assertEqual(self.path.read_text(), 'new')
        action = result.meta['action_id']
        self.assertEqual(self.executor.query(ctx, action)['meta']['outcome'], 'succeeded')
        records = [e for e in self.events if e.get('action_id') == action]
        self.assertEqual([e['type'] for e in records], ['tool_started', 'tool_completed'])
        for e in records:
            self.assertEqual(e['parent_run_id'], 'parent')
            self.assertTrue(e['task_id'] and e['run_id'] and e['event_id'])

    def test_subscriber_failure_does_not_change_action_result(self):
        def broken(_): raise RuntimeError('view destroyed')
        result = self.execute(self.context(subscriber=broken), path='value.txt', content='new')
        self.assertTrue(result.ok)

    def test_preflight_denial_records_without_dispatch(self):
        ctx = self.context(mode=Mode.READ_ONLY)
        result = self.execute(ctx, path='value.txt', content='new')
        self.assertEqual(result.meta['execution_state'], 'not_started')
        self.assertEqual(self.path.read_text(), 'old')
        self.assertEqual([e['type'] for e in self.events], ['tool_started', 'tool_completed'])

    def test_cancelled_context_never_starts_handler(self):
        cancel = threading.Event(); cancel.set()
        result = self.execute(self.context(cancellation=cancel), path='value.txt', content='new')
        self.assertEqual(result.meta['outcome'], 'cancelled')
        self.assertEqual(self.path.read_text(), 'old')

    def test_duplicate_action_is_not_executed_twice(self):
        ctx = self.context()
        request = ActionRequest('write_file', {'path': 'value.txt', 'content': 'once'})
        with patch.object(ctx.registry, 'invoke', wraps=ctx.registry.invoke) as invoke:
            first = self.executor.execute(ctx, request)
            second = ActionExecutor().execute(ctx, request)
        self.assertEqual(invoke.call_count, 1)
        self.assertTrue(first.as_dict()['ok'] and second.as_dict()['meta']['deduplicated'])

    def test_concurrent_duplicate_identity_has_one_dispatch(self):
        registry = ToolRegistry()
        calls = []
        def handler(a,c):
            calls.append(1)
            time.sleep(.03)
            return ToolResult(True)
        registry.register(ToolSpec('once', '', handler), trusted=True)
        ctx = self.context(registry=registry)
        request = ActionRequest('once', {})
        with concurrent.futures.ThreadPoolExecutor(2) as pool:
            results = list(pool.map(lambda _: ActionExecutor().execute(ctx, request), range(2)))
        self.assertEqual(calls, [1])
        self.assertTrue(all(r.outcome == 'succeeded' for r in results))
        self.assertEqual(sum(bool(r.meta.get('deduplicated')) for r in results), 1)

    def test_running_action_is_queryable_without_waiting_for_completion(self):
        registry = ToolRegistry()
        entered, proceed = threading.Event(), threading.Event()
        def handler(a,c):
            entered.set()
            if not proceed.wait(5): raise TimeoutError('fixture release missing')
            return ToolResult(True)
        registry.register(ToolSpec('bounded', '', handler), trusted=True)
        ctx = self.context(registry=registry)
        request = ActionRequest('bounded', {})
        with concurrent.futures.ThreadPoolExecutor(1) as pool:
            future = pool.submit(self.executor.execute, ctx, request)
            try:
                self.assertTrue(entered.wait(5))
                result = self.executor.query(ctx, request.action_id)
                self.assertEqual(result['meta']['execution_state'], 'running')
                self.assertEqual(result['meta']['action_id'], request.action_id)
            finally:
                proceed.set()
            self.assertEqual(future.result().outcome, 'succeeded')

    def test_checkpoint_failure_does_not_dispatch(self):
        class BrokenCheckpoint:
            def snapshot(self, label): raise OSError('checkpoint disk unavailable')
        result = self.execute(self.context(checkpoints=BrokenCheckpoint()), path='value.txt', content='new')
        self.assertEqual(result.meta['execution_state'], 'not_started')
        self.assertEqual(result.meta['error_code'], 'preparation_failed')
        self.assertEqual(self.path.read_text(), 'old')

    def test_action_identity_cannot_change_arguments(self):
        ctx = self.context()
        request = ActionRequest('write_file', {'path': 'value.txt', 'content': 'once'})
        self.executor.execute(ctx, request)
        result = self.executor.execute(ctx, ActionRequest('write_file', {'path': 'value.txt', 'content': 'twice'}, request.action_id))
        self.assertEqual(result.error_code, 'action_id_conflict')
        self.assertEqual(self.path.read_text(), 'once')

    def test_lost_completion_is_unknown_and_not_replayed(self):
        ctx = self.context()
        request = ActionRequest('write_file', {'path': 'value.txt', 'content': 'changed'})
        append = ctx.session.append
        def fail(kind, **data):
            if kind == 'tool_completed': raise OSError('disk unavailable')
            return append(kind, **data)
        with patch.object(ctx.session, 'append', side_effect=fail):
            with self.assertRaises(OSError): self.executor.execute(ctx, request)
        self.assertEqual(self.path.read_text(), 'changed')
        result = ActionExecutor().execute(ctx, request)
        self.assertEqual(result.execution_state, 'unknown')
        self.assertEqual(result.outcome, 'unknown')
        self.assertTrue(result.meta['deduplicated'])

    def test_partial_completion_tail_keeps_unknown_action_queryable(self):
        ctx = self.context()
        request = ActionRequest('write_file', {'path': 'value.txt', 'content': 'new'})
        digest = hashlib.sha256(json.dumps([request.name, request.arguments], sort_keys=True,
                                          ensure_ascii=False, allow_nan=False).encode()).hexdigest()
        ctx.publish('tool_started', action_id=request.action_id, tool=request.name, request_digest=digest)
        ctx.session.close()
        with ctx.session.path.open('ab') as stream: stream.write(b'{"type":"tool_comp')
        self.assertEqual(self.executor.query(ctx, request.action_id)['meta']['execution_state'], 'unknown')
        result = ActionExecutor().execute(ctx, request)
        self.assertEqual(result.outcome, 'unknown')
        self.assertEqual(self.path.read_text(), 'old')

    def test_intent_persistence_failure_prevents_side_effect(self):
        ctx = self.context()
        with patch.object(ctx.session, 'sync', side_effect=OSError('fsync failed')):
            with self.assertRaises(OSError): self.execute(ctx, path='value.txt', content='new')
        self.assertEqual(self.path.read_text(), 'old')

    def test_partial_writer_exception_is_unknown(self):
        registry = ToolRegistry()
        def write_then_fail(a,c):
            self.path.write_text('partial', encoding='utf-8')
            raise OSError('after write')
        registry.register(ToolSpec('partial_writer', '', write_then_fail, read_only=False), trusted=True)
        result = self.execute(self.context(registry=registry), 'partial_writer')
        self.assertEqual(result.meta['execution_state'], 'unknown')
        self.assertEqual(self.path.read_text(), 'partial')

    def test_malformed_result_after_write_is_unknown_not_safely_retryable(self):
        for result in ({'ok': True}, ToolResult(True, meta=[]), ToolResult(True, content={'bad': 'type'})):
            with self.subTest(result_type=type(result).__name__):
                registry = ToolRegistry()
                def handler(a,c):
                    self.path.write_text('changed')
                    return result
                registry.register(ToolSpec('bad_writer', '', handler, read_only=False), trusted=True)
                ctx = self.context(registry=registry)
                value = self.execute(ctx, 'bad_writer', path='value.txt')
                self.assertEqual(value.meta['execution_state'], 'unknown')
                self.assertEqual(self.path.read_text(), 'changed')

    def test_unknown_action_prevents_run_success_and_is_recovery_evidence(self):
        from forge.loop import RunReport
        from forge.execution_guard import workflow_status
        events = [{'type': 'workflow_state', 'run_id': 'r', 'phase': 'completed'},
                  {'type': 'tool_completed', 'run_id': 'r', 'action_id': 'a', 'execution_state': 'unknown'}]
        report = RunReport('model ended', [], {}, events=events, run_id='r', task_id='t')
        self.assertEqual(report.as_result().outcome, 'unknown')
        status = workflow_status(events, 'r')
        self.assertEqual(status['unknown_actions'], ['a'])
        self.assertTrue(status['requires_inspection'])
        self.assertFalse(status['replay_safe'])

    def test_failed_patch_preserves_all_targets(self):
        ctx = self.context()
        result = self.execute(ctx, 'apply_patch', patches=[{'path': 'value.txt', 'old': 'old', 'new': 'new'},
            {'path': 'missing.txt', 'old': 'absent', 'new': 'bad'}])
        self.assertFalse(result.ok)
        self.assertEqual(self.path.read_text(), 'old')
        self.assertFalse((self.ws / 'missing.txt').exists())

    def test_real_checkpoint_can_restore_failed_task_effect(self):
        from forge.checkpoint import CheckpointStore
        store = CheckpointStore(self.home, self.ws)
        if not store.enabled: self.skipTest('Git unavailable')
        ctx = self.context(checkpoints=store)
        result = self.execute(ctx, path='value.txt', content='new')
        self.assertTrue(result.ok)
        self.assertTrue(store.rollback(result.meta['checkpoint']))
        self.assertEqual(self.path.read_text(), 'old')

    def test_parallel_projects_do_not_mix_shadow_git_snapshots(self):
        from forge.checkpoint import CheckpointStore
        projects = [self.root / 'project-a', self.root / 'project-b']
        stores = []
        for i, project in enumerate(projects):
            project.mkdir()
            (project / ('owned-' + str(i) + '.txt')).write_text(str(i))
            stores.append(CheckpointStore(self.home, project))
        if not stores[0].enabled: self.skipTest('Git unavailable')
        barrier = threading.Barrier(2)
        def snapshot(i):
            barrier.wait()
            return stores[i].snapshot('project ' + str(i))
        with concurrent.futures.ThreadPoolExecutor(2) as pool:
            points = list(pool.map(snapshot, range(2)))
        for i, point in enumerate(points):
            self.assertIsNotNone(point)
            tree = stores[i]._git('ls-tree', '-r', '--name-only', point.commit).stdout.splitlines()
            self.assertEqual(tree, ['owned-' + str(i) + '.txt'])

    def test_same_revision_race_has_one_winner(self):
        ctxs = [self.context(), self.context()]
        digest = hashlib.sha256(self.path.read_bytes()).hexdigest()
        barrier = threading.Barrier(2)
        def write(i):
            barrier.wait()
            return self.execute(ctxs[i], path='value.txt', content=str(i),
                                _expected_revisions={str(self.path): digest})
        with concurrent.futures.ThreadPoolExecutor(2) as pool:
            results = list(pool.map(write, range(2)))
        self.assertEqual(sum(r.ok for r in results), 1)
        self.assertEqual([r.meta['error_code'] for r in results if not r.ok], ['revision_conflict'])

    def test_nested_workspaces_share_file_revision_lock(self):
        nested = self.ws / 'nested'; nested.mkdir()
        path = nested / 'shared.txt'; path.write_text('old')
        contexts = []
        for ws in (self.ws, nested):
            contexts.append(create_execution_context(home=self.home, workspace=ws,
                registry=build_builtin_registry(), policy=Policy(mode=Mode.ACCEPT_EDITS, workspace=ws)))
            self.addCleanup(contexts[-1].secret_scope.close)
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        barrier = threading.Barrier(2)
        def write(i):
            barrier.wait()
            return ActionExecutor().execute(contexts[i], ActionRequest('write_file', {
                'path': str(path), 'content': str(i), '_expected_revisions': {str(path): digest}}))
        with concurrent.futures.ThreadPoolExecutor(2) as pool:
            results = list(pool.map(write, range(2)))
        self.assertEqual(sum(r.outcome == 'succeeded' for r in results), 1)

    def test_gateway_reuses_lifecycle_and_exposes_query(self):
        cfg, base = self.gateway()
        body = {'name': 'write_file', 'arguments': {'path': 'value.txt', 'content': 'gateway'},
                'run_id': 'gateway_run', 'task_id': 'gateway_task', 'action_id': 'gateway_action'}
        code, result = self.http(base, '/v1/tools/call', body)
        self.assertEqual(code, 200); self.assertTrue(result['ok'], result)
        code, found = self.http(base, '/v1/runs/gateway_run/actions/gateway_action')
        self.assertEqual(code, 200); self.assertEqual(found['meta']['task_id'], 'gateway_task')
        self.assertEqual(found['meta']['event_id'], result['meta']['event_id'])
        code, page = self.http(base, '/v1/runs/gateway_run/events?after=0')
        self.assertEqual(code, 200)
        self.assertEqual([e['type'] for e in page['data']], ['tool_started', 'tool_completed'])
        self.assertTrue(all(e['event_id'] and e['action_id'] == 'gateway_action' for e in page['data']))
        _, next_page = self.http(base, '/v1/runs/gateway_run/events?after=' + str(page['next_after']))
        self.assertEqual(next_page['data'], [])
        _, duplicate = self.http(base, '/v1/tools/call', body)
        self.assertTrue(duplicate['meta']['deduplicated'])
        self.assertNotIn('gateway_run', cfg.registry._activated)

    def test_gateway_query_requires_existing_auth(self):
        _, base = self.gateway(key='rc1-gateway-auth-long-key')
        code, _ = self.http(base, '/v1/runs/known/actions/a', key='')
        self.assertEqual(code, 401)

    def test_legacy_keyless_tools_do_not_expose_new_execution_data_endpoints(self):
        _, base = self.gateway(key='')
        _, result = self.http(base, '/v1/tools/call', {'name': 'read_file', 'arguments': {'path': 'value.txt'}})
        self.assertTrue(result['ok'])
        self.assertEqual(self.http(base, '/v1/runs/known/actions/a')[0], 503)
        self.assertEqual(self.http(base, '/v1/runs/known/events')[0], 503)
        self.assertEqual(self.http(base, '/v1/runs/known/cancel', {})[0], 503)

    def test_gateway_cancel_token_prevents_new_action(self):
        _, base = self.gateway()
        self.http(base, '/v1/runs/cancel_run/cancel', {})
        _, result = self.http(base, '/v1/tools/call', {'run_id': 'cancel_run', 'name': 'write_file',
                                                      'arguments': {'path': 'value.txt', 'content': 'new'}})
        self.assertEqual(result['meta']['outcome'], 'cancelled')
        self.assertEqual(self.path.read_text(), 'old')

    def test_gateway_context_identity_conflict_is_rejected(self):
        _, base = self.gateway()
        self.http(base, '/v1/tools/call', {'run_id': 'same_run', 'task_id': 'task_one',
                                         'name': 'read_file', 'arguments': {'path': 'value.txt'}})
        code, _ = self.http(base, '/v1/tools/call', {'run_id': 'same_run', 'task_id': 'task_two',
                                                   'name': 'read_file', 'arguments': {'path': 'value.txt'}})
        self.assertEqual(code, 400)

    def test_protected_run_cannot_be_joined_without_its_scope(self):
        _, base = self.gateway()
        body = {'run_id': 'protected', 'name': 'read_file', 'arguments': {'path': 'value.txt'}}
        code, first = self.http(base, '/v1/tools/call', body, scope='a' * 32)
        self.assertEqual(code, 200); self.assertTrue(first['ok'])
        self.assertEqual(self.http(base, '/v1/tools/call', body)[0], 400)
        self.assertEqual(self.http(base, '/v1/tools/call', body, scope='b' * 32)[0], 400)
        self.assertTrue(self.http(base, '/v1/tools/call', body, scope='a' * 32)[1]['ok'])

    def test_gateway_run_neither_gains_grants_nor_ignores_revocation(self):
        cfg, base = self.gateway(policy=Policy(mode=Mode.READ_ONLY, workspace=self.ws))
        self.http(base, '/v1/tools/call', {'run_id': 'frozen', 'name': 'read_file', 'arguments': {'path': 'value.txt'}})
        cfg.policy = Policy(mode=Mode.ACCEPT_EDITS, workspace=self.ws)
        _, frozen = self.http(base, '/v1/tools/call', {'run_id': 'frozen', 'name': 'write_file',
                                                      'arguments': {'path': 'value.txt', 'content': 'new'}})
        self.assertFalse(frozen['ok'])
        self.http(base, '/v1/tools/call', {'run_id': 'revoke', 'name': 'read_file', 'arguments': {'path': 'value.txt'}})
        cfg.policy = Policy(mode=Mode.READ_ONLY, workspace=self.ws)
        _, revoked = self.http(base, '/v1/tools/call', {'run_id': 'revoke', 'name': 'write_file',
                                                       'arguments': {'path': 'value.txt', 'content': 'new'}})
        self.assertFalse(revoked['ok'])
        self.assertEqual(self.path.read_text(), 'old')

    def test_gateway_restart_reads_durable_action_without_replay(self):
        cfg, base = self.gateway()
        _, result = self.http(base, '/v1/tools/call', {'run_id': 'resume_run', 'task_id': 'resume_task',
            'action_id': 'resume_action', 'name': 'write_file', 'arguments': {'path': 'value.txt', 'content': 'once'}})
        cfg.close_runtime()
        _, duplicate = self.http(base, '/v1/tools/call', {'run_id': 'resume_run', 'task_id': 'resume_task',
            'action_id': 'resume_action', 'name': 'write_file', 'arguments': {'path': 'value.txt', 'content': 'once'}})
        self.assertEqual(duplicate['meta']['task_id'], result['meta']['task_id'])
        self.assertTrue(duplicate['meta']['deduplicated'])
        _, new_action = self.http(base, '/v1/tools/call', {'run_id': 'resume_run', 'task_id': 'resume_task',
            'name': 'write_file', 'arguments': {'path': 'value.txt', 'content': 'resumed without grant'}})
        self.assertEqual(new_action['meta']['error_code'], 'recovery_requires_new_run')
        self.assertEqual(self.path.read_text(), 'once')

    def test_gateway_threads_compete_on_same_revision(self):
        _, base = self.gateway()
        digest = hashlib.sha256(self.path.read_bytes()).hexdigest()
        def call(i):
            return self.http(base, '/v1/tools/call', {'name': 'write_file',
                'arguments': {'path': 'value.txt', 'content': str(i), '_expected_revisions': {str(self.path): digest}}})[1]
        with concurrent.futures.ThreadPoolExecutor(4) as pool:
            results = list(pool.map(call, range(4)))
        self.assertEqual(sum(r['ok'] for r in results), 1)
        self.assertEqual(len({r['meta']['run_id'] for r in results}), 4)

    def test_cli_gateway_permission_and_expose_consistency(self):
        from forge import cli
        config = Config()
        config.apply_patch([{'id': 'policy', 'name': 'policy', 'config': {'mode': 'read-only'}},
                            {'id': 'tools', 'name': 'tools', 'config': {'expose': ['read_file', 'write_file']}}])
        args = cli.build_parser().parse_args(['gateway', '--home', str(self.home),
            '--workspace', str(self.ws), '--tools', '--upstream', 'http://127.0.0.1:1'])
        captured = []
        class Server:
            def serve_forever(self): raise KeyboardInterrupt()
            def server_close(self): pass
        with patch('forge.cli._compose', return_value=config), patch('forge.gateway.serve', side_effect=lambda c: captured.append(c) or Server()):
            self.assertEqual(cli.cmd_gateway(args), 0)
        cfg = captured[0]; self.addCleanup(cfg.close_runtime)
        agent = build_agent(home=self.home, workspace=self.ws, config=config, router=Router(['done']),
                            expose=config.get('tools', 'expose'), mount_contrib=False)
        self.addCleanup(agent.session.close); self.addCleanup(agent.secret_scope.close)
        contexts = [agent.execution_context(), cfg.execution_context()]
        for name, args in [('read_file', {'path': 'value.txt'}), ('write_file', {'path': 'value.txt', 'content': 'new'}),
                           ('calculator', {'expression': '1+1'})]:
            results = [ActionExecutor().execute(c, ActionRequest(name, args)).as_tool_result() for c in contexts]
            self.assertEqual(results[0].ok, results[1].ok)
            self.assertEqual(results[0].meta['error_code'], results[1].meta['error_code'])

    def test_cli_public_result_marks_step_limit_unsuccessful(self):
        from forge import cli
        config = Config()
        config.apply_patch([{'id': 'policy', 'name': 'policy', 'config': {'mode': 'acceptEdits'}},
                            {'id': 'loop', 'name': 'loop', 'config': {'maxSteps': 1}}])
        args = cli.build_parser().parse_args(['run', '--home', str(self.home), '--workspace', str(self.ws), '--json', 'read'])
        router = Router(['<tool_call>{"tool":"read_file","args":{"path":"value.txt"}}</tool_call>'])
        output = io.StringIO()
        with patch('forge.cli._compose', return_value=config), patch('forge.cli.ModelRouter.from_config', return_value=router), contextlib.redirect_stdout(output), contextlib.redirect_stderr(io.StringIO()):
            code = cli.cmd_run(args)
        value = json.loads(output.getvalue())
        self.assertEqual(code, 1); self.assertFalse(value['ok'])
        self.assertEqual(value['error_code'], 'max_steps')
        self.assertTrue(value['run_id'] and value['task_id'])

    def test_default_agent_sessions_are_distinct(self):
        agents = [build_agent(home=self.home, workspace=self.ws, config=Config(), router=Router(['done']), mount_contrib=False) for _ in range(2)]
        for agent in agents:
            self.addCleanup(agent.session.close); self.addCleanup(agent.secret_scope.close)
        self.assertNotEqual(agents[0].session.path, agents[1].session.path)

    def test_agent_api_compatibility_and_parent_lineage(self):
        router = Router(['<tool_call>{"tool":"spawn_subagent","args":{"task":"child"}}</tool_call>', 'child answer', 'parent answer'])
        journal = Session(self.home / 'agent.jsonl', meta={'cwd': str(self.ws)})
        self.addCleanup(journal.close)
        agent = Agent(home=self.home, workspace=self.ws, router=router, registry=build_builtin_registry(),
            policy=Policy(mode=Mode.ACCEPT_EDITS, workspace=self.ws), session=journal,
            limits=LoopLimits(max_steps=4))
        self.addCleanup(agent.secret_scope.close)
        with journal:
            report = agent.run('parent')
        self.assertEqual(report.stopped, 'final')
        self.assertEqual(report.as_result().outcome, 'succeeded')
        children = [e for e in journal.replay() if e.data.get('depth') == 1 and e.data.get('run_id') != report.run_id]
        self.assertTrue(children)
        self.assertTrue(all(e.data['parent_run_id'] == report.run_id for e in children))
        self.assertTrue(all(e.data['task_id'] == report.task_id for e in children))

    def test_large_session_record_preserves_ordinals(self):
        a, b = Session(self.home / 'shared.jsonl'), Session(self.home / 'shared.jsonl')
        self.addCleanup(a.close); self.addCleanup(b.close)
        first = a.append('large', text='x' * 180000)
        second = b.append('small')
        self.assertEqual((first.ordinal, second.ordinal), (1, 2))

    def test_partial_tail_recovery_keeps_evidence(self):
        path = self.home / 'tail.jsonl'
        session = Session(path); session.append('good'); session.close()
        with path.open('ab') as stream: stream.write(b'{"type":"tool_sta')
        with Session(path) as fresh:
            record = fresh.append('next')
        self.assertEqual(record.ordinal, 2)
        self.assertEqual(len(list(path.parent.glob('tail.jsonl.incomplete-*'))), 1)

    def processes(self, code, count=3):
        go = self.root / 'go'
        prefix = "import sys; sys.path.insert(0, " + repr(str(ROOT)) + ")\n"
        processes = []
        for i in range(count):
            ready = self.root / ('ready-' + str(i))
            script = prefix + "from pathlib import Path\nimport time\n" + code
            proc = subprocess.Popen([sys.executable, '-B', '-c', script, str(self.root), str(i)],
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, encoding='utf-8',
                creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
            processes.append(proc)
        try:
            deadline = time.monotonic() + 20
            while not all((self.root / ('ready-' + str(i))).exists() for i in range(count)):
                if time.monotonic() > deadline or any(p.poll() is not None for p in processes):
                    self.fail('Workers did not reach the simultaneous start barrier')
                time.sleep(.01)
            go.write_text('start')
            outputs = []
            for proc in processes:
                out, err = proc.communicate(timeout=30)
                self.assertEqual(proc.returncode, 0, err)
                outputs.append(json.loads(out))
            return outputs
        finally:
            for proc in processes:
                if proc.poll() is None:
                    proc.kill(); proc.communicate()

    def test_cross_process_session_append_is_atomic_and_monotonic(self):
        self.processes('''
from forge.session import Session
root=Path(sys.argv[1]); i=sys.argv[2]
s=Session(root/'shared.jsonl'); s.open()
(root/('ready-'+i)).write_text('ready')
while not (root/'go').exists(): time.sleep(.005)
for n in range(45): s.append('writer', worker=i, n=n, text='中'*4000)
s.close(); print('{}')
''', count=4)
        events = list(Session(self.root / 'shared.jsonl').replay())
        self.assertEqual(sum(e.type == 'session_meta' for e in events), 1)
        rows = [e for e in events if e.type == 'writer']
        self.assertEqual([e.ordinal for e in rows], list(range(1, 181)))
        self.assertEqual(len({(e.data['worker'], e.data['n']) for e in rows}), 180)
        self.assertTrue(all(len(e.data['text']) == 4000 for e in rows))

    def test_cross_process_revision_competition_has_one_winner(self):
        outputs = self.processes('''
from forge.tools import build_builtin_registry, ToolContext
from forge.policy import Policy,Mode
import hashlib, json
root=Path(sys.argv[1]); i=sys.argv[2]; ws=root/'project'; p=ws/'value.txt'
digest=hashlib.sha256(p.read_bytes()).hexdigest()
ctx=ToolContext(Policy(mode=Mode.ACCEPT_EDITS,workspace=ws),ws)
(root/('ready-'+i)).write_text('ready')
while not (root/'go').exists(): time.sleep(.005)
r=build_builtin_registry().invoke('write_file',{'path':'value.txt','content':i,'_expected_revisions':{str(p):digest}},ctx)
print(json.dumps(r.as_dict()))
''', count=4)
        self.assertEqual(sum(r['ok'] for r in outputs), 1)
        self.assertEqual([r['meta']['error_code'] for r in outputs if not r['ok']], ['revision_conflict'] * 3)


if __name__ == '__main__':
    unittest.main()
