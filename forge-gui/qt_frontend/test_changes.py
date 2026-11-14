"""Real local gateway/file IO; only the model stream is substituted."""
import json
from pathlib import Path
import tempfile
import threading
import time
import unittest
from PySide6.QtCore import QCoreApplication
from forge.gateway import GatewayConfig, serve
from forge.policy import Policy, Mode
from forge.tools import build_builtin_registry
from forge_client import ForgeGatewayClient, CompletionResult, GatewayError, GenerationCancelled
from .controller import ChatController
from .permissions import authorize

APP = QCoreApplication.instance() or QCoreApplication([])


def until(predicate):
    end = time.monotonic() + 10
    while time.monotonic() < end:
        APP.processEvents()
        if predicate():
            return
        time.sleep(.01)
    raise AssertionError('timed out')


class ReplyChangeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)
        self.ws, self.home = root / 'work', root / 'home'
        self.ws.mkdir()
        (self.ws / 'sample.py').write_bytes(b'# user draft\r\nvalue = 1\r\n')
        self.cfg = GatewayConfig(upstream='http://127.0.0.1:1', port=0, home=self.home,
            workspace=str(self.ws), registry=build_builtin_registry(),
            policy=Policy(mode=Mode.ACCEPT_EDITS, workspace=self.ws, non_interactive=True))
        self.server = serve(self.cfg)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.endpoint = 'http://127.0.0.1:' + str(self.server.server_address[1])
        self.controller = ChatController(self.home)
        self.controller._gateway = self.endpoint
        self.controller._connected = True
        self.controller.tool_authorizer = lambda: lambda name, args, cancel: authorize(
            'sandbox', self.ws, name, args, lambda *_: False, cancel)

    def tearDown(self):
        self.controller.shutdown()
        self.server.shutdown()
        self.server.server_close()
        self.cfg.close_runtime()
        self.temp.cleanup()

    def generate(self, ending='success'):
        client = ForgeGatewayClient(self.endpoint)
        # Use the real advertised wire name so this also covers the desktop
        # policy boundary, rather than bypassing it with a canonical test name.
        write_name = next(t['function']['name'] for t in client.list_tools()
                          if t['function']['name'] == 'forge_write_file')
        turns = []
        def stream(*_, **kwargs):
            if not turns:
                turns.append(1)
                return CompletionResult('', tool_calls=[{'id': 'edit-1', 'type': 'function',
                    'function': {'name': write_name, 'arguments': json.dumps({'path': 'sample.py', 'content': 'value = 2\n'})}}])
            if ending == 'failure':
                raise GatewayError('model unavailable after write')
            if ending == 'cancel':
                raise GenerationCancelled('stopped after write')
            return CompletionResult('已更新代码。')
        client.stream_chat = stream
        self.controller._client = client
        self.controller.draft = 'update sample'
        self.controller.send()
        until(lambda: not self.controller.busy)
        self.assertFalse(self.controller.changeReview.error, self.controller.changeReview.error)
        return self.controller.messages.rows[-1]['changeKey']

    def test_reply_card_reload_diff_and_revert(self):
        key = self.generate()
        card = self.controller.messages.rows[-1]['changeInfo']
        self.assertEqual(card['count'], 1)
        self.assertEqual((card['added'], card['removed']), (1, 2))
        self.assertTrue(card['canRevert'])
        sid = self.controller.sessionId
        self.controller.newSession()
        self.controller.selectSession(sid)
        self.assertEqual(self.controller.messages.rows[-1]['changeKey'], key)
        self.controller.changeReview.open(key)
        until(lambda: not self.controller.busy)
        self.assertIn('-# user draft', self.controller.changeReview.data['files'][0]['diff'])
        self.controller.changeReview.revert()
        until(lambda: not self.controller.busy)
        self.assertFalse(self.controller.changeReview.error)
        self.assertEqual((self.ws / 'sample.py').read_bytes(), b'# user draft\r\nvalue = 1\r\n')
        self.assertTrue(self.controller.messages.rows[-1]['changeInfo']['restored'])
        self.assertIn('sample.py', self.controller.changeReview.notice())
        self.controller.newSession()
        self.controller.selectSession(sid)
        self.assertTrue(self.controller.messages.rows[-1]['changeInfo']['restored'])

    def test_later_edit_blocks_ui_revert(self):
        key = self.generate()
        (self.ws / 'sample.py').write_text('later user edit')
        self.controller.changeReview.open(key)
        until(lambda: not self.controller.busy)
        self.assertFalse(self.controller.changeReview.data['canRevert'])
        self.controller.changeReview.revert()
        self.assertEqual((self.ws / 'sample.py').read_text(), 'later user edit')

    def test_withdrawal_retains_file_receipt_without_model_history(self):
        key = self.generate()
        sid = self.controller.sessionId
        modified = (self.ws / 'sample.py').read_bytes()
        self.assertTrue(self.controller.withdrawMessage(sid, 0))
        self.assertEqual((self.ws / 'sample.py').read_bytes(), modified)
        self.controller.newSession()
        self.controller.selectSession(sid)
        self.assertEqual(self.controller._history, [])
        self.assertEqual(self.controller.messages.rows[0]['changeKey'], key)
        self.controller.changeReview.open(key)
        until(lambda: not self.controller.busy)
        self.controller.changeReview.revert()
        until(lambda: not self.controller.busy)
        self.assertEqual((self.ws / 'sample.py').read_bytes(), b'# user draft\r\nvalue = 1\r\n')
        self.assertTrue(self.controller.store.sessions()[0]['changes'][key]['restored'])

    def test_failed_reply_keeps_changes_on_reload(self):
        key = self.generate('failure')
        self.assertEqual(self.controller.messages.rows[-1]['state'], 'error')
        sid = self.controller.sessionId
        self.controller.newSession()
        self.controller.selectSession(sid)
        self.assertEqual(self.controller.messages.rows[-1]['changeKey'], key)

    def test_cancelled_reply_keeps_changes_and_can_restore(self):
        key = self.generate('cancel')
        self.assertEqual(self.controller.messages.rows[-1]['state'], 'stopped')
        self.controller.changeReview.open(key)
        until(lambda: not self.controller.busy)
        self.controller.changeReview.revert()
        until(lambda: not self.controller.busy)
        self.assertTrue(self.controller.changeReview.data['restored'])


if __name__ == '__main__':
    unittest.main()
