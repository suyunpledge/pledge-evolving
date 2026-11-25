"""Role templates in the Qt app: picker state, persistence, and that a role only ever narrows."""
import json
from http.server import ThreadingHTTPServer
from pathlib import Path
import tempfile
import threading
import unittest

from revon.agent_templates import get_template
from .controller import ChatController
from .conversation import MAX_TURNS, turn_limit
from revon.loop_limits import NO_STEP_LIMIT
from .permissions import authorize
from .test_backend import Gateway, until

from ._test_app import APP


class RoleTemplateServiceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.home = Path(self.temp.name)
        self.controllers = []

    def tearDown(self):
        for controller in self.controllers:
            controller.shutdown()
            controller.deleteLater()
        APP.processEvents()
        self.temp.cleanup()

    def make(self, **options):
        controller = ChatController(self.home, **options)
        self.controllers.append(controller)
        return controller

    def test_lists_builtins_and_persists_the_active_role(self):
        roles = self.make().templates
        self.assertEqual(roles.activeId, '')
        self.assertEqual(roles.activeName, '通用助手')
        self.assertEqual([row['name'] for row in roles.rows][:7],
                         ['全栈开发师', '建模设计师', '学习助手', '智能管家', '写作助手', '代码审查员', '信息调研师'])
        roles.select('researcher')
        self.assertEqual((roles.activeName, roles.activeIcon), ('信息调研师', 'globe'))
        self.assertTrue(roles.activeStarters)
        self.assertTrue(next(row for row in roles.rows if row['id'] == 'researcher')['active'])
        self.assertEqual(self.make().templates.activeId, 'researcher')  # survives a restart
        roles.select('no-such-role')
        self.assertEqual(roles.activeId, 'researcher')

    def test_duplicate_edit_and_delete_a_copy(self):
        roles = self.make().templates
        copy = roles.duplicate('writer')
        self.assertEqual(copy, 'writer-copy')
        self.assertTrue((self.home / 'agents' / 'writer-copy.json').is_file())
        self.assertTrue(roles.save(copy, '公文写作', '按公文格式起草。', '你是公文写作助手。', '写通知\n\n写请示'))
        saved = json.loads((self.home / 'agents' / 'writer-copy.json').read_text(encoding='utf-8'))
        self.assertEqual((saved['name'], saved['starters']), ('公文写作', ['写通知', '写请示']))
        self.assertEqual(saved['policy']['mode'], 'acceptEdits')  # permissions follow the source
        self.assertFalse(roles.save('writer', 'x', 'y', 'z', ''))  # built-ins are read-only
        self.assertIn('复制', roles.error)
        roles.select(copy)
        roles.remove(copy)
        self.assertEqual(roles.activeId, '')
        self.assertFalse((self.home / 'agents' / 'writer-copy.json').exists())

    def test_demo_never_writes_into_the_home(self):
        roles = self.make(demo=True).templates
        self.assertEqual(roles.activeId, 'fullstack')
        self.assertTrue(roles.duplicate('tutor'))
        self.assertFalse((self.home / 'agents').exists())

    def test_starter_fills_the_composer(self):
        controller = self.make()
        controller.templates.select('tutor')
        controller.templates.useStarter(controller.templates.activeStarters[0])
        self.assertEqual(controller.draft, controller.templates.activeStarters[0])

    def test_role_narrows_the_work_tool_filter(self):
        controller = self.make()
        _, allowed = controller.toolSettings.snapshot()
        self.assertTrue(allowed('keystone_write_file') and allowed('mcp_remote'))
        controller.templates.select('reviewer')
        _, allowed = controller.toolSettings.snapshot()
        self.assertTrue(allowed('keystone_read_file'))
        self.assertFalse(allowed('keystone_write_file'))
        self.assertFalse(allowed('keystone_shell_exec'))
        self.assertFalse(allowed('mcp_remote'))
        controller.templates.select('researcher')
        _, allowed = controller.toolSettings.snapshot()
        self.assertTrue(allowed('keystone_public_search'))  # desktop alias of web_search


class RolePermissionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.workspace = Path(self.temp.name)
        self.asked = []

    def tearDown(self):
        self.temp.cleanup()

    def approve(self, answer):
        def ask(name, args, cancel):
            self.asked.append(name)
            return answer
        return ask

    def test_reviewer_cannot_write_even_with_full_access(self):
        reviewer = get_template('reviewer')
        with self.assertRaisesRegex(PermissionError, '代码审查员'):
            authorize('full', self.workspace, 'keystone_write_file', {'path': 'a.txt', 'content': 'x'},
                      self.approve(True), template=reviewer)
        authorize('full', self.workspace, 'keystone_read_file', {'path': 'a.txt'}, self.approve(True),
                  template=reviewer)
        self.assertEqual(self.asked, [])

    def test_role_ask_adds_an_approval_but_never_removes_one(self):
        tutor = get_template('tutor')  # writes ask
        authorize('sandbox', self.workspace, 'keystone_write_file', {'path': 'notes.md', 'content': 'x'},
                  self.approve(True), template=tutor)
        self.assertEqual(self.asked, ['write_file'])
        with self.assertRaisesRegex(PermissionError, '未批准'):
            authorize('sandbox', self.workspace, 'keystone_write_file', {'path': 'notes.md', 'content': 'x'},
                      self.approve(False), template=tutor)
        # Manual approval asks for everything; a write-happy role does not lift that.
        self.asked.clear()
        authorize('manual', self.workspace, 'keystone_write_file', {'path': 'a.py', 'content': 'x'},
                  self.approve(True), template=get_template('fullstack'))
        self.assertEqual(self.asked, ['write_file'])

    def test_researcher_cannot_write_outside_the_workspace_under_full_access(self):
        outside = str(Path(self.workspace.anchor) / 'elsewhere' / 'report.md')
        with self.assertRaises(PermissionError):
            authorize('full', self.workspace, 'keystone_write_file', {'path': outside, 'content': 'x'},
                      self.approve(True), template=get_template('researcher'))

    def test_turn_limit_uses_the_role_budget_under_the_users_ceiling(self):
        with tempfile.TemporaryDirectory() as home:
            self.assertEqual(turn_limit(home), NO_STEP_LIMIT)
            self.assertEqual(turn_limit(home, get_template('fullstack')), 40)
            self.assertEqual(turn_limit(home, get_template('tutor')), 16)
            Path(home, 'revon.patch.json').write_text(
                json.dumps([{'id': 'loop', 'config': {'maxSteps': 9}}]), encoding='utf-8')
            self.assertEqual(turn_limit(home, get_template('fullstack')), 9)
            # "No limit" set by the user (0) is also a ceiling: no role budget applies.
            Path(home, 'revon.patch.json').write_text(
                json.dumps([{'id': 'loop', 'config': {'maxSteps': 0}}]), encoding='utf-8')
            self.assertEqual(turn_limit(home, get_template('tutor')), NO_STEP_LIMIT)


class RoleGuidanceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        Gateway.requests, Gateway.fail = [], False
        self.server = ThreadingHTTPServer(('127.0.0.1', 0), Gateway)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.controller = ChatController(Path(self.temp.name))
        self.controller.setGateway(f'http://127.0.0.1:{self.server.server_port}')
        until(lambda: self.controller.connected)

    def tearDown(self):
        self.controller.shutdown()
        self.controller.deleteLater()
        APP.processEvents()
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(2)
        self.temp.cleanup()

    def test_active_role_guidance_rides_the_system_context(self):
        for mode in ('work', 'chat'):
            with self.subTest(mode):
                self.controller.setConversationMode(mode)
                self.controller.templates.select('butler' if mode == 'work' else 'writer')
                self.controller.draft = '安排一下明天'
                self.controller.send()
                until(lambda: not self.controller.busy)
                first = Gateway.requests[-1]['messages'][0]
                self.assertEqual(first['role'], 'system')
                name = '智能管家' if mode == 'work' else '写作助手'
                self.assertIn(f'[角色模板：{name}]', first['content'])
                self.controller.newSession()
        self.controller.templates.select('')
        self.controller.draft = '普通问题'
        self.controller.send()
        until(lambda: not self.controller.busy)
        self.assertNotIn('角色模板', json.dumps(Gateway.requests[-1]['messages'], ensure_ascii=False))


if __name__ == '__main__':
    unittest.main()
