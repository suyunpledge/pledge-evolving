"""Real Web/Agent handoff and permission results; no external model calls."""
import json
import unittest

import test_client_runtime as fixtures
from forge.execution import ActionExecutor, ActionRequest, create_execution_context
from forge.policy import Policy, Mode, Sandbox
from forge.tools import build_builtin_registry
from forge.loop import Agent
from forge.model import Completion, Usage
from forge.test_project_rules import Router
from client import server as client


class PlanningExecutionControls(unittest.TestCase):
    setUp = fixtures.ClientRuntimeTests.setUp
    server = fixtures.ClientRuntimeTests.server
    http = fixtures.ClientRuntimeTests.http
    wait_terminal = fixtures.ClientRuntimeTests.wait_terminal
    transport = fixtures.ClientRuntimeTests.transport
    use_transport = fixtures.ClientRuntimeTests.use_transport

    def test_http_selects_planner_then_executor_and_returns_plan(self):
        self.use_transport(self.transport(['{"tasks":["Inspect the workspace and verify the result"]}', 'finished']))
        base = self.server()
        code, response = self.http(base, '/api/run', {'task': 'Inspect the workspace',
            'planning': 'low', 'planning_model': client.CATALOG[0]['label'], 'model': client.CATALOG[1]['label']})
        self.assertEqual(code, 202)
        record = self.wait_terminal(response['run_id'])
        self.assertEqual(record['state'], 'done', record.get('error'))
        self.assertEqual([c[0] for c in self.calls], [client.CATALOG[0]['model'], client.CATALOG[1]['model']])
        self.assertIn('execution instructions', str(self.calls[1][1]))
        self.assertEqual(record['planning']['tasks'], ['Inspect the workspace and verify the result'])

    def test_http_disabled_planning_has_one_model_call_and_invalid_options_create_no_job(self):
        self.use_transport(self.transport(['finished']))
        base = self.server()
        for options in ({'planning': True}, {'planning': 'always'}, {'planning_model': 'missing'}):
            status, _ = self.http(base, '/api/run', {'task': 'Inspect', **options})
            self.assertEqual(status, 400)
        self.assertEqual(self.store.list_recent(), [])
        code, response = self.http(base, '/api/run', {'task': 'Inspect', 'planning': 'none',
            'planning_model': client.CATALOG[0]['label'], 'model': client.CATALOG[1]['label']})
        self.assertEqual(code, 202)
        self.assertEqual(self.wait_terminal(response['run_id'])['state'], 'done')
        self.assertEqual(len(self.calls), 1)
        self.assertEqual(self.calls[0][0], client.CATALOG[1]['model'])

    def test_planner_receives_plain_evidence_after_native_tool_continuation(self):
        (self.ws / 'note.txt').write_text('observed fact', encoding='utf-8')
        router = Router([Completion('', Usage(), tool_calls=[{'id': 'one', 'name': 'read_file',
            'args': {'path': 'note.txt'}, 'wire': 'openai'}]), 'observed',
            '{"tasks":["Verify the observed fact"]}', 'verified'])
        agent = Agent(home=self.home, workspace=self.ws, router=router,
            registry=build_builtin_registry(), policy=Policy(workspace=self.ws, mode=Mode.READ_ONLY), project_rules=False)
        self.addCleanup(agent.secret_scope.close)
        first = agent.run('Read the note')
        agent.planning_level = 'low'
        result = agent.run('Verify the note', history=[{'role': 'user', 'content': 'Read the note'},
            {'role': 'assistant', 'content': first.text}])
        planner = router.calls[2]
        self.assertEqual(result.stopped, 'final')
        self.assertTrue(all(m['role'] in {'system','user'} and isinstance(m['content'], str)
                            and 'tool_calls' not in m for m in planner))
        self.assertIn('observed fact', str(planner))
        self.assertIn('execution instructions', str(router.calls[3]))

    def test_configuration_write_approval_reports_actual_gate_without_mutation(self):
        path = self.ws / 'settings.json'
        path.write_text('{"name":"before"}', encoding='utf-8')
        ctx = create_execution_context(home=self.home, workspace=self.ws, registry=build_builtin_registry(),
            policy=Policy(workspace=self.ws, mode=Mode.DEFAULT, sandbox=Sandbox.WORKSPACE_WRITE,
                          allow=('edit_config',), ask=('write_file',), non_interactive=True))
        self.addCleanup(ctx.secret_scope.close)
        result = ActionExecutor().execute(ctx, ActionRequest('edit_config', {'path': 'settings.json',
            'patch': [{'op':'replace','path':'/name','value':'after'}]}))
        self.assertEqual(result.error_code, 'approval_unavailable')
        self.assertEqual(result.meta['permission_gate'], 'write_file')
        self.assertEqual(result.execution_state, 'not_started')
        self.assertEqual(json.loads(path.read_text())['name'], 'before')

    def test_explicit_denial_is_not_misreported_as_an_approval_request(self):
        ctx = create_execution_context(home=self.home, workspace=self.ws, registry=build_builtin_registry(),
            policy=Policy(workspace=self.ws, mode=Mode.ACCEPT_EDITS, deny=('write_file',), non_interactive=True))
        self.addCleanup(ctx.secret_scope.close)
        result = ActionExecutor().execute(ctx, ActionRequest('write_file', {'path': 'blocked.txt', 'content': 'x'}))
        self.assertEqual(result.error_code, 'policy_denied')
        self.assertEqual(result.meta['raw_authorization'], 'deny')
        self.assertFalse((self.ws / 'blocked.txt').exists())

    def test_follow_execution_does_not_silently_select_a_small_model(self):
        from forge.model import ModelRouter, Provider
        calls = []
        class Transport:
            def complete(inner, provider, model, messages, **options):
                calls.append(model)
                return ('{"tasks":["Inspect"]}' if len(calls) == 1 else 'done', Usage(1,1))
        router = ModelRouter([Provider('p', 'https://fixture.invalid', default_model='chosen', small_model='cheap')],
                             transport=Transport(), retries_per_provider=0)
        agent = Agent(home=self.home, workspace=self.ws, router=router, planning='low',
            registry=build_builtin_registry(), policy=Policy(workspace=self.ws, mode=Mode.READ_ONLY), project_rules=False)
        self.addCleanup(agent.secret_scope.close)
        self.assertEqual(agent.run('Inspect').stopped, 'final')
        self.assertEqual(calls, ['chosen','chosen'])
