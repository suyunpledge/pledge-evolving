"""思考强度控件与已有模型编辑：档位随模型变化、偏好持久化、编辑往返且密钥不外泄。"""
import json
from http.server import ThreadingHTTPServer
from pathlib import Path
import tempfile
import threading
import unittest

from PySide6.QtCore import QCoreApplication, QObject, QUrl
from PySide6.QtQml import QQmlApplicationEngine
from PySide6.QtTest import QTest

from config_model import load_user_layer, save_user_layer
import secret_store
from . import effort
from .controller import ChatController
from .model_rows import ModelRows, edited_rows, env_ref, removed_rows
from .test_backend import Gateway, until
from .workbench import WorkbenchController
from ._test_app import APP, FONT

ROOT = Path(__file__).parent
SECRET = 'sk-test-0123456789abcdef'


def provider(row_id, model, url, label, **extra):
    return {'id': row_id, 'name': f'provider:{row_id}', 'config': {
        'wire': 'openai', 'baseURL': url, 'model': model, 'modelLabel': label, 'temperature': 0.7,
        'apiKey': {'$expr': f"get('env.FORGE_{row_id.upper().replace('-', '_')}_KEY', '')"}, **extra}}


ROWS = [
    provider('ds-1', 'deepseek-v4-pro', 'https://api.deepseek.com', 'ds'),
    provider('mimo-1', 'mimo-v2.6-pro', 'https://token-plan-cn.xiaomimimo.com/v1', 'mimo'),
    provider('proxy-1', 'house-model', 'https://llm.example.test/v1', 'proxy',
             routeCapabilities={'reasoningEfforts': ['high']}),
]


class ManagedGateway(Gateway):
    """Loopback gateway that serves the managed model labels, like the desktop gateway does."""
    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(json.dumps({'data': [{'id': m} for m in ('ds', 'mimo', 'proxy')]}).encode())


class EffortSupportTests(unittest.TestCase):
    def test_levels_follow_declarations_then_vendor_profile(self):
        # 沉思 is always offered: it also turns on Keystone task contemplation.
        self.assertEqual(effort.support(ROWS[2]['config'])['levels'], ['off', 'high', 'contemplate'])
        none = effort.support({**ROWS[2]['config'], 'routeCapabilities': {'reasoningEfforts': []}})
        self.assertEqual((none['levels'], none['supported']), (['off', 'contemplate'], False))
        mimo = effort.support(ROWS[1]['config'])
        self.assertEqual((mimo['levels'], mimo['supported']), (['off', 'contemplate'], False))
        self.assertTrue(mimo['hint'])
        self.assertEqual(effort.support(ROWS[0]['config'])['levels'], ['off', 'low', 'high', 'contemplate'])
        unknown = effort.support({'baseURL': 'https://unknown.example.test/v1', 'model': 'x'})
        self.assertEqual(unknown['levels'], list(effort.LEVELS))

    def test_effective_level_never_exceeds_the_choice(self):
        self.assertEqual(effort.effective('medium', ['off', 'low', 'high']), 'low')
        self.assertEqual(effort.effective('high', ['off', 'low', 'high']), 'high')
        self.assertEqual(effort.effective('medium', ['off', 'high']), 'off')
        self.assertEqual(effort.effective('high', ['off']), 'off')
        self.assertEqual(effort.wire_effort('contemplate', ['off', 'low', 'high', 'contemplate']), 'high')
        self.assertEqual(effort.wire_effort('contemplate', ['off', 'low', 'contemplate']), 'low')
        self.assertIsNone(effort.wire_effort('contemplate', ['off', 'contemplate']))
        self.assertIsNone(effort.wire_effort('off', list(effort.LEVELS)))
        self.assertEqual([effort.task_thinking(l) for l in effort.LEVELS], ['off', 'smart', 'smart', 'smart', 'on'])


class ControllerEffortTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.home = Path(self.temp.name)
        save_user_layer(self.home, ROWS)
        Gateway.requests, Gateway.fail = [], False
        self.server = ThreadingHTTPServer(('127.0.0.1', 0), ManagedGateway)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.url = f'http://127.0.0.1:{self.server.server_port}'
        self.controller = ChatController(self.home)
        self.controller.setManagedModels(['ds', 'mimo', 'proxy'])

    def tearDown(self):
        self.controller.shutdown()
        self.controller.deleteLater()
        APP.processEvents()
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(2)
        self.temp.cleanup()

    def connect(self, model):
        self.controller.setManagedGateway(self.url, {'model': model})
        until(lambda: self.controller.connected)

    def send(self):
        Gateway.requests.clear()
        self.controller.draft = '你好'
        self.controller.send()
        until(lambda: not self.controller.busy)
        return Gateway.requests[-1]

    def test_effort_is_bound_to_the_current_model_and_sent_clamped(self):
        self.connect('ds')
        self.controller.setReasoning('medium')
        self.assertEqual(list(self.controller.reasoningLevels), ['off', 'low', 'high', 'contemplate'])
        self.assertTrue(self.controller.reasoningSupported)
        self.assertEqual(self.controller.effectiveReasoning, 'low')
        self.assertEqual(self.send().get('reasoning_effort'), 'low')
        self.controller.setReasoning('high')
        self.assertEqual(self.send().get('reasoning_effort'), 'high')
        self.connect('mimo')
        self.assertFalse(self.controller.reasoningSupported)
        self.assertEqual(self.controller.effectiveReasoning, 'off')
        self.assertIsNone(self.send().get('reasoning_effort'))
        self.connect('proxy')
        self.assertEqual(list(self.controller.reasoningLevels), ['off', 'high', 'contemplate'])
        self.assertEqual(self.send().get('reasoning_effort'), 'high')

    def thinking_mode(self):
        row = next((r for r in load_user_layer(self.home) if r['id'] == 'thinking'), {})
        return row.get('config', {}).get('mode', 'off')

    def test_contemplate_sends_top_effort_and_turns_on_task_contemplation(self):
        self.connect('ds')
        self.assertEqual(self.thinking_mode(), 'off')
        self.controller.setReasoning('contemplate')
        self.assertEqual(self.controller.effectiveReasoning, 'contemplate')
        self.assertEqual(self.send().get('reasoning_effort'), 'high')
        self.assertEqual(self.thinking_mode(), 'on')
        self.connect('mimo')
        self.assertEqual(self.controller.effectiveReasoning, 'contemplate')
        self.assertIsNone(self.send().get('reasoning_effort'))
        self.controller.setReasoning('low')
        self.assertEqual(self.thinking_mode(), 'smart')
        self.controller.setReasoning('off')
        self.assertEqual(self.thinking_mode(), 'off')
        # Other rows of the user layer are untouched.
        self.assertEqual([r['id'] for r in load_user_layer(self.home)], [r['id'] for r in ROWS] + ['thinking'])

    def test_choice_persists_like_the_model_choice(self):
        self.connect('ds')
        self.controller.setReasoning('low')
        self.controller.setReasoning('bogus')
        reopened = ChatController(self.home)
        try:
            self.assertEqual(reopened.reasoning, 'low')
            self.assertEqual(reopened.model, 'ds')
        finally:
            reopened.shutdown()
            reopened.deleteLater()


class ModelRowEditingTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.home = Path(self.temp.name)
        shared = provider('ds-2', 'deepseek-flash', 'https://api.deepseek.com', 'flash')
        shared['config']['apiKey'] = {'$expr': "get('env.FORGE_DS_1_KEY', '')"}
        save_user_layer(self.home, [*ROWS, shared, {'id': 'planning', 'name': 'planning:level',
                                                   'config': {'model': ['mimo-1', 'mimo-v2.6-pro']}}])
        self.secrets = self.home / 'secrets.json'
        secret_store.save({'ds-1': 'sk-original-key-000000'}, path=self.secrets)
        self.service = ModelRows(self.home)
        self.service.reload()

    def tearDown(self):
        self.service.deleteLater()
        APP.processEvents()
        self.temp.cleanup()

    def row(self, row_id):
        return next(r for r in load_user_layer(self.home) if r['id'] == row_id)

    def values(self, row_id, **change):
        view = next(r for r in self.service.rows if r['id'] == row_id)
        return {**{k: view[k] for k in ('label', 'baseURL', 'model', 'wire', 'temperature', 'efforts')}, **change}

    def test_projection_reports_key_state_without_values(self):
        rows = {r['id']: r for r in self.service.rows}
        self.assertEqual(rows['ds-1']['keyState'], 'saved')
        self.assertEqual(rows['ds-2']['keyState'], 'saved')
        self.assertEqual(rows['mimo-1']['keyState'], 'missing')
        self.assertEqual(rows['proxy-1']['efforts'], 'high')
        self.assertNotIn('sk-original', json.dumps(self.service.rows))
        self.assertNotIn('planning', rows)

    def test_edit_round_trips_and_keeps_legacy_reference_without_new_key(self):
        before = self.row('ds-1')
        self.service.save('ds-1', self.values('ds-1', label='DeepSeek Pro', baseURL='https://api.deepseek.com/v1/',
                                               model='deepseek-v4-pro-0601', temperature='0.3',
                                               efforts='low,high'), '')
        self.assertTrue(self.service.success, self.service.status)
        after = self.row('ds-1')['config']
        self.assertEqual(after['baseURL'], 'https://api.deepseek.com/v1')
        self.assertEqual((after['model'], after['modelLabel'], after['temperature']),
                         ('deepseek-v4-pro-0601', 'DeepSeek Pro', 0.3))
        self.assertEqual(after['routeCapabilities'], {'reasoningEfforts': ['low', 'high']})
        self.assertEqual(after['apiKey'], before['config']['apiKey'])
        self.assertEqual(secret_store.load(path=self.secrets), {'ds-1': 'sk-original-key-000000'})
        view = next(r for r in self.service.rows if r['id'] == 'ds-1')
        self.assertEqual(view['efforts'], 'low,high')
        self.service.save('ds-1', self.values('ds-1', efforts='auto', label=''), '')
        after = self.row('ds-1')['config']
        self.assertNotIn('routeCapabilities', after)
        self.assertNotIn('modelLabel', after)

    def test_new_key_goes_to_the_secret_store_only(self):
        self.service.save('ds-1', self.values('ds-1'), SECRET)
        self.assertTrue(self.service.success, self.service.status)
        layer = (self.home / 'keystone.patch.json').read_text(encoding='utf-8')
        self.assertNotIn(SECRET, layer)
        # Own legacy FORGE_* reference stays; the store injects both names for this row id.
        self.assertEqual(env_ref(self.row('ds-1')['config']), 'FORGE_DS_1_KEY')
        self.assertEqual(secret_store.load(path=self.secrets)['ds-1'], SECRET)
        self.assertEqual(secret_store.env_for(secret_store.load(path=self.secrets))['FORGE_DS_1_KEY'], SECRET)
        self.assertNotIn(SECRET, json.dumps(self.service.rows))
        # A row borrowing another row's key gets its own KEYSTONE_* reference; the lender is untouched.
        self.service.save('ds-2', self.values('ds-2'), 'sk-second-key-111111')
        self.assertEqual(env_ref(self.row('ds-2')['config']), 'KEYSTONE_DS_2_KEY')
        self.assertEqual(env_ref(self.row('ds-1')['config']), 'FORGE_DS_1_KEY')
        stored = secret_store.load(path=self.secrets)
        self.assertEqual((stored['ds-1'], stored['ds-2']), (SECRET, 'sk-second-key-111111'))

    def test_invalid_edits_are_rejected_without_writing(self):
        before = load_user_layer(self.home)
        for change in ({'baseURL': 'ftp://x'}, {'model': ''}, {'wire': 'grpc'}, {'temperature': '3'},
                       {'efforts': 'turbo'}, {'baseURL': 'https://user:pw@host.test/v1'}):
            self.service.save('ds-1', self.values('ds-1', **change), '')
            self.assertFalse(self.service.success, change)
        self.service.save('ds-1', self.values('ds-1'), 'two words')
        self.assertFalse(self.service.success)
        self.assertEqual(load_user_layer(self.home), before)
        self.assertEqual(secret_store.load(path=self.secrets), {'ds-1': 'sk-original-key-000000'})

    def test_delete_refuses_referenced_rows_and_keeps_shared_keys(self):
        with self.assertRaises(ValueError):
            removed_rows(load_user_layer(self.home), 'mimo-1')
        self.service.remove('ds-1')
        self.assertTrue(self.service.success, self.service.status)
        self.assertNotIn('ds-1', [r['id'] for r in load_user_layer(self.home)])
        # ds-2 still reads FORGE_DS_1_KEY, so the stored credential stays.
        self.assertIn('ds-1', secret_store.load(path=self.secrets))
        self.service.remove('ds-2')
        self.assertEqual([r['id'] for r in self.service.rows], ['mimo-1', 'proxy-1'])

    def test_unknown_row_is_reported(self):
        with self.assertRaises(ValueError):
            edited_rows(load_user_layer(self.home), 'planning', {'baseURL': 'https://a.test', 'model': 'm'})


class MainScreenControlTests(unittest.TestCase):
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
                            'brandUrl': QUrl.fromLocalFile(str(ROOT.parent / 'assets' / 'keystone-logo-104.png'))}.items():
            context.setContextProperty(name, value)
        self.engine.load(QUrl.fromLocalFile(str(ROOT / 'qml' / 'Main.qml')))
        self.assertTrue(self.engine.rootObjects(), self.warnings)
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

    def test_effort_button_sits_in_the_composer_and_follows_the_controller(self):
        button = self.window.findChild(QObject, 'effortButton')
        composer = self.window.findChild(QObject, 'composer')
        self.assertIsNotNone(button)
        self.assertTrue(button.property('visible'))
        self.assertIn('标准', button.property('text'))
        self.controller.setReasoning('high')
        self.assertIn('深入', button.property('text'))
        point = button.mapToItem(composer, 0, 0)
        self.assertGreaterEqual(point.x(), 0)
        self.assertLessEqual(point.x() + button.property('width'), composer.property('width'))
        picker = self.window.findChild(QObject, 'effortPicker')
        button.clicked.emit()
        until(lambda: picker.property('opened'))
        slider = self.window.findChild(QObject, 'effortSlider')
        self.assertEqual(slider.property('value'), 3)
        self.controller.setReasoning('low')
        self.assertEqual(slider.property('value'), 1)
        self.controller.setReasoning('contemplate')
        self.assertEqual(slider.property('value'), 4)
        self.assertIn('沉思', button.property('text'))

    def test_model_editor_and_entry_points_exist(self):
        editor = self.window.findChild(QObject, 'modelEditor')
        self.assertIsNotNone(editor)
        self.assertIsNotNone(self.window.findChild(QObject, 'manageModelsButton'))
        self.workbench.editModel('')
        until(lambda: editor.property('opened'))


if __name__ == '__main__':
    unittest.main()
