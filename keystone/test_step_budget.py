"""Step-budget awareness: transient reminders near max_steps and a tool-free wrap-up summary."""
import copy
import json
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from .context_transaction import valid_tool_pairs
from .loop import Agent, LoopLimits
from .model import Completion, RateLimited, TransportError, Usage
from .policy import Mode, Policy
from .test_project_rules import Router
from .tools import build_builtin_registry


def read(path, wire='openai'):
    return Completion('', Usage(1, 1, 'test', 'fake'), wire=wire, tool_calls=[{
        'id': 'read-' + path, 'name': 'read_file', 'args': {'path': path}, 'wire': wire}])


class FailingSummaryRouter(Router):
    def complete(self, messages, **kwargs):
        if 'step budget for this run is used up' in str(messages[-1]):
            self.calls.append(copy.deepcopy(messages))
            raise TransportError('upstream down')
        return super().complete(messages, **kwargs)


BAD = '<tool_call>{invalid}</tool_call>'


class RaisingRouter(Router):
    def complete(self, messages, **kwargs):
        value = next(self.answers)
        self.calls.append(copy.deepcopy(messages))
        if isinstance(value, Exception):
            raise value
        return value if isinstance(value, Completion) else Completion(value, Usage(1, 1, 'test', 'fake'))


class StepBudgetTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.ws = self.root / 'project'
        self.ws.mkdir()
        for i in range(8):
            (self.ws / f'f{i}.txt').write_text(f'value {i}', encoding='utf-8')

    def agent(self, router, max_steps=6, wire_hint='openai', **limits):
        agent = Agent(home=self.root / 'home', workspace=self.ws, router=router, wire_hint=wire_hint,
            registry=build_builtin_registry(), policy=Policy(workspace=self.ws, mode=Mode.READ_ONLY),
            limits=LoopLimits(context_chars=100000, max_steps=max_steps, **limits), project_rules=False)
        self.addCleanup(agent.secret_scope.close)
        return agent

    def test_reminders_are_transient_and_summary_follows_the_limit(self):
        for wire in ('openai', 'anthropic'):
            with self.subTest(wire=wire):
                router = Router([*(read(f'f{i}.txt', wire) for i in range(6)), 'Read six files; two remain.'])
                agent = self.agent(router, wire_hint=wire)
                report = agent.run('Read every file.')
                self.assertEqual(report.stopped, 'max_steps')
                self.assertTrue(report.text.startswith('[stopped: max_steps=6]'))
                self.assertIn('Read six files; two remain.', report.text)
                self.assertEqual(len(router.calls), 7)
                # turns 1-4 carry no reminder; turns 5 and 6 carry one each, on the request tail only
                for call in router.calls[:4]:
                    self.assertNotIn('Step budget', str(call))
                self.assertIn('2 model turns left', str(router.calls[4][-1]))
                self.assertIn('last model turn', str(router.calls[5][-1]))
                self.assertNotIn('2 model turns left', str(router.calls[5]))
                self.assertIn('used up', str(router.calls[6][-1]))
                warnings = [e for e in report.events if e['type'] == 'step_budget_warning']
                self.assertEqual([e['remaining'] for e in warnings], [2, 1])
                self.assertTrue(any(e['type'] == 'step_budget_summary' and e['ok'] for e in report.events))
                self.assertEqual(report.usage['prompt_tokens'], 7)
                saved = agent._conversation_prefix._saved['messages']
                self.assertTrue(valid_tool_pairs(saved))
                self.assertNotIn('Step budget', str(saved))
                self.assertNotIn('used up', str(saved))

    def test_summary_failure_keeps_the_plain_stop(self):
        router = FailingSummaryRouter([read(f'f{i}.txt') for i in range(6)])
        report = self.agent(router).run('Read every file.')
        self.assertEqual(report.stopped, 'max_steps')
        self.assertEqual(report.text, '[stopped: max_steps=6] last tool: read_file')
        self.assertTrue(any(e['type'] == 'step_budget_summary' and not e['ok'] for e in report.events))

    def test_tool_attempt_in_summary_is_discarded(self):
        router = Router([*(read(f'f{i}.txt') for i in range(6)), read('f7.txt')])
        report = self.agent(router).run('Read every file.')
        self.assertEqual(report.text, '[stopped: max_steps=6] last tool: read_file')
        self.assertEqual(sum(1 for s in report.steps if s.tool), 6)

    def test_small_budgets_and_opt_out_keep_legacy_behaviour(self):
        for steps, wrap in ((5, True), (6, False)):
            with self.subTest(steps=steps, wrap=wrap):
                router = Router([read(f'f{i}.txt') for i in range(steps)])
                report = self.agent(router, max_steps=steps, wrap_up_on_limit=wrap).run('Read every file.')
                self.assertEqual(len(router.calls), steps)
                self.assertNotIn('Step budget', str(router.calls))
                self.assertEqual(report.text, f'[stopped: max_steps={steps}] last tool: read_file')

    # -- 第三组：哪些回合在消耗步数 ------------------------------------------
    def test_format_errors_count_only_when_consecutive(self):
        router = Router([BAD, read('f0.txt'), BAD, read('f1.txt'), BAD, read('f2.txt'), 'done'])
        report = self.agent(router, max_steps=8).run('Read files.')
        self.assertEqual(report.stopped, 'final')
        self.assertEqual(report.text, 'done')
        router = Router([BAD] * 5)
        self.assertEqual(self.agent(router, max_steps=8).run('Read files.').stopped, 'format_error')
        self.assertEqual(len(router.calls), 3)

    def test_format_error_turns_are_refunded_up_to_a_cap(self):
        router = Router([BAD, BAD, *(read(f'f{i}.txt') for i in range(4)), 'done'])
        report = self.agent(router, max_steps=6).run('Read files.')
        self.assertEqual(report.stopped, 'final')
        extended = [e for e in report.events if e['type'] == 'step_budget_extended']
        self.assertEqual([e['reason'] for e in extended], ['format_error'] * 2)
        # 第 4 次起不再退还：3 次退还后预算回到 max_steps + 3
        answers = [BAD, BAD, read('f0.txt'), BAD, BAD, read('f1.txt'), *(read(f'f{i}.txt') for i in range(2, 8))]
        router = Router(answers)
        report = self.agent(router, max_steps=6).run('Read files.')
        self.assertEqual(report.stopped, 'max_steps')
        self.assertEqual(len([c for c in router.calls if 'used up' not in str(c[-1])]), 9)

    def test_acceptance_repair_keeps_room_to_fix_and_answer(self):
        result = self.ws / 'result.json'
        result.write_text('{"ready":false}', encoding='utf-8')
        fix = Completion('', Usage(1, 1, 'test', 'fake'), wire='openai', tool_calls=[{
            'id': 'fix', 'name': 'write_file', 'args': {'path': 'result.json', 'content': '{"ready":true}'},
            'wire': 'openai'}])
        router = Router([*(read(f'f{i}.txt') for i in range(5)), 'done', fix, 'done'])
        agent = Agent(home=self.root / 'home', workspace=self.ws, router=router, wire_hint='openai',
                      registry=build_builtin_registry(),
                      policy=Policy(workspace=self.ws, mode=Mode.ACCEPT_EDITS, non_interactive=True),
                      limits=LoopLimits(max_steps=6), project_rules=False)
        self.addCleanup(agent.secret_scope.close)
        report = agent.run('finish', acceptance={'max_attempts': 2, 'criteria': [{
            'id': 'environment', 'kind': 'env_probe', 'env': 'workspace', 'probe': 'json_value',
            'input': {'path': 'result.json', 'pointer': '/ready'}, 'expect': True}]})
        self.assertEqual(report.stopped, 'final')
        self.assertEqual(json.loads(result.read_text(encoding='utf-8')), {'ready': True})
        self.assertTrue(any(e['type'] == 'step_budget_extended' and e['reason'] == 'acceptance_repair'
                            for e in report.events))

    def test_model_retries_are_per_step_with_a_run_budget_and_retry_after(self):
        sleeps = []
        answers = [RateLimited('429', retry_after=7), read('f0.txt'),
                   TransportError('502'), read('f1.txt'),
                   TransportError('503'), read('f2.txt'),
                   TransportError('504'), 'never reached']
        router = RaisingRouter(answers)
        with mock.patch('keystone.loop.time.sleep', sleeps.append):
            report = self.agent(router, max_steps=8).run('Read files.')
        self.assertEqual(report.stopped, 'error')
        self.assertEqual(sleeps, [7.0, 10.0, 20.0])
        retries = [e for e in report.events if e['type'] == 'model_retry']
        self.assertEqual([e['retries_left'] for e in retries], [2, 1, 0])
        self.assertEqual(sum(1 for s in report.steps if s.tool), 3)
        with mock.patch('keystone.loop.time.sleep', sleeps.append):
            sleeps.clear()
            ok = self.agent(RaisingRouter([RateLimited('429', retry_after=900), 'done'])).run('hi')
        self.assertEqual((ok.stopped, sleeps), ('final', [60.0]))


if __name__ == '__main__':
    unittest.main()
