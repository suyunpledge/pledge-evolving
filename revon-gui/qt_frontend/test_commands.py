"""从 Tk 版补回的能力：斜杠命令、命令面板（Ctrl+K）、长期记忆检索。"""
from http.server import ThreadingHTTPServer
from pathlib import Path
import tempfile
import threading
import unittest
from unittest import mock

from PySide6.QtCore import Q_ARG, QCoreApplication, QMetaObject, QObject, QUrl
from PySide6.QtQml import QQmlApplicationEngine
from PySide6.QtTest import QTest

from config_model import save_user_layer
from . import memory, slash
from .controller import ChatController
from .test_backend import until
from .test_model_controls import ROWS, ManagedGateway, Gateway
from .workbench import WorkbenchController
from ._test_app import APP, FONT

ROOT = Path(__file__).parent


def js_rows(value, *keys):
    """Plain dicts from a QML array of objects (entries hold functions, so no toVariant)."""
    if hasattr(value, 'property') and not isinstance(value, list):
        return [{k: value.property(i).property(k).toString() for k in keys}
                for i in range(value.property('length').toInt())]
    return [{k: str(row[k]) for k in keys} for row in value]


class SlashParseTests(unittest.TestCase):
    def test_model_command_matches_visible_ids_without_guessing_the_provider(self):
        titles = {'short': 'vendor/model:free', 'other': 'vendor/model:paid'}
        self.assertEqual(slash.parse('/model vendor/model:free', models=titles, model_titles=titles), ('model', 'short'))
        titles['backup'] = titles['short']
        self.assertEqual(slash.parse('/model vendor/model:free', models=titles, model_titles=titles)[0], 'error')
        self.assertEqual(slash.parse('/model backup', models=titles, model_titles=titles), ('model', 'backup'))

    def test_known_commands(self):
        roles = [('fullstack', '全栈开发师'), ('writer', '写作助手')]
        models = ['deepseekpro', 'deepseekflash', 'mimo']
        self.assertEqual(slash.parse('/new'), ('new', ''))
        self.assertEqual(slash.parse('/help'), ('palette', ''))
        self.assertEqual(slash.parse('/task 整理 README'), ('task', '整理 README'))
        self.assertEqual(slash.parse('/tools'), ('navigate', 'tools'))
        self.assertEqual(slash.parse('/effort 沉思'), ('effort', 'contemplate'))
        self.assertEqual(slash.parse('/effort high'), ('effort', 'high'))
        self.assertEqual(slash.parse('/model mimo', models=models), ('model', 'mimo'))
        self.assertEqual(slash.parse('/model deepseekp', models=models), ('model', 'deepseekpro'))
        self.assertEqual(slash.parse('/model deepseek', models=models)[0], 'error')
        self.assertEqual(slash.parse('/role 写作', roles=roles), ('role', 'writer'))
        self.assertEqual(slash.parse('/role 通用', roles=roles), ('role', ''))
        self.assertEqual(slash.parse('/effort 超级')[0], 'error')

    def test_everything_else_is_a_normal_message(self):
        for text in ('hello', '/unknown thing', '/new 带参数', '/tools\n第二行', 'a /new'):
            self.assertIsNone(slash.parse(text), text)


class FakeBridge:
    def __init__(self):
        self.queries, self.ingested = [], []

    def context_for(self, query, model=None):
        self.queries.append((query, model))
        return '<memory>用户偏好：中文回答</memory>'

    def consume_notice(self):
        return ''

    def ingest_turn_async(self, user_text, reply, *, session=None):
        self.ingested.append((user_text, reply, session))


class ControllerCommandTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.home = Path(self.temp.name)
        save_user_layer(self.home, ROWS)
        Gateway.requests, Gateway.fail = [], False
        self.server = ThreadingHTTPServer(('127.0.0.1', 0), ManagedGateway)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.controller = ChatController(self.home)
        self.controller.setManagedModels(['ds', 'mimo', 'proxy'])
        self.controller.setManagedGateway(f'http://127.0.0.1:{self.server.server_port}', {'model': 'ds'})
        until(lambda: self.controller.connected)

    def tearDown(self):
        self.controller.shutdown()
        self.controller.deleteLater()
        APP.processEvents()
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(2)
        self.temp.cleanup()

    def test_slash_commands_run_locally_and_never_reach_the_model(self):
        seen = []
        self.controller.commandRequested.connect(lambda action, arg: seen.append((action, arg)))
        for text in ('/effort 深入', '/tools', '/task 修复测试', '/help'):
            self.controller.draft = text
            self.controller.send()
            self.assertEqual(self.controller.draft, '')
        self.assertEqual(self.controller.reasoning, 'high')
        self.assertEqual(seen, [('navigate', 'tools'), ('task', '修复测试'), ('palette', '')])
        self.controller.draft = '/effort 超级'
        self.controller.send()
        self.assertEqual(self.controller.draft, '/effort 超级')
        self.assertIn('思考强度可选', self.controller.error)
        self.assertEqual(Gateway.requests, [])
        self.assertEqual(self.controller.messages.rows, [])

    def test_long_term_memory_is_injected_and_the_turn_ingested(self):
        bridge = FakeBridge()
        with mock.patch.object(memory, 'bridge_for', return_value=bridge):
            self.controller.draft = '继续上次的方案'
            self.controller.send()
            until(lambda: not self.controller.busy)
        self.assertEqual(bridge.queries, [('继续上次的方案', 'ds')])
        # Turn-local context rides after the history as a separate message, never saved to the chat.
        sent = [str(m.get('content')) for m in Gateway.requests[-1]['messages']]
        self.assertTrue(any('用户偏好：中文回答' in text for text in sent), sent)
        self.assertEqual(bridge.ingested, [('继续上次的方案', '真实流式 reply 123', self.controller.sessionId)])
        self.assertNotIn('用户偏好', str([m.content for m in self.controller._history]))


class PaletteTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.controller = ChatController(self.temp.name, demo=True)
        self.workbench = WorkbenchController(self.temp.name, demo=True)
        self.engine = QQmlApplicationEngine()
        self.warnings = []
        self.engine.warnings.connect(lambda rows: self.warnings.extend(str(x) for x in rows))
        context = self.engine.rootContext()
        for name, value in {'controller': self.controller, 'workbench': self.workbench, 'gpuEnabled': True,
                            'uiFont': FONT, 'smokeMode': True,
                            'brandUrl': QUrl.fromLocalFile(str(ROOT.parent / 'assets' / 'revon-logo-104.png'))}.items():
            context.setContextProperty(name, value)
        self.engine.load(QUrl.fromLocalFile(str(ROOT / 'qml' / 'Main.qml')))
        self.window = self.engine.rootObjects()[0]
        self.window.resize(1280, 860)
        self.window.show()
        QTest.qWait(200)

    def tearDown(self):
        self.workbench.shutdown()
        self.controller.shutdown()
        self.window.close()
        self.engine.deleteLater()
        QCoreApplication.sendPostedEvents(None, 0)
        APP.processEvents()
        self.temp.cleanup()
        self.assertFalse(self.warnings, self.warnings)

    def test_palette_lists_commands_sessions_and_runs_the_choice(self):
        palette = self.window.findChild(QObject, 'commandPalette')
        QMetaObject.invokeMethod(self.window, 'openPalette', Q_ARG('QVariant', ''))
        until(lambda: palette.property('opened'))
        items = js_rows(palette.property('items'), 'group', 'label')
        groups = {item['group'] for item in items}
        self.assertTrue({'命令', '页面', '思考', '角色', '对话'} <= groups, groups)
        self.assertTrue(any(item['label'] == '字体与阅读体验' for item in items))
        palette.setProperty('query', '思考强度 深入')
        matches = js_rows(palette.property('matches'), 'label')
        self.assertEqual([m['label'] for m in matches], ['思考强度：深入'])
        QMetaObject.invokeMethod(palette, 'runCurrent')
        until(lambda: self.controller.reasoning == 'high')
        self.assertFalse(palette.property('opened'))
        # A slash command from the composer opens the palette too.
        self.controller.commandRequested.emit('palette', '')
        until(lambda: palette.property('opened'))


if __name__ == '__main__':
    unittest.main()
