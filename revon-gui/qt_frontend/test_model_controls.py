"""思考强度控件与已有模型编辑：档位随模型变化、偏好持久化、编辑往返且密钥不外泄。"""
import json
from http.server import ThreadingHTTPServer
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch

from PySide6.QtCore import QCoreApplication, QMetaObject, QObject, QUrl
from PySide6.QtGui import QAccessible
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
        # 沉思 is always offered: it also turns on revon task contemplation.
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

    def test_picker_disambiguates_shared_route_labels_and_clears_external_attribution(self):
        rows = [provider('a', 'gpt-4', 'https://api.openai.com/v1', 'Shared', models=['also-a']),
                provider('b', 'gpt-4', 'https://alt.example.test/v1', 'Shared', models=['also-a'])]
        save_user_layer(self.home, rows)
        self.controller.refreshModelChoices()
        self.assertEqual(self.controller.routeProviders, {
            'Shared': 'OpenAI', 'also-a': 'OpenAI', 'Shared (2)': 'alt.example.test', 'also-a (2)': 'alt.example.test'})
        self.controller.setGateway(self.url)
        self.assertEqual(self.controller.routeProviders, {})

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

    def test_contemplate_runs_rounds_before_answering(self):
        self.connect('ds')
        self.controller.setReasoning('contemplate')
        Gateway.requests.clear()
        self.controller.draft = '设计一个缓存层'
        self.controller.send()
        until(lambda: not self.controller.busy)
        requests = list(Gateway.requests)
        # The fake gateway answers identically, so the second round converges: 2 rounds + the answer.
        self.assertEqual(len(requests), 3)
        self.assertIn('设计一个缓存层', requests[0]['messages'][-1]['content'])
        self.assertTrue(all(r.get('reasoning_effort') == 'high' for r in requests))
        final = [str(m.get('content')) for m in requests[-1]['messages']]
        self.assertTrue(any('<contemplation>' in text for text in final), final)
        reply = self.controller.messages.rows[-1]
        self.assertEqual(reply['contemplation']['rounds'], 2)
        self.assertIn('已收敛', reply['contemplation']['label'])
        self.assertEqual(reply['textContent'], '真实流式 reply 123')
        # Notes are per request only: the next turn does not resend them from history.
        self.controller.setReasoning('high')
        follow = self.send()
        self.assertFalse(any('<contemplation>' in str(m.get('content')) for m in follow['messages']))

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

    def test_access_provider_is_independent_from_model_name_and_note(self):
        self.service.save('proxy-1', self.values('proxy-1', model='anthropic/claude-sonnet',
            baseURL='https://openrouter.ai/api/v1', providerLabel='团队订阅'), '')
        row = next(r for r in self.service.rows if r['id'] == 'proxy-1')
        self.assertEqual((row['providerName'], row['modelBrand'], row['providerLabel']),
                         ('OpenRouter', 'Claude', '团队订阅'))
        self.service.save('proxy-1', self.values('proxy-1', baseURL='https://api.openai.com.proxy.test/development/v1'), '')
        row = next(r for r in self.service.rows if r['id'] == 'proxy-1')
        self.assertEqual(row['providerName'], 'api.openai.com.proxy.test')
        self.assertEqual(row['providerLabel'], '团队订阅')
        self.assertEqual(row['baseURL'], 'https://api.openai.com.proxy.test/development/v1')
        self.assertEqual(row['displayBaseURL'], 'https://api.openai.com.proxy.test/…/v1')
        self.assertEqual(row['providerIcon'], '')

    def test_protocol_follows_actual_transport_rules_for_responses(self):
        rows = [provider('official', 'gpt-6.2-astra', 'https://api.openai.com/v1', 'GPT'),
                provider('relay', 'gpt-6.2-astra', 'https://proxy.example/v1/responses', 'GPT')]
        save_user_layer(self.home, rows)
        self.service.reload()
        official, relay = self.service.rows
        self.assertEqual((official['protocolLabel'], relay['protocolLabel']), ('OpenAI Responses', 'OpenAI 兼容'))

    def test_history_uses_observed_url_even_after_row_is_changed_or_removed(self):
        records = [{'provider_id': pid, 'base_url': 'https://configured.example.test/v1',
                    'request_url': 'https://openrouter.ai/api/v1/chat/completions', 'source': 'gateway',
                    'http_status': 200, 'last_seen': 1.5, 'attempts': 3} for pid in ('ds-1', 'deleted')]
        with patch('qt_frontend.model_rows.recent_requests', return_value=records):
            self.service.refreshHistory()
        first, removed = self.service.requestHistory
        self.assertEqual((first['providerName'], first['rowLabel'], first['attempts']), ('OpenRouter', 'ds', 3))
        self.assertEqual((removed['providerName'], removed['rowLabel']), ('OpenRouter', ''))

    def test_cherryin_label_requires_the_registered_origin(self):
        from provider_catalog import route_fields
        for url, expected in [('https://open.cherryin.net/v1', 'CherryIN'),
                              ('https://open.cherryin.net.relay.example/v1', 'open.cherryin.net.relay.example')]:
            fields = route_fields({'baseURL': url, 'model': 'agent/deepseek-v4-flash'})
            self.assertEqual((fields['providerName'], fields['modelBrand']), (expected, 'DeepSeek'))

    def test_opencode_current_and_cherry_routes_keep_distinct_history_labels(self):
        from provider_catalog import route_fields
        from revon.provider_requests import safe_url
        for path, name in [('inference/go/openai/v1', 'OpenCode Go'), ('zen/go/v1', 'OpenCode Go'),
                           ('inference/openai/v1', 'OpenCode Zen'), ('zen/v1', 'OpenCode Zen'),
                           ('inference/goose/v1', 'OpenCode'), ('account', 'OpenCode')]:
            url = 'https://opencode.ai/' + path
            fields = route_fields({'baseURL': url, 'model': 'deepseek-v4-pro'})
            self.assertEqual((fields['providerName'], fields['modelBrand']), (name, 'DeepSeek'))
            if name != 'OpenCode':
                self.assertEqual(safe_url(url + '/chat/completions'), url + '/chat/completions')

    def test_old_credential_bearing_addresses_never_enter_editor_or_preview(self):
        for address in ('https://user:testpass@host.example/v1?token=x',
                        'https://host.example/sk-original-key-000000/v1'):
            save_user_layer(self.home, [provider('old', 'gpt-4', address, 'Legacy')])
            self.service.reload()
            row, = self.service.rows
            self.assertEqual(row['baseURL'], '')
            self.assertNotIn('testpass', json.dumps(row))
            self.assertNotIn('sk-original', json.dumps(row))

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
        layer = (self.home / 'revon.patch.json').read_text(encoding='utf-8')
        self.assertNotIn(SECRET, layer)
        # Own legacy FORGE_* reference stays; the store injects both names for this row id.
        self.assertEqual(env_ref(self.row('ds-1')['config']), 'FORGE_DS_1_KEY')
        self.assertEqual(secret_store.load(path=self.secrets)['ds-1'], SECRET)
        self.assertEqual(secret_store.env_for(secret_store.load(path=self.secrets))['FORGE_DS_1_KEY'], SECRET)
        self.assertNotIn(SECRET, json.dumps(self.service.rows))
        # A row borrowing another row's key gets its own REVON_* reference; the lender is untouched.
        self.service.save('ds-2', self.values('ds-2'), 'sk-second-key-111111')
        self.assertEqual(env_ref(self.row('ds-2')['config']), 'REVON_DS_2_KEY')
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
                            'brandUrl': QUrl.fromLocalFile(str(ROOT.parent / 'assets' / 'revon-logo-104.png'))}.items():
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

    @staticmethod
    def preset_buttons(field):
        return [child for sibling in field.parentItem().childItems() for child in sibling.childItems()
                if child.objectName().startswith(field.objectName() + 'Preset')]

    def test_mimo_context_and_output_presets_survive_reopening_the_editor(self):
        home = Path(self.temp.name)
        model_id = 'mimo-v2.6-pro'
        initial = {model_id: {'contextWindow': 65536, 'outputTokenBudget': 4096}}
        save_user_layer(home, [provider('mimo-test', model_id, 'https://token-plan-cn.xiaomimimo.com/v1',
                                        'mimopro', modelCapabilities=initial)])
        self.workbench._model_rows.demo = False
        self.workbench.editModel('mimo-test')
        editor = self.window.findChild(QObject, 'modelEditor')
        until(lambda: editor.property('opened'))
        presets = [('contextWindowField', '1M', '1048576'), ('outputTokenBudgetField', '128K', '131072')]
        for name, label, _ in presets:
            field = editor.findChild(QObject, name)
            next(b for b in self.preset_buttons(field) if b.property('text') == label).clicked.emit()
        editor.findChild(QObject, 'saveModel').clicked.emit()
        self.assertTrue(self.workbench._model_rows.success, self.workbench._model_rows.status)
        self.assertEqual(load_user_layer(home)[0]['config']['modelCapabilities'][model_id],
                         {'contextWindow': 1048576, 'outputTokenBudget': 131072})
        QMetaObject.invokeMethod(editor, 'close')
        until(lambda: not editor.property('opened'))
        self.workbench.editModel('mimo-test')
        until(lambda: editor.property('opened'))
        for name, label, value in presets:
            field = editor.findChild(QObject, name)
            self.assertEqual(field.property('text'), value)
            self.assertEqual([b.property('text') for b in self.preset_buttons(field) if b.property('selected')], [label])

    def test_token_limit_fields_save_and_reload_independent_wire_models(self):
        home = Path(self.temp.name)
        save_user_layer(home, [provider('multi', 'one', 'https://example.test/v1', 'Multi', models=['two'])])
        self.workbench._model_rows.demo = False
        self.workbench.editModel('multi')
        editor = self.window.findChild(QObject, 'modelEditor')
        until(lambda: editor.property('opened'))
        context_field = editor.findChild(QObject, 'contextWindowField')
        output_field = editor.findChild(QObject, 'outputTokenBudgetField')
        choice = editor.findChild(QObject, 'tokenModelChoice')
        self.assertEqual(choice.property('count'), 2)
        def check_presets(field, expected):
            buttons = self.preset_buttons(field)
            self.assertEqual([(b.property('text'), int(b.property('modelData'))) for b in buttons], expected)
            for button, (_, count) in zip(buttons, expected):
                button.clicked.emit()
                self.assertEqual(field.property('text'), str(count))
                self.assertTrue(button.property('selected'))
        check_presets(context_field, [('64K', 65536), ('128K', 131072), ('256K', 262144), ('1M', 1048576)])
        context_field.setProperty('text', '300000')
        context_field.textEdited.emit()
        choice.setProperty('currentIndex', 1)
        self.assertEqual(context_field.property('text'), '')
        check_presets(output_field, [('4K', 4096), ('8K', 8192), ('16K', 16384), ('32K', 32768),
                                     ('64K', 65536), ('128K', 131072), ('384K', 384000)])
        editor.findChild(QObject, 'saveModel').clicked.emit()
        self.assertTrue(self.workbench._model_rows.success, self.workbench._model_rows.status)
        self.assertEqual(load_user_layer(home)[0]['config']['modelCapabilities'],
                         {'one': {'contextWindow': 300000}, 'two': {'outputTokenBudget': 384000}})
        self.assertEqual(output_field.property('text'), '384000')
        choice.setProperty('currentIndex', 0)
        self.assertEqual(context_field.property('text'), '300000')

    def test_model_editor_and_entry_points_exist(self):
        editor = self.window.findChild(QObject, 'modelEditor')
        self.assertIsNotNone(editor)
        self.assertIsNotNone(self.window.findChild(QObject, 'manageModelsButton'))
        save_user_layer(Path(self.temp.name), ROWS)
        self.workbench.editModel('')
        until(lambda: editor.property('opened'))
        field = self.window.findChild(QObject, 'editLabel')
        until(field.hasActiveFocus)
        self.assertEqual(QAccessible.queryAccessibleInterface(field).text(QAccessible.Text.Name), '显示名称')
        QMetaObject.invokeMethod(editor, 'close')
        setup = self.window.findChild(QObject, 'onboardingPopup')
        QMetaObject.invokeMethod(setup, 'open')
        until(lambda: setup.property('opened'))
        key = self.window.findChild(QObject, 'setupKey')
        accessible = QAccessible.queryAccessibleInterface(key)
        self.assertTrue(accessible.state().passwordEdit)
        self.assertIn('API 密钥', [target.text(QAccessible.Text.Name) for target, _ in accessible.relations()])

    def test_editor_reopens_with_google_native_protocol_selected(self):
        from .gateway_service import chat_model_routes
        base = 'https://generativelanguage.googleapis.com'
        row = provider('google', 'gemini-test', base, 'Gemini', wire='google')
        save_user_layer(Path(self.temp.name), [row])
        self.workbench.editModel('google')
        editor = self.window.findChild(QObject, 'modelEditor')
        until(lambda: editor.property('opened'))
        choice = self.window.findChild(QObject, 'editWire')
        self.assertEqual(choice.property('currentValue'), 'google')
        self.assertIn('Google 原生', choice.property('displayText'))
        self.assertEqual(chat_model_routes([row])['Gemini'][1]['wire'], 'google')

    def test_searching_a_model_selects_its_provider_and_history_displays_observed_routes(self):
        relay = 'https://openrouter.ai/api/v1'
        save_user_layer(Path(self.temp.name), [
            *ROWS, provider('first', 'anthropic/claude-sonnet', relay, 'Design'),
            provider('second', 'openai/gpt-4', relay, 'Review', providerLabel='我的备用线路')])
        editor = self.window.findChild(QObject, 'modelEditor')
        self.workbench.editModel('ds-1')
        until(lambda: editor.property('opened'))
        groups = self.window.findChild(QObject, 'modelEditorList')
        self.assertEqual(groups.property('count'), 4)
        search = self.window.findChild(QObject, 'modelProviderSearch')
        search.setProperty('text', '备用')
        until(lambda: groups.property('count') == 1)
        search.accepted.emit()
        until(lambda: editor.property('currentId') == 'second')
        choice = self.window.findChild(QObject, 'providerModelChoice')
        self.assertEqual((choice.property('count'), choice.property('displayText')), (2, 'Review'))
        self.assertEqual(self.window.findChild(QObject, 'editEndpoint').property('text'), relay)
        self.assertEqual(self.window.findChild(QObject, 'editProviderLabel').property('text'), '我的备用线路')
        tab = self.window.findChild(QObject, 'requestHistoryTab')
        tab.clicked.emit()
        requests = self.window.findChild(QObject, 'providerRequestList')
        self.assertEqual(requests.property('count'), 0)
        record = {'provider_id': 'second', 'base_url': relay, 'request_url': relay + '/chat/completions',
                  'source': 'gateway', 'http_status': 429, 'last_seen': 42, 'attempts': 2}
        with patch('qt_frontend.model_rows.recent_requests', return_value=[record]):
            tab.clicked.emit()
        def visual_texts(item):
            result = []
            for child in item.childItems():
                if child.isVisible() and child.metaObject().indexOfProperty('text') >= 0:
                    result.append(child.property('text'))
                result.extend(visual_texts(child))
            return result
        until(lambda: '最近 HTTP 429' in visual_texts(requests))
        displayed = visual_texts(requests)
        self.assertIn('OpenRouter  ·  Review', displayed)
        self.assertIn('最近 HTTP 429', displayed)
        self.assertIn(relay, displayed)
        self.assertIn(relay + '/chat/completions', displayed)


if __name__ == '__main__':
    unittest.main()
