import tempfile
import threading
import unittest
from pathlib import Path
from .permissions import authorize
from .conversation import run
from forge_client import ChatMessage, CompletionResult, GenerationCancelled


class DesktopBoundaryTests(unittest.TestCase):
    def test_sandbox_paths_and_manual_or_full_host_grant(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder) / 'workspace'
            root.mkdir()
            approved = []
            def approve(name, args, cancel):
                approved.append(name)
                return True
            authorize('sandbox', root, 'write_file', {'path': 'in.txt'}, approve)
            authorize('sandbox', root, 'tool_search', {}, approve)
            for name, args in [('write_file', {'path': '../out.txt'}), ('read_file', {'path': '../out.txt'}),
                               ('apply_patch', {'patches': [{'path': '../out.txt'}]}),
                               ('shell_exec', {'command': 'echo hi'}), ('connector_post', {})]:
                with self.assertRaises(PermissionError, msg=name):
                    authorize('sandbox', root, name, args, approve)
            self.assertFalse(approved)
            authorize('manual', root, 'connector_post', {}, approve)
            self.assertEqual(approved, ['connector_post'])
            authorize('full', root, 'connector_post', {}, approve)
            authorize('full', root, 'write_file', {'path': '../out.txt'}, approve)
            self.assertEqual(len(approved), 1)
            with self.assertRaises(PermissionError):
                authorize('manual', root, 'write_file', {'path': 'a'}, lambda *_: False)

    def test_cancelling_a_batch_completes_protocol_without_more_dispatch(self):
        cancel, checkpoints, executions = threading.Event(), [], []
        class Client:
            def list_tools(self, **_): return []
            def stream_chat(self, *_args, **_kwargs):
                return CompletionResult('', tool_calls=[{'id': 'a' + str(i), 'type': 'function',
                    'function': {'name': 'write_file', 'arguments': '{"path":"a.txt"}'}} for i in range(2)])
            def call_tool(self, name, args): executions.append(name)
        def approve(*_): cancel.set()
        with self.assertRaises(GenerationCancelled):
            run(Client(), [ChatMessage('user', 'test')], model='test', effort=None, cancel=cancel,
                authorize=approve, chunk=lambda _: None, checkpoint=checkpoints.append,
                status=lambda _: None, next_round=lambda: None)
        self.assertFalse(executions)
        self.assertEqual([m.role for m in checkpoints[0]], ['user', 'assistant', 'tool', 'tool'])


if __name__ == '__main__': unittest.main()
