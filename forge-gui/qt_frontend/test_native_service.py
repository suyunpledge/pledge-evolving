"""Exercise native-page operations against isolated real stores and files."""
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest.mock import patch

from config_model import load_user_layer, save_user_layer
from .service_host import ServiceHost
from .store import SessionStore


class NativeServiceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.root = Path(self.temp.name).resolve()
        self.home, self.project = self.root / 'home', self.root / 'project'
        self.project.mkdir()
        SessionStore.atomic(self.home / 'forge-desktop.json', {'project_workspace': str(self.project)})
        self.events = []
        import secret_store
        self.old_secrets = secret_store.SECRETS_FILE
        self.host = ServiceHost(self.home, demo=True, emit=lambda event, **data: self.events.append((event, data)))
        self.host.approve = lambda *_: True

    def tearDown(self):
        self.host.close()
        import secret_store
        secret_store.SECRETS_FILE = self.old_secrets
        self.temp.cleanup()

    def values(self, page):
        return {f['key']: f['value'] for f in self.host.pages.view(page)['fields']}

    def test_file_save_preserves_crlf_and_refuses_external_conflicts(self):
        path = self.project / 'sample.txt'
        path.write_bytes(b'first\r\nsecond\r\n')
        pages = self.host.pages
        pages.action('files', 'open', {'path': str(path)})
        pages.update_document(str(path), 'first\nsecond edited\n', 1)
        self.assertTrue(pages.unsaved)
        pages.action('files', 'save', {'path': str(path)})
        self.assertEqual(path.read_bytes(), b'first\r\nsecond edited\r\n')
        pages.update_document(str(path), 'first\nmy draft\n', 2)
        path.write_text('external\nsecond edited\n', encoding='utf-8')
        with self.assertRaisesRegex(RuntimeError, 'changed'):
            pages.action('files', 'save', {'path': str(path)})
        self.assertIn('external', path.read_text())
        pages.action('files', 'merge', {'path': str(path)})
        self.assertEqual(pages.documents[str(path)].text, 'external\nmy draft\n')
        pages.action('files', 'save', {'path': str(path)})
        self.assertFalse(pages.unsaved)
        copied = self.project / 'copy.txt'
        pages.action('files', 'saveAs', {'path': str(path), 'target': str(copied)})
        self.assertEqual(copied.read_text(), 'external\nmy draft\n')
        self.assertEqual(pages.active_document, str(copied))
        pages.action('files', 'search', {'query': 'copy'})
        self.assertEqual([r['title'] for r in pages.view('files')['rows']], ['copy.txt'])
        with self.assertRaises(PermissionError):
            pages.action('files', 'open', {'path': '../outside.txt'})

    def test_config_optimistic_save_and_model_disable(self):
        row = dict(id='fixture', name='provider:fixture', config={'baseURL': 'http://127.0.0.1:1/v1', 'model': 'fixture'})
        save_user_layer(self.home, [row])
        values = self.values('config')
        extra = dict(id='feature', config={'enabled': True}, name='feature:test')
        save_user_layer(self.home, [row, extra])
        with self.assertRaises(ValueError):
            self.host.pages.action('config', 'save', values)
        self.assertEqual(len(load_user_layer(self.home)), 2)
        self.host.pages.action('config', 'toggle', {'id': 'fixture'})
        self.assertTrue(load_user_layer(self.home)[0]['disabled'])
        self.assertEqual(load_user_layer(self.home)[1], extra)

    def test_knowledge_evolution_and_schedule_use_existing_stores(self):
        pages = self.host.pages
        pages.action('knowledge', 'record', {'text': 'Always preserve CRLF when editing files.', 'kind': 'lesson'})
        rows = pages.view('knowledge')['rows']
        self.assertEqual(len(rows), 1)
        pages.action('knowledge', 'search', {'text': 'CRLF editing'})
        self.assertIn('CRLF', pages.results['knowledge'])
        pages.action('knowledge', 'archive', {'id': rows[0]['id']})
        self.assertEqual(pages.view('knowledge')['rows'], [])
        pages.action('evolution', 'nominate', {'text': 'Remember: preserve CRLF line endings.'})
        self.assertIn('rows', pages.view('evolution'))
        pages.action('schedules', 'add', {'name': 'Read project', 'prompt': 'List source files', 'model': '["fixture", "model"]', 'delay': '60', 'interval': '0'})
        jobs = pages.store.jobs()
        self.assertEqual(len(jobs), 1)
        pages.action('schedules', 'pause', {'id': jobs[0]['id']})
        self.assertFalse(pages.store.jobs()[0]['enabled'])
        self.assertEqual(pages.store.job_runs(jobs[0]['id']), [])

    def test_advanced_config_preserves_redacted_credentials_when_reordered(self):
        reference = {'$expr': "get('env.FORGE_FIXTURE_API_KEY', '')"}
        rows = [dict(id='first', name='provider:first', config={'baseURL': 'http://127.0.0.1:1/v1', 'apiKey': reference, 'model': 'one'}),
                dict(id='second', name='provider:second', config={'apiKey': 'fixture-secret-not-for-qml-123456', 'model': 'two'})]
        save_user_layer(self.home, rows)
        values = self.values('config')
        self.assertNotIn('fixture-secret-not-for-qml-123456', values['json'])
        edited = json.loads(values['json'])
        edited.reverse()
        edited[1]['config']['model'] = 'updated'
        values['json'] = json.dumps(edited)
        self.host.pages.action('config', 'save', values)
        saved = {r['id']: r for r in load_user_layer(self.home)}
        self.assertEqual(saved['first']['config']['apiKey'], reference)
        self.assertEqual(saved['first']['config']['model'], 'updated')
        self.assertEqual(saved['second']['config']['apiKey'], rows[1]['config']['apiKey'])

    def test_agent_settings_are_scoped_to_home_and_concurrency_checked(self):
        values = self.values('agents')
        values.update(mode='on', count='3', cluster=True)
        self.host.pages.action('agents', 'save', values)
        import sub_agent
        self.assertEqual(sub_agent.load_config(self.home / 'agent-cluster.json')['cluster']['count'], 3)
        self.assertEqual(SessionStore.read(self.home / 'forge-desktop.json', {})['team_mode'], 'on')
        changed = sub_agent.load_config(self.home / 'agent-cluster.json')
        changed['memory_mode'] = 'unified'
        sub_agent.save_config(changed, self.home / 'agent-cluster.json')
        with self.assertRaisesRegex(ValueError, '外部'):
            self.host.pages.action('agents', 'save', values)

    def test_connector_credentials_are_not_projected_to_qml_and_grants_persist(self):
        values = self.values('connector_accounts')
        values.update(token='fixture-native-credential-123456', read=True, agent=True)
        self.host.pages.action('connector_accounts', 'save', values)
        view = json.dumps(self.host.pages.view('connector_accounts'))
        self.assertNotIn(values['token'], view)
        self.assertTrue(self.host.pages.store.account('github')['authenticated'])
        self.assertTrue(self.host.pages.connectors.schemas())
        self.host.pages.action('connector_accounts', 'disconnect', values)
        self.assertEqual(self.host.pages.connectors.schemas(), [])

    def test_coding_draft_version_context_and_dirty_buffer_guard(self):
        self.host.coding.initialize()
        self.host.coding.command({'action': 'draft', 'value': 'current draft', 'version': 2})
        self.host.coding.command({'action': 'draft', 'value': 'stale', 'version': 1})
        self.assertEqual(self.host.coding.draft, 'current draft')
        path = self.project / 'code.py'
        path.write_text('value = 1\n', encoding='utf-8')
        self.host.pages.action('development', 'open', {'path': str(path)})
        self.host.pages.update_document(str(path), 'value = 2\n', 1)
        self.host.demo = False
        with patch.object(self.host.coding.runtime, 'run') as run:
            with self.assertRaisesRegex(ValueError, '保存'):
                self.host.coding.run(('hello', False, '继承配置', 'default', 'none', (str(path),)))
            run.assert_not_called()
        self.host.pages.action('development', 'save', {'path': str(path)})
        with patch.object(self.host.coding.runtime, 'run', return_value={'text': 'done'}) as run:
            self.host.coding.run(('hello', False, '继承配置', 'default', 'none', (str(path),)))
            self.assertEqual(run.call_args.kwargs['contexts'], [{'path': str(path), 'content': 'value = 2\n'}])

    def test_cold_process_imports_no_tk_and_exits_cleanly(self):
        script = Path(__file__).with_name('service_host.py')
        proc = subprocess.run([sys.executable, '-B', str(script), '--home', str(self.root / 'other'), '--demo'],
            input='{"command":"navigate","page":"knowledge","ticket":1}\n{"command":"close"}\n',
            text=True, encoding='utf-8', capture_output=True, timeout=12,
            creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn('"backend": "headless"', proc.stdout)
        # A cold interpreter, rather than this test process where other test suites
        # may intentionally exercise legacy frontends.
        code = 'import sys; from qt_frontend.service_host import ServiceHost; assert "tkinter" not in sys.modules'
        proc = subprocess.run([sys.executable, '-B', '-c', code], capture_output=True, text=True, timeout=12)
        self.assertEqual(proc.returncode, 0, proc.stderr)

    def test_coding_queue_resumes_after_other_background_operation(self):
        release, executed = threading.Event(), threading.Event()
        with patch.object(self.host.coding, 'run', side_effect=lambda request: executed.set()) as run:
            self.host.work(lambda: release.wait(3))
            first_worker = self.host.worker
            try:
                self.host.command({'command': 'coding', 'action': 'draft', 'value': 'queued task', 'version': 1})
                self.host.command({'command': 'coding', 'action': 'send'})
                self.assertEqual(len(self.host.coding.queue), 1)
                self.assertFalse(executed.is_set())
            finally:
                release.set()
            self.assertTrue(executed.wait(3))
            first_worker.join(3)
            self.host.worker.join(3)
            self.assertEqual(run.call_args.args[0][0], 'queued task')
            self.assertEqual(run.call_count, 1)
            self.assertFalse(self.host.running)
            self.assertFalse(self.host.coding.queue)

    def test_cancel_during_coding_initialization_does_not_submit_request(self):
        self.host.coding.initialize()
        self.host.demo = False
        entered, release = threading.Event(), threading.Event()
        def initialize():
            entered.set()
            release.wait(3)
        with patch.object(self.host.coding, 'initialize', side_effect=initialize), patch.object(self.host.coding.runtime, 'run') as run:
            self.host.command({'command': 'coding', 'action': 'draft', 'value': 'never submit', 'version': 1})
            self.host.command({'command': 'coding', 'action': 'send'})
            try:
                self.assertTrue(entered.wait(3))
                self.host.command({'command': 'coding', 'action': 'cancel'})
            finally:
                release.set()
            self.host.worker.join(3)
            run.assert_not_called()
            self.assertFalse(self.host.running)

    def test_manual_computer_capture_waits_for_host_approval(self):
        from forge.computer_use import ComputerSession
        from .computer_service import ComputerService

        class Driver:
            target_id = 'test:computer-capture'
            calls = 0

            def screenshot(self, **_):
                self.calls += 1
                return {'target': self.target_id, 'png': b'fixture', 'width': 400, 'height': 300,
                        'rect': [0, 0, 400, 300], 'client_rect': [0, 0, 400, 300], 'dpi': 96,
                        'sha256': 'fixture', 'client_sha256': 'fixture'}

        driver = Driver()
        service = self.host.pages.computer = ComputerService(self.host)
        service.session = ComputerSession(driver)
        self.host.profile = 'manual'
        decisions = iter((False, True, False, True))
        requests = []

        def approve(name, arguments, _cancel):
            requests.append((name, arguments, driver.calls))
            return next(decisions)

        self.host.approve = approve
        service.action('capture', {})
        self.assertIsNone(service.frame)
        self.assertEqual(driver.calls, 0)
        service.action('capture', {})
        self.assertEqual(driver.calls, 1)
        self.assertIsNotNone(service.frame)
        for identifier, approved in [('denied', False), ('approved', True)]:
            self.host.command({'command': 'call_tool', 'id': identifier,
                               'name': 'computer_screenshot', 'arguments': {}})
            self.host.worker.join(3)
            self.assertFalse(self.host.worker.is_alive())
            response = next(row['result'] for kind, row in self.events
                            if kind == 'call_result' and row['id'] == identifier)
            self.assertIs(response['ok'], approved, response)
            self.assertEqual(driver.calls, 2 if approved else 1)
        self.assertEqual(requests, [('computer_screenshot', {}, before) for before in (0, 0, 1, 1)])

    def test_gateway_starts_from_saved_custom_endpoint_and_stops_its_child(self):
        requests = []
        class Endpoint(BaseHTTPRequestHandler):
            def log_message(self, *_):
                pass
            def do_POST(self):
                payload = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
                requests.append((self.path, payload))
                raw = json.dumps({'choices': [{'message': {'role': 'assistant', 'content': 'gateway-ok'}, 'finish_reason': 'stop'}], 'usage': {}}).encode()
                self.send_response(200)
                self.send_header('Content-Type', 'application/json')
                self.send_header('Content-Length', str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)
        server = ThreadingHTTPServer(('127.0.0.1', 0), Endpoint)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            save_user_layer(self.home, [dict(id='custom', name='provider:custom', config={
                'baseURL': f'http://127.0.0.1:{server.server_port}/custom/v1', 'model': 'fixture', 'wire': 'openai'})])
            self.host.gateway.demo = False
            self.host.gateway.start('custom')
            proc = self.host.gateway.process
            self.assertIsNone(proc.poll())
            from forge_client import ForgeGatewayClient, ChatMessage
            result = ForgeGatewayClient(self.host.gateway.url).chat([ChatMessage('user', 'test')], model='fixture')
            self.assertEqual(result.text, 'gateway-ok')
            self.assertEqual(requests[0][0], '/custom/v1/chat/completions')
            self.host.gateway.close()
            self.assertIsNotNone(proc.poll())
        finally:
            server.shutdown()
            server.server_close()



if __name__ == '__main__':
    unittest.main()
