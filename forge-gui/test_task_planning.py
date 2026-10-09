"""Planning controls and the real GUI execution path stay responsive/offline."""
import json
import threading
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from forge.planning import TaskPlan, PlanningError, token_cap
from forge_client import ChatMessage, ForgeGatewayClient, CompletionResult, GenerationCancelled
from interaction_model import task_command
from test_layout_dpi import isolated_app, pump


PLAN = {'tasks': ['Inspect', 'Apply patch', 'Verify'], 'requirements': ['Keep key'],
        'acceptance': ['Key unchanged'], 'design': ['Structured edit'], 'risks': ['Concurrent edits']}


class PlanningClientTests(unittest.TestCase):
    def test_plan_is_tool_free_bounded_and_returns_usage(self):
        client = ForgeGatewayClient()
        self.addCleanup(client.secret_scope.close)
        result = CompletionResult(json.dumps(PLAN), usage={'total_tokens': 15})
        for level in ('low', 'medium', 'high'):
            with patch.object(client, 'chat', return_value=result) as call:
                plan, usage = client.plan_task([ChatMessage('user', 'change model')], level)
                self.assertEqual(plan.level, level)
                self.assertEqual(usage['total_tokens'], 15)
                self.assertEqual(call.call_args.kwargs['max_tokens'], token_cap(level))
                self.assertNotIn('tools', call.call_args.kwargs)
        with patch.object(client, 'chat', side_effect=AssertionError('No request')):
            self.assertEqual(client.plan_task([], 'none'), (None, None))

    def test_plan_cannot_smuggle_native_call_or_permissions(self):
        client = ForgeGatewayClient()
        self.addCleanup(client.secret_scope.close)
        for result in [CompletionResult(json.dumps(PLAN), tool_calls=[{'name': 'write_file'}]),
                       CompletionResult(json.dumps({**PLAN, 'grant': 'bypass'})),
                       CompletionResult(json.dumps(PLAN), raw={'choices': [{'message': {'tool_calls': [{}]}}]})]:
            with patch.object(client, 'chat', return_value=result), self.assertRaises(PlanningError):
                client.plan_task([], 'high')

    def test_task_command_carries_planning_independent_of_routing(self):
        command = task_command('python', 'repo/run.py', 'home', 'task', 'premium', 'low')
        self.assertEqual(command[command.index('--strategy') + 1], 'premium')
        self.assertEqual(command[command.index('--planning') + 1], 'low')
        with self.assertRaises(ValueError):
            task_command('python', 'repo/run.py', 'home', 'task', 'base', 'bypass')

    def test_actual_http_planning_payload_is_secret_free_and_cancellable(self):
        received = threading.Event()
        release = threading.Event()
        payloads = []
        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args): pass
            def do_POST(self):
                payloads.append(json.loads(self.rfile.read(int(self.headers['Content-Length']))))
                received.set()
                release.wait(5)
                try:
                    body = json.dumps({'choices': [{'message': {'content': json.dumps(PLAN)}}]}).encode()
                    self.send_response(200); self.send_header('Content-Length', str(len(body)))
                    self.end_headers(); self.wfile.write(body)
                except OSError: pass  # expected disconnect after cancellation
        server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        server_thread = threading.Thread(target=server.serve_forever, daemon=True)
        server_thread.start()
        client = ForgeGatewayClient('http://127.0.0.1:' + str(server.server_port), timeout=5)
        self.addCleanup(client.secret_scope.close)
        event = threading.Event()
        failures = []
        value = 'sk-offline-planning-synthetic-0123456789abcdef'
        def request():
            try:
                client.plan_task([ChatMessage('user', 'OPENAI_API_KEY=' + value)], 'high', cancel_event=event)
            except Exception as exc: failures.append(exc)
        worker = threading.Thread(target=request)
        worker.start()
        try:
            self.assertTrue(received.wait(2))
            self.assertNotIn(value, json.dumps(payloads))
            self.assertIn('SECRET_REF:', json.dumps(payloads))
            self.assertNotIn('tools', payloads[0])
            self.assertEqual(payloads[0]['max_tokens'], token_cap('high'))
            start = time.monotonic(); event.set(); worker.join(2)
            self.assertFalse(worker.is_alive())
            self.assertLess(time.monotonic() - start, 2)
            self.assertIsInstance(failures[0], GenerationCancelled)
        finally:
            release.set(); event.set(); worker.join(5)
            server.shutdown(); server.server_close(); server_thread.join(2)


class PlanningUiTests(unittest.TestCase):
    def test_levels_persist_preserve_layout_and_are_visible_at_five_scales(self):
        for scale in (1, 1.25, 1.5, 1.75, 2):
            with isolated_app(size=(900, 660), scale=scale) as (root, app, errors):
                before = (app.split.panes(), app.sidebar.master, app.sidebar.winfo_manager())
                for level in ('high', 'medium', 'low', 'none'):
                    self.assertTrue(app._set_planning_level(level))
                    self.assertEqual(app._read_planning_level(), level)
                    pump(root, .04)
                    pill = app.input_card.planning_pill
                    pill.configure(text='stale')
                    app._sync_composer_metadata()
                    from forge_gui_v2 import PLANNING_LABELS
                    self.assertEqual(pill.cget('text'), PLANNING_LABELS[level])
                    with patch('forge_gui_v2.show_popover_menu') as menu:
                        pill.invoke()
                        self.assertIs(menu.call_args.args[0], pill)
                        self.assertEqual(len(menu.call_args.args[1][:4]), 4)
                        self.assertTrue(menu.call_args.args[1][-1]['command'])
                    self.assertTrue(pill.winfo_ismapped())
                    self.assertLessEqual(pill.winfo_rootx() + pill.winfo_width(),
                                         app.input_card.winfo_rootx() + app.input_card.winfo_width())
                    self.assertEqual(before, (app.split.panes(), app.sidebar.master, app.sidebar.winfo_manager()))
                app._show_view('tasks')
                pump(root, .06)
                row = app._task_planning_row
                for control in row.winfo_children():
                    self.assertLessEqual(control.winfo_rootx() + control.winfo_width(),
                                         row.winfo_rootx() + row.winfo_width())
                self.assertFalse(errors)

    def exercise(self, result):
        with isolated_app() as (root, app, errors):
            calls = []
            class Client:
                base_url = 'http://127.0.0.1:12345'
                def health(self, **kwargs): return True, 'ok'
                def plan_task(self, messages, level, **kwargs):
                    calls.append(('plan', threading.current_thread().name, level))
                    time.sleep(.1)
                    if isinstance(result, Exception): raise result
                    return TaskPlan.parse(json.dumps(PLAN), level), {'total_tokens': 15}
                def list_tools(self): return []
                def stream_chat(self, messages, **kwargs):
                    calls.append(('execute', [m.content for m in messages]))
                    kwargs['on_chunk']('verified reply')
                    return SimpleNamespace(text='verified reply', tool_calls=[])
            app.client = Client()
            self.assertTrue(app._set_planning_level('high'))
            with patch.object(app, '_plan_sidecars', return_value=None), \
                 patch.object(app, '_reload_plugin_tools', return_value=([], None)):
                app.send_var.set('inspect')
                app._do_send()
                # A scheduled UI callback runs while the planner waits.
                responsive = []
                root.after(10, lambda: responsive.append(True))
                deadline = time.monotonic() + 5
                while app._sending and time.monotonic() < deadline:
                    pump(root, .03)
            self.assertTrue(responsive)
            self.assertFalse(app._sending)
            self.assertNotEqual(calls[0][1], 'MainThread')
            self.assertFalse(errors)
            return calls, app._chat_history

    def test_streaming_path_consumes_plan_before_execution(self):
        calls, history = self.exercise(None)
        self.assertEqual([c[0] for c in calls], ['plan', 'execute'])
        self.assertIn('Task plan', str(calls[1]))
        self.assertEqual([m.content for m in history], ['inspect', 'verified reply'])

    def test_invalid_plan_never_starts_execution(self):
        calls, history = self.exercise(PlanningError('Malformed plan'))
        self.assertEqual([c[0] for c in calls], ['plan'])
        self.assertEqual(history, [])

    def test_cancelled_plan_never_starts_execution(self):
        calls, history = self.exercise(GenerationCancelled('stopped'))
        self.assertEqual([c[0] for c in calls], ['plan'])
        self.assertEqual(history, [])
