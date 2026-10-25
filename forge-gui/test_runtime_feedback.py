"""Real Tk send lifecycle, with isolated homes and controlled model/tool replies."""
from contextlib import contextmanager, ExitStack
import threading
import time
import unittest
from unittest.mock import patch

from forge_client import CompletionResult, ForgeGatewayClient, GenerationCancelled
from http_transport import RequestCancelled
from test_layout_dpi import isolated_app, pump


@contextmanager
def chat_fixture():
    with isolated_app() as (root, app, errors), ExitStack() as stack:
        for obj, name, result in (
            (app.client, 'health', (True, 'ok')),
            (app.client, 'list_tools', []),
            (app, '_plan_sidecars', None),
            (app, '_read_planning_level', 'none'),
            (app, '_reload_plugin_tools', ([], None)),
            (app.desktop_features, 'schemas', []),
        ):
            stack.enter_context(patch.object(obj, name, return_value=result))
        try:
            yield root, app
        finally:
            if app._sending:
                app._stop_send()
                settle(root, app)
        assert not errors, errors


def settle(root, app):
    deadline = time.monotonic() + 8
    while app._sending and time.monotonic() < deadline:
        pump(root, .03)
    assert not app._sending, app.status_var.get()


class RuntimeFeedbackTests(unittest.TestCase):
    def test_nonstream_fallback_is_visible_and_persisted_without_duplicate_deltas(self):
        with chat_fixture() as (root, app):
            for streamed in (False, True):
                def reply(messages, **options):
                    if streamed:
                        options['on_chunk']('完整回复')
                    return CompletionResult('完整回复')
                with patch.object(app.client, 'stream_chat', side_effect=reply):
                    app.send_var.set('检查回复')
                    app._do_send(); settle(root, app)
                self.assertEqual(app._chat_history[-1].content, '完整回复')
                self.assertEqual(app._agent_msg._text_source, '完整回复')

    def test_empty_reply_is_failure_with_prompt_preserved(self):
        with chat_fixture() as (root, app):
            with patch.object(app.client, 'stream_chat', return_value=CompletionResult('')):
                app.send_var.set('请回答')
                app._do_send(); settle(root, app)
            self.assertEqual(app._chat_history, [])
            self.assertEqual(app.send_var.get(), '请回答')
            self.assertIn('请求失败', app.status_var.get())

    def test_setup_failure_restores_prompt_and_next_send_works(self):
        with chat_fixture() as (root, app):
            with patch.object(app, '_plan_sidecars', side_effect=RuntimeError('setup failed')):
                app.send_var.set('不能丢失的问题')
                app._do_send()
            self.assertFalse(app._sending)
            self.assertEqual(app.send_var.get(), '不能丢失的问题')
            self.assertEqual(app._chat_history, [])
            with patch.object(app.client, 'stream_chat', return_value=CompletionResult('恢复成功')):
                app._do_send(); settle(root, app)
            self.assertEqual(app._chat_history[-1].content, '恢复成功')

    def test_tool_discovery_cancel_does_not_fall_through_to_model(self):
        with chat_fixture() as (root, app):
            entered = threading.Event()
            def discover(*, timeout, cancel_event):
                self.assertEqual(timeout, 5.0)
                entered.set()
                cancel_event.wait(3)
                raise GenerationCancelled('stopped')
            with patch.object(app.client, 'list_tools', side_effect=discover), \
                 patch.object(app.client, 'stream_chat') as stream:
                app.send_var.set('取消加载工具')
                app._do_send()
                self.assertTrue(entered.wait(2))
                app._stop_send(); settle(root, app)
                stream.assert_not_called()
            self.assertEqual(app.send_var.get(), '取消加载工具')

    def test_tool_receipt_is_visible_before_next_tool_and_unknown_survives_stop(self):
        with chat_fixture() as (root, app):
            entered, release = threading.Event(), threading.Event()
            calls = []
            def dispatch(name, args):
                calls.append(name)
                if len(calls) == 2:
                    entered.set(); release.wait(5)
                    return {'ok': False, 'meta': {'execution_state': 'unknown'}}
                return {'ok': True, 'content': 'first action done'}
            response = CompletionResult('', tool_calls=[
                {'id': str(i), 'function': {'name': 'operate', 'arguments': '{}'}} for i in range(2)])
            with patch.object(app.client, 'stream_chat', return_value=response) as stream, \
                 patch.object(app.client, 'call_tool', side_effect=dispatch), \
                 patch.object(app, '_post_tool_round', wraps=app._post_tool_round) as cards, \
                 patch.object(app, '_set_request_status', wraps=app._set_request_status) as statuses:
                try:
                    app.send_var.set('依次执行')
                    app._do_send()
                    self.assertTrue(entered.wait(2))
                    pump(root, .1)
                    self.assertEqual(cards.call_count, 1)
                    self.assertTrue(any('正在执行 operate' in c.args[0] for c in statuses.call_args_list))
                    app._stop_send()
                finally:
                    release.set()
                settle(root, app)
                self.assertEqual(cards.call_count, 2)
                self.assertEqual(stream.call_count, 1)
            self.assertIn('待确认', app.status_var.get())

    def test_client_tool_discovery_propagates_transport_cancellation(self):
        token = threading.Event()
        with patch('forge_client.open_response', side_effect=RequestCancelled()) as transport:
            with self.assertRaises(GenerationCancelled):
                ForgeGatewayClient().list_tools(timeout=5, cancel_event=token)
            self.assertIs(transport.call_args.kwargs['cancel_event'], token)


if __name__ == '__main__':
    unittest.main()
