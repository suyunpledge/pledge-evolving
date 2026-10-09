"""Independent provider wire, settings and message-bound manual review."""
import json
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest.mock import patch
from forge.code_review import ReviewReport
from forge.secrets import SecretScope
from forge_client import ChatMessage, GenerationCancelled
from phase_client import PhaseClient
from test_layout_dpi import isolated_app, pump, descendants
from test_task_planning import PLAN
import forge_gui_v2 as gui


class PhaseClientTests(unittest.TestCase):
    def test_independent_provider_supports_both_wires_and_never_puts_key_in_body(self):
        captured = []
        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args): pass
            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
                captured.append((self.path, body, dict(self.headers)))
                text = json.dumps(PLAN)
                response = ({'content': [{'type': 'text', 'text': text}], 'usage': {'input_tokens': 10, 'output_tokens': 5}}
                    if self.path.endswith('/messages') else {'choices': [{'message': {'content': text}}], 'usage': {'prompt_tokens': 10, 'completion_tokens': 5}})
                data = json.dumps(response).encode()
                self.send_response(200); self.send_header('Content-Length', str(len(data)))
                self.end_headers(); self.wfile.write(data)
        server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True); thread.start()
        scope = SecretScope(); self.addCleanup(scope.close)
        key = 'sk-offline-phase-provider-abc0123456789'
        try:
            for wire in ['openai', 'anthropic']:
                rows = [{'id': 'planner', 'config': {'baseURL': f'http://127.0.0.1:{server.server_port}/v1',
                    'wire': wire, 'model': 'astra', 'apiKey': {'$expr': "get('env.FORGE_PLAN_KEY', '')"}}}]
                client = PhaseClient(rows, ['planner', 'astra'], {'FORGE_PLAN_KEY': key}, scope)
                plan, usage = client.plan_task([ChatMessage('user', 'PASSWORD=synthetic-password-92879')], 'high')
                self.assertEqual(plan.level, 'high'); self.assertEqual(usage['prompt_tokens'], 10)
                self.assertEqual(captured[-1][1]['model'], 'astra')
                self.assertNotIn('tools', captured[-1][1]); self.assertNotIn(key, json.dumps(captured[-1][1]))
                self.assertNotIn('synthetic-password-92879', json.dumps(captured[-1][1]))
                self.assertIn(key, captured[-1][2].values()) if wire == 'anthropic' else self.assertIn('Bearer ' + key, captured[-1][2].values())
        finally:
            server.shutdown(); server.server_close(); thread.join(2)

    def test_cancel_independent_phase_aborts_header_wait(self):
        received, release = threading.Event(), threading.Event()
        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args): pass
            def do_POST(self):
                self.rfile.read(int(self.headers['Content-Length'])); received.set(); release.wait(5)
        server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True); thread.start()
        scope = SecretScope(); self.addCleanup(scope.close)
        cancel, failures = threading.Event(), []
        client = PhaseClient([{'id': 'p', 'config': {'baseURL': f'http://127.0.0.1:{server.server_port}/v1',
            'model': 'm'}}], ['p', 'm'], {}, scope, cancel)
        def request():
            try: client.review_code('print(1)')
            except Exception as exc: failures.append(exc)
        worker = threading.Thread(target=request); worker.start()
        try:
            self.assertTrue(received.wait(2)); cancel.set(); worker.join(2)
            self.assertFalse(worker.is_alive()); self.assertIsInstance(failures[0], GenerationCancelled)
        finally:
            release.set(); cancel.set(); worker.join(5)
            server.shutdown(); server.server_close(); thread.join(2)


class PhaseUiTests(unittest.TestCase):
    def test_settings_preserve_execution_model_layout_and_existing_config(self):
        with isolated_app() as (root, app, errors):
            writer = app.model_var.get()
            layout = (app.split.panes(), app.sidebar.master, app.sidebar.winfo_manager())
            self.assertTrue(app._set_planning_level('high'))
            self.assertTrue(app._save_phase_settings(['mimo', writer], None, True))
            self.assertEqual(app._read_planning_level(), 'high')
            self.assertEqual(app.model_var.get(), writer)
            self.assertEqual(layout, (app.split.panes(), app.sidebar.master, app.sidebar.winfo_manager()))
            self.assertTrue(app._phase_setting('review', 'enabled'))
            before = gui.load_user_layer(app.home)
            with self.assertRaises(ValueError): app._save_phase_settings(['missing', 'astra'], None, True)
            self.assertEqual(gui.load_user_layer(app.home), before)
            app._sending = True
            self.assertFalse(app._save_phase_settings(None, None, False)); app._sending = False
            self.assertFalse(errors)

    def test_streaming_chat_uses_independent_planner_and_keeps_writer(self):
        from types import SimpleNamespace
        from forge.planning import TaskPlan
        with isolated_app() as (root, app, errors):
            calls = []
            class Writer:
                base_url = 'http://127.0.0.1:12345'
                def health(self, **kwargs): return True, 'ok'
                def plan_task(self, *args, **kwargs): raise AssertionError('Writer used for independent planning')
                def list_tools(self): return []
                def stream_chat(self, messages, **kwargs):
                    calls.append(('writer', kwargs['model'], [m.content for m in messages]))
                    kwargs['on_chunk']('actual reply')
                    return SimpleNamespace(text='actual reply', tool_calls=[])
            class Planner:
                def plan_task(self, messages, level, **kwargs):
                    calls.append(('planner', level))
                    return TaskPlan.parse(json.dumps(PLAN), level), {'total_tokens': 15}
            rows = gui.load_user_layer(app.home)
            rows.append({'id': 'planning-vendor', 'config': {'baseURL': 'https://planner.invalid/v1', 'model': 'astra'}})
            gui.save_user_layer(app.home, rows)
            app.client = Writer()
            self.assertTrue(app._save_phase_settings(['planning-vendor', 'astra'], None, False))
            self.assertTrue(app._set_planning_level('high'))
            writer_model = app.model_var.get()
            with patch('phase_client.PhaseClient', return_value=Planner()) as factory, \
                 patch.object(app, '_plan_sidecars', return_value=None), \
                 patch.object(app, '_reload_plugin_tools', return_value=([], None)):
                app.send_var.set('write code'); app._do_send()
                deadline = time.monotonic() + 4
                while app._sending and time.monotonic() < deadline: pump(root, .03)
            self.assertEqual([c[0] for c in calls], ['planner', 'writer'])
            self.assertEqual(calls[1][1], writer_model)
            self.assertEqual(factory.call_args.args[1], ['planning-vendor', 'astra'])
            self.assertIn('Task plan', str(calls[1]))
            self.assertEqual([m.content for m in app._chat_history], ['write code', 'actual reply'])
            self.assertFalse(errors)

    def exercise_review(self, *, stale=False, cancelled=False):
        with isolated_app() as (root, app, errors):
            calls = []
            class Client:
                def review_code(self, code, **kwargs):
                    calls.append((code, threading.current_thread().name)); time.sleep(.15)
                    return ReviewReport('Observed issue and fix', {'prompt_tokens': 10})
            app.client = Client()
            self.assertTrue(app._save_phase_settings(None, None, True))
            app._agent_msg = app.chat_area.add_agent()
            msg = app._agent_msg
            app._chat_history = [ChatMessage('user', 'code'), ChatMessage('assistant', 'print(1)')]
            app._attach_review_action(msg, 'print(1)')
            history = list(app._chat_history)
            app._review_response(msg, 'print(1)')
            app._review_response(msg, 'print(1)')  # duplicate click is ignored
            self.assertTrue(app._sending)
            self.assertEqual(str(msg._review_action._buttons[0].cget('state')), 'disabled')
            responsive = []; root.after(10, lambda: responsive.append(True))
            if stale: app._session_id = 'new-session'
            if cancelled: app._stop_send()
            deadline = time.monotonic() + 4
            while app._sending and time.monotonic() < deadline: pump(root, .03)
            self.assertFalse(app._sending); self.assertTrue(responsive)
            self.assertEqual(len(calls), 1); self.assertNotEqual(calls[0][1], 'MainThread')
            self.assertEqual(app._chat_history, history)
            self.assertEqual(str(msg._review_action._buttons[0].cget('state')), 'normal')
            if stale: self.assertFalse(hasattr(msg, '_review_feedback'))
            elif cancelled: self.assertIn('复审已停止', str(msg._review_feedback.cget('text')))
            else: self.assertIn('Observed issue', str(msg._review_feedback.cget('text')))
            self.assertFalse(errors)

    def test_review_is_manual_responsive_and_not_added_to_history(self): self.exercise_review()
    def test_stale_review_cannot_appear_in_another_session(self): self.exercise_review(stale=True)
    def test_cancel_does_not_attach_completed_review(self): self.exercise_review(cancelled=True)

    def test_phase_dialog_fits_at_five_scales_and_default_is_not_a_paid_model(self):
        for scale in [1, 1.25, 1.5, 1.75, 2]:
            with isolated_app(size=(900, 660), scale=scale) as (root, app, errors):
                app._open_phase_settings()
                app._phase_dialog.attributes('-alpha', 0)
                pump(root, .04)
                dialogs = [w for w in root.winfo_children() if isinstance(w, gui.tk.Toplevel)]
                self.assertTrue(dialogs)
                controls = [w for w in descendants(dialogs[-1]) if isinstance(w, gui.ttk.Combobox)]
                self.assertEqual(len(controls), 2)
                self.assertEqual([w.current() for w in controls], [0, 0])
                for control in controls:
                    self.assertLessEqual(control.winfo_rootx() + control.winfo_width(), dialogs[-1].winfo_rootx() + dialogs[-1].winfo_width())
                self.assertTrue(dialogs[-1].bind('<Escape>'))
                app._phase_settings_close_btn.invoke()
                self.assertFalse(dialogs[-1].winfo_exists())
                self.assertIsNone(root.grab_current())
                self.assertFalse(errors)
