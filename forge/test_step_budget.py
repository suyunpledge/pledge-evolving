"""Step-budget awareness: transient reminders near max_steps and a tool-free wrap-up summary."""
import copy
from pathlib import Path
import tempfile
import unittest

from .context_transaction import valid_tool_pairs
from .loop import Agent, LoopLimits
from .model import Completion, TransportError, Usage
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


if __name__ == '__main__':
    unittest.main()
