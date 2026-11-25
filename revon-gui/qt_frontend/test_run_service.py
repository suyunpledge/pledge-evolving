"""Code mode run/build/test service: permission gating, planning and problem publication."""
from pathlib import Path
import sys
import tempfile
import threading
import unittest
from unittest.mock import Mock

from revon.config import load_config
from revon.desktop_permissions import profile_policy
from revon.policy import Policy
from revon.development.toolchains import Toolchains
from .run_service import RunService


class Pages:
    def __init__(self):
        self.active_document, self.unsaved, self.refreshed = '', False, 0

    def refresh_documents(self):
        self.refreshed += 1


class Terminal:
    def __init__(self):
        self.text = []

    def publish(self, text, reveal=False):
        self.text.append(text)


class Host:
    def __init__(self, home, workspace, profile='full'):
        self.home, self.workspace, self.profile, self.demo = Path(home), Path(workspace), profile, False
        self.pages, self.terminal = Pages(), Terminal()
        self.events, self.approvals, self.answer = [], [], True
        self.cancel, self.closed = threading.Event(), threading.Event()

    def emit(self, event, **data):
        self.events.append((event, data))

    def state(self):
        return [d['state'] for e, d in self.events if e == 'run_state'][-1]

    def policy(self):
        return profile_policy(Policy.from_config(load_config(self.home), workspace=self.workspace,
                                                 non_interactive=False), self.profile)

    def approve(self, title, detail, cancel=None):
        self.approvals.append((title, detail))
        return self.answer

    def work(self, fn, *, cancel_event):
        self.cancel = cancel_event
        self.cancel.clear()
        fn()


class RunServiceTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(prefix='revon run 中文 ')
        self.addCleanup(temp.cleanup)
        base = Path(temp.name).resolve()
        self.project = base / '项目 空格'
        self.project.mkdir()
        (base / 'home').mkdir()
        self.script = self.project / '主 程序.py'
        self.script.write_text('print("运行 ok")\nraise RuntimeError("第二行")\n', encoding='utf-8')
        self.host = Host(base / 'home', self.project)
        self.host.pages.active_document = str(self.script)
        self.service = RunService(self.host)
        # Describe the machine: only this interpreter is on PATH.
        self.service.chains = lambda: Toolchains(self.project, which=lambda n: sys.executable if n == 'python' else None)

    def test_publishes_toolchains_and_file_tasks(self):
        self.service.publish()
        state = self.host.state()
        self.assertEqual(state['language'], 'python')
        self.assertEqual([r['language'] for r in state['toolchains']][:1], ['python'])
        self.assertIn('file.run', [t['id'] for t in state['tasks']])
        self.assertTrue(state['canRun'])

    def test_status_updates_reuse_plan_and_refresh_finds_new_project_tasks(self):
        self.service.chains = Mock(wraps=self.service.chains)
        self.service.publish()
        self.service.command({'action': 'clearProblems'})
        self.assertEqual(self.service.chains.call_count, 1)
        (self.project / 'pyproject.toml').write_text('[project]\nname = "example"\n', encoding='utf-8')
        self.service.command({'action': 'refresh'})
        self.assertEqual(self.service.chains.call_count, 2)
        self.assertTrue(any(t['scope'] == 'project' for t in self.host.state()['tasks']))

    def test_manual_profile_asks_and_maps_problems(self):
        self.host.profile = 'manual'
        self.service.command({'action': 'run'})
        self.assertEqual(len(self.host.approvals), 1)
        self.assertIn(str(self.script), self.host.approvals[0][1]['commands'][0])
        state = self.host.state()
        self.assertEqual(state['last']['status'], 'failed')
        self.assertEqual((state['problems'][0]['path'], state['problems'][0]['line']), (str(self.script), 2))
        self.assertIn('运行 ok', ''.join(self.host.terminal.text))
        self.assertEqual(self.host.pages.refreshed, 1)
        self.service.command({'action': 'clearProblems'})
        self.assertEqual(self.host.state()['problems'], [])

    def test_declined_approval_and_sandbox_run_nothing(self):
        self.script.write_text('open("ran.txt", "w").write("x")\n', encoding='utf-8')
        self.host.profile, self.host.answer = 'manual', False
        with self.assertRaises(PermissionError):
            self.service.command({'action': 'run'})
        self.assertEqual(self.host.state()['running'], '')
        self.host.profile = 'sandbox'
        with self.assertRaises(PermissionError):
            self.service.command({'action': 'run'})
        self.assertFalse((self.project / 'ran.txt').exists())
        self.assertFalse(self.host.state()['canRun'])

    def test_cancel_before_worker_starts_then_run_again(self):
        self.script.write_text('with open("runs.txt", "a") as f: f.write("1")\n', encoding='utf-8')
        self.host.profile = 'manual'
        pending = []
        self.host.work = lambda fn, **kwargs: pending.append(fn)
        self.service.command({'action': 'run'})
        self.service.command({'action': 'cancel'})
        pending.pop()()
        self.assertEqual(self.host.state()['last']['status'], 'cancelled')
        self.assertEqual(self.host.approvals, [])
        marker = self.project / 'runs.txt'
        self.assertFalse(marker.exists())
        del self.host.work
        self.service.command({'action': 'run'})
        self.assertEqual(self.host.state()['last']['status'], 'completed')
        self.assertEqual(len(self.host.approvals), 1)
        self.assertEqual(marker.read_text(), '1')

    def test_unsaved_editor_and_missing_task_are_reported(self):
        self.host.pages.unsaved = True
        with self.assertRaises(ValueError):
            self.service.command({'action': 'run'})
        self.host.pages.unsaved = False
        self.host.pages.active_document = str(self.project / 'notes.md')
        with self.assertRaises(LookupError):
            self.service.command({'action': 'build'})
        self.assertIn('pyproject.toml', ''.join(self.host.terminal.text))


if __name__ == '__main__':
    unittest.main()
