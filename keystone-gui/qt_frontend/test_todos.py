"""Desktop checklist lifecycle over separate loopback planner and executor endpoints."""
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import Mock

from config_model import load_user_layer, save_user_layer
from keystone_client import KeystoneGatewayClient
import secret_store
from .controller import ChatController
from .test_backend import until


TASKS = ['读取示例数据', '验证结果']
PLANNER_KEY = 'offline-credential-for-planner'
WRITER_KEY = 'offline-credential-for-executor'
SECOND_KEY = 'isolated-second-test-credential'


def items(first, second):
    return [{'text': text, 'status': status} for text, status in zip(TASKS, (first, second))]


class CodingStatusTests(unittest.TestCase):
    def test_echoed_status_rows_require_the_current_titles_and_order(self):
        from types import SimpleNamespace
        from keystone.todos import install
        from keystone.tools import ToolRegistry
        agent = SimpleNamespace(registry=ToolRegistry(), extra_system='')
        install(agent, current_items=lambda run_id: items('pending', 'pending') if run_id == 'active' else [])
        update = agent.registry.visible()[0].handler
        ctx = SimpleNamespace(extras={'run_id': 'active'})
        rows = items('completed', 'in_progress')
        self.assertEqual(update({'statuses': rows}, ctx).meta['todos'], rows)
        self.assertFalse(update({'statuses': rows[::-1]}, ctx).ok)
        self.assertFalse(update({'statuses': items('in_progress', 'in_progress')}, ctx).ok)
        self.assertFalse(update({'statuses': rows}, SimpleNamespace(extras={})).ok)


class DesktopTodoTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.home = Path(temp.name) / 'home'
        self.requests = []
        self.received = threading.Event()
        self.release = threading.Event()
        self.plan_text = json.dumps({'tasks': TASKS})
        self.updates = []
        self.pause_round = 0
        self.writer_url = self.endpoint('writer')
        planner_url = self.endpoint('planner')
        self.addCleanup(self.release.set)
        save_user_layer(self.home, [{'id': 'planner', 'config': {
            'baseURL': planner_url + '/v1', 'model': 'planning-model',
            'apiKey': {'$expr': "get('env.KEYSTONE_PLANNER_KEY', '')"}}}])
        secret_store.save({'planner': PLANNER_KEY}, path=self.home / 'secrets.json')
        self.chat = ChatController(self.home, client_factory=lambda url: KeystoneGatewayClient(url, WRITER_KEY))
        self.addCleanup(self.chat.shutdown)
        self.chat.setGateway(self.writer_url)
        until(lambda: self.chat.connected)
        self.authorize = Mock()
        self.chat.tool_authorizer = lambda: self.authorize

    def endpoint(self, role):
        fixture = self
        class Endpoint(BaseHTTPRequestHandler):
            def log_message(self, *_):
                pass

            def do_GET(self):
                self.send_response(200)
                self.end_headers()
                self.wfile.write(b'{"data":[{"id":"writing-model"}]}' if self.path.endswith('models') else b'{"tools":[]}')

            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
                planning = role == 'planner' or (body['messages'][0]['role'] == 'system' and
                    body['messages'][0]['content'].startswith('Produce a concise actionable pre-execution'))
                fixture.requests.append(('planner' if planning else role, self.headers.get('Authorization'), body))
                if planning:
                    plan = fixture.plan_text.pop(0) if isinstance(fixture.plan_text, list) else fixture.plan_text
                    data = json.dumps({'choices': [{'message': {'content': plan}}]}).encode()
                    self.send_response(200)
                    self.send_header('Content-Type', 'application/json')
                else:
                    number = sum(r[0] == role for r in fixture.requests)
                    if number == fixture.pause_round:
                        fixture.received.set()
                        fixture.release.wait(5)
                    if number <= len(fixture.updates):
                        update = fixture.updates[number - 1]
                        arguments = update if isinstance(update, dict) else {'items': update}
                        delta = {'tool_calls': [{'index': 0, 'id': 'call_' + str(number), 'type': 'function',
                            'function': {'name': 'keystone_update_todos', 'arguments': json.dumps(arguments)}}]}
                        finish = 'tool_calls'
                    else:
                        delta, finish = {'content': '执行结束'}, 'stop'
                    event = json.dumps({'choices': [{'delta': delta, 'finish_reason': finish}]})
                    self.send_response(200)
                    if body.get('stream'):
                        data = ('data: ' + event + '\n\ndata: [DONE]\n\n').encode()
                        self.send_header('Content-Type', 'text/event-stream')
                    else:
                        if 'tool_calls' in delta:
                            delta['tool_calls'][0]['function']['name'] = 'update_todos'
                        data = json.dumps({'choices': [{'message': dict(role='assistant', **delta),
                                                       'finish_reason': finish}]}).encode()
                        self.send_header('Content-Type', 'application/json')
                self.send_header('Content-Length', str(len(data)))
                try:
                    self.end_headers()
                    self.wfile.write(data)
                except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
                    pass

        server = ThreadingHTTPServer(('127.0.0.1', 0), Endpoint)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(thread.join, 3)
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        return f'http://127.0.0.1:{server.server_port}'

    def enable_planner(self):
        settings = self.chat.planning
        value = next(r['value'] for r in settings.options if r['label'] == 'planner / planning-model')
        settings.save('model', value)
        settings.save('level', 'low')
        self.assertEqual(load_user_layer(self.home)[-1]['config'], {
            'model': ['planner', 'planning-model'], 'level': 'low'})

    def set_planning_key(self):
        settings = self.chat.planning
        self.assertTrue(settings.setCredential(settings.model, settings.endpoint, SECOND_KEY))
        self.assertTrue(settings.hasKey)
        self.assertNotIn(SECOND_KEY, secret_store.env_for(secret_store.load(path=self.home / 'secrets.json')).values())

    def test_manual_plan_is_used_once_and_auto_decides_from_prompt(self):
        self.enable_planner()
        settings = self.chat.planning
        self.send('chat')
        until(lambda: not self.chat.busy)
        self.assertEqual(self.chat._history[0].requested_planning, 'low')
        self.assertEqual(settings.level, 'none')
        self.send('chat')
        until(lambda: not self.chat.busy)
        self.assertEqual(sum(r[0] == 'planner' for r in self.requests), 1)
        settings.save('policy', 'auto')
        self.assertEqual(settings.take_level('你好'), 'none')
        self.assertEqual(settings.take_level('先读数据，然后生成图表'), 'low')
        self.assertEqual(settings.take_level('跳过规划，先读数据再画图'), 'none')
        settings.refresh()
        self.assertEqual(settings.policy, 'auto')

    def test_coding_policy_persists_but_manual_activation_is_one_task(self):
        from keystone.development.state import ModeState
        state = ModeState(self.home / 'coding')
        state.configure_planning_policy('auto')
        self.assertEqual(state.take_planning('先检查文件，再做回归测试'), 'low')
        self.assertEqual(state.take_planning('hello'), 'none')
        state.configure_planning_policy('manual')
        state.configure_planning('high')
        self.assertEqual(state.take_planning('short'), 'high')
        reopened = ModeState(self.home / 'coding')
        self.assertEqual(reopened.take_planning('修复并验证结果'), 'none')

    def test_planner_can_hide_checklist_and_executor_can_still_start_it(self):
        self.enable_planner()
        self.plan_text = json.dumps({'tasks': TASKS, 'show_todos': False})
        self.updates = [items('completed', 'completed')]
        self.pause_round = 1
        self.send('work')
        until(lambda: self.received.is_set())
        self.assertFalse(self.chat.messages.rows[-1].get('todos'))
        self.release.set()
        until(lambda: not self.chat.busy)
        self.assertEqual(self.chat.messages.rows[-1]['todos'], self.updates[0])
        self.assertFalse(self.chat._history[0].task_plan.show_todos)

    def send(self, mode):
        self.chat.setConversationMode(mode)
        self.chat.draft = '整理示例数据并验证结果'
        self.chat.send()

    def test_independent_planner_then_writer_and_persisted_progress(self):
        self.enable_planner()
        self.set_planning_key()
        self.pause_round = 1
        self.updates = [items('completed', 'in_progress'), items('completed', 'completed')]
        self.send('chat')
        until(lambda: self.received.is_set() and self.chat.messages.rows[-1].get('todos') == items('pending', 'pending'))
        self.release.set()
        until(lambda: not self.chat.busy)
        self.assertEqual(self.chat.error, '')
        self.assertEqual([r[0] for r in self.requests], ['planner', 'writer', 'writer', 'writer'])
        planner, *writers = self.requests
        self.assertEqual((planner[1], planner[2]['model']), ('Bearer ' + SECOND_KEY, 'planning-model'))
        self.assertNotIn('tools', planner[2])
        for _, credential, body in writers:
            self.assertEqual((credential, body['model']), ('Bearer ' + WRITER_KEY, 'writing-model'))
            self.assertIn('Task plan', next(m['content'] for m in body['messages'] if m['role'] == 'user'))
            self.assertTrue(any(t['function']['name'] == 'keystone_update_todos' for t in body['tools']))
        self.authorize.assert_not_called()
        sid = self.chat.sessionId
        self.chat.newSession()
        self.chat.selectSession(sid)
        cards = [r for r in self.chat.messages.rows if r.get('todos')]
        self.assertEqual(len(cards), 1)
        self.assertEqual(cards[0]['todos'], self.updates[-1])
        self.assertFalse(cards[0].get('toolReceipts'))
        saved = self.chat.store.sessions()[0]['messages']
        self.assertEqual(saved[0]['task_plan']['tasks'], TASKS)
        self.assertEqual(self.chat._history[0].task_plan.level, 'low')
        for key in (PLANNER_KEY, WRITER_KEY, SECOND_KEY):
            self.assertNotIn(key, json.dumps(saved))
            self.assertTrue(all(key not in json.dumps(row[2]) for row in self.requests))

    def test_work_without_planner_and_invalid_update_preserves_last_progress(self):
        self.updates = [items('in_progress', 'pending'), items('in_progress', 'in_progress')]
        self.send('work')
        until(lambda: not self.chat.busy)
        self.assertEqual(self.chat.error, '')
        self.assertEqual([r[0] for r in self.requests], ['writer'] * 3)
        self.assertEqual(self.chat.messages.rows[-1]['todos'], self.updates[0])
        receipt = self.requests[-1][2]['messages'][-1]
        self.assertFalse(json.loads(receipt['content'])['ok'])
        self.authorize.assert_not_called()

    def test_coding_execution_uses_selected_planner_and_projects_real_tool_events(self):
        from keystone.compat import desktop_config
        from .service_host import ServiceHost
        from .store import SessionStore
        self.enable_planner()
        rows = load_user_layer(self.home)
        rows.append({'id': 'writer', 'name': 'provider:writer', 'config': {
            'baseURL': self.writer_url + '/v1', 'model': 'writing-model',
            'apiKey': {'$expr': "get('env.KEYSTONE_WRITER_KEY', '')"}}})
        save_user_layer(self.home, rows)
        secret_store.save({'writer': WRITER_KEY, 'planner': PLANNER_KEY}, path=self.home / 'secrets.json')
        self.chat.planning.refresh()
        self.chat.planning.save('model', json.dumps(['writer', 'writing-model']))
        self.set_planning_key()
        project = self.home.parent / 'project'
        project.mkdir()
        SessionStore.atomic(desktop_config(self.home), {'project_workspace': str(project)})
        self.addCleanup(setattr, secret_store, 'SECRETS_FILE', secret_store.SECRETS_FILE)
        states = []
        host = ServiceHost(self.home, demo=True, emit=lambda event, **data: states.append((event, data)))
        self.addCleanup(host.close)
        host.demo = False
        host.coding.initialize()
        host.coding.command({'action': 'model', 'value': 'writer / writing-model'})
        host.coding.command({'action': 'planning', 'value': 'low'})
        self.updates = [{'statuses': ['completed', 'blocked']}]
        self.pause_round = 2
        host.coding.command({'action': 'draft', 'value': '检查示例并验证结果'})
        host.coding.command({'action': 'send'})
        until(lambda: self.received.is_set(), timeout=12)
        host.coding.command({'action': 'cancel'})
        self.release.set()
        until(lambda: not host.running, timeout=12)
        task_id = host.coding.runtime.last_task_id
        self.assertTrue(host.coding.interrupted_request)
        host.coding.command({'action': 'resume'})
        until(lambda: not host.running, timeout=12)
        self.assertEqual(host.coding.runtime.last_task_id, task_id)
        self.assertFalse(host.coding.interrupted_request)
        self.assertFalse([v for k, v in states if k == 'error'])
        self.assertEqual([(role, key, body['model']) for role, key, body in self.requests],
                         [('planner', 'Bearer ' + SECOND_KEY, 'writing-model'),
                          ('writer', 'Bearer ' + WRITER_KEY, 'writing-model'),
                          ('writer', 'Bearer ' + WRITER_KEY, 'writing-model'),
                          ('writer', 'Bearer ' + WRITER_KEY, 'writing-model')])
        checklists = [v['state']['todos'] for k, v in states if k == 'coding_state']
        self.assertIn(items('pending', 'pending'), checklists)
        self.assertEqual(checklists[-1], items('completed', 'blocked'))

    def test_saved_key_is_bound_to_the_model_and_endpoint_and_can_be_removed(self):
        self.enable_planner()
        self.set_planning_key()
        settings = self.chat.planning
        original = settings.endpoint
        rows = load_user_layer(self.home)
        rows[0]['config']['baseURL'] += '/changed'
        save_user_layer(self.home, rows)
        settings.refresh()
        self.assertFalse(settings.hasKey)
        self.assertFalse(settings.setCredential(settings.model, original, 'stale-key'))
        rows[0]['config']['baseURL'] = original
        save_user_layer(self.home, rows)
        settings.refresh()
        self.assertTrue(settings.hasKey)
        self.assertTrue(settings.setCredential(settings.model, original, ''))
        self.assertFalse(settings.hasKey)
        self.assertEqual(secret_store.load(path=self.home / 'secrets.json'), {'planner': PLANNER_KEY})

    def test_planner_failure_stops_work_and_retry_reuses_accepted_plan(self):
        self.enable_planner()
        self.plan_text = 'invalid'
        context = Mock(return_value='')
        self.chat.work_context = context
        self.send('work')
        until(lambda: not self.chat.busy)
        self.assertTrue(self.chat.error)
        self.assertEqual([r[0] for r in self.requests], ['planner', 'planner'])
        context.assert_not_called()
        self.plan_text = json.dumps({'tasks': TASKS})
        self.pause_round = 1
        self.chat.retry()
        until(lambda: self.received.is_set() and bool(self.chat.messages.rows[-1].get('todos')))
        self.chat.stop()
        self.release.set()
        until(lambda: not self.chat.busy)
        self.assertEqual(self.chat.messages.rows[-1]['state'], 'stopped')
        self.chat.retry()
        until(lambda: not self.chat.busy)
        self.assertEqual(self.chat.error, '')
        self.assertEqual([r[0] for r in self.requests], ['planner', 'planner', 'planner', 'writer', 'writer'])
        self.assertEqual(len([r for r in self.chat.messages.rows if r.get('todos')]), 1)
        self.assertEqual(self.chat.messages.rows[-1]['todos'], items('pending', 'pending'))

    def test_continue_after_tool_checkpoint_preserves_receipts_plan_and_draft(self):
        self.enable_planner()
        self.plan_text = ['malformed', json.dumps({'tasks': TASKS})]
        self.updates = [items('completed', 'pending')]
        self.pause_round = 2
        self.send('work')
        until(lambda: self.received.is_set() and self.chat._history[-1].role == 'tool')
        self.chat.stop()
        self.release.set()
        until(lambda: not self.chat.busy)
        sid = self.chat.sessionId
        self.chat.newSession()
        self.chat.selectSession(sid)
        self.assertTrue(self.chat.messages.rows[-1]['continuable'])
        self.chat.draft = '下一条的草稿'
        self.chat.continueWork()
        until(lambda: not self.chat.busy)
        self.assertEqual(self.chat.error, '')
        self.assertEqual(self.chat.draft, '下一条的草稿')
        self.assertEqual([r[0] for r in self.requests], ['planner', 'planner', 'writer', 'writer', 'writer'])
        messages = self.requests[-1][2]['messages']
        self.assertEqual([m['role'] for m in messages][-3:], ['assistant', 'tool', 'user'])
        self.assertIn('completed', messages[-1]['content'])
        self.assertFalse(self.chat._history[0].interrupted)
        self.assertEqual(sum(m.role == 'user' for m in self.chat._history), 1)
