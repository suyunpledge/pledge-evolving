"""Exercise the actual SSE transport against a loopback gateway; never a vendor."""
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import tempfile
import threading
import time
import unittest

from PySide6.QtCore import QCoreApplication, QUrl
from forge_client import CompletionResult, GenerationCancelled
from .controller import ChatController
from .store import SessionStore


APP = QCoreApplication.instance() or QCoreApplication([])


def until(predicate, timeout=5):
    limit = time.monotonic() + timeout
    while time.monotonic() < limit:
        APP.processEvents()
        if predicate():
            return
        time.sleep(.005)
    raise AssertionError("condition timed out")


class Gateway(BaseHTTPRequestHandler):
    requests = []
    fail = False

    def log_message(self, *_):
        pass

    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b'{"data":[{"id":"local-test"}]}')

    def do_POST(self):
        self.__class__.requests.append(json.loads(self.rfile.read(int(self.headers["Content-Length"]))))
        if self.__class__.fail:
            self.send_error(503, "temporary test failure")
            return
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.end_headers()
        for piece in ("真实流式", " reply 123"):
            self.wfile.write(("data: " + json.dumps({"choices":[{"delta":{"content":piece}}]}, ensure_ascii=False) + "\n\n").encode())
            self.wfile.flush()
        self.wfile.write(b'data: [DONE]\n\n')


class BackendTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.home = Path(self.temp.name)
        Gateway.requests, Gateway.fail = [], False
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Gateway)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.controller = ChatController(self.home)
        self.controller.setGateway(f"http://127.0.0.1:{self.server.server_port}")
        until(lambda: self.controller.connected)

    def tearDown(self):
        self.controller.shutdown()
        self.controller.deleteLater()
        APP.processEvents()
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(2)
        self.temp.cleanup()

    def test_real_sse_and_legacy_compatible_history(self):
        self.controller.draft = "中文 English 123"
        self.controller.send()
        until(lambda: not self.controller.busy)
        self.assertEqual(self.controller.messages.rows[-1]["textContent"], "真实流式 reply 123")
        self.assertEqual(Gateway.requests[-1]["messages"][0]["content"], "中文 English 123")
        self.assertEqual(Gateway.requests[-1]["model"], "local-test")
        saved = self.controller.store.sessions()[0]
        self.assertEqual(saved["messages"][-1]["role"], "assistant")
        self.assertFalse((self.home / "gui" / "sessions.json").exists())

    def test_failed_send_can_retry_without_duplicating_user_turn(self):
        Gateway.fail = True
        self.controller.draft = "retry this turn"
        self.controller.send()
        until(lambda: not self.controller.busy)
        self.assertEqual(self.controller.messages.rows[-1]["state"], "error")
        Gateway.fail = False
        self.controller.retry()
        until(lambda: not self.controller.busy)
        self.assertEqual(len(Gateway.requests[-1]["messages"]), 1)
        self.assertEqual(self.controller.messages.rows[-1]["state"], "complete")

    def test_switching_sessions_preserves_unsent_draft(self):
        self.controller.draft = "first"
        self.controller.send()
        until(lambda: not self.controller.busy)
        sid = self.controller.sessionId
        self.controller.draft = "尚未发送的草稿"
        self.controller.newSession()
        self.controller.selectSession(sid)
        self.assertEqual(self.controller.draft, "尚未发送的草稿")

    def test_edit_earlier_message_resends_from_that_point_with_original_attachment(self):
        attachment = self.home / 'note.txt'
        attachment.write_text('original attachment', encoding='utf-8')
        self.controller.addAttachment(QUrl.fromLocalFile(str(attachment)))
        for prompt in ('original question', 'later question'):
            self.controller.draft = prompt
            self.controller.send()
            until(lambda: not self.controller.busy)
        attachment.write_text('new attachment contents', encoding='utf-8')
        sid = self.controller.sessionId
        self.controller.newSession()
        self.controller.selectSession(sid)
        self.controller.draft = 'keep this separate draft'
        self.assertTrue(self.controller.editMessage(sid, 0, 'revised question'))
        until(lambda: not self.controller.busy)
        messages = Gateway.requests[-1]['messages']
        self.assertEqual(len(messages), 1)
        self.assertTrue(messages[0]['content'].startswith('revised question\n'))
        self.assertIn('original attachment', messages[0]['content'])
        self.assertNotIn('new attachment contents', messages[0]['content'])
        self.assertEqual(self.controller.draft, 'keep this separate draft')
        self.assertEqual(self.controller.store.sessions()[0]['messages'][0]['display']['text'], 'revised question')

    def test_withdraw_first_imported_message_persists_empty_history(self):
        legacy = self.home / 'gui' / 'sessions.json'
        SessionStore.atomic(legacy, {'sessions': [{'id': 'legacy', 'title': 'legacy', 'updated': 1,
            'messages': [{'role': 'user', 'content': 'old'}, {'role': 'assistant', 'content': 'reply'}]}]})
        self.controller.selectSession('legacy')
        self.assertFalse(self.controller.withdrawMessage('legacy', 1))
        self.assertTrue(self.controller.withdrawMessage('legacy', 0))
        self.controller.newSession()
        self.controller.selectSession('legacy')
        self.assertEqual(self.controller.messages.rows, [])
        self.assertEqual(len(Gateway.requests), 0)
        self.assertEqual(SessionStore.read(legacy, {})['sessions'][0]['messages'][0]['content'], 'old')

    def test_stale_gateway_probe_cannot_replace_current_state(self):
        ticket = self.controller._connection_epoch
        self.controller._on_worker("connection", (ticket - 1, self.controller.gateway, False, [], "stale"))
        self.assertTrue(self.controller.connected)

    def test_legacy_history_is_read_without_overwriting_its_file(self):
        legacy = self.home / "gui" / "sessions.json"
        legacy.parent.mkdir(parents=True, exist_ok=True)
        content = {"sessions": [{"id": "../../unsafe", "title": "原会话", "updated": 10, "messages": [{"role":"user","content":"old"}]}]}
        legacy.write_text(json.dumps(content), encoding="utf-8")
        original = legacy.read_bytes()
        self.controller._refresh_sessions()
        self.controller.selectSession("../../unsafe")
        self.controller.renameSession("更新标题")
        self.assertEqual(legacy.read_bytes(), original)
        self.assertEqual(self.controller.store.sessions()[0]["title"], "更新标题")
        self.assertEqual(len(list((self.home / "gui" / "qt" / "sessions").glob("*.json"))), 1)

    def test_cancellation_keeps_partial_reply_and_ignores_late_chunks(self):
        class SlowClient:
            def stream_chat(inner, messages, on_chunk, cancel_event, **_):
                on_chunk("partial")
                cancel_event.wait(2)
                raise GenerationCancelled("stopped")
        self.controller.draft = "cancel test"
        from forge_client import ForgeGatewayClient
        self.controller._client = ForgeGatewayClient(self.controller.gateway)
        self.controller._client.stream_chat = SlowClient().stream_chat
        self.controller.send()
        until(lambda: self.controller.messages.rows[-1]["textContent"] == "partial")
        self.controller.stop()
        until(lambda: not self.controller.busy)
        self.assertEqual(self.controller.messages.rows[-1]["state"], "stopped")
        self.controller._on_worker("chunk", (self.controller._generation, "late"))
        self.assertEqual(self.controller.messages.rows[-1]["textContent"], "partial")

    def test_work_chat_dispatches_discovered_mcp_tool_after_approval(self):
        from forge_client import ForgeGatewayClient
        from .permissions import authorize

        client = ForgeGatewayClient(self.controller.gateway)
        client.list_tools = lambda **_: []
        name, target = 'mcp_search_fixture', ('preset.amap', 'search')
        declaration = {'type': 'function', 'function': {'name': name, 'parameters': {'type': 'object'}}}
        approved, dispatched = [], []
        client.call_tool = lambda *_: self.fail('MCP tool sent to gateway')

        def stream(messages, *, tools, **_):
            self.assertEqual(tools, [declaration])
            if messages[-1].role == 'tool':
                self.assertTrue(json.loads(messages[-1].content)['ok'])
                return CompletionResult('地图结果')
            return CompletionResult('', tool_calls=[
                {'id': 'mcp-fixture', 'type': 'function',
                 'function': {'name': name, 'arguments': '{"keyword":"北京"}'}}])

        def approve(tool, args, _):
            approved.append((tool, args))
            return True

        def dispatch(chosen, args, _):
            dispatched.append((chosen, args))
            return {'ok': True, 'result': 'fixture'}

        client.stream_chat = stream
        self.controller._client = client
        self.controller.tool_authorizer = lambda: lambda tool, args, cancel: authorize(
            'manual', self.home, tool, args, approve, cancel)
        self.controller.mcp_tools = lambda: ({name: (declaration, target)}, dispatch)
        self.controller.draft = '搜北京'
        self.controller.send()
        until(lambda: not self.controller.busy)
        self.assertFalse(self.controller.error, self.controller.error)
        self.assertEqual(approved, [(name, {'keyword': '北京'})])
        self.assertEqual(dispatched, [(target, {'keyword': '北京'})])
        self.assertEqual(self.controller._history[-1].content, '地图结果')

    def test_unknown_tool_result_is_saved_and_retry_never_reexecutes_it(self):
        from forge_client import ForgeGatewayClient, GatewayError
        client = ForgeGatewayClient(self.controller.gateway)
        calls = []
        client.list_tools = lambda **_: [{'type': 'function', 'function': {'name': 'write_file'}}]
        client.stream_chat = lambda *args, **kwargs: CompletionResult('', tool_calls=[
            {'id': 't1', 'type': 'function', 'function': {'name': 'write_file', 'arguments': '{"path":"a.txt","content":"x"}'}}])
        def lost_receipt(name, arguments):
            calls.append((name, arguments))
            raise GatewayError('connection lost after dispatch')
        client.call_tool = lost_receipt
        self.controller._client = client
        self.controller.tool_authorizer = lambda: lambda *_: None
        self.controller.draft = 'write a file'
        self.controller.send()
        until(lambda: not self.controller.busy)
        self.assertEqual(len(calls), 1)
        self.assertIn('结果未知', self.controller.error)
        self.assertEqual(self.controller._history[-1].role, 'tool')
        self.controller.retry()
        APP.processEvents()
        self.assertEqual(len(calls), 1)
        sid = self.controller.sessionId
        self.controller.newSession()
        self.controller.selectSession(sid)
        self.assertEqual(self.controller._history[-1].tool_call_id, 't1')
        self.assertEqual(self.controller._history[-2].tool_calls[0]['id'], 't1')


if __name__ == "__main__":
    unittest.main()
