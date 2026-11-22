"""Exercise the actual SSE transport against a loopback gateway; never a vendor."""
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import tempfile
import threading
import time
import unittest

from PySide6.QtCore import QUrl
from keystone_client import CompletionResult, GenerationCancelled
from .controller import ChatController
from .store import SessionStore


from ._test_app import APP


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
        steps = saved['messages'][-1]['steps']
        self.assertEqual([s['phase'] for s in steps], ['prepare', 'wait', 'packet', 'output'])
        self.assertTrue(all(s['ms'] >= 0 for s in steps))
        sid = self.controller.sessionId
        self.controller.newSession()
        self.controller.selectSession(sid)
        self.assertEqual(self.controller.messages.rows[-1]['steps'], steps)
        self.assertNotIn('steps', self.controller._history[-1].to_dict())
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
        from keystone_client import KeystoneGatewayClient
        self.controller._client = KeystoneGatewayClient(self.controller.gateway)
        self.controller._client.stream_chat = SlowClient().stream_chat
        self.controller.send()
        until(lambda: self.controller.messages.rows[-1]["textContent"] == "partial")
        self.controller.stop()
        until(lambda: not self.controller.busy)
        self.assertEqual(self.controller.messages.rows[-1]["state"], "stopped")
        self.controller._on_worker("chunk", (self.controller._generation, "late"))
        self.assertEqual(self.controller.messages.rows[-1]["textContent"], "partial")

    def test_burst_stream_is_rendered_in_batches_and_final_text_is_preserved(self):
        from keystone_client import KeystoneGatewayClient
        text = "stream 中文 " * 200
        client = KeystoneGatewayClient(self.controller.gateway)
        def stream(messages, on_chunk, **_):
            for character in text:
                on_chunk(character)
            return CompletionResult(text)
        client.stream_chat = stream
        self.controller._client = client
        updates = []
        self.controller.messages.dataChanged.connect(lambda *_: updates.append(1))
        self.controller.draft = "burst"
        self.controller.send()
        until(lambda: not self.controller.busy)
        self.assertEqual(self.controller.messages.rows[-1]["textContent"], text)
        self.assertEqual(self.controller.store.sessions()[0]["messages"][-1]["content"], text)
        self.assertLess(len(updates), 20)
        self.controller.newSession()
        until(lambda: not self.controller._stream_timer.isActive())
        self.assertEqual(self.controller.messages.rows, [])

    def test_turn_budget_reply_offers_continue_without_eating_the_draft(self):
        from keystone_client import KeystoneGatewayClient
        client = KeystoneGatewayClient(self.controller.gateway)
        def stream(messages, on_chunk, **_):
            on_chunk("小结")
            result = CompletionResult("小结")
            result.budget_exhausted = len(stream.calls) == 0
            stream.calls.append(messages)
            return result
        stream.calls = []
        client.stream_chat = stream
        self.controller._client = client
        self.controller.draft = "长任务"
        self.controller.send()
        until(lambda: not self.controller.busy)
        summary_row = len(self.controller.messages.rows) - 1
        self.assertTrue(self.controller.messages.rows[summary_row]["continuable"])
        self.controller.draft = "还没写完的草稿"
        self.controller.continueWork()
        until(lambda: not self.controller.busy)
        self.assertFalse(self.controller.messages.rows[summary_row]["continuable"])
        self.assertEqual(self.controller.messages.rows[summary_row + 1]["textContent"], "继续完成剩余工作")
        self.assertFalse(self.controller.messages.rows[-1].get("continuable", False))
        self.assertEqual(self.controller.draft, "还没写完的草稿")
        self.assertEqual(stream.calls[-1][-1].content, "继续完成剩余工作")

    def test_invalid_legacy_timestamps_do_not_prevent_history_loading(self):
        legacy = self.home / "gui" / "sessions.json"
        rows = [{"id": str(i), "updated": value, "title": "legacy",
                 "messages": [{"role": "user", "content": "retained"}]}
                for i, value in enumerate((None, "invalid", "NaN", "Infinity", 1e100, 10))]
        SessionStore.atomic(legacy, {"sessions": rows})
        original = legacy.read_bytes()
        self.controller._refresh_sessions()
        self.assertEqual(self.controller.sessions.rowCount(), len(rows))
        self.assertEqual(self.controller.sessions.rows[0]["sessionId"], "5")
        self.controller.selectSession("0")
        self.assertEqual(self.controller.messages.rows[0]["textContent"], "retained")
        self.assertEqual(legacy.read_bytes(), original)

    def test_work_chat_dispatches_discovered_mcp_tool_after_approval(self):
        from keystone_client import KeystoneGatewayClient
        from .permissions import authorize

        client = KeystoneGatewayClient(self.controller.gateway)
        client.list_tools = lambda **_: []
        name, target = 'mcp_search_fixture', ('preset.amap', 'search')
        declaration = {'type': 'function', 'function': {'name': name, 'parameters': {'type': 'object'}}}
        approved, dispatched = [], []
        client.call_tool = lambda *_: self.fail('MCP tool sent to gateway')

        def stream(messages, *, tools, **_):
            self.assertEqual(tools[:-1], [declaration])
            self.assertEqual(tools[-1]['function']['name'], 'keystone_update_todos')
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
        expected = [{'name': name, 'outcome': '已完成', 'detail': 'fixture'}]
        self.assertEqual(self.controller.messages.rows[-1]['toolReceipts'], expected)
        sid = self.controller.sessionId
        self.controller.newSession()
        self.controller.selectSession(sid)
        self.assertEqual(self.controller.messages.rows[-1]['toolReceipts'], expected)

    def test_chat_handoff_withholds_computer_tools_until_qt_switches_mode(self):
        from keystone_client import KeystoneGatewayClient
        from .conversation import ENTER_WORK
        from .permissions import authorize

        owner = self.controller
        owner.setConversationMode('chat')
        client = KeystoneGatewayClient(owner.gateway)
        approved, calls, stages, context_calls = [], [], [], []

        def declaration(name):
            return {'type': 'function', 'function': {'name': name}}

        def tool_call(identifier, name, args):
            return {'id': identifier, 'type': 'function',
                    'function': {'name': name, 'arguments': json.dumps(args)}}

        def work_mcp(target, args, cancel):
            self.assertEqual(owner.conversationMode, 'work')
            calls.append((target, args))
            return {'ok': True, 'content': 'captured fixture'}

        def list_tools(**_):
            self.assertEqual(owner.conversationMode, 'work')
            return [declaration('keystone_read_file')]

        def stream(messages, *, tools, **_):
            mode = owner.conversationMode
            stages.append(mode)
            names = {schema['function']['name'] for schema in tools}
            if stages == ['chat']:
                self.assertEqual(names, {'mcp_notes', ENTER_WORK, 'keystone_update_todos'})
                return CompletionResult('', tool_calls=[
                    tool_call('t1', ENTER_WORK, {'reason': '需要检查项目文件'}),
                    tool_call('t2', 'computer_screenshot', {}),
                    tool_call('t3', 'keystone_read_file', {'path': 'README.md'}),
                    tool_call('t4', 'mcp_notes', {'topic': 'test'}),
                ])
            self.assertEqual(mode, 'work')
            self.assertNotIn(ENTER_WORK, names)
            self.assertIn('computer_screenshot', names)
            background = [i for i, message in enumerate(messages) if 'work-context-fixture' in message.content]
            self.assertEqual(len(background), 1)
            slot = background[0]
            self.assertEqual(messages[slot].role, 'user')
            receipts = messages[slot - 4:slot]
            self.assertEqual([m.tool_call_id for m in receipts], ['t1', 't2', 't3', 't4'])
            if len(stages) == 2:
                self.assertIn('先进入 Work', json.loads(receipts[1].content)['error'])
                self.assertIn('先进入 Work', json.loads(receipts[2].content)['error'])
                self.assertTrue(json.loads(receipts[3].content)['ok'])
                return CompletionResult('', tool_calls=[tool_call('t5', 'computer_screenshot', {})])
            return CompletionResult('完成')

        client.stream_chat = stream
        client.list_tools = list_tools
        client.call_tool = lambda *_: self.fail('unexpected gateway dispatch')
        owner._client = client
        owner.chat_mcp_tools = lambda: ({'mcp_notes': (declaration('mcp_notes'), 'notes')},
            lambda target, args, _: calls.append((target, args)) or {'ok': True, 'content': 'notes'})
        owner.mcp_tools = lambda: ({'computer_screenshot': (declaration('computer_screenshot'), 'screen')}, work_mcp)
        owner.tool_authorizer = lambda: lambda name, args, token: authorize('manual', self.home, name, args,
            lambda tool, payload, _: approved.append(tool) or True, token)
        owner.work_context = lambda *_: context_calls.append(owner.conversationMode) or 'work-context-fixture'
        owner.draft = '请求测试'
        owner.send()
        until(lambda: not owner.busy)
        self.assertFalse(owner.error, owner.error)
        self.assertEqual(stages, ['chat', 'work', 'work'])
        self.assertEqual(context_calls, ['work'])
        self.assertEqual(approved, ['mcp_notes', 'computer_screenshot'])
        self.assertEqual(calls, [('notes', {'topic': 'test'}), ('screen', {})])
        outcomes = [row['outcome'] for row in owner.messages.rows[-1]['toolReceipts']]
        self.assertEqual(outcomes, ['已完成', '未执行', '未执行', '已完成', '已完成'])

    def test_plain_chat_stays_in_chat_and_never_lists_work_tools(self):
        from keystone_client import KeystoneGatewayClient
        owner = self.controller
        owner.setConversationMode('chat')
        client = KeystoneGatewayClient(owner.gateway)
        client.list_tools = lambda **_: self.fail('Chat fetched gateway tools')
        client.stream_chat = lambda *_args, **_kwargs: CompletionResult('普通对话')
        owner._client = client
        owner.work_context = lambda *_: self.fail('Chat started Work preparation')
        owner.draft = '你好'
        owner.send()
        until(lambda: not owner.busy)
        self.assertFalse(owner.error, owner.error)
        self.assertEqual(owner.conversationMode, 'chat')
        self.assertEqual(owner.messages.rows[-1]['textContent'], '普通对话')

    def test_work_recovers_an_unavailable_tool_and_replays_a_source_line_verbatim(self):
        from keystone_client import KeystoneGatewayClient

        owner = self.controller
        client = KeystoneGatewayClient(owner.gateway)
        approved = []
        arguments = {'path': 'fixture.txt', 'start': 2, 'end': 2}
        client.list_tools = lambda **_: [{'type': 'function', 'function': {'name': 'keystone_read_range'}}]

        def stream(messages, **_):
            last = messages[-1]
            if last.role == 'user':
                name, identifier = 'Read', 'unavailable'
            elif last.tool_call_id == 'unavailable':
                result = json.loads(last.content)
                self.assertFalse(result['ok'])
                self.assertEqual(result['meta']['execution_state'], 'not_started')
                self.assertIn('本轮提供', result['error'])
                name, identifier = 'read_range', 'read-line'
            else:
                self.assertEqual(last.tool_call_id, 'read-line')
                self.assertEqual(messages[-2].tool_calls[0]['function']['name'], 'keystone_read_range')
                result = json.loads(last.content)
                self.assertEqual(result['content'], '\t  exact line')
                self.assertEqual(result['meta']['start'], 2)
                return CompletionResult(result['content'])
            return CompletionResult('', tool_calls=[{'id': identifier, 'type': 'function',
                'function': {'name': name, 'arguments': json.dumps(arguments)}}])

        client.stream_chat = stream
        client.call_tool = lambda *_: {'ok': True, 'content': '2\t\t  exact line',
            'meta': {'start': 2, 'end': 2, 'execution_state': 'completed'}}
        owner._client = client
        owner.tool_authorizer = lambda: lambda name, args, _: approved.append((name, args))
        owner.draft = '原样读取第二行'
        owner.send()
        until(lambda: not owner.busy)
        self.assertFalse(owner.error, owner.error)
        self.assertEqual(approved, [('keystone_read_range', arguments)])
        self.assertEqual(owner._history[-1].content, '\t  exact line')
        self.assertEqual([row['outcome'] for row in owner.messages.rows[-1]['toolReceipts']], ['未执行', '已完成'])

    def test_disabled_work_tools_stay_disabled_after_reload_and_are_not_dispatched(self):
        from keystone_client import KeystoneGatewayClient
        from .tool_settings import WorkToolSettings

        client = KeystoneGatewayClient(self.controller.gateway)
        schemas = [{'type': 'function', 'function': {'name': 'keystone_' + name}}
                   for name in ('calculator', 'unit_convert')]
        settings = self.controller.toolSettings
        settings._receive('', schemas, '')
        settings.setAll(False)
        settings.setTool('keystone_unit_convert', True)
        reloaded = WorkToolSettings(self.controller)
        enabled, accepts = reloaded.snapshot()
        self.assertTrue(enabled)
        self.assertTrue(accepts('keystone_unit_convert'))
        self.assertFalse(accepts('calculator'))

        client.list_tools = lambda **_: schemas
        client.call_tool = lambda name, _: self.fail(f'{name} should not execute')
        def stream(messages, *, tools, **_):
            self.assertEqual(tools[:-1], schemas[1:])
            self.assertEqual(tools[-1]['function']['name'], 'keystone_update_todos')
            if messages[-1].role == 'tool':
                return CompletionResult('tool unavailable')
            return CompletionResult('', tool_calls=[{'id': 'disabled-call', 'type': 'function',
                'function': {'name': 'calculator', 'arguments': '{"expression":"1+2"}'}}])
        client.stream_chat = stream
        self.controller._client = client
        self.controller.tool_authorizer = lambda: lambda *_: self.fail('Disabled call reached approval')
        self.controller.draft = 'fixture'
        self.controller.send()
        until(lambda: not self.controller.busy)
        self.assertFalse(self.controller.error, self.controller.error)
        self.assertEqual(self.controller.messages.rows[-1]['toolReceipts'][0]['outcome'], '未执行')

        settings.setEnabled(False)
        self.assertFalse(WorkToolSettings(self.controller).snapshot()[0])
        client.list_tools = lambda **_: self.fail('Master switch should not query tool metadata')
        def text_only(_messages, *, tools, **_):
            self.assertIsNone(tools)
            return CompletionResult('text response')
        client.stream_chat = text_only
        self.controller.draft = 'fixture without tools'
        self.controller.send()
        until(lambda: not self.controller.busy)
        self.assertFalse(self.controller.error, self.controller.error)
        self.assertEqual(self.controller._history[-1].content, 'text response')

    def test_unknown_tool_result_is_saved_and_retry_never_reexecutes_it(self):
        from keystone_client import KeystoneGatewayClient, GatewayError
        client = KeystoneGatewayClient(self.controller.gateway)
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
        self.assertEqual(self.controller.messages.rows[-1]['toolReceipts'][0]['outcome'], '结果未知')
        self.assertFalse(self.controller.canRetry)
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
