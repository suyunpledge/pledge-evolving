"""Policy/lifecycle tests plus opt-in real bundled Chromium on a loopback site."""
from concurrent.futures import ThreadPoolExecutor
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import tempfile
import subprocess
import sys
import threading
import time
import unittest
from unittest.mock import patch

from .browser import BrowserManager, BrowserSession, _BrowserActor, failure, validate_url, verify_bundle
from .browser_tools import BrowserLane
from .execution import ActionExecutor, ActionRequest, create_execution_context
from .policy import Mode, Policy, Sandbox
from .tools import ToolResult


class StubBrowser:
    def __init__(self, result=None):
        self.revoked = threading.Event()
        self.calls = []
        self.result = result or ToolResult(True, '{"page_id":"p","elements":[],"observation_id":"o","url":"about:blank"}')

    def call(self, op, args, cancel):
        self.calls.append((op, args))
        return self.result


class BrowserContractTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.session = StubBrowser()
        self.approvals = []

    def lane(self, mode=Mode.DEFAULT, **kw):
        policy = kw.pop('policy', Policy(mode=mode, workspace=self.root, non_interactive=True))
        def approve(*args):
            self.approvals.append(args)
            return True
        return BrowserLane(self.session, home=self.root / 'control', workspace=self.root,
                           policy=policy, approve=kw.pop('approve', approve), **kw)

    def test_readonly_denies_arbitrary_browser_writes_without_prompt(self):
        lane = self.lane(Mode.READ_ONLY)
        for name, args in [('browser_open', {'url': 'https://example.invalid'}),
                           ('browser_click', {'page_id': 'p', 'observation_id': 'o', 'ref': '1'}),
                           ('browser_fill', {'page_id': 'p', 'observation_id': 'o', 'ref': '1', 'text': 'a'})]:
            result = lane.call(name, args)
            self.assertFalse(result['ok'])
            self.assertEqual(result['meta']['execution_state'], 'not_started')
        self.assertEqual(self.session.calls, [])
        self.assertEqual(self.approvals, [])
        self.assertTrue(lane.call('browser_observe', {})['ok'])

    def test_readonly_sandbox_wins_over_bypass(self):
        lane = self.lane(policy=Policy(workspace=self.root, mode=Mode.BYPASS, sandbox=Sandbox.READ_ONLY))
        self.assertFalse(lane.call('browser_open', {'url': 'https://example.invalid'})['ok'])
        self.assertFalse(self.session.calls)

    def test_explicit_denial_wins(self):
        lane = self.lane(policy=Policy(workspace=self.root, mode=Mode.BYPASS, deny=('browser_*',)))
        self.assertFalse(lane.call('browser_observe', {})['ok'])
        self.assertFalse(self.session.calls)

    def test_missing_approval_even_in_bypass(self):
        result = self.lane(Mode.BYPASS, approve=None).call('browser_open', {'url': 'https://example.invalid'})
        self.assertEqual(result['meta']['error_code'], 'approval_required')
        self.assertFalse(self.session.calls)

    def test_context_without_host_has_no_executable_browser(self):
        lane = self.lane()
        context = create_execution_context(home=self.root/'control', workspace=self.root,
                                           policy=lane.ctx.policy, registry=lane.ctx.registry)
        self.assertEqual(context.registry.available_specs(context.tool_context()), [])
        result = ActionExecutor().execute(context, ActionRequest('browser_open', {'url': 'about:blank'}))
        self.assertEqual(result.error_code, 'missing_capability')
        self.assertFalse(self.session.calls)

    def test_identity_deduplicates_unknown_action_and_persists_events(self):
        self.session.result = failure('timeout', 'No execution evidence', 'unknown')
        lane = self.lane()
        first = lane.call('browser_open', {'url': 'about:blank'}, action_id='once')
        second = lane.call('browser_open', {'url': 'about:blank'}, action_id='once')
        self.assertEqual(len(self.session.calls), 1)
        self.assertEqual(first['meta']['execution_state'], 'unknown')
        self.assertTrue(second['meta']['deduplicated'])
        self.assertEqual(second['meta']['run_id'], lane.ctx.run.run_id)
        self.assertIn('event_id', first['meta'])
        conflict = lane.call('browser_open', {'url': 'https://example.invalid'}, action_id='once')
        self.assertEqual(conflict['meta']['error_code'], 'action_id_conflict')

    def test_cancel_before_dispatch_and_during_approval(self):
        cancel = threading.Event()
        lane = self.lane(cancel=cancel, approve=lambda *a: (cancel.set() or True))
        # Stub also models the dispatch token check the real actor performs.
        def call(op, args, token):
            self.assertTrue(token.is_set())
            return failure('cancelled', 'Cancelled')
        self.session.call = call
        self.assertEqual(lane.call('browser_open', {'url': 'about:blank'})['meta']['execution_state'], 'not_started')
        self.assertEqual(lane.call('browser_observe', {})['meta']['error_code'], 'cancelled')

    def test_observation_cannot_be_borrowed_from_another_run(self):
        result = self.lane().call('browser_click', {'page_id': 'p', 'observation_id': 'o', 'ref': '1'})
        self.assertEqual(result['meta']['error_code'], 'stale_observation')
        self.assertFalse(self.approvals)

    def test_schema_errors_are_not_unknown_effects(self):
        lane = self.lane()
        for args in ({'url': []}, {}, {'url': 'about:blank', 'script': 'bad'}):
            result = lane.call('browser_open', args)
            self.assertEqual(result['meta']['execution_state'], 'not_started')
        self.assertFalse(self.session.calls)

    def test_url_rejects_local_script_and_credential_access(self):
        for url in ('file:///etc/passwd', 'javascript:alert(1)', 'data:text/html,x',
                    'https://user:pass@example.invalid', 'http://', 'https://a\nx'):
            with self.assertRaises(ValueError):
                validate_url(url)
        self.assertEqual(validate_url('http://127.0.0.1:3000'), 'http://127.0.0.1:3000')

    def test_missing_runtime_fails_closed_and_does_not_leave_actor_running(self):
        class Missing(_BrowserActor):
            def _start(self):
                raise RuntimeError('Bundle missing')
        browser = Missing()
        self.addCleanup(browser.close)
        result = browser.call('open')
        self.assertEqual(result.meta['execution_state'], 'not_started')
        self.assertTrue(browser.close())
        self.assertEqual(browser.call('open').meta['error_code'], 'browser_closed')

    def test_frozen_bundle_rejects_modified_or_extra_native_resources(self):
        import hashlib
        directory = self.root / 'browsers'
        folder = directory/'chromium-1'; folder.mkdir(parents=True)
        payload = folder/'chrome.exe'; payload.write_bytes(b'trusted')
        manifest = self.root/'embedded.json'
        manifest.write_text(json.dumps({'chromium-1/chrome.exe': hashlib.sha256(b'trusted').hexdigest()}))
        verify_bundle(directory, manifest)
        payload.write_bytes(b'changed')
        with self.assertRaises(RuntimeError):
            verify_bundle(directory, manifest)
        payload.write_bytes(b'trusted')
        (folder/'injected.dll').write_bytes(b'extra')
        with self.assertRaises(RuntimeError):
            verify_bundle(directory, manifest)

    def test_hung_helper_has_bounded_unknown_result_and_owned_cleanup(self):
        process = subprocess.Popen([sys.executable, '-u', '-c',
            'import sys,time; sys.stdin.readline(); time.sleep(30)'],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            text=True, encoding='utf-8', creationflags=subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0,
            start_new_session=os.name != 'nt')
        self.addCleanup(lambda: process.kill() if process.poll() is None else None)
        browser = BrowserSession(headless=True)
        browser.deadline = .2
        self.addCleanup(browser.close)
        with patch('forge.browser.subprocess.Popen', return_value=process):
            result = browser.call('open', {'url': 'about:blank'})
        self.assertEqual(result.meta['execution_state'], 'unknown')
        self.assertEqual(result.meta['error_code'], 'browser_worker_lost')
        self.assertIsNotNone(process.poll())
        self.assertTrue(browser.close())
        self.assertEqual(browser.call('open').meta['execution_state'], 'not_started')

    def test_modes_projects_and_conversations_have_separate_sessions(self):
        manager = BrowserManager()
        self.addCleanup(manager.close)
        first = manager.session(self.root, 'work', '1')
        self.assertIs(first, manager.session(self.root, 'work', '1'))
        others = [manager.session(self.root, 'coding', '1'),
                  manager.session(self.root/'other', 'work', '1'), manager.session(self.root, 'work', '2')]
        self.assertEqual(len({s.id for s in [first, *others]}), 4)
        with self.assertRaises(RuntimeError):
            manager.session(self.root, 'work', '3')
        manager.close()
        with self.assertRaises(RuntimeError):
            manager.session(self.root, 'work', '1')

    def test_owner_thread_serializes_concurrent_callers_and_cleans_up(self):
        class Actor(BrowserSession):
            def _dispatch(self, op, args, cancel):
                owners.append(threading.get_ident())
                time.sleep(.01)
                return ToolResult(True, str(args['number']))
        owners = []
        actor = Actor()
        self.addCleanup(actor.close)
        with ThreadPoolExecutor(max_workers=8) as pool:
            results = list(pool.map(lambda i: actor.call('test', {'number': i}), range(16)))
        self.assertEqual([r.content for r in results], list(map(str, range(16))))
        self.assertEqual(len(set(owners)), 1)
        self.assertTrue(actor.close())
        self.assertEqual(actor.call('observe').meta['execution_state'], 'not_started')

    def test_queued_cancel_does_not_execute_and_inflight_completion_is_retained(self):
        entered, release = threading.Event(), threading.Event()
        calls = []
        class Actor(BrowserSession):
            def _dispatch(self, op, args, cancel):
                calls.append(op)
                entered.set(); release.wait(2)
                return ToolResult(True, 'completed')
        actor = Actor()
        self.addCleanup(actor.close)
        token = threading.Event()
        with ThreadPoolExecutor(max_workers=2) as pool:
            first = pool.submit(actor.call, 'first')
            self.assertTrue(entered.wait(1))
            second = pool.submit(actor.call, 'second', {}, token)
            token.set(); release.set()
            self.assertTrue(first.result().ok)
            self.assertEqual(second.result().meta['execution_state'], 'not_started')
        self.assertEqual(calls, ['first'])


@unittest.skipUnless(os.environ.get('FORGE_BROWSER_TEST') == '1', 'Set FORGE_BROWSER_TEST=1 after preparing bundled Chromium')
class ChromiumIntegrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                body = b'''<html><title>Forge fixture</title><body>
                    <input aria-label="Name"><input type="password" aria-label="Password">
                    <button onclick="document.querySelector('#out').textContent='clicked';this.remove()">Commit</button>
                    <button onclick="document.cookie='fixture=partition_one';document.querySelector('#out').textContent='cookie set'">Cookie</button>
                    <button disabled>Disabled</button>
                    <div id="out">ready</div><div id="cookie"></div>
                    <script>document.querySelector('#cookie').textContent=document.cookie</script></body></html>'''
                self.send_response(200)
                self.send_header('Content-Type', 'text/html')
                self.send_header('Content-Length', str(len(body)))
                self.end_headers(); self.wfile.write(body)
            def log_message(self, *_):
                pass
        cls.server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        cls.server_thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.server_thread.start()
        cls.url = 'http://127.0.0.1:' + str(cls.server.server_port)

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown(); cls.server.server_close(); cls.server_thread.join()

    def setUp(self):
        self.browser = BrowserSession(headless=True)
        self.addCleanup(self.browser.close)
        opened = self.browser.call('open', {'url': self.url})
        self.assertTrue(opened.ok, opened.error)

    def observe(self, browser=None):
        result = (browser or self.browser).call('observe')
        self.assertTrue(result.ok, result.error)
        return json.loads(result.content)

    def target(self, label):
        snapshot = self.observe()
        element = next(e for e in snapshot['elements'] if e['label'] == label)
        return {'page_id': snapshot['page_id'], 'observation_id': snapshot['observation_id'], 'ref': element['ref']}

    def test_real_navigation_observe_fill_click_and_stale_guard(self):
        target = self.target('Name')
        self.assertTrue(self.browser.call('fill', {**target, 'text': 'Forge'}).ok)
        self.assertEqual(self.browser.call('fill', {**target, 'text': 'Again'}).meta['error_code'], 'stale_observation')
        target = self.target('Commit')
        self.assertTrue(self.browser.call('click', target).ok)
        self.assertIn('clicked', self.observe()['text'])
        self.assertEqual(self.browser.call('click', target).meta['error_code'], 'stale_observation')

    def test_password_file_scheme_and_closed_session(self):
        self.assertEqual(self.browser.call('fill', {**self.target('Password'), 'text': 'no'}).meta['error_code'], 'sensitive_input')
        self.assertEqual(self.browser.call('open', {'url': 'file:///C:/Windows/win.ini'}).meta['execution_state'], 'not_started')
        self.assertTrue(self.browser.close())
        self.assertFalse(self.browser._thread.is_alive())
        self.assertEqual(self.browser.call('observe').meta['error_code'], 'browser_closed')

    def test_cookies_are_isolated_between_real_browsers(self):
        self.assertTrue(self.browser.call('click', self.target('Cookie')).ok)
        self.assertTrue(self.browser.call('open', {'url': self.url}).ok)
        self.assertIn('fixture=partition_one', self.observe()['text'])
        other = BrowserSession(headless=True)
        self.addCleanup(other.close)
        self.assertTrue(other.call('open', {'url': self.url}).ok)
        self.assertNotIn('fixture=partition_one', self.observe(other)['text'])

    def test_real_lifecycle_records_tool_result_and_deduplicates(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            lane = BrowserLane(self.browser, home=root/'home', workspace=root,
                policy=Policy(workspace=root), approve=lambda *a: True)
            observed = lane.call('browser_observe', {})
            snap = json.loads(observed['content'])
            target = next(e for e in snap['elements'] if e['label'] == 'Commit')
            args = {'page_id': snap['page_id'], 'observation_id': snap['observation_id'], 'ref': target['ref']}
            clicked = lane.call('browser_click', args, action_id='fixture-click')
            self.assertTrue(clicked['ok'], clicked)
            self.assertTrue(lane.call('browser_click', args, action_id='fixture-click')['meta']['deduplicated'])
            self.assertEqual(clicked['meta']['execution_state'], 'completed')
            self.assertIn('clicked', self.observe()['text'])

    def test_real_timeout_is_unknown_and_cancel_prevents_followup(self):
        target = self.target('Disabled')
        result = self.browser.call('click', target)
        self.assertFalse(result.ok)
        self.assertEqual(result.meta['execution_state'], 'unknown')
        self.assertEqual(self.browser.call('click', target).meta['error_code'], 'stale_observation')
        cancel = threading.Event(); cancel.set()
        result = self.browser.call('open', {'url': self.url}, cancel)
        self.assertEqual(result.meta['execution_state'], 'not_started')


if __name__ == '__main__':
    unittest.main()
