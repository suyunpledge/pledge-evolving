"""Offline phase isolation and manual code review regressions."""
import json
import tempfile
import unittest
import io
from contextlib import redirect_stdout
from unittest.mock import patch
from pathlib import Path
from .loop import Agent
from .model import Provider, Usage, ModelRouter, TransportError, BadRequest
from .policy import Policy, Mode, Sandbox
from .tools import build_builtin_registry
from .test_task_planning import plan_json


class Transport:
    def __init__(self, answers): self.answers, self.calls = iter(answers), []
    def complete(self, provider, model, messages, **options):
        self.calls.append((provider.name, model, messages, options))
        answer = next(self.answers)
        if isinstance(answer, Exception): raise answer
        return answer, Usage(10, 5)


class PhaseModelTests(unittest.TestCase):
    def agent(self, answers, **kwargs):
        tmp = tempfile.TemporaryDirectory(); self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        transport = Transport(answers)
        router = ModelRouter([Provider('writer', 'https://writer.invalid', default_model='opus'),
                              Provider('planner', 'https://planner.invalid', default_model='astra')],
                             transport=transport, primary=('writer', 'opus'), retries_per_provider=0)
        agent = Agent(home=root, workspace=root, router=router, registry=build_builtin_registry(),
                      policy=Policy(mode=Mode.DONT_ASK, sandbox=Sandbox.READ_ONLY), **kwargs)
        self.addCleanup(agent.secret_scope.close)
        return agent, transport

    def test_independent_planner_does_not_change_writer(self):
        agent, transport = self.agent([plan_json(), 'written'], planning='low', planning_model=['planner', 'astra'])
        report = agent.run('write code')
        self.assertEqual([(p, m) for p, m, _, _ in transport.calls], [('planner', 'astra'), ('writer', 'opus')])
        self.assertNotIn('tools', transport.calls[0][3])
        self.assertEqual(report.usage['prompt_tokens'], 20)

    def test_review_is_manual_and_does_not_execute_tools_or_enter_history(self):
        agent, transport = self.agent(['code', 'One concrete issue'], review_enabled=True, review_model=['planner', 'astra'])
        report = agent.run('write code')
        self.assertEqual(len(transport.calls), 1)
        review = agent.review_code(report.text)
        self.assertEqual(review.text, 'One concrete issue')
        self.assertEqual(transport.calls[1][:2], ('planner', 'astra'))
        self.assertNotIn('tools', transport.calls[1][3])
        self.assertEqual(transport.calls[1][3]['max_tokens'], 1200)
        self.assertEqual(len(report.events), len(agent.events))
        self.assertFalse(any(e.get('type') == 'assistant_message' and e.get('content') == review.text for e in agent.events))

    def test_review_requires_enable_and_nonempty_input(self):
        agent, transport = self.agent([])
        with self.assertRaises(PermissionError): agent.review_code('code')
        agent.review_enabled = True
        with self.assertRaises(ValueError): agent.review_code('  ')
        self.assertEqual(transport.calls, [])

    def test_bad_stage_choice_rejected_without_calling_other_provider(self):
        for pair in [['missing', 'astra'], ['planner', 'fake'], ['planner']]:
            agent, transport = self.agent([], planning='high', planning_model=pair) if len(pair) == 2 else (None, None)
            if agent:
                with self.assertRaises(ValueError): agent.run('task')
                self.assertEqual(transport.calls, [])
            else:
                with self.assertRaises(ValueError): self.agent([], planning_model=pair)

    def test_review_redacts_secrets_and_explains_truncation(self):
        secret = 'sk-synthetic-phase-test-0123456789abcdef'
        agent, transport = self.agent(['No observed issues'], review_enabled=True)
        review = agent.review_code('OPENAI_API_KEY=' + secret + '\n' + 'x' * 21000)
        self.assertNotIn(secret, json.dumps(transport.calls))
        self.assertTrue(review.truncated)
        self.assertEqual(review.reviewed_chars, 20000)
        self.assertIn('partial', str(transport.calls).lower())

    def test_review_failure_is_not_a_pass_and_native_call_is_rejected(self):
        agent, _ = self.agent([BadRequest('failure')], review_enabled=True)
        with self.assertRaises(TransportError): agent.review_code('code')
        agent, transport = self.agent(['unused'], review_enabled=True)
        transport.complete = lambda *a, **kw: ('', Usage(10, 5), {'tool_calls': [{'name': 'write_file'}]})
        with self.assertRaises(ValueError): agent.review_code('code')

    def test_review_reentry_is_denied(self):
        agent, transport = self.agent([], review_enabled=True)
        agent._run_lock.acquire()
        try:
            with self.assertRaises(RuntimeError): agent.review_code('code')
        finally: agent._run_lock.release()
        self.assertEqual(transport.calls, [])

    def test_explicit_planner_failure_does_not_fall_back_to_writer(self):
        agent, transport = self.agent([BadRequest('planner rejected')], planning='high',
                                       planning_model=['planner', 'astra'])
        report = agent.run('task')
        self.assertEqual(report.stopped, 'planning_error')
        self.assertEqual([c[:2] for c in transport.calls], [('planner', 'astra')])

    def test_manual_review_cli_uses_selected_model_and_writes_audit(self):
        from . import cli
        from .config import Config
        agent, transport = self.agent(['One observed issue'])
        output = io.StringIO()
        with patch('forge.cli._compose', return_value=Config()), \
             patch('forge.cli.ModelRouter.from_config', return_value=agent.router), redirect_stdout(output):
            code = cli.main(['review', 'print(1)', '--home', str(agent.home),
                             '--review-model', 'planner', 'astra', '--json'])
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(output.getvalue())['text'], 'One observed issue')
        self.assertEqual(transport.calls[0][:2], ('planner', 'astra'))
        logs = list((agent.home / 'sessions' / 'reviews').glob('*.jsonl'))
        self.assertEqual(len(logs), 1)
        self.assertIn('review_result', logs[0].read_text(encoding='utf-8'))

    def test_rejected_review_retains_billed_usage_and_audit(self):
        from types import SimpleNamespace
        charged = []
        ledger = SimpleNamespace(record=lambda **kw: charged.append(kw))
        agent, transport = self.agent([], review_enabled=True, cost_ledger=ledger)
        transport.complete = lambda *a, **kw: ('', Usage(10, 5), {'tool_calls': [{'name': 'write_file'}]})
        with self.assertRaises(ValueError): agent.review_code('code')
        self.assertEqual(charged[0]['tokens'], 15)
        error = next(e for e in agent.events if e.get('type') == 'review_error')
        self.assertEqual(error['usage']['prompt_tokens'], 10)
