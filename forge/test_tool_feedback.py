"""Tool-result feedback: post-write checks, missing-path suggestions, per-run tool blocking."""
from pathlib import Path
import tempfile
import unittest

from .loop import Agent, LoopLimits
from .model import Completion, Usage
from .policy import Mode, Policy
from .test_project_rules import Router
from .tool_feedback import ToolBlocker, path_suggestions, write_checks
from .tools import ToolResult, ToolSpec, build_builtin_registry


def call(name, **args):
    return Completion('', Usage(1, 1, 'test', 'fake'), wire='openai', tool_calls=[{
        'id': f'{name}-{len(str(args))}-{id(args)}', 'name': name, 'args': args, 'wire': 'openai'}])


class ToolFeedbackTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.ws = self.root / 'project'
        (self.ws / 'src').mkdir(parents=True)
        (self.ws / 'src' / 'utils.py').write_text('x = 1\n', encoding='utf-8')
        (self.ws / 'README.md').write_text('# demo\n', encoding='utf-8')

    def agent(self, answers, registry=None):
        router = Router([*answers, 'done'])
        agent = Agent(home=self.root / 'home', workspace=self.ws, router=router, wire_hint='openai',
                      registry=registry or build_builtin_registry(),
                      policy=Policy(workspace=self.ws, mode=Mode.ACCEPT_EDITS, non_interactive=True),
                      limits=LoopLimits(max_steps=8), project_rules=False)
        self.addCleanup(agent.secret_scope.close)
        return agent, router

    def test_write_reports_syntax_problems_in_the_same_result(self):
        agent, _ = self.agent([
            call('write_file', path='src/bad.py', content='def f(:\n    pass\n'),
            call('write_file', path='conf.json', content='{"a": 1}'),
            call('write_file', path='notes.txt', content='plain'),
            call('apply_patch', patches=[{'path': 'src/utils.py', 'old': 'x = 1', 'new': 'x = ('}])])
        report = agent.run('write files')
        bad, good, plain, patched = (s for s in report.steps if s.tool)
        self.assertTrue(bad.decision == 'ok' and (self.ws / 'src' / 'bad.py').exists())
        self.assertIn('PROBLEM src/bad.py:1:', bad.result)
        self.assertIn('def f(:', bad.result)
        self.assertFalse(bad.meta['post_write_checks'][0]['ok'])
        self.assertIn('syntax OK: conf.json', good.result)
        self.assertNotIn('post-write check', plain.result)
        self.assertNotIn('post_write_checks', plain.meta)
        self.assertIn('PROBLEM src/utils.py', patched.result)

    def test_conflict_markers_are_flagged_for_any_text_file(self):
        text = 'a\n<<<<<<< HEAD\nmine\n=======\ntheirs\n>>>>>>> branch\n'
        note, rows = write_checks({'path': 'merge.md'}, self.ws,
                                  lambda raw: (self.ws / raw, (self.ws / raw).write_text(text, encoding='utf-8'))[0])
        self.assertEqual(rows[0]['kind'], 'ConflictMarker')
        self.assertEqual(rows[0]['line'], 2)
        self.assertIn('merge.md:2:1', note)
        heading = 'Title\n=======\n'
        (self.ws / 'doc.md').write_text(heading, encoding='utf-8')
        self.assertEqual(write_checks({'path': 'doc.md'}, self.ws, lambda raw: self.ws / raw), ('', []))

    def test_missing_path_suggests_the_closest_workspace_files(self):
        agent, _ = self.agent([call('read_file', path='src/utls.py'), call('read_file', path='utils.py')])
        report = agent.run('read utils')
        first, second = (s for s in report.steps if s.tool)
        self.assertEqual(first.decision, 'error')
        self.assertIn('Did you mean: src/utils.py?', first.result)
        self.assertEqual(second.meta['path_suggestions'][0], 'src/utils.py')
        self.assertEqual(path_suggestions('nothing-like-it.zzz', self.ws, [self.ws / 'README.md']), [])

    def test_tool_refused_as_a_whole_is_blocked_for_the_rest_of_the_run(self):
        calls = []
        registry = build_builtin_registry()

        def native(args, ctx):
            calls.append(args)
            return ToolResult(False, error='Native execution is unavailable in a protected agent',
                              meta={'authorization': 'deny', 'boundary': 'secret.isolation'})
        registry.register(ToolSpec('native_probe', 'test', native), trusted=True)
        agent, _ = self.agent([call('native_probe', n=1), call('native_probe', n=2), call('native_probe', n=3)],
                              registry=registry)
        report = agent.run('probe')
        steps = [s for s in report.steps if s.tool]
        self.assertEqual(len(calls), 1)
        self.assertIn("unavailable for the rest of this run", steps[0].result)
        self.assertEqual(steps[1].meta['error_code'], 'tool_blocked')
        blocked = [e for e in report.events if e['type'] == 'tool_blocked']
        self.assertEqual([e['repeated'] for e in blocked], [False, True, True])
        self.assertEqual(report.stopped, 'final')

    def test_repeated_denials_block_and_success_resets(self):
        blocker = ToolBlocker()
        deny = ToolResult(False, error='denied by policy: write_file', meta={'authorization': 'deny'})
        self.assertEqual(blocker.observe('write_file', deny), '')
        self.assertEqual(blocker.observe('write_file', deny), '')
        self.assertEqual(blocker.observe('write_file', ToolResult(True)), '')
        self.assertEqual(blocker.observe('write_file', deny), '')
        self.assertEqual(blocker.observe('write_file', deny), '')
        note = blocker.observe('shell_exec', ToolResult(False, error='x', meta={'boundary': 'secret.isolation'}),
                               available=['git_status', 'grep'])
        self.assertIn('Use instead: git_status, grep.', note)
        self.assertTrue(blocker.observe('write_file', deny))
        self.assertTrue(blocker.refusal('write_file'))


if __name__ == '__main__':
    unittest.main()
